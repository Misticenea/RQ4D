"""Bundles the viewer into one self-contained HTML file.

The served viewer needs no build step — the host hands out `index.html` and the
modules beside it. This build exists for the other case: a single file you can
copy to a laptop, a phone, a USB stick, and open directly, with three.js and
every module inlined and no network fetch of any kind.

Opened from disk there is no origin to infer the host from, so the page asks
for the address once and remembers it.

    python tools/build_viewer.py
    -> viewer-web/dist/rq4d-viewer.html

Requires esbuild, which is fetched on demand if it is not already present.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIEWER = ROOT / "viewer-web"
CAPTURE = ROOT / "quest-webxr"
OUT = VIEWER / "dist" / "rq4d-viewer.html"
CAPTURE_OUT = CAPTURE / "dist" / "rq4d-capture.html"


def find_esbuild() -> str:
    for candidate in (
        shutil.which("esbuild"),
        str(ROOT / "node_modules/.bin/esbuild"),
        str(VIEWER / "node_modules/.bin/esbuild"),
    ):
        if candidate and Path(candidate).exists():
            return candidate

    print("esbuild not found, installing into viewer-web/…", file=sys.stderr)
    subprocess.run(
        ["npm", "install", "--silent", "--no-audit", "--no-fund", "esbuild"],
        cwd=VIEWER,
        check=True,
    )
    return str(VIEWER / "node_modules/.bin/esbuild")


def bundle(esbuild: str, minify: bool, entry: Path) -> str:
    """Everything reachable from `entry`, dependencies included, as one IIFE."""
    cmd = [
        esbuild,
        str(entry),
        "--bundle",
        "--format=iife",
        "--target=es2020",
        "--log-level=warning",
        f"--alias:rq4d/wire={ROOT / 'protocol/js/wire.js'}",
    ]
    if minify:
        cmd.append("--minify")
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    return result.stdout


def build(minify: bool = True, source: Path = VIEWER, out: Path = OUT) -> Path:
    esbuild = find_esbuild()
    entry = source / ("src/app.js" if (source / "src/app.js").exists() else "src/capture.js")
    js = bundle(esbuild, minify, entry)
    html = (source / "index.html").read_text()

    # The module tag is what pulls in the separate files; replace it with the
    # bundle. A plain <script> rather than type=module because the IIFE has no
    # imports left and module scripts are blocked on file:// in some browsers.
    # A callable replacement, not a string: bundled JS is full of backslashes
    # that re.sub would otherwise read as escape sequences and reject.
    # The import map only exists to resolve the shared codec at serve time;
    # the bundle has it inlined, so drop it rather than ship a dead mapping.
    html = re.sub(r'\s*<script type="importmap">.*?</script>', "", html, flags=re.S)
    html, count = re.subn(
        r'\s*<script type="module" src="[^"]*"></script>',
        lambda _: "\n  <script>\n" + js.rstrip() + "\n  </script>",
        html,
        count=1,
    )
    if count != 1:
        raise SystemExit("could not find the module script tag in index.html")
    html = html.replace(
        "<title>RQ4D viewer</title>",
        "<title>RQ4D viewer</title>\n"
        '<meta name="description" content="Self-contained RQ4D viewer — no network fetches.">',
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--no-minify", action="store_true", help="readable output, ~3x larger")
    args = p.parse_args()

    targets = [(VIEWER, OUT), (CAPTURE, CAPTURE_OUT)]
    for source, destination in targets:
        out = build(minify=not args.no_minify, source=source, out=destination)

        # A single external fetch would defeat the purpose, so check rather
        # than assume: no src=, no href= to anything but an anchor.
        leaks = re.findall(r'(?:src|href)="(?!#)([^"]+)"', out.read_text())
        if leaks:
            print(f"WARNING: external references remain in {out.name}: {leaks}", file=sys.stderr)
            return 1
        print(f"{out.relative_to(ROOT)}  {out.stat().st_size / 1024:.0f} KB  (self-contained)")

    print(
        "\nNote: the capture page needs a *secure context* — https, or localhost\n"
        "via `adb reverse`. Opened from file:// or plain http on a LAN address,\n"
        "navigator.xr does not exist and WebXR cannot start."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
