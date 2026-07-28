# rq4d-viewer.html

The whole viewer in one file — three.js, all modules, all styles inlined. No
build step, no server, no internet. Copy it anywhere and open it.

On first open it asks for the host address. Type what the headset shows
(`192.168.1.42:8787` is enough — scheme, port and path are filled in) and it is
remembered for next time. `?host=192.168.1.42:8787` skips the prompt.

Rebuild after changing anything under `viewer-web/src/`:

    python tools/build_viewer.py

The build fails loudly if any external reference survives, because a single
fetch would defeat the point.

Verified: opened from `file://` in Chromium against a live host — 444 chunks,
1.77M triangles, pose stream at 8.3 Hz, no external requests.
