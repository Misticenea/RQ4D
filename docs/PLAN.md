# Implementation plan

Seven milestones. Each one ends with something demonstrable — no milestone is
purely internal plumbing, because on a project with this many unknowns the only
reliable progress signal is a thing you can look at.

Effort estimates assume one developer with Unity experience and a Quest 3 on the
desk. They are ranges because the depth pipeline has genuine unknowns that only
device measurement resolves.

---

## M0 — Foundations

**~3 days**

Get the loop closed before writing any feature code: edit, build, deploy, observe.

- Monorepo scaffolding (`quest-app/`, `host/`, `viewer-web/`, `protocol/`, `tools/`)
- Unity 6 project, Meta XR All-in-One SDK, Android build target, IL2CPP/ARM64
- `protocol/rq4d.fbs` with `Hello`, `HelloAck`, `Heartbeat`; codegen for C#, Python, TS
- Host service skeleton — WebSocket accept, frame decode, log
- Viewer skeleton — connect, render an empty grid
- `adb reverse` dev script, one-command build-and-deploy
- CI: build the APK, build the host, typecheck the viewer

**Done means:** `./scripts/dev.sh` puts a build on the headset, and the headset's
`Hello` shows up in the host log and in the browser console.

---

## M1 — Headset skeleton and pose streaming

**~4 days**

The full pipeline end to end with the simplest possible payload. Everything after
this is replacing payloads, not building pipes.

- Passthrough enabled, no projection layer content
- 2D HUD quad — connection state, FPS, bitrate, battery, thermal state
- 72 Hz lock, debug overlay behind `RQ4D_DEBUG_VIZ` compile flag
- Head and per-eye pose capture, `STAGE → ANCHOR` conversion (temporary fixed origin)
- `PoseFrame` at 30 Hz over WebSocket
- Viewer renders a headset frustum gizmo moving in realtime
- Session lifecycle: focus loss, headset removal, reconnect with backoff

**Done means:** wearing the headset and walking around moves a frustum in the
browser, with sub-100 ms perceived lag, and yanking the Wi-Fi and restoring it
recovers cleanly.

**Why this ordering:** pose is small, needs no compression, and exercises
transport, framing, coordinate conversion, and the viewer. Every bug found here is
a bug you don't have to find while also debugging depth.

---

## M2 — Calibration and the room profile

**~1.5 weeks**

The "base room info" requirement, in full. See [CALIBRATION.md](CALIBRATION.md).

- Permission flow — `USE_SCENE`, with denial handled properly
- MRUK scene load, `requestSceneCaptureIfNoDataFound`, return handling
- Spatial anchor origin: create, persist UUID, resolve on relaunch
- Tracking settle and floor sanity check
- Guided coverage sweep with directional HUD hints
- Quality gate with per-check failure guidance
- `RoomProfile` assembly: planes, volumes, labels, decimated scene mesh, bounds
- Host stores the profile, viewer renders it — labelled planes and furniture boxes
- Fast-path relaunch when the anchor resolves and the room hash matches
- Recalibration from HUD and from viewer `Control`

**Done means:** launching in an unconfigured room walks the user through Space
Setup, the sweep, and the gate; and within ~40 s the browser shows a recognizable
labelled model of the actual room. Relaunching takes under 10 s.

**Risk concentrated here:** MRUK's scene capture request flow, anchor persistence
across reboots, and coverage-metric tuning are all things that behave differently
on device than the docs suggest. Budget for that.

---

## M3 — Depth streaming and live reconstruction

**~2 weeks** — the hard milestone

- Depth swapchain acquisition, **measure and record actual resolution/rate/format**
- Depth → metric conversion, validity masking, hand removal where supported
- `DepthFrame` with pose, projection, Raw16 + zstd
- Rate control, bounded queue, drop policy
- Host: Open3D `VoxelBlockGrid`, 2 cm voxels, volume from `RoomProfile` bounds
- Per-frame TSDF integration at the transmitted pose
- Dirty-chunk tracking, marching cubes per 1.28 m chunk
- `MeshChunkUpdate` with meshopt encoding
- Viewer chunk manager: apply, replace, dispose, version tracking
- `tools/record` and `tools/replay` — capture a raw session, replay it into the
  host without the headset

