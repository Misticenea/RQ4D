"""Pins the byte layout that quest-app/scripts/wire.gd writes by hand.

The Godot side cannot be executed here — no engine, no headset — so the next
best thing is to nail down the reference side. If someone changes a struct in
wire.py, this fails and the GDScript comment block that mirrors it becomes
visibly stale, instead of the mismatch surfacing as garbled geometry on a
device three steps later.

The offsets asserted here are the ones written in the header comment of
`encode_depth_frame` in wire.gd. Keep them identical.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rq4d_host.wire import (  # noqa: E402
    HEADER_SIZE,
    DepthEncoding,
    DepthFrame,
    MsgType,
    Pose,
    PoseFrame,
    Tracking,
    decode,
    encode,
    encode_json,
)

IDENTITY = Pose(np.array([1.0, 2.0, 3.0], np.float32), np.array([0, 0, 0, 1], np.float32))
FOV = (-0.9, 0.9, 0.79, -0.79)


def test_header_is_sixteen_bytes():
    payload = b"\x00" * 7
    frame = encode(MsgType.HEARTBEAT, payload, 12345)
    assert HEADER_SIZE == 16
    assert len(frame) == 16 + 7
    length, mtype, flags, reserved, ts = struct.unpack_from("<IBBHQ", frame, 0)
    assert (length, mtype, flags, reserved, ts) == (7, int(MsgType.HEARTBEAT), 0, 0, 12345)


def test_depth_frame_field_offsets():
    """These offsets are duplicated in wire.gd. Both must agree."""
    depth = np.arange(4 * 3, dtype=np.float32).reshape(3, 4)
    df = DepthFrame(
        view_index=1,
        width=4,
        height=3,
        pose=IDENTITY,
        fov=FOV,
        depth=depth,
        depth_scale=1.0,
        near=0.2,
        far=6.0,
        hands_removed=True,
        encoding=DepthEncoding.RAW_F32_NDC,
        inv_proj=np.eye(4, dtype=np.float32),
    )
    blob = df.pack()

    assert blob[0] == 1  # view_index
    assert blob[1] == int(DepthEncoding.RAW_F32_NDC)
    assert blob[2] == 1  # hands_removed
    assert struct.unpack_from("<HH", blob, 4) == (4, 3)  # width, height
    np.testing.assert_allclose(struct.unpack_from("<fff", blob, 8), (1.0, 0.2, 6.0), rtol=1e-6)
    assert struct.unpack_from("<3f", blob, 20) == (1.0, 2.0, 3.0)  # pose position
    np.testing.assert_allclose(struct.unpack_from("<4f", blob, 48), FOV, rtol=1e-6)
    assert struct.unpack_from("<16f", blob, 64) == tuple(np.eye(4, dtype=np.float32).ravel())
    assert len(blob) == 128 + depth.nbytes  # payload starts at 128


def test_depth_frame_round_trip_float():
    depth = np.random.default_rng(0).random((6, 8)).astype(np.float32)
    # depth_scale is meaningful for float frames too: WebXR supplies metres
    # as raw * rawValueToMeters, so an already-metric frame declares 1.0.
    df = DepthFrame(
        0, 8, 6, IDENTITY, FOV, depth, depth_scale=1.0,
        encoding=DepthEncoding.RAW_F32,
    )
    back = DepthFrame.unpack(memoryview(df.pack()))
    assert back.width == 8 and back.height == 6
    assert back.encoding == DepthEncoding.RAW_F32
    np.testing.assert_allclose(back.metric(), depth)


def test_depth_frame_round_trip_uint16():
    depth = (np.arange(24, dtype=np.uint16) * 100).reshape(4, 6)
    df = DepthFrame(0, 6, 4, IDENTITY, FOV, depth, depth_scale=1.0 / 4000.0)
    back = DepthFrame.unpack(memoryview(df.pack()))
    np.testing.assert_allclose(back.metric(), depth.astype(np.float32) / 4000.0, rtol=1e-6)


def test_pose_frame_round_trip():
    pf = PoseFrame(IDENTITY, [IDENTITY, IDENTITY], [FOV, FOV], Tracking.LIMITED)
    back = PoseFrame.unpack(memoryview(pf.pack()))
    assert back.tracking == Tracking.LIMITED
    assert len(back.views) == 2
    np.testing.assert_allclose(back.head.position, IDENTITY.position)
    np.testing.assert_allclose(back.fovs[1], FOV, rtol=1e-6)


def test_json_messages_round_trip():
    payload = {"protocol_version": "0.1", "device": "quest3", "clock_ns": 99}
    frame = decode(encode_json(MsgType.HELLO, payload, 7))
    assert frame.type == MsgType.HELLO
    assert frame.timestamp_ns == 7
    assert frame.json() == payload


def test_ndc_linearisation_recovers_metric_depth():
    """A point at a known distance must come back at that distance.

    Godot hands over clip-space depth plus the inverse projection-view matrix,
    and the host linearises. Getting the handedness wrong here yields depth
    that is plausible, mirrored, and very hard to spot on a live mesh.
    """
    near, far = 0.1, 100.0
    f = 1.0 / np.tan(0.9)
    proj = np.array(
        [
            [f, 0, 0, 0],
            [0, f, 0, 0],
            [0, 0, (far + near) / (near - far), 2 * far * near / (near - far)],
            [0, 0, -1, 0],
        ],
        np.float32,
    )
    inv = np.linalg.inv(proj).astype(np.float32)

    target_z = 3.0
    clip = proj @ np.array([0.0, 0.0, -target_z, 1.0], np.float32)
    ndc_depth = clip[2] / clip[3]

    depth = np.full((5, 5), ndc_depth, np.float32)
    df = DepthFrame(
        0, 5, 5,
        Pose(np.zeros(3, np.float32), np.array([0, 0, 0, 1], np.float32)),
        FOV, depth, encoding=DepthEncoding.RAW_F32_NDC, inv_proj=inv,
    )
    centre = df.metric()[2, 2]
    assert abs(centre - target_z) < 0.02, f"expected {target_z} m, linearised to {centre}"
