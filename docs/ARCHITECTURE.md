# Architecture

## The shape of the system

```
┌─────────────────────────────┐
│  QUEST 3 / 3S  (capture)    │
│                             │
│  Space Setup / MRUK  ──┐    │
│  Depth API           ──┼──► Sampler ──► Encoder ──► Transport
│  Passthrough RGB (opt)─┤    │                          │
│  Head + view poses   ──┘    │                          │
│                             │                          │
│  Renders: passthrough +     │                          │
│  one 2D status quad. That's │                          │
│  all. No 3D on the lenses.  │                          │
└─────────────────────────────┘                          │
                                            WebRTC / WebSocket (LAN)
                                                         │
┌────────────────────────────────────────────────────────▼──┐
│  HOST SERVICE  (reconstruction)                            │
│                                                            │
│  Depth+pose ──► TSDF voxel-hash fusion ──► Marching cubes   │
│                       │                         │          │
│                 free-space carving        dirty-chunk queue │
│                                                 │          │
│  RoomProfile ──────────────────────────────► Publisher     │
└────────────────────────────────────────────────┬───────────┘
                                                 │  mesh deltas
                        ┌────────────────────────┴────────────┐
                        ▼                                     ▼
              ┌──────────────────┐                  ┌──────────────────┐
              │  WEB VIEWER      │                  │  DESKTOP VIEWER  │
              │  three.js        │                  │  Tauri + same JS │
              └──────────────────┘                  └──────────────────┘
```

**One sentence:** the headset senses and encodes, the host reconstructs, the
clients render.

## Why reconstruction lives on the host

This is the central architectural call. See
[DECISIONS.md ADR-002](DECISIONS.md#adr-002-fuse-on-the-host-not-on-device) for
the full rationale, but in short:

- The Quest's GPU budget is better spent on nothing at all. An app that renders
  no content can run cool, at low clock, with a long battery life — which is
  exactly what a capture device wants.
- Reconstruction quality is where the iteration happens. Changing a fusion
  parameter should be a host restart, not an APK rebuild and redeploy.
- The browser can't fuse depth at any useful quality. If the host does it, the
  web viewer gets the same mesh as the desktop viewer for free.

**The escape hatch:** the protocol's downstream contract is `MeshChunkUpdate`.
An on-device fusion implementation that emits the same message is a drop-in
replacement, and viewers never know the difference. If bandwidth or policy
forces fusion onto the headset later, only the producer changes.

## Component responsibilities

### Quest capture app (`quest-app/`)

Unity 6 + Meta XR All-in-One SDK + MRUK. Responsibilities:

- Permission acquisition and Space Setup orchestration
- The launch calibration flow ([CALIBRATION.md](CALIBRATION.md))
- Spatial anchor lifecycle — creating, persisting, and re-localizing the world origin
- Per-frame acquisition: depth swapchain image, view poses, projection params
- Optional RGB acquisition via Passthrough Camera API
- Encoding and rate control
- Transport, reconnection, backpressure
- A 2D status HUD — and nothing else on screen

Explicit non-responsibility: it never builds, holds, or draws the reconstruction.

### Host service (`host/`)

Python + Open3D for v1 (`VoxelBlockGrid` gives GPU TSDF and marching cubes out
of the box). Rust rewrite is a later optimization, not a starting point.

- Session management, one active capture session at a time
- Allocates the voxel volume from the `RoomProfile` bounds
- Integrates each `DepthFrame` at its accompanying pose
- Free-space carving so removed objects actually disappear
- Meshes dirty chunks, tracks per-chunk versions
- Fans out mesh deltas to N subscribed viewers
- Records raw sessions to disk for offline replay

### Viewers (`viewer-web/`, `viewer-desktop/`)

TypeScript + three.js. One codebase, two shells.

- Chunk manager keyed by `(x, y, z, lod)`, applies deltas, disposes removed chunks
- Renders the `RoomProfile` immediately on connect — walls and furniture appear
  before any live geometry arrives, so the viewer is never blank
- Headset frustum gizmo showing where the wearer is looking
- Freshness overlay: chunks tinted by age since last update, so it's obvious
  what's live versus stale baseline
- Export to GLB / PLY
- Desktop shell adds LAN discovery and direct filesystem export

## Coordinate frames

Getting this wrong is the most common way a project like this produces a mesh
that looks correct but drifts, doubles, or smears. The rules:

| Frame | Definition |
| --- | --- |
| `ANCHOR` | The canonical world frame. A persisted spatial anchor created during calibration. **Everything on the wire is expressed in this frame.** |
| `STAGE` | Quest tracking space. Drifts and jumps on re-localization. Never leaves the device. |
| `VIEW_L` / `VIEW_R` | Per-eye camera frames for the depth images. |
| `RGB` | Passthrough camera frame — a *different* pose and intrinsics from the depth views. |

Rules that follow:

1. Every frame message carries the pose it was captured at. Never assume a
   receiver can pair a frame with "the most recent pose."
2. Poses are converted `STAGE → ANCHOR` on-device before transmission. The host
   never sees tracking-space coordinates and never has to care about drift.
3. On a tracking loss / re-localization event, the app re-resolves the anchor and
   emits an `AnchorUpdate`. If the anchor cannot be recovered, capture **pauses** —
   streaming garbage poses corrupts the volume permanently.
4. Unity is left-handed Y-up; glTF and most tooling is right-handed Y-up. The
   conversion happens exactly once, at the encoder, and is unit-tested. Not
   sprinkled through the codebase.

## Timing

Every message carries a timestamp in a single monotonic clock domain
(`CLOCK_MONOTONIC` nanoseconds, device-local). The XR predicted display time is
converted into this domain at capture. Host and device clocks are related by an
offset estimated during handshake (simple NTP-style round-trip); the host stores
both the device timestamp and its own arrival timestamp.

Depth and RGB are captured on independent cadences and will not be aligned.
Colorization pairs each vertex with the *temporally nearest* RGB frame whose
frustum contains it, not with "the current frame."

## Latency budget

Target: **under 200 ms** glass-to-viewer for a geometry change.

| Stage | Budget |
| --- | --- |
| Depth acquisition + readback | 20–40 ms (Meta-documented for camera; measure for depth) |
| Encode (zstd or MediaCodec) | 5–15 ms |
| Network (LAN, 5/6 GHz) | 5–20 ms |
| TSDF integration | 10–30 ms |
| Marching cubes on dirty chunks | 10–30 ms |
| Delta encode + transmit | 10–30 ms |
| Viewer decode + upload + render | 16–33 ms |
| **Total** | **76–198 ms** |

Anything that blows this budget gets measured before it gets optimized. `tools/`
ships a latency probe that injects a marker frame and reports the full path.

## Rendering on the headset

The constraint "not displayed on the lenses" has real implications worth stating
as design, not as an omission:

- The app submits a **passthrough layer + one quad layer** (the HUD) per frame.
  No projection layer with scene content. GPU cost stays near zero.
- It still must submit *something* every frame. An OpenXR app that stops
  submitting frames gets throttled and then killed. The HUD quad is the keepalive.
- Lock to **72 Hz**. There's no rendered content whose smoothness depends on
  refresh rate, and the saved power goes to the radio and the encoder instead.
- A debug point-cloud overlay exists behind a build flag for development. It is
  compiled out of release builds — not merely toggled off.
- The app must stay foregrounded. Horizon OS does not permit background camera or
  depth capture. Taking the headset off pauses the session; see
  [RISKS.md](RISKS.md#r-04-session-lifecycle).
