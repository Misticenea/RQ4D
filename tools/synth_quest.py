"""A synthetic Quest: renders depth frames of a virtual room along a camera path.

This is the highest-leverage tool in the project. It lets the entire host
pipeline be built, profiled and regression-tested with no headset, no Unity
build, and no one wearing anything. Device work then only has to answer
"does the real sensor match the model", instead of also "is the fusion right".

The room includes an object that moves partway through the run. If free-space
carving works, it moves in the reconstruction; if it does not, it leaves a
ghost — a visible pass/fail for the property that makes this "realtime"
rather than "accumulating".
"""

from __future__ import annotations

import argparse
import math
import time
from dataclasses import dataclass, field

import numpy as np

from rq4d_host.wire import DepthFrame, Pose

DEFAULT_FOV = (-0.90, 0.90, 0.79, -0.79)  # radians, roughly Quest-like


@dataclass
class Box:
    lo: np.ndarray
    hi: np.ndarray
    name: str = ""

    @staticmethod
    def make(cx, cy, cz, sx, sy, sz, name="") -> "Box":
        c = np.array([cx, cy, cz], np.float32)
        h = np.array([sx, sy, sz], np.float32) * 0.5
        return Box(c - h, c + h, name)

    def translated(self, dx, dy, dz) -> "Box":
        d = np.array([dx, dy, dz], np.float32)
        return Box(self.lo + d, self.hi + d, self.name)


@dataclass
class SyntheticRoom:
    """A 6 x 2.7 x 5 m room, walls modelled as thin slabs so every surface is
    approached from outside and the ray-box test stays a plain slab test."""

    width: float = 6.0
    depth: float = 5.0
    height: float = 2.7
    boxes: list[Box] = field(default_factory=list)
    mover_index: int = -1

    def __post_init__(self):
        if self.boxes:
            return
        w, d, h, t = self.width, self.depth, self.height, 0.1
        cx, cz = w / 2, d / 2
        self.boxes = [
            Box.make(cx, -t / 2, cz, w, t, d, "floor"),
            Box.make(cx, h + t / 2, cz, w, t, d, "ceiling"),
            Box.make(-t / 2, h / 2, cz, t, h, d, "wall_x0"),
            Box.make(w + t / 2, h / 2, cz, t, h, d, "wall_x1"),
            Box.make(cx, h / 2, -t / 2, w, h, t, "wall_z0"),
            Box.make(cx, h / 2, d + t / 2, w, h, t, "wall_z1"),
            # furniture
            Box.make(1.2, 0.37, 1.0, 1.8, 0.75, 0.9, "desk"),
            Box.make(4.6, 0.40, 1.2, 2.0, 0.80, 0.95, "couch"),
            Box.make(2.9, 0.35, 3.6, 1.4, 0.70, 1.4, "table"),
            Box.make(5.4, 1.10, 4.0, 0.6, 2.2, 0.6, "shelf"),
            Box.make(0.5, 1.35, 4.2, 0.5, 1.6, 0.5, "plant"),
        ]
        self.boxes.append(Box.make(3.0, 0.45, 2.0, 0.55, 0.9, 0.55, "chair"))
        self.mover_index = len(self.boxes) - 1

    def snapshot(self, t: float, move_at: float = 12.0, move_to=(1.4, 0.0, 1.6)):
        """Boxes at time t. The chair jumps to a new spot at `move_at`."""
        boxes = list(self.boxes)
        if self.mover_index >= 0 and t >= move_at:
            boxes[self.mover_index] = boxes[self.mover_index].translated(*move_to)
        return boxes

    def bounds(self) -> list[list[float]]:
        return [[-0.3, -0.3, -0.3], [self.width + 0.3, self.height + 0.3, self.depth + 0.3]]


