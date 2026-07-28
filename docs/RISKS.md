# Risks and platform constraints

Ordered by how likely they are to change the plan.

---

## R-01 — Depth API characteristics are unmeasured

**Impact:** high · **Likelihood:** certain

Meta documents the Depth API's behavior and range but not its resolution or frame
rate — resolution is queried at runtime via `xrGetEnvironmentDepthSwapchainStateMETA`,
and rate isn't specified at all. Every bandwidth figure in
[PROTOCOL.md](PROTOCOL.md) rests on a placeholder assumption.

**Mitigation:** the first task of M3 is a measurement harness that logs actual
resolution, format, delivered rate, latency, and valid-pixel ratio in a real room.
Bandwidth and reconstruction parameters get recomputed from those numbers before
any encoder work begins.

**If it's worse than assumed** — say, low resolution or under 10 Hz — the mesh gets
coarser and convergence slower, but the architecture is unaffected. Compensate
with a longer calibration sweep and a larger voxel size.

---

## R-02 — Camera data policy

**Impact:** high (if color is enabled) · **Likelihood:** medium

Passthrough camera imagery is Device User Data under Meta's Developer Data Use
Policy. The policy prohibits surveillance uses and unique device/user
identification, and Meta's documentation steers firmly toward on-device
processing. Streaming raw camera frames off-device is the shape of thing that
attracts scrutiny, particularly under store review.

