# Wire protocol

Version `0.1` — draft, expected to change through M3.

## Transport

Two planes:

| Plane | Transport | Carries |
| --- | --- | --- |
| **Control** | WebSocket (TLS optional on LAN) | Handshake, `RoomProfile`, commands, stats |
| **Media** | WebRTC `DataChannel` (unordered, `maxRetransmits: 0`) | `PoseFrame`, `DepthFrame`, `ColorFrame` |
| **Mesh out** | WebRTC `DataChannel` (ordered, reliable) | `MeshChunkUpdate`, `ChunkRemoved` |

Rationale: depth frames are worthless late — drop them rather than head-of-line
block. Mesh deltas are cumulative state — losing one corrupts the client's view,
so those go reliable and ordered.

**v1 simplification:** ship everything over a single WebSocket first. It works on
LAN, it's trivially debuggable, and it defers ICE/signalling entirely. Introduce
WebRTC in M4 when the browser client needs to work off-LAN. The message framing
below is transport-agnostic, so the swap is contained.

**Dev loop shortcut:** `adb reverse tcp:8787 tcp:8787` gives the headset a
reliable USB-speed path to the host. Use it for development; it removes Wi-Fi
variability from every measurement you take.

## Framing

Every message is a length-prefixed frame:

```
┌────────────┬──────┬───────┬──────────┬──────────────┬─────────┐
│ length u32 │ type │ flags │ reserved │ timestamp u64│ payload │
│            │  u8  │  u8   │   u16    │  (ns, mono)  │         │
└────────────┴──────┴───────┴──────────┴──────────────┴─────────┘
     4          1       1        2            8          length
```

`flags` bit 0 = payload is zstd-compressed. Bit 1 = keyframe/full-state (as
opposed to delta). Bits 2–7 reserved.

Payloads are **FlatBuffers** tables for structured messages and **raw blobs**
for bulk pixel data. FlatBuffers over Protobuf because the host and viewer both
want zero-copy access to large vertex arrays, and the JS decoder is dependency-free.

Schema lives in `protocol/rq4d.fbs` and is the single source of truth. C#, Python,
and TypeScript bindings are generated at build time — never hand-written.

## Message types

| ID | Message | Direction | Rate |
| --- | --- | --- | --- |
| `0x01` | `Hello` | device → host | once |
| `0x02` | `HelloAck` | host → device | once |
| `0x03` | `RoomProfile` | device → host → viewer | on calibration |
| `0x04` | `AnchorUpdate` | device → host | on re-localization |
| `0x10` | `PoseFrame` | device → host → viewer | 30–72 Hz |
| `0x11` | `DepthFrame` | device → host | 10–30 Hz |
| `0x12` | `ColorFrame` | device → host | 5–15 Hz |
| `0x20` | `MeshChunkUpdate` | host → viewer | as dirty |
| `0x21` | `ChunkRemoved` | host → viewer | as needed |
| `0x30` | `Control` | viewer → host → device | on demand |
| `0x31` | `Stats` | any | 1 Hz |
| `0x3F` | `Heartbeat` | any | 1 Hz |

### `Hello` / `HelloAck`

Capability negotiation. Device advertises protocol version, device model, depth
swapchain dimensions (queried at runtime — never hardcoded), available codecs,
whether color is enabled. Host replies with accepted codec, target rates, and its
monotonic clock reading for offset estimation.

### `PoseFrame`

```
PoseFrame {
  head        Pose            // ANCHOR frame
  views[2]    { pose, fov }   // per-eye, ANCHOR frame
  tracking    enum { Tracked, Limited, Lost }
}
```

Small and frequent. Drives the viewer's frustum gizmo and gives the host a pose
stream independent of depth cadence.

### `DepthFrame`

```
DepthFrame {
  view_index    u8            // 0 = left, 1 = right, 2 = both (array)
  pose          Pose          // ANCHOR frame, at capture time
  projection    { fov, near, far }
  width, height u16
  encoding      enum { Raw16, Zstd16, HevcPacked }
  hands_removed bool
  payload       [ubyte]
}
```

Depth values are metric metres in a 16-bit fixed-point format; the exact scale
factor is carried in `HelloAck` because it's device-dependent.

