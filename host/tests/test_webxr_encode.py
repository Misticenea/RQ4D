"""Cross-language check: the WebXR client's frames must decode in Python.

The GDScript client's byte layout is only pinned by a comment and a Python-side
test; nothing proves the two agree until a headset produces garbled geometry.
The WebXR client can do better, because its encoder is plain JavaScript that
runs here: this bundles it with esbuild, encodes real frames in Node, and
decodes them with the same `wire.py` the host uses.

A layout mismatch shows up as a failing assert on a laptop instead of as
plausible nonsense on a headset.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "host"))

from rq4d_host.wire import (  # noqa: E402
    DepthEncoding,
    DepthFrame,
    MsgType,
    PoseFrame,
    decode,
)

ENCODE_JS = ROOT / "quest-webxr" / "src" / "encode.js"
WIRE_JS = ROOT / "protocol" / "js" / "wire.js"


def find_esbuild() -> str | None:
    for candidate in (
        shutil.which("esbuild"),
        str(ROOT / "node_modules/.bin/esbuild"),
        str(ROOT / "viewer-web/node_modules/.bin/esbuild"),
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return None


requires_node = pytest.mark.skipif(
    shutil.which("node") is None or find_esbuild() is None,
    reason="needs node and esbuild (see tools/build_viewer.py)",
)


# Stands in for the WebXR objects the encoder reads. Only the fields the
# encoder actually touches are modelled — a fuller fake would mostly assert
# that the fake matches itself.
FIXTURE = """
const DEPTH_W = 8, DEPTH_H = 6;
const pos = { x: 1.5, y: 2.25, z: -3.5 };
const rot = { x: 0, y: 0, z: 0, w: 1 };

// Column-major perspective matrix with a deliberately asymmetric frustum, so
// a symmetric-frustum assumption in the encoder would show up here.
const proj = new Float32Array(16);
proj[0] = 1.6; proj[5] = 1.2; proj[8] = 0.25; proj[9] = -0.1;
proj[10] = -1.0; proj[11] = -1.0; proj[14] = -0.2;

const xrView = { transform: { position: pos, orientation: rot }, projectionMatrix: proj };

const raw = new Float32Array(DEPTH_W * DEPTH_H);
for (let i = 0; i < raw.length; i++) raw[i] = i * 0.25;
const depth = {
  data: raw.buffer,
  width: DEPTH_W,
  height: DEPTH_H,
  rawValueToMeters: 0.5,
  normDepthBufferFromNormView: { matrix: new Float32Array([
    1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1,
  ]) },
};

const viewerPose = { transform: { position: pos, orientation: rot }, views: [xrView] };

const out = {
  depth: Array.from(new Uint8Array(encodeDepthFrame(depth, xrView, 0))),
  pose: Array.from(new Uint8Array(encodePoseFrame(viewerPose))),
  fov: Array.from(fovFromProjection(proj)),
  drift: checkAlignmentMatrix(depth),
  expectedMetres: Array.from(raw).map((v) => v * depth.rawValueToMeters),
};
process.stdout.write(JSON.stringify(out));
"""


def run_encoder() -> dict:
    esbuild = find_esbuild()
    with tempfile.TemporaryDirectory() as tmp:
        entry = Path(tmp) / "entry.mjs"
        entry.write_text(
            f"import {{ encodeDepthFrame, encodePoseFrame, fovFromProjection, "
            f"checkAlignmentMatrix }} from {json.dumps(str(ENCODE_JS))};\n{FIXTURE}"
        )
        bundle = Path(tmp) / "bundle.mjs"
        subprocess.run(
            [
                esbuild,
                str(entry),
                "--bundle",
                "--format=esm",
                "--platform=node",
                f"--alias:rq4d/wire={WIRE_JS}",
                f"--outfile={bundle}",
                "--log-level=error",
            ],
            check=True,
            capture_output=True,
        )
        result = subprocess.run(
            ["node", str(bundle)], check=True, capture_output=True, text=True
        )
    return json.loads(result.stdout)


@requires_node
def test_depth_frame_decodes_in_python():
    out = run_encoder()
    frame = decode(bytes(out["depth"]))
    assert frame.type == MsgType.DEPTH_FRAME

    depth = DepthFrame.unpack(frame.payload)
    assert (depth.width, depth.height) == (8, 6)
    assert depth.encoding == DepthEncoding.RAW_F32
    assert depth.view_index == 0

    # The decisive check: metres out of the host must match metres the browser
    # intended, including rawValueToMeters.
    np.testing.assert_allclose(
        depth.metric().ravel(), np.array(out["expectedMetres"], np.float32), rtol=1e-6
    )


@requires_node
def test_depth_frame_pose_and_fov_survive():
    out = run_encoder()
    depth = DepthFrame.unpack(decode(bytes(out["depth"])).payload)

    np.testing.assert_allclose(depth.pose.position, [1.5, 2.25, -3.5], rtol=1e-6)
    np.testing.assert_allclose(depth.pose.rotation, [0, 0, 0, 1], atol=1e-7)
    # Asymmetric on purpose; a symmetric-frustum bug would make these mirror.
    np.testing.assert_allclose(depth.fov, out["fov"], rtol=1e-6)
    assert depth.fov[0] != pytest.approx(-depth.fov[1]), "frustum should be asymmetric"


@requires_node
def test_pose_frame_decodes_in_python():
    out = run_encoder()
    frame = decode(bytes(out["pose"]))
    assert frame.type == MsgType.POSE_FRAME

    pose = PoseFrame.unpack(frame.payload)
    np.testing.assert_allclose(pose.head.position, [1.5, 2.25, -3.5], rtol=1e-6)
    assert len(pose.views) == 1
    np.testing.assert_allclose(pose.fovs[0], out["fov"], rtol=1e-6)


@requires_node
def test_alignment_check_reports_identity_as_aligned():
    """The client warns when the depth buffer is not view-aligned. Confirm the
    detector reads a clean identity as zero drift, so the warning means
    something when it does fire."""
    assert run_encoder()["drift"] == pytest.approx(0.0, abs=1e-6)
