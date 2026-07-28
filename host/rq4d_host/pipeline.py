"""Session pipeline: ingest -> fuse -> mesh -> publish.

Stage boundaries are bounded queues with explicit drop policy, not unbounded
buffers. Under overload an unbounded queue converts into latency and keeps
converting until the stream is useless; a bounded one sheds the frames that
had already stopped being worth anything.

The drop order is a product decision, not a technical one:

    colour        dropped first  — degrades appearance only
    depth         drop oldest    — a stale depth frame is worse than none
    pose          decimated      — cheap, and the viewer needs continuity
    mesh/control  never dropped  — cumulative state; a gap corrupts the client

Fusion runs on a worker thread. numpy releases the GIL across the array
operations that dominate here, and it keeps the event loop free for network
I/O — a fusion stall must never become a transport stall.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .mesher import MeshConfig, MeshScheduler
from .metrics import Metrics, Stopwatch
from .volume import ChunkKey, TSDFVolume, VolumeConfig
from .wire import DepthFrame, MeshChunk, PoseFrame


@dataclass
class PipelineConfig:
    ingest_depth: int = 2  # newest N depth frames kept
    max_frame_age_ms: float = 250.0  # older than this is discarded unintegrated
    mesh_interval_s: float = 0.1  # 10 Hz mesh ticks
    idle_sleep_s: float = 0.002
    volume: VolumeConfig = field(default_factory=VolumeConfig)
    mesh: MeshConfig = field(default_factory=MeshConfig)


class LatestSlot:
    """Bounded latest-wins buffer.

    Overflow drops the *oldest* entry. For depth this is the correct policy:
    a frame that could not be integrated in time describes a moment that has
    already passed, and integrating it late puts stale geometry into the
    volume at a pose the wearer has already left.
    """

    def __init__(self, capacity: int = 2):
        self._cap = capacity
        self._items: list = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self.dropped = 0

    def put(self, item) -> None:
        with self._lock:
            self._items.append(item)
            while len(self._items) > self._cap:
                self._items.pop(0)
                self.dropped += 1
        self._wake.set()

    def get(self, timeout: float = 0.05):
        if self._wake.wait(timeout):
            with self._lock:
                if self._items:
                    item = self._items.pop(0)
                    if not self._items:
                        self._wake.clear()
                    return item
                self._wake.clear()
        return None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._wake.clear()


@dataclass
class ClientState:
    """Per-viewer transmission state.

    Chunk versions are tracked per client so a viewer that joins late receives
    the whole volume while established viewers keep receiving only deltas.
    """

    ident: str
    sent: dict[ChunkKey, int] = field(default_factory=dict)
    needs_keyframe: bool = True


class Session:
    """One capture session: one headset, N viewers."""

    def __init__(self, config: PipelineConfig | None = None):
        self.cfg = config or PipelineConfig()
        self.volume = TSDFVolume(self.cfg.volume)
        self.mesher = MeshScheduler(self.volume, self.cfg.mesh)
        self.metrics = Metrics()
        self.room_profile: dict | None = None

        self._depth = LatestSlot(self.cfg.ingest_depth)
        self._out: queue.Queue[MeshChunk] = queue.Queue()
        self._camera = np.zeros(3, np.float32)
        self._camera_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._clock_offset_ns = 0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rq4d-fusion", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def set_clock_offset(self, offset_ns: int) -> None:
        """Device-to-host monotonic clock offset from the handshake."""
        self._clock_offset_ns = offset_ns

    def set_room_profile(self, profile: dict) -> None:
        self.room_profile = profile
        bounds = profile.get("bounds")
        if bounds:
            self.volume.bounds = np.array(bounds, np.float32).reshape(2, 3)

    # -- ingest -------------------------------------------------------------

    def submit_depth(self, frame: DepthFrame, capture_ns: int) -> None:
        before = self._depth.dropped
        self._depth.put((frame, capture_ns))
        self.metrics.counters.frames_received += 1
        dropped = self._depth.dropped - before
        if dropped:
            self.metrics.counters.frames_dropped_backpressure += dropped

    def submit_pose(self, pose: PoseFrame) -> None:
        with self._camera_lock:
            self._camera = pose.head.position.copy()

    def camera_position(self) -> np.ndarray:
        with self._camera_lock:
            return self._camera.copy()

    # -- fusion thread ------------------------------------------------------

    def _run(self) -> None:
        next_mesh = time.perf_counter()
        while not self._stop.is_set():
            item = self._depth.get(timeout=0.02)
            if item is not None:
                self._integrate(*item)

            now = time.perf_counter()
            if now >= next_mesh:
                next_mesh = now + self.cfg.mesh_interval_s
                self._mesh_tick()
            elif item is None:
                time.sleep(self.cfg.idle_sleep_s)

    def _integrate(self, frame: DepthFrame, capture_ns: int) -> None:
        host_ns = time.monotonic_ns()
        age_ms = (host_ns - (capture_ns + self._clock_offset_ns)) / 1e6
        self.metrics.ingest_age.add(age_ms)

        if age_ms > self.cfg.max_frame_age_ms:
            self.metrics.counters.frames_dropped_stale += 1
            return

        with Stopwatch(self.metrics.integrate):
            self.volume.integrate(frame, host_ns)
        self.metrics.counters.frames_integrated += 1
        self.metrics.depth_rate.tick()
        self._frame_capture_ns = capture_ns

    def _mesh_tick(self) -> None:
        self.mesher.mark_dirty(self.volume.take_dirty())
        chunks = self.mesher.tick(self.camera_position(), time.monotonic_ns())
        self.metrics.mesh_tick.add(self.mesher.stats_last_tick_ms)
        if not chunks:
            return
        self.metrics.counters.chunks_meshed += len(chunks)
        self.metrics.chunk_rate.tick(len(chunks))
        for c in chunks:
            self._out.put(c)

    # -- publish ------------------------------------------------------------

    def drain_chunks(self, max_items: int = 256) -> list[MeshChunk]:
        out = []
        for _ in range(max_items):
            try:
                out.append(self._out.get_nowait())
            except queue.Empty:
                break
        return out

    def keyframe_chunks(self, client: ClientState) -> list[MeshChunk]:
        """Everything a freshly connected viewer needs to catch up.

        Re-meshed on demand rather than cached: the volume is the source of
        truth, and a cache of encoded chunks would be one more thing to keep
        coherent with it for no measurable gain at room scale.
        """
        out = []
        for key in list(self.volume.chunks.keys()):
            chunk = self.mesher._mesh_one(key, time.monotonic_ns())
            if chunk is not None:
                out.append(chunk)
        client.needs_keyframe = False
        return out

    def backlog(self) -> int:
        return self.mesher.stats_backlog

    def status(self) -> dict:
        return {
            "chunks": len(self.volume.chunks),
            "backlog": self.mesher.stats_backlog,
            "volume_mb": round(self.volume.memory_bytes() / 1e6, 1),
            "has_profile": self.room_profile is not None,
            **self.metrics.snapshot(),
        }