### `MeshChunkUpdate`

The downstream contract. **Anything that can produce this message can replace the
reconstruction backend** — host-side TSDF, on-device fusion, a recorded replay.

```
MeshChunkUpdate {
  key           { x, y, z: i32 }   // chunk coords, 1.28 m cubes
  lod           u8
  version       u32               // monotonic per chunk
  updated_at    u64
  bounds        AABB
  vertices      [ubyte]           // meshopt-encoded, u16-quantized in chunk space
  normals       [ubyte]           // oct16
  colors        [ubyte]?          // rgb8, present only if colour enabled
  indices       [ubyte]           // meshopt-encoded u16/u32
}
```

Chunk size 1.28 m = 64³ voxels at 2 cm. Big enough that per-chunk overhead is
negligible, small enough that a local change doesn't retransmit the room.

Position quantization: u16 within the chunk AABB gives 1.28 m / 65536 ≈ 0.02 mm.
Vastly finer than the sensor, so quantization is free accuracy-wise.

### `Control`

```
Control {
  command  enum { Recalibrate, Pause, Resume, SetQuality,
                  SetROI, ExportSnapshot, ClearVolume }
  quality  { depth_hz, color_hz, voxel_size, max_bitrate }?
  roi      AABB?
}
```

`SetROI` is the pressure valve: restrict fusion to a sub-volume when bandwidth or
compute is short.

## Compression

### Depth

| Stage | Method | Notes |
| --- | --- | --- |
| v1 (M3) | Raw16 + zstd level 1 | Simple, lossless, ~3–5× on typical depth |
| v2 (M4) | 16-bit packed into NV12, HEVC via MediaCodec | Hardware encoder, ~20–50× |

The v2 packing must be a proper depth encoding (split high/low bits across
chroma planes with a continuity-preserving mapping), not naive truncation to 8
bits. Naive truncation loses ~4 cm of precision at 5 m and produces banding that
survives into the mesh as visible terracing.

### Mesh

`meshoptimizer` vertex/index codecs, then zstd. Chosen over Draco for encode
speed — the host is in the realtime path and Draco's encoder is not fast enough
for per-frame chunk updates.

## Bandwidth

Working numbers. **Depth resolution must be measured on device** via
`xrGetEnvironmentDepthSwapchainStateMETA` — the figures below assume 512×512 per
view as a placeholder and should be recomputed once measured.

| Configuration | Raw | After compression |
| --- | --- | --- |
| Depth, 2 views, 30 Hz | 240 Mbit/s | ~60 Mbit/s (zstd) |
| Depth, 1 view, 15 Hz | 60 Mbit/s | ~15 Mbit/s (zstd) |
| Depth, 1 view, 15 Hz, HEVC | 60 Mbit/s | ~3 Mbit/s |
| Color 1280×960 YUV420 @ 10 Hz | 147 Mbit/s | ~8 Mbit/s (HEVC) |
| Pose stream @ 72 Hz | negligible | < 0.1 Mbit/s |
| Mesh deltas, active scanning | — | 1–5 Mbit/s |
| Mesh deltas, idle | — | < 0.2 Mbit/s |

**Default v1 target: single view, 15 Hz, zstd — roughly 15 Mbit/s.** Comfortable
on 5 GHz Wi-Fi 6, leaves headroom, and one view is sufficient because the two
Quest depth views overlap heavily; the second view adds little coverage for
double the bitrate.

## Backpressure

The device runs a bounded send queue. On overflow, drop policy by message type:

1. `ColorFrame` — drop first, colorization degrades gracefully
2. `DepthFrame` — drop oldest, keep newest; stale depth is worse than no depth
3. `PoseFrame` — decimate to 15 Hz
4. `MeshChunkUpdate`, `RoomProfile`, `Control` — **never dropped**

Sustained overflow triggers an automatic `SetQuality` step-down and a HUD warning.
The viewer shows the degraded state explicitly — a silently degraded stream that
looks like a broken sensor is the worst failure mode.

## Versioning

`Hello` carries `protocol_version`. Mismatched major versions refuse the
connection with a clear message rather than attempting partial compatibility.
Below `1.0`, breaking changes are expected and the version bumps freely.
