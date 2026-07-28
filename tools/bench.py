"""End-to-end pipeline benchmark against the synthetic Quest.

Runs the real Session — real TSDF, real scheduler, real bounded queues — with
synthetic depth, and reports what it actually achieved rather than what the
design intends. Transport is excluded on purpose so the numbers isolate
fusion and meshing.

Also asserts the carving property: after the chair moves, its original
location must read as empty. That check is the difference between a
reconstruction that tracks the room and one that merely accumulates.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rq4d_host.pipeline import PipelineConfig, Session  # noqa: E402
from rq4d_host.volume import VolumeConfig  # noqa: E402
from synth_quest import SyntheticQuest  # noqa: E402


def solid_voxels(session: Session, lo, hi) -> int:
    """Count observed voxels inside an AABB that sit on or behind a surface.

    Counting the *surface shell* rather than sampling the interior is the
    correct probe here: a TSDF only ever writes within the truncation band
    around observed surfaces, so voxels deep inside a solid are unobserved by
    construction and say nothing about whether the object is still there.
    """
    vol = session.volume
    cfg = vol.cfg
    lo = np.asarray(lo, np.float32)
    hi = np.asarray(hi, np.float32)
    c = cfg.chunk_voxels
    total = 0

    for key, chunk in vol.chunks.items():
        c_lo = np.asarray(key, np.float32) * cfg.chunk_extent
        c_hi = c_lo + cfg.chunk_extent
        if np.any(c_hi < lo) or np.any(c_lo > hi):
            continue
        # Voxel index window of this chunk that falls inside the AABB.
        i0 = np.clip(np.ceil((lo - c_lo) / cfg.voxel_size), 0, c).astype(int)
        i1 = np.clip(np.floor((hi - c_lo) / cfg.voxel_size), 0, c).astype(int)
        if np.any(i1 <= i0):
            continue
        sl = tuple(slice(int(a), int(b)) for a, b in zip(i0, i1))
        t = chunk.tsdf[sl]
        w = chunk.weight[sl]
        total += int(((w > 0.5) & (t < 0.0)).sum())
    return total


def run(args) -> int:
    vol_cfg = VolumeConfig(voxel_size=args.voxel, chunk_voxels=args.chunk)
    cfg = PipelineConfig(volume=vol_cfg)
    cfg.mesh.budget_ms = args.budget

    session = Session(cfg)
    quest = SyntheticQuest(args.width, args.height)
    session.set_room_profile(quest.room_profile())
    session.start()

    chair = quest.room.boxes[quest.room.mover_index]
    move = np.array([1.4, 0.0, 1.6], np.float32)
    pad = np.array([0.06, 0.06, 0.06], np.float32)
    old_aabb = (chair.lo - pad, chair.hi + pad)
    new_aabb = (chair.lo + move - pad, chair.hi + move + pad)

    interval = 1.0 / args.hz
    t0 = time.perf_counter()
    next_at = t0
    produced = 0
    render_ms = []

    print(
        f"running {args.duration:.0f}s @ {args.hz:g} Hz  "
        f"depth {args.width}x{args.height}  voxel {args.voxel * 100:g} cm  "
        f"chunk {args.chunk}^3  mesh budget {args.budget:g} ms\n"
    )

    while True:
        elapsed = time.perf_counter() - t0
        if elapsed >= args.duration:
            break
        now = time.perf_counter()
        if now < next_at:
            time.sleep(min(next_at - now, 0.005))
            continue
        next_at += interval

        r0 = time.perf_counter()
        frame, cam = quest.frame_at(elapsed)
        render_ms.append((time.perf_counter() - r0) * 1000.0)

        session.submit_depth(frame, time.monotonic_ns())
        session.volume  # noqa: B018 - readability anchor
        with session._camera_lock:
            session._camera = cam.copy()
        produced += 1

        session.drain_chunks()

        if produced % int(args.hz * 4) == 0:
            print(f"[t={elapsed:5.1f}s]")
            print(session.metrics.render())
            print(
                f"  chunks {len(session.volume.chunks):4d}  "
                f"backlog {session.backlog():4d}  "
                f"volume {session.volume.memory_bytes() / 1e6:5.1f} MB"
            )
            print()

    # Let the backlog drain so the final state is fully meshed.
    deadline = time.perf_counter() + 5.0
    while session.backlog() > 0 and time.perf_counter() < deadline:
        session.drain_chunks()
        time.sleep(0.05)
    session.stop()

    m = session.metrics
    print("=" * 68)
    print("RESULT")
    print("=" * 68)
    print(session.metrics.render())
    achieved = m.counters.frames_integrated / max(args.duration, 1e-9)
    print(
        f"\n  produced {produced} frames, integrated "
        f"{m.counters.frames_integrated} ({achieved:.1f} Hz achieved)"
    )
    print(
        f"  synthetic render cost: p50 "
        f"{np.percentile(render_ms, 50):.1f} ms (excluded from pipeline numbers)"
    )
    print(f"  chunks {len(session.volume.chunks)}, meshed {m.counters.chunks_meshed}")
    print(f"  volume memory {session.volume.memory_bytes() / 1e6:.1f} MB")

    print("\n" + "=" * 68)
    print("CARVING CHECK  (chair moves at t=12s)")
    print("=" * 68)
    n_old = solid_voxels(session, *old_aabb)
    n_new = solid_voxels(session, *new_aabb)
    print(f"  solid voxels at original location  {n_old:6d}   (want low  — carved away)")
    print(f"  solid voxels at new location       {n_new:6d}   (want high — surface built)")

    ok = True
    if args.duration >= 20:
        if n_new < 200:
            print("\n  FAIL: no surface built at the new location")
            ok = False
        elif n_old > n_new * 0.4:
            print("\n  FAIL: ghost left behind at the original location")
            ok = False
        else:
            print(
                f"\n  PASS: the object moved in the reconstruction "
                f"(residual {n_old / max(n_new, 1):.1%} of the new surface)"
            )
    else:
        print("\n  (skipped: needs --duration >= 20 to pass the move at t=12s)")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--duration", type=float, default=24.0)
    p.add_argument("--hz", type=float, default=15.0)
    p.add_argument("--width", type=int, default=160)
    p.add_argument("--height", type=int, default=160)
    p.add_argument("--voxel", type=float, default=0.02)
    p.add_argument("--chunk", type=int, default=32)
    p.add_argument("--budget", type=float, default=8.0)
    return run(p.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
