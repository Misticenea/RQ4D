# Decisions

Architectural decisions with the reasoning behind them, so that revisiting one
later is a considered change rather than an archaeology exercise.

---

## ADR-001: Unity 6 + Meta XR SDK for the capture app

**Alternatives:** native OpenXR C++, Godot 4, WebXR in the Quest browser.

**Decision:** Unity 6 with the Meta XR All-in-One SDK and MRUK.

**Why:**

- MRUK hands us the entire scene model — planes, volumes, semantic labels, global
  mesh — through one API. Reimplementing that against raw `OVRAnchor` is weeks of
  work for no benefit.
- Depth API, Passthrough Camera API, and spatial anchors all have first-class,
  documented Unity paths. Meta's samples are Unity samples.
- Native OpenXR gives finer control over the depth swapchain and lower overhead —
  genuinely attractive for a capture-only app that renders nothing. It costs
  reimplementing scene understanding and anchor persistence. Not worth it up front;
  revisit only if Unity's frame overhead measurably threatens the latency budget.
- WebXR is out: no passthrough camera access, and its depth-sensing module is far
  more limited than the native Depth API.

**Cost accepted:** Unity's build-deploy cycle is slow. M0 invests in scripting the
loop, and M3's session recorder moves most reconstruction iteration off-device
entirely.

---

## ADR-002: Fuse on the host, not on device

**Decision:** the headset streams depth + poses. The host runs TSDF fusion and
meshing.

**Why:**

- The reconstruction algorithm is where nearly all the iteration happens. On the
  host that's a process restart; on device it's a rebuild, deploy, re-don the
  headset, re-calibrate. The difference compounds across hundreds of iterations.
- A capture app that renders nothing and fuses nothing runs cool and long. Thermal
  throttling on Quest is a real constraint for sustained sessions, and fusion plus
  camera plus radio together is exactly the load that triggers it.
- The browser cannot fuse depth at useful quality. Host-side fusion means the web
  viewer and the desktop viewer receive identical meshes with no duplicated logic.
- Host-side, GPU TSDF at 2 cm across a room is unremarkable. On a mobile SoC it is
  a project in itself.

**Cost accepted:** depth has to go over the wire — roughly 15 Mbit/s at v1
settings. Fine on 5 GHz Wi-Fi, and [PROTOCOL.md](PROTOCOL.md) has the fallbacks.

**How it stays reversible:** viewers consume `MeshChunkUpdate` and nothing else.
An on-device fusion backend emitting the same message swaps in without touching
the viewer. If bandwidth or policy ever forces the move, only the producer changes.

---

## ADR-003: A spatial anchor is the world origin

**Decision:** calibration creates or resolves a persisted spatial anchor. Every
pose and vertex on the wire is expressed relative to it.

**Why:** Quest tracking space drifts, and jumps discretely when the headset
re-localizes after a tracking loss. Data anchored to tracking space produces a
reconstruction that shears over a long session — and worse, the shear is
invisible until it's badly wrong. A spatial anchor is maintained against the
system's own map of the room and is stable across relocalization and reboots.

**Consequence:** if the anchor cannot be resolved, capture **pauses**. Streaming
frames with untrustworthy poses permanently corrupts the voxel volume, and no
amount of downstream cleverness recovers it. A pause is recoverable; corruption
is not.

---

## ADR-004: Chunked mesh deltas, not point clouds

**Alternatives:** stream raw point clouds; stream full-mesh snapshots.

**Decision:** space is divided into 1.28 m chunks; only dirty chunks are
retransmitted, versioned per chunk.

**Why:**

- Point clouds are simple and immediate, but they don't accumulate into a coherent
  model, they can't be occluded correctly, and they're bandwidth-hungry for the
  visual quality delivered.
- Full-mesh snapshots don't scale — retransmitting a room because one chair moved
  makes realtime impossible.
- Chunking makes bandwidth proportional to *change*, which is what "realtime"
  actually demands. An idle scene costs nearly nothing; an active one costs a few
  Mbit/s.
- Chunks also give LOD and priority for free — send what's near the wearer's gaze
  first, coarse geometry for distant regions.

**Chunk size rationale:** 1.28 m = 64³ voxels at 2 cm. Small enough that a local
change is a small update, large enough that per-chunk overhead stays negligible.
Tunable, but not per-session — it's baked into the chunk key space.

---

## ADR-005: Color is opt-in and computed on device

**Decision:** color is disabled by default. When enabled, the headset projects
mesh vertices into the passthrough camera frame and transmits *vertex colors*.
Raw camera images never leave the device.

**Why:**

- Meta classifies camera imagery as Device User Data under the Developer Data Use
  Policy, and the documentation's clear steer is toward on-device processing.
  Vertex colors are derived data, orders of magnitude smaller, and cannot be
  reassembled into a photograph of the room.
- It removes the "is this app streaming video of my house" question entirely,
  which matters for anyone who has to explain the app to its users.
