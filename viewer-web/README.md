# RQ4D web viewer

Renders the live reconstruction in a browser. Served by the host itself, on the
same port as the WebSocket — **one address**, which the headset displays on its
status panel so it can be read off the lenses and typed into any device on the
same Wi-Fi.

```
  headset  ──ws──►  host  ──ws──►  browser
                     │
                     └── serves this page over HTTP on the same port
```

## Use

Start the host, then open the address it prints:

```
====================================================
  viewer   http://192.168.1.42:8787
  headset  ws://192.168.1.42:8787/ws
====================================================
```

The page connects back to whatever host served it, so there is nothing to
configure and no way to end up pointed at a different host than the one whose
data you are looking at. `?host=ws://other:8787/ws` overrides it if needed.

## Controls

| | |
| --- | --- |
| **solid / freshness / wireframe** | Freshness tints chunks by time since last update — green is live, grey is stale |
| **cut** | Hides chunks above a height. Defaults just under the ceiling, because a reconstructed room is a closed box and from outside that is all you see |
| **room** | The calibration profile — bounds, walls, furniture — drawn before any live geometry arrives |
| **recalibrate** | Sends the command through the host to the headset |
| drag / shift-drag / scroll | orbit / pan / zoom |

The orange marker is the wearer. It is the most useful thing on screen for
understanding why a region is or is not filling in: geometry only appears where
someone has looked.

## No internet required

three.js is vendored in `vendor/` rather than loaded from a CDN. A headset and
a laptop on a home network is the normal case, and that network often has no
route out.

## Verified

Run end to end in this repo against the synthetic Quest, in Chromium:

- 442 chunks, 1.8M triangles, 17 updates/s
- headset pose stream at 8.0 Hz, matching the producer exactly
- host telemetry live in the panel
- no console errors

## Structure

| File | Role |
| --- | --- |
| `src/wire.js` | Binary decoder — must match `host/rq4d_host/wire.py` |
| `src/chunks.js` | Applies mesh deltas, disposes GPU buffers, freshness and clipping |
| `src/scene.js` | Renderer, orbit controls, room profile, headset gizmo |
| `src/app.js` | WebSocket, message dispatch, stats, controls |
