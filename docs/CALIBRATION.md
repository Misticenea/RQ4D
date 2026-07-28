# Launch calibration

> "when the app starts it makes you calibrate to get the base room info"

Calibration produces the **room profile**: a static baseline model of the space
that (a) tells the host how big a voxel volume to allocate, (b) gives viewers
something to render before live geometry arrives, and (c) establishes the
room-fixed origin every subsequent message is expressed in.

It is mandatory. The app will not stream until calibration completes.

## Flow

```
 LAUNCH
   │
   ├─► [1] Permission gate
   │        USE_SCENE, HEADSET_CAMERA (if color enabled)
   │        └─ denied ──► explain + retry, no bypass
   │
   ├─► [2] Scene data check  (MRUK.LoadSceneFromDevice)
   │        └─ none found ──► request system Space Setup ──┐
   │                          (user walks Meta's flow)     │
   │        ◄────────────────────────────────────────────── ┘
   │
   ├─► [3] Origin anchor
   │        resolve persisted UUID  ──or──  create new at floor centre
   │
   ├─► [4] Tracking settle           (~2 s, hold still)
   │        verify TRACKED, sanity-check floor height
   │
   ├─► [5] Guided coverage sweep
   │        "look slowly around the room" — progress = voxel coverage
   │
   ├─► [6] Quality gate
   │        coverage % / hole check / drift check
   │        └─ fail ──► back to [5] with specific guidance
   │
   └─► [7] Emit RoomProfile ──► STREAMING
```

## Step detail

### 1. Permission gate

| Permission | Needed for | When |
| --- | --- | --- |
| `com.oculus.permission.USE_SCENE` | Scene model, anchors, room mesh | Always |
| `horizonos.permission.HEADSET_CAMERA` | Passthrough RGB for colorization | Only if color is enabled |

Depth API needs no runtime permission but does require passthrough to be active.

Request only what the current configuration uses. A geometry-only build should
never ask for camera access — it's the difference between a benign permission
prompt and one that makes users nervous, and it changes the policy surface
(see [RISKS.md](RISKS.md#r-02-camera-data-policy)).

### 2. Scene data

Load via `MRUK.Instance.LoadSceneFromDevice(requestSceneCaptureIfNoDataFound: true)`.

If the user has never run Space Setup, this launches Meta's system flow. Two
things to internalize:

- **The app cannot script Space Setup.** The user walks Meta's own UI, drawing
  walls and marking furniture. We can only request it and wait for return.
- **The app cannot assume it succeeded.** The user can back out. Re-query on
  return; if there's still no scene, show a clear explanation and offer retry.

What comes back:

- **Planes** — walls, floor, ceiling, doors, windows, wall art
- **Volumes** — table, couch, bed, screen, storage, lamp, plant, other
- **Semantic labels** on both
- **Global/scene mesh** — a triangle mesh of the room (Quest 3 family only)

This is the "base room info" in the truest sense: Meta's own room model,
captured once, persisted by the system, and reusable across launches.

### 3. Origin anchor

Create (or resolve) a `OVRSpatialAnchor` and persist its UUID to local storage.
This is the single most important step for long-session stability.

Default placement: floor centroid of the largest room plane, axes aligned to the
dominant wall normal — deterministic, so a re-launch in the same room lands on
the same origin without user input.

Storage: `{ anchorUuid, roomHash, createdAt, profileVersion }` in
`Application.persistentDataPath`. On next launch, if the anchor resolves and the
room hash matches, calibration takes the **fast path** (step 4 + a 3 s sweep)
instead of the full flow.

Why this matters: Quest tracking space drifts and jumps when the headset
re-localizes. A session anchored to tracking space produces a reconstruction
that slowly shears. A session anchored to a spatial anchor does not.

### 4. Tracking settle

Two seconds of "hold still, looking forward" while:

- Tracking state is confirmed `Tracked` (not `Limited`)
- Head pose variance is under threshold
- Headset height above the floor plane is sane (1.0–2.2 m) — catches a
  mis-detected floor, which otherwise poisons every subsequent frame

Fail → specific message ("the floor doesn't look right — re-run Space Setup"),
not a generic error.

### 5. Guided coverage sweep

The wearer is the scanner. Nothing gets captured that nobody looked at, so the
sweep is where the baseline actually gets built.

- Voxelize the room AABB into a coarse coverage grid (~20 cm cells)
- Mark cells observed by incoming depth frames
- Progress = observed cells / expected-observable cells, where "expected
  observable" excludes cells outside the scene mesh's interior
- HUD shows a percentage and a directional hint pointing at the largest
  unobserved region ("look left", "look up")
- Encourage slow motion; fast rotation produces motion-blurred depth

Typical room: 20–40 s. Cap at 90 s with an "accept partial" option — a stubborn
corner shouldn't hold the whole session hostage.

### 6. Quality gate

| Check | Threshold | On failure |
| --- | --- | --- |
| Coverage | ≥ 80 % of expected-observable cells | Resume sweep, point at gaps |
| Floor consistency | Floor plane within 3 cm of depth-derived floor | Re-run Space Setup |
| Anchor stability | Origin drift < 2 cm over the sweep | Re-settle, then re-anchor |
| Depth validity | ≥ 60 % valid pixels in a median frame | Check lighting; warn about glass/mirrors |

Thresholds are config values in `quest-app/Config/calibration.json`, tuned on
device. The numbers above are starting points, not measurements.

### 7. Emit the room profile

One `RoomProfile` message, sent before any depth frames:

```
RoomProfile {
  profile_id            uuid
  captured_at           timestamp
  anchor_uuid           uuid           // the origin
  room_hash             string         // for fast-path matching

  bounds                AABB           // in ANCHOR frame
  floor_height          float
  ceiling_height        float

  planes[]              { label, centre, normal, extents, boundary[] }
  volumes[]             { label, centre, rotation, extents }
  scene_mesh            { vertices[], indices[], face_labels[] }   // decimated

  depth_intrinsics      { width, height, fov_l, fov_r, near, far }
  color_intrinsics      { width, height, k[], distortion[] }?      // if colour enabled
  color_extrinsics      pose?                                      // RGB relative to ANCHOR

  device                { model, os_version, app_version }
  coverage_pct          float
  quality_flags         bitfield
}
```

The scene mesh is decimated before transmission — the raw global mesh is dense
and this message should stay under a few hundred KB so the viewer populates
instantly.

The host uses `bounds` to allocate its voxel volume and `scene_mesh` to
initialize the TSDF with a prior, which measurably speeds up convergence of the
live layer.

## Recalibration

Triggered by:

- **User request** — always available from the HUD
- **Anchor loss** — origin can't be resolved after N retries → capture pauses,
  full recalibration required
- **Room change detected** — sustained divergence between live depth and the
  baseline scene mesh beyond a threshold suggests furniture moved or the user
  walked into a different room
- **Viewer command** — `Control{recalibrate}` from a connected client, so an
  operator at the PC can trigger it without the wearer touching anything

Recalibration emits a new `RoomProfile` with a fresh `profile_id`. Viewers
discard their chunk cache and rebuild — a clean reset is more predictable than
attempting to diff two baselines.

## Multi-room

Out of scope for v1, but the design doesn't preclude it: Space Setup supports
multiple rooms, and `RoomProfile` is keyed by anchor. A future version can hold
several profiles and switch on detected room transition. Nothing in the protocol
needs to change.