**Mitigation:** [ADR-005](DECISIONS.md#adr-005-colour-is-opt-in-and-computed-on-device)
— colorize on device, transmit only vertex colors, never raw frames. Geometry-only
is the default and never requests camera permission.

**Note:** depth data is derived from the cameras but is a separate API with
separate handling. This risk applies specifically to the color path.

---

## R-03 — Wi-Fi throughput and stability

**Impact:** high · **Likelihood:** medium

Quest Wi-Fi throughput varies a lot with AP quality, band, congestion, and how the
wearer is oriented relative to the AP (the wearer's own head attenuates the
signal). A stream that works at the desk can collapse across the room.

**Mitigation:**

- Adaptive quality with automatic step-down and a *visible* degraded state
- Drop policy that sheds color first, then depth, never mesh or control
- `adb reverse` over USB during development, so measurements aren't polluted by
  radio variance
- Document the AP requirement plainly: 5 GHz or 6 GHz, ideally Wi-Fi 6, ideally
  same-room

---

## R-04 — Session lifecycle

**Impact:** medium · **Likelihood:** certain

Horizon OS does not allow background camera or depth capture. Capture stops when:

- the headset is removed (proximity sensor)
- the app loses focus (system menu, notification, guardian breach)
- the user enters passthrough-only system state

**Mitigation:** treat these as first-class states, not errors. The HUD and the
viewer both show `PAUSED — headset removed` explicitly. Resume restores the
session without recalibration as long as the anchor still resolves. Nothing is
lost on the host, since the volume persists across pauses.

**Not mitigable:** unattended capture is impossible. Someone wears the headset for
as long as capture runs.

---

## R-05 — Thermal throttling

**Impact:** medium · **Likelihood:** medium

Depth + camera + encoder + sustained radio is a meaningful thermal load, even
with no rendering. Quest throttles CPU/GPU clocks as it heats, which shows up as
degraded frame delivery long before anything visibly fails.

**Mitigation:** rendering nothing is already the single biggest thermal saving
available. Beyond that: 72 Hz lock, surface the OS thermal state
in `Stats` and on the HUD, automatic quality step-down on elevated thermals, and
document expected session length once measured.

---

## R-06 — Reconstruction quality in real rooms

**Impact:** medium · **Likelihood:** high

Stereo-derived depth degrades on the surfaces that fill actual rooms: windows,
mirrors, glossy tables, TV screens, blank white walls, thin structures like chair
legs. Beyond roughly 4–5 m, precision falls off sharply.

**Mitigation:**

- Initialize the TSDF from the scene mesh the room capture provides — the
  system's own scan already has plausible geometry where live depth is unreliable
- Use the plane semantics to constrain walls, floor, and ceiling, which are the
  large flat surfaces stereo depth handles worst
- Per-voxel confidence from depth validity, surfaced in the viewer so low-confidence
  geometry is visibly marked rather than silently wrong
- Set expectations: this produces a useful realtime spatial model, not a
  metrology-grade scan

---

## R-07 — Anchor loss and drift

**Impact:** medium · **Likelihood:** low

If the origin anchor can't be resolved — the system's map was reset, lighting
changed drastically, the room was rearranged — poses become untrustworthy.

**Mitigation:** [ADR-003](DECISIONS.md#adr-003-a-spatial-anchor-is-the-world-origin).
Capture pauses rather than streaming suspect data. Recovery attempts run with
backoff; sustained failure forces recalibration. Corrupting the volume is strictly
worse than stopping.

---

## R-08 — Engine frame overhead

**Impact:** low · **Likelihood:** low

Godot's per-frame overhead exists even with nothing rendered, and could in
principle eat into the latency budget. It is likely smaller than Unity's would
have been — the app submits a passthrough layer and one quad, with the mobile
renderer and no scene content.

**Mitigation:** measure at M1, when the app is at its simplest. If it is a
problem it will be obvious then, and a GDExtension for the hot path stays open —
[ADR-001](DECISIONS.md#adr-001-godot-45-with-the-openxr-vendors-plugin)
documents what that costs.

---

## R-09 — Space Setup dependency

**Impact:** low · **Likelihood:** medium

The app can request Meta's Space Setup flow but cannot script it. The user might
back out, do it badly, or produce a room model that doesn't match reality.

**Mitigation:** the M2 quality gate catches bad captures with specific, actionable
guidance rather than a generic failure. Floor-height sanity checking specifically
catches the most damaging failure mode, where a mis-detected floor poisons every
subsequent frame.

---

## R-10 — Depth readback rate on Godot

**Impact:** high on the Godot path · **Status:** sidestepped, not solved

**Update:** measured on device, WebXR's depth sensing on Quest 3 is realtime.
Its API is only valid *inside* the animation-frame callback, which is a
per-frame contract by construction rather than an async readback with a
cost caveat. `quest-webxr/` exists because of that measurement.

This risk still stands for the Godot client, whose rate remains unmeasured.
It is no longer a risk to the *project*, because the producer is replaceable
by design and a producer without the problem now exists.

The rest of this entry is the original assessment.

Godot's `get_environment_depth_map_async` is documented as something to call
"approximately every 1–2 seconds, not per-frame". Every bandwidth, latency and
convergence figure in this plan assumes depth at 10–15 Hz. If the documented
guidance reflects a hard ceiling rather than a caution about readback cost,
the streaming architecture does not work through that API.

This risk arrived with [ADR-001](DECISIONS.md#adr-001-godot-45-with-the-openxr-vendors-plugin).
Meta's Unity Depth API carries no equivalent warning.

**Mitigation:** `depth_capture.gd` requests at a configurable rate, keeps at
most one request in flight, and reports the rate it achieved on the HUD.
Measuring it is the first task of the first device session — see
`quest-app/README.md`.

**If the rate is ~1 Hz:** the calibration room model is unaffected, so the
system still produces a usable static reconstruction with slow dynamic
updates. Restoring full rate means a C++ GDExtension reading the
`XR_META_environment_depth` swapchain directly. That is bounded work, and
because viewers consume `MeshChunkUpdate` and the host consumes `DepthFrame`,
only the producer changes.

**Not mitigable by planning:** nobody can measure this without a Quest 3 on a
head. It is the single largest unknown in the project.

---

## Hardware requirements summary

| | Requirement |
| --- | --- |
| **Headset** | Quest 3 or Quest 3S, Horizon OS v74+ (v83+ for 1280×1280 camera) |
| **Not supported** | Quest 2, Quest Pro, Quest 1 — no Depth API, no camera access |
| **Network** | 5 GHz or 6 GHz Wi-Fi, same room as the AP preferred |
| **Host** | GPU with 4 GB+ VRAM for realtime TSDF at 2 cm |
| **Viewer** | Any WebGL2 browser; desktop app on Windows/macOS/Linux |