- A geometry-only build never requests camera permission at all — a materially
  different and much less alarming install experience.

**Cost accepted:** vertex colors at 2 cm resolution are coarser than projective
texturing. Acceptable for the target use cases; if photorealistic texture is ever
required, that's a different project with a different policy conversation.

---

## ADR-006: Web viewer first, desktop as a shell

**Decision:** one TypeScript/three.js viewer. The desktop app is a Tauri shell
around the same code.

**Why:** the request names both a web app and a PC app. Two independent renderers
is double the work and guaranteed drift between them. Tauri (rather than Electron)
keeps the desktop binary small and adds native filesystem export and mDNS
discovery, which is exactly the delta the desktop app needs over the browser.

**Cost accepted:** a native desktop renderer could push far more triangles. Room
-scale meshes at 2 cm are within WebGL2/WebGPU's comfortable range, so this isn't
a live constraint until multi-room.

---

## ADR-007: Fixed-layout binary framing, JSON for control

**Superseded:** an earlier draft of this ADR chose FlatBuffers.

**Decision:** a fixed 16-byte header plus fixed-layout binary payloads for the
hot path (`PoseFrame`, `DepthFrame`, `MeshChunkUpdate`); JSON payloads for
control messages. Implemented in `host/rq4d_host/wire.py`, which is the
reference for all other languages.

**Why the change:** the bulk payloads — depth blobs, vertex and index arrays —
are opaque byte ranges. FlatBuffers adds a schema and a codegen step without
changing how those bytes are read: in both designs the receiver ends up
wrapping a numpy or typed-array view over an offset. The structured parts are
small and fixed-size, so a struct layout describes them completely.

What that buys: no `flatc` in the build, no generated sources to keep in sync,
and a codec that is a couple of hundred readable lines. Control messages stay
JSON because they are rare, small, and much easier to debug by eye.

**Cost accepted:** the layout is hand-maintained across C#, Python and
TypeScript. Mitigated by keeping every struct in one file per language with the
byte offsets stated, and by the `Hello` version check refusing mismatches
outright rather than misparsing them.

---

## ADR-008: Carving is projective, integration is ray-based

**Decision:** surface integration unprojects depth pixels and writes a band
around each measured point. Carving does the opposite — it projects
currently-solid voxels into the depth image and erases the ones the camera can
now see through.

**Why they differ:** the two operations need density in different spaces.
Integration needs to be dense on the *surface*, and depth pixels are already
distributed exactly there. Carving needs to be dense in *voxel* space, and
rays diverge — at 1.25 m a 6-pixel stride puts neighbouring samples six voxels
apart, so most voxels are never touched no matter how many samples are thrown
down the rays.

This was not a prediction. The first implementation carved by marching along
rays; an isolated test — fixed camera, object removed — measured **zero** hits
on a given ghost voxel over eight frames, and a ghost that survived 100%
intact. Inverting the loop for the carve pass made the same test pass, and the
same object now clears to a ~25% residual in the full benchmark, that residual
being the underside resting on the floor, which no camera position can observe.

**Cost accepted:** the carve pass costs reconstructed surface area rather than
swept volume, and needs a chunk-level frustum cull to stay affordable —
without it, 500 chunks x 32768 voxels per frame measured 94 ms and dropped the
achieved rate from 15 Hz to 8.

**Kept honest by:** `host/tests/test_carving.py`, which asserts both directions
— a removed object disappears, and a stationary one does not erode.

---

## Open questions

These change the plan materially. Reasonable defaults are in place so work isn't
blocked, but they're worth an explicit call.

### Q1 — Is color needed, or is geometry enough?

**Default assumed:** geometry-only for v1, color as optional M5.

Geometry-only is a week cheaper, needs no camera permission, and sidesteps the
data-policy surface entirely. If the end use is spatial understanding, robotics,
or remote presence-of-space, geometry is likely sufficient. If a human needs to
*recognize* the room visually, color matters a lot.

### Q2 — How is this distributed?

**Default assumed:** sideload / Meta Quest Developer Hub.

Horizon Store review for an app that streams depth off-device — and camera-derived
data if color is enabled — is an unknown quantity worth scoping early if store
distribution is the goal. Sideloading for internal or research use avoids the
question entirely. This decision also affects Q1: store review raises the bar on
the camera path considerably.

### Q3 — Does the wearer need any headset UI beyond status?

**Default assumed:** status HUD only — connection, quality, battery, recalibrate.

The constraint is that the reconstruction isn't shown. That leaves room for
coverage hints, ROI selection, or annotation tools if the wearer is an active
participant rather than just a camera operator.

### Q4 — How many concurrent viewers?

**Default assumed:** a handful on the same LAN.

Ten-plus simultaneous viewers, or viewers over the internet, changes the host from
a direct fan-out into something needing an SFU-style relay. Worth knowing before
M4 fixes the transport design.
