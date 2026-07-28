"""Serves the web viewer from the same port as the WebSocket.

One port means one address. The headset displays it, you type it into any
browser on the LAN, and the page connects back to the host it was served from
with no configuration — there is no second address to get wrong, and no way
for the viewer to end up pointed at a different host than the one that served
it.

Everything is served from disk with no build step and no CDN: the viewer has
to work on a LAN with no internet, which is the normal case for a headset and
a laptop on someone's home network.
"""

from __future__ import annotations

import socket
from pathlib import Path

from websockets.datastructures import Headers
from websockets.http11 import Response

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}

WS_PATH = "/ws"


class StaticFiles:
    """Serves the viewer, the WebXR capture client, and the codec they share.

    Mounted rather than a single root so both clients can import the *same*
    `wire.js`. Two hand-maintained copies of a byte layout is precisely how a
    protocol drifts out of sync and produces geometry that decodes into
    plausible nonsense.
    """

    def __init__(self, root: Path, mounts: dict[str, Path] | None = None):
        self.root = root.resolve()
        self.mounts = {p: d.resolve() for p, d in (mounts or {}).items()}

    def response(self, path: str) -> Response | None:
        """An HTTP response for `path`, or None to let the WebSocket upgrade."""
        if path == WS_PATH or path.startswith(WS_PATH + "?"):
            return None

        clean = path.split("?", 1)[0]
        if clean in ("", "/"):
            clean = "/index.html"
        if clean == "/favicon.ico":
            # Browsers request this on every page load. Answering "no content"
            # keeps a 404 out of the console on an otherwise clean startup.
            return Response(204, "No Content", Headers({"Content-Length": "0"}))

        root = self.root
        for prefix, directory in self.mounts.items():
            if clean == prefix.rstrip("/") or clean.startswith(prefix):
                root = directory
                clean = clean[len(prefix.rstrip("/")) :] or "/"
                if clean.endswith("/"):
                    clean += "index.html"
                break

        target = (root / clean.lstrip("/")).resolve()
        # Containment check before touching the filesystem: this server binds
        # to a LAN interface, and path traversal here would hand out anything
        # readable by the process.
        if not target.is_relative_to(root) or not target.is_file():
            return _text(404, "Not Found", f"no such file: {clean}\n")

        body = target.read_bytes()
        return Response(
            200,
            "OK",
            Headers(
                {
                    "Content-Type": CONTENT_TYPES.get(target.suffix, "application/octet-stream"),
                    "Content-Length": str(len(body)),
                    "Cache-Control": "no-cache",
                }
            ),
            body,
        )


def _text(status: int, reason: str, body: str) -> Response:
    data = body.encode()
    return Response(
        status,
        reason,
        Headers({"Content-Type": "text/plain; charset=utf-8", "Content-Length": str(len(data))}),
        data,
    )


def lan_address() -> str:
    """This machine's address on the LAN.

    Opening a UDP socket toward an off-link address makes the routing table
    pick the interface that actually reaches the network; no packet is sent.
    Reading the hostname instead commonly yields 127.0.0.1, which is useless
    as something to type on another device.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1, guaranteed unrouted
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def viewer_url(port: int) -> str:
    return f"http://{lan_address()}:{port}"


def capture_url(port: int) -> str:
    """The page the *headset* opens. Shown on the viewer and logged at start."""
    return f"http://{lan_address()}:{port}/capture/"
