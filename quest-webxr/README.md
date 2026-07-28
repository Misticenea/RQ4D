# RQ4D capture client (WebXR)

The headset half, with no install. Open a URL in the Quest browser and it
starts streaming. **Nothing of the reconstruction is drawn on the lenses** —
the wearer sees passthrough plus a small status overlay.

## WebXR needs a secure context — read this first

`navigator.xr` only exists in a secure context. `https`, `localhost` and
`127.0.0.1` qualify; **a LAN address over plain http does not**. Opening
`http://192.168.1.42:8787/capture/` in the headset gives no `navigator.xr` at
all, and the page can only report "WebXR unavailable" without being able to
say why.

Two ways to satisfy it:

**Over USB — no certificate needed.**

```bash
adb reverse tcp:8787 tcp:8787
# then in the headset: http://localhost:8787/capture/
```

localhost is a secure context. This is also the link that takes Wi-Fi variance
out of every measurement, so it is the right choice for bring-up regardless.

**Wirelessly — needs https.**

```bash
python -m rq4d_host.server --tls
```

Generates a self-signed certificate covering the host's LAN address, prints
its fingerprint, and serves `https`/`wss`. The headset warns once and you
accept it. That warning is expected, not a symptom.

## Use

Start the host, then open the `capture` address it prints, in the headset:

```
==================================================================
  viewer   https://192.168.1.42:8787
  capture  https://192.168.1.42:8787/capture/
  godot    wss://192.168.1.42:8787/ws

  TLS is self-signed — the headset will warn once. Fingerprint
  starts 6D:A1:2B:EF:E0:FE:47:6F…
==================================================================
```

Tap **enter capture session**, grant the permissions, and the model appears in
the viewer on any other device.

The page connects back to whatever host served it, so there is nothing to
configure. `?host=192.168.1.42:8787` overrides it.

## Why this exists alongside the Godot client

Depth rate. Godot's CPU depth readback is documented as a 1–2 second
operation; WebXR's `getDepthInformation` is only valid *inside* the animation
frame callback, which is a per-frame contract by construction. Measured on a
Quest 3, WebXR depth is realtime — which is what
[RISKS.md R-10](../docs/RISKS.md) was waiting on.

See [ADR-009](../docs/DECISIONS.md) for the full comparison.

## What it does

- `immersive-ar` session with `depth-sensing` (cpu-optimized), `local-floor`,
  and optional `anchors`, `plane-detection`, `mesh-detection`, `dom-overlay`
- Builds a `RoomProfile` from detected planes and meshes with their semantic
  labels, plus a persistent anchor as the world origin ([ADR-003](../docs/DECISIONS.md))
- Streams `PoseFrame` at 30 Hz and `DepthFrame` at a configurable rate
- Drops frames rather than queueing them when the link falls behind — a late
  depth frame describes a moment the wearer has already left

## Requirements

Quest 3 or 3S. The depth module does not exist on Quest 2 or Pro, and neither
does the hardware behind it.

## Structure

| File | Role |
| --- | --- |
| `src/encode.js` | Frame encoding — must agree byte-for-byte with `host/rq4d_host/wire.py` |
| `src/capture.js` | Session, calibration, transport, overlay |
| `index.html` | Entry point and DOM overlay |

`encode.js` is deliberately free of DOM and session code so it can be tested
directly: `host/tests/test_webxr_encode.py` bundles it, encodes real frames in
Node, and decodes them with the host's own `wire.py`. That test found a genuine
bug on its first run — the host was ignoring `depth_scale` on float frames,
which would have scaled every reconstruction wrong.

## Known unknown

The depth buffer is not required to cover the view rectangle exactly;
`normDepthBufferFromNormView` describes the mapping. This client currently
builds rays as though it were identity, and **measures the deviation on every
first frame**, warning on the overlay if it is not. If that warning appears,
the mapping has to be applied before the geometry will sit in the right place.

## Single-file build

```bash
python tools/build_viewer.py   # -> quest-webxr/dist/rq4d-capture.html
```

Everything inlined, no external fetches. Useful for hosting the page somewhere
else — but note it still has to be *served* from a secure context. Opened from
`file://` it cannot start a session.

## Status

Runs in a desktop browser far enough to connect to the host and correctly
refuse the session (`immersive-ar not supported`). The encoding path is tested
against the host. Everything that requires an actual headset — depth, anchors,
planes, meshes — is unverified.
