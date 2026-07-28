# RQ4D capture app (Godot)

The headset half: calibrates the room, then streams depth and poses to the
host. **Nothing of the reconstruction is drawn on the lenses** — the wearer
sees passthrough plus a small status panel.

## Requirements

- **Godot 4.5 or later** (4.6+ preferred — Spatial Entities landed there)
- **Godot OpenXR Vendors plugin** — provides every Meta extension used here
- **Quest 3 or Quest 3S.** Quest 2, Pro and 1 have no Depth API and cannot run this
- Space Setup completed on the headset (the app will prompt if not)

## Setup

1. Install the OpenXR Vendors plugin into `quest-app/addons/`, either from the
   Godot Asset Library ("Godot OpenXR Vendors Plugin") or from
   <https://github.com/GodotVR/godot_openxr_vendors/releases>.
2. Open `quest-app/project.godot` in Godot.
3. **Project → Install Android Build Template** (the export preset uses gradle
   builds, which the Meta extensions require).
4. Editor → Manage Export Templates, install templates for your Godot version.
5. Point the app at your host: create `user://rq4d_host.txt` containing e.g.
   `ws://192.168.1.42:8787/ws`, or edit `DEFAULT_HOST` in `scripts/main.gd`.

## Build and deploy

```bash
# with the headset connected over USB and developer mode enabled
adb devices
godot --headless --export-release "Meta Quest" build/rq4d-capture.apk
adb install -r build/rq4d-capture.apk

# USB link to the host — removes Wi-Fi variance from every measurement
adb reverse tcp:8787 tcp:8787

adb logcat -s godot
```

With `adb reverse` active, set the host URL to `ws://127.0.0.1:8787/ws`.

## What to measure first

**The depth capture rate is the open question of this project.** Godot's
documentation for `get_environment_depth_map_async` says requests should happen
"approximately every 1–2 seconds, not per-frame". If that is a hard ceiling
rather than a caution about cost, streaming depth at 15 Hz is not possible
through this API, and every bandwidth and latency figure in `docs/` rests on a
number nobody has measured.

So the first run is a measurement, not a demo:

1. Launch the app and complete calibration.
2. Read **`depth  N Hz of 15 target`** off the HUD.
3. Note whether `skipped, readback busy` appears and how fast it climbs.

| Achieved rate | What it means |
| --- | --- |
| ≥ 10 Hz | The plan holds as written. Proceed to M4. |
| 3–10 Hz | Usable. Lower `target_hz` to match, widen voxels to 3 cm, expect slower convergence. |
| ~1 Hz | The async API is a ceiling. Static room model still works; dynamic updates need the escape hatch below. |

**Escape hatch if the rate is ~1 Hz:** a GDExtension in C++ reading the
`XR_META_environment_depth` swapchain directly. It is a bounded piece of work
and disturbs nothing else — the wire format, the host and the viewer all stay
as they are, because only the producer changes.

Second measurement, once depth is flowing: the actual depth **resolution**,
logged on the first frame. `docs/PROTOCOL.md` assumes 512×512 as a placeholder
and the bandwidth table should be recomputed from the real figure.

## Structure

| File | Role |
| --- | --- |
| `scripts/wire.gd` | Binary protocol codec — must match `host/rq4d_host/wire.py` byte for byte |
| `scripts/calibration.gd` | Scene capture, spatial anchor origin, guided sweep, room profile |
| `scripts/depth_capture.gd` | Depth readback, rate control, instrumentation |
| `scripts/net_client.gd` | WebSocket transport with bounded queues and drop policy |
| `scripts/hud.gd` | The only thing on the lenses |
| `scripts/main.gd` | Lifecycle, pose streaming, control handling |

## Status

**Written but never executed.** There is no Godot install and no headset in the
environment this was authored in, so nothing here has been run — not the
scripts, not the export, not a single frame. The host half is different: that
was built and measured against a synthetic Quest, and its numbers are real.

Expect the first device run to surface API-shape mismatches, particularly
around the exact keys returned by `get_environment_depth_map_async` and the
Image format the depth arrives in. Both are handled defensively and logged
rather than assumed, so the failures should be legible.