def render_depth(
    boxes: list[Box],
    cam_pos: np.ndarray,
    cam_rot: np.ndarray,
    fov: tuple[float, float, float, float],
    width: int,
    height: int,
    noise_sigma: float = 0.004,
    dropout: float = 0.02,
    max_range: float = 6.0,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Z-depth in metres, float32. Zero means no return."""
    rng = rng or np.random.default_rng(0)
    al, ar, au, ad = fov
    u = (np.arange(width, dtype=np.float32) + 0.5) / width
    v = (np.arange(height, dtype=np.float32) + 0.5) / height
    x = math.tan(al) + u * (math.tan(ar) - math.tan(al))
    y = math.tan(au) + v * (math.tan(ad) - math.tan(au))
    xx, yy = np.meshgrid(x, y)
    dirs = np.stack([xx, yy, -np.ones_like(xx)], -1).reshape(-1, 3)  # unit -Z
    dirs = dirs @ cam_rot.T  # to world

    n = dirs.shape[0]
    best = np.full(n, np.inf, np.float32)
    inv = np.divide(1.0, dirs, out=np.full_like(dirs, np.inf), where=np.abs(dirs) > 1e-9)

    for box in boxes:
        t1 = (box.lo - cam_pos) * inv
        t2 = (box.hi - cam_pos) * inv
        tmin = np.maximum.reduce(np.minimum(t1, t2), axis=1)
        tmax = np.minimum.reduce(np.maximum(t1, t2), axis=1)
        hit = (tmax >= np.maximum(tmin, 0.0)) & (tmin > 0.0)
        np.minimum(best, np.where(hit, tmin, np.inf), out=best)

    depth = np.where(np.isfinite(best) & (best < max_range), best, 0.0).astype(np.float32)
    ok = depth > 0
    if noise_sigma > 0:
        # Stereo depth error grows with the square of range.
        sigma = noise_sigma * (depth / 2.0) ** 2
        depth = np.where(ok, depth + rng.normal(0, 1, n).astype(np.float32) * sigma, 0.0)
    if dropout > 0:
        depth = np.where(rng.random(n) < dropout, 0.0, depth)
    return depth.reshape(height, width)


def yaw_pitch_quat(yaw: float, pitch: float) -> np.ndarray:
    cy, sy = math.cos(yaw * 0.5), math.sin(yaw * 0.5)
    cp, sp = math.cos(pitch * 0.5), math.sin(pitch * 0.5)
    # yaw about +Y then pitch about +X
    return np.array([sp * cy, cp * sy, -sp * sy, cp * cy], np.float32)


def quat_matrix(q: np.ndarray) -> np.ndarray:
    x, y, z, w = q
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        np.float32,
    )


@dataclass
class CameraPath:
    """A wearer walking a slow loop while looking around — roughly what the
    calibration sweep asks for, and what produces usable coverage."""

    room: SyntheticRoom
    period: float = 24.0  # one lap of the room
    look_period: float = 7.0  # one full head rotation
    radius: float = 1.5
    eye_height: float = 1.6

    def pose_at(self, t: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        a = 2 * math.pi * (t / self.period)
        cx, cz = self.room.width / 2, self.room.depth / 2
        pos = np.array(
            [
                cx + self.radius * math.cos(a),
                self.eye_height + 0.04 * math.sin(a * 6.0),
                cz + self.radius * math.sin(a),
            ],
            np.float32,
        )
        # Gaze is decoupled from the walk and turns through a full circle. An
        # orbit that only ever looks outward never observes the middle of the
        # room, which silently leaves every central object unreconstructed.
        yaw = 2 * math.pi * (t / self.look_period)
        pitch = 0.4 * math.sin(2 * math.pi * t / (self.look_period * 0.61))
        q = yaw_pitch_quat(yaw, pitch)
        return pos, q, quat_matrix(q)


class SyntheticQuest:
    """Produces protocol-shaped DepthFrames from the virtual room."""

    def __init__(
        self,
        width: int = 160,
        height: int = 160,
        fov=DEFAULT_FOV,
        seed: int = 7,
        room: SyntheticRoom | None = None,
    ):
        self.room = room or SyntheticRoom()
        self.path = CameraPath(self.room)
        self.width, self.height, self.fov = width, height, fov
        self.rng = np.random.default_rng(seed)
        self.depth_scale = 1.0 / 4000.0

    def frame_at(self, t: float) -> tuple[DepthFrame, np.ndarray]:
        pos, quat, rot = self.path.pose_at(t)
        boxes = self.room.snapshot(t)
        d = render_depth(
            boxes, pos, rot, self.fov, self.width, self.height, rng=self.rng
        )
        raw = np.clip(d / self.depth_scale, 0, 65535).astype(np.uint16)
        frame = DepthFrame(
            view_index=0,
            width=self.width,
            height=self.height,
            pose=Pose(pos, quat),
            fov=self.fov,
            depth=raw,
            depth_scale=self.depth_scale,
        )
        return frame, pos

    def room_profile(self) -> dict:
        return {
            "profile_id": "synthetic-0001",
            "anchor_uuid": "synthetic-anchor",
            "bounds": self.room.bounds(),
            "floor_height": 0.0,
            "ceiling_height": self.room.height,
            "planes": [
                {"label": b.name, "lo": b.lo.tolist(), "hi": b.hi.tolist()}
                for b in self.room.boxes[:6]
            ],
            "volumes": [
                {"label": b.name, "lo": b.lo.tolist(), "hi": b.hi.tolist()}
                for b in self.room.boxes[6:]
            ],
            "depth_intrinsics": {
                "width": self.width,
                "height": self.height,
                "fov": list(self.fov),
            },
            "device": {"model": "synthetic", "app_version": "0.1"},
            "coverage_pct": 100.0,
        }


async def _stream(args) -> None:
    import websockets

    from rq4d_host.wire import MsgType, encode, encode_json

    quest = SyntheticQuest(args.width, args.height)
    async with websockets.connect(args.url, max_size=None) as ws:
        await ws.send(
            encode_json(
                MsgType.HELLO,
                {
                    "protocol_version": "0.1",
                    "device": "synthetic",
                    "depth": {"width": args.width, "height": args.height},
                    "clock_ns": time.monotonic_ns(),
                },
                time.monotonic_ns(),
            )
        )
        await ws.recv()
        await ws.send(
            encode_json(MsgType.ROOM_PROFILE, quest.room_profile(), time.monotonic_ns())
        )

        t0 = time.perf_counter()
        interval = 1.0 / args.hz
        next_at = t0
        sent = 0
        while args.duration <= 0 or (time.perf_counter() - t0) < args.duration:
            now = time.perf_counter()
            if now < next_at:
                import asyncio

                await asyncio.sleep(next_at - now)
            next_at += interval
            frame, _ = quest.frame_at(time.perf_counter() - t0)
            await ws.send(encode(MsgType.DEPTH_FRAME, frame.pack(), time.monotonic_ns()))
            sent += 1
            if sent % 30 == 0:
                print(f"sent {sent} frames")


def main() -> None:
    p = argparse.ArgumentParser(description="Synthetic Quest depth streamer")
    p.add_argument("--url", default="ws://127.0.0.1:8787")
    p.add_argument("--hz", type=float, default=15.0)
    p.add_argument("--width", type=int, default=160)
    p.add_argument("--height", type=int, default=160)
    p.add_argument("--duration", type=float, default=30.0)
    args = p.parse_args()

    import asyncio

    asyncio.run(_stream(args))


if __name__ == "__main__":
    main()
