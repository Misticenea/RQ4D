"""Isolated test of the property that makes the reconstruction realtime.

A fixed camera watches a box in front of a wall, the box is removed, and the
camera keeps watching. The box must disappear from the volume.

Deliberately free of camera motion, noise and scheduling: if this fails the
fusion rule is wrong, and if it passes then any ghost seen in the full
benchmark is a coverage problem — the wearer never looked — not a fusion bug.
Separating those two is worth a dedicated test, because they look identical
from the outside and have completely different fixes.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from rq4d_host.volume import TSDFVolume, VolumeConfig  # noqa: E402
from rq4d_host.wire import DepthFrame, Pose  # noqa: E402
from synth_quest import Box, SyntheticRoom, render_depth  # noqa: E402

FOV = (-0.7, 0.7, 0.6, -0.6)
W = H = 96


def _scene(with_box: bool) -> list[Box]:
    wall = Box.make(0.0, 0.0, -3.0, 8.0, 6.0, 0.2, "wall")
    if not with_box:
        return [wall]
    return [wall, Box.make(0.0, 0.0, -1.5, 0.6, 0.6, 0.6, "box")]


def _frame(boxes: list[Box]) -> DepthFrame:
    cam_pos = np.zeros(3, np.float32)
    rot = np.eye(3, dtype=np.float32)
    d = render_depth(boxes, cam_pos, rot, FOV, W, H, noise_sigma=0.0, dropout=0.0)
    scale = 1.0 / 4000.0
    return DepthFrame(
        view_index=0,
        width=W,
        height=H,
        pose=Pose(cam_pos, np.array([0, 0, 0, 1], np.float32)),
        fov=FOV,
        depth=np.clip(d / scale, 0, 65535).astype(np.uint16),
        depth_scale=scale,
    )


def _solid_in(vol: TSDFVolume, lo, hi) -> int:
    cfg = vol.cfg
    c = cfg.chunk_voxels
    lo, hi = np.asarray(lo, np.float32), np.asarray(hi, np.float32)
    total = 0
    for key, chunk in vol.chunks.items():
        c_lo = np.asarray(key, np.float32) * cfg.chunk_extent
        if np.any(c_lo + cfg.chunk_extent < lo) or np.any(c_lo > hi):
            continue
        i0 = np.clip(np.ceil((lo - c_lo) / cfg.voxel_size), 0, c).astype(int)
        i1 = np.clip(np.floor((hi - c_lo) / cfg.voxel_size), 0, c).astype(int)
        if np.any(i1 <= i0):
            continue
        sl = tuple(slice(int(a), int(b)) for a, b in zip(i0, i1))
        total += int(((chunk.weight[sl] > 0.5) & (chunk.tsdf[sl] < 0.0)).sum())
    return total


def _run(vol: TSDFVolume, boxes, n: int) -> None:
    for _ in range(n):
        vol.integrate(_frame(boxes), time.monotonic_ns())


BOX_AABB = (
    np.array([-0.35, -0.35, -1.85], np.float32),
    np.array([0.35, 0.35, -1.15], np.float32),
)


def test_removed_object_is_carved_away():
    vol = TSDFVolume(VolumeConfig())
    _run(vol, _scene(True), 30)
    before = _solid_in(vol, *BOX_AABB)
    assert before > 500, f"box was never reconstructed ({before} voxels)"

    _run(vol, _scene(False), 30)
    after = _solid_in(vol, *BOX_AABB)

    residual = after / before
    assert residual < 0.10, (
        f"ghost survived: {after}/{before} voxels remain ({residual:.0%}). "
        "Free-space carving is not overcoming accumulated surface weight."
    )


def test_static_surface_is_not_eroded():
    """The converse: carving must not eat surfaces that are still there."""
    vol = TSDFVolume(VolumeConfig())
    _run(vol, _scene(True), 20)
    early = _solid_in(vol, *BOX_AABB)
    _run(vol, _scene(True), 40)
    late = _solid_in(vol, *BOX_AABB)

    assert late > early * 0.85, (
        f"a stationary surface eroded under continued observation: "
        f"{early} -> {late} voxels"
    )


def test_chunk_keys_survive_negative_coordinates():
    """Packing regression: chunk coords are signed and the local index is
    shifted in beside them, which overflowed int64 in an earlier layout and
    silently wrote geometry to wrong coordinates."""
    vol = TSDFVolume(VolumeConfig())
    _run(vol, _scene(True), 5)

    keys = np.array(list(vol.chunks.keys()))
    assert keys.size, "nothing was integrated"
    # Camera at the origin looking down -Z: everything must land in front.
    assert keys[:, 2].max() <= 0, f"chunks behind the camera: {keys[:, 2].max()}"
    assert np.abs(keys).max() < 64, f"implausible chunk coordinate: {np.abs(keys).max()}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