**Done means:** walking around the room grows a live mesh in the browser that
visibly matches the space, at ≥ 10 Hz update rate, within the 200 ms latency
budget.

**Build the recorder early in this milestone, not at the end.** Once you can
replay a captured session, host-side reconstruction work stops requiring a
headset on your head, and the iteration speed roughly triples.

---

## M4 — Realtime quality

**~2 weeks**

M3 gives a mesh that accumulates. This makes it a mesh that *tracks reality*.

- **Free-space carving** — voxels observed as empty get cleared, so moved and
  removed objects disappear. Without this the model only ever grows and a chair
  pushed aside leaves a ghost forever.
- Static baseline layer versus dynamic recent layer, separately queryable
- Per-chunk freshness timestamps; viewer age-tint overlay
- LOD: coarse mesh for distant chunks, full detail near the headset
- Frustum- and distance-prioritized chunk transmission
- HEVC depth codec via MediaCodec with proper 16-bit packing
- Adaptive quality: bitrate probing, automatic step-down, visible degraded state
- WebRTC transport for off-LAN browser clients, with WebSocket signalling
- Latency probe in `tools/`, measuring the full glass-to-viewer path

**Done means:** move a chair, and it moves in the viewer within a couple of
seconds with no ghost left behind. Throttle the network to 5 Mbit/s and the
stream degrades visibly but keeps working.

---

## M5 — Color

**~1 week** — gated, see [DECISIONS.md ADR-005](DECISIONS.md#adr-005-colour-is-opt-in-and-computed-on-device)

- Passthrough Camera API integration, `horizonos.permission.HEADSET_CAMERA`
- Camera intrinsics/extrinsics, RGB-to-depth temporal association
- **On-device** vertex colorization — project chunk vertices into the RGB frame,
  accumulate weighted color, transmit color with the mesh
- Raw camera frames never leave the headset
- Exposure/white-balance normalization across accumulated views
- Viewer color toggle

**Done means:** the mesh is colored and recognizable as the actual room, and a
packet capture confirms no camera imagery on the wire.

Skip this milestone entirely if geometry-only is acceptable — it's the only one
that touches the camera policy surface.

---

## M6 — Desktop app, export, packaging

**~1 week**

- Tauri shell around `viewer-web`, LAN host discovery via mDNS
- Export: GLB, PLY, and raw session bundles
- Snapshot capture — freeze and export the current state
- Session record/playback with a timeline scrubber
- Host as a service: config file, logging, graceful restart
- Packaging: signed APK, host installers, viewer static bundle
- Distribution via Meta Quest Developer Hub / sideload

**Done means:** a non-developer can install three artifacts and get a working
capture session, and export a GLB that opens in Blender.

---

## Timeline

| Milestone | Effort | Cumulative |
| --- | --- | --- |
| M0 Foundations | 3 d | 3 d |
| M1 Skeleton + pose | 4 d | 1.5 wk |
| M2 Calibration | 1.5 wk | 3 wk |
| M3 Depth + reconstruction | 2 wk | 5 wk |
| M4 Realtime quality | 2 wk | 7 wk |
| M5 Color *(optional)* | 1 wk | 8 wk |
| M6 Desktop + packaging | 1 wk | 9 wk |

**Roughly 7 weeks to a working geometry-only system, 9 with color and polish.**

A demoable prototype exists at the end of M3 — about 5 weeks. If the goal is to
prove the concept before committing further, M3 is the natural checkpoint.

## Sequencing notes

- **M2 before M3 is deliberate.** Depth fusion needs the room bounds and a stable
  anchor to be worth anything. Doing depth first means redoing it.
- **M4 is not optional polish.** Without free-space carving the system fails the
  "realtime" requirement in a way users notice immediately.
- **M5 is genuinely optional.** Nothing else depends on it.
- **The recorder in M3 is the highest-leverage tool in the project.** Prioritize it.

## What isn't planned here

Deliberately out of scope for v1, listed so the omission is visible rather than
accidental:

- Multi-headset capture of one space
- Multi-room capture and room-transition handling
- Cloud relay / internet-scale streaming (LAN and direct WebRTC only)
- Photogrammetric texture mapping (vertex colors only)
- Object detection / semantic segmentation beyond MRUK's own labels
- Quest 2 / Pro support — the hardware cannot do this
