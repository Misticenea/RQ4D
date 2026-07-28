"""Latency and throughput instrumentation.

Every stage stamps the frame it handles, so end-to-end latency is measured
rather than estimated. This exists from the first commit on purpose: a
pipeline whose latency you cannot see is a pipeline you cannot keep realtime.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


class Rolling:
    """Fixed-window sample buffer with percentiles.

    Deliberately not a full histogram — a few thousand recent samples answer
    "is it fast right now", which is the only question that matters live.
    """

    __slots__ = ("_v", "name", "unit")

    def __init__(self, name: str, unit: str = "ms", window: int = 512):
        self.name = name
        self.unit = unit
        self._v: deque[float] = deque(maxlen=window)

    def add(self, value: float) -> None:
        self._v.append(value)

    def __len__(self) -> int:
        return len(self._v)

    def summary(self) -> dict:
        if not self._v:
            return {"name": self.name, "unit": self.unit, "n": 0}
        s = sorted(self._v)
        n = len(s)
        return {
            "name": self.name,
            "unit": self.unit,
            "n": n,
            "mean": round(sum(s) / n, 2),
            "p50": round(s[n // 2], 2),
            "p95": round(s[min(n - 1, int(n * 0.95))], 2),
            "max": round(s[-1], 2),
        }


class Rate:
    """Events per second over a sliding wall-clock window."""

    __slots__ = ("_t", "name", "window")

    def __init__(self, name: str, window: float = 4.0):
        self.name = name
        self.window = window
        self._t: deque[float] = deque()

    def tick(self, n: int = 1) -> None:
        now = time.perf_counter()
        for _ in range(n):
            self._t.append(now)
        self._trim(now)

    def _trim(self, now: float) -> None:
        cutoff = now - self.window
        while self._t and self._t[0] < cutoff:
            self._t.popleft()

    def value(self) -> float:
        now = time.perf_counter()
        self._trim(now)
        if len(self._t) < 2:
            return 0.0
        span = now - self._t[0]
        return len(self._t) / span if span > 0 else 0.0


@dataclass
class Counters:
    frames_received: int = 0
    frames_integrated: int = 0
    frames_dropped_stale: int = 0
    frames_dropped_backpressure: int = 0
    chunks_meshed: int = 0
    chunks_sent: int = 0
    bytes_sent: int = 0


@dataclass
class Metrics:
    """One of these per session."""

    ingest_age: Rolling = field(default_factory=lambda: Rolling("ingest_age"))
    integrate: Rolling = field(default_factory=lambda: Rolling("integrate"))
    mesh_tick: Rolling = field(default_factory=lambda: Rolling("mesh_tick"))
    publish: Rolling = field(default_factory=lambda: Rolling("publish"))
    end_to_end: Rolling = field(default_factory=lambda: Rolling("end_to_end"))
    depth_rate: Rate = field(default_factory=lambda: Rate("depth_hz"))
    chunk_rate: Rate = field(default_factory=lambda: Rate("chunk_hz"))
    counters: Counters = field(default_factory=Counters)

    def snapshot(self) -> dict:
        return {
            "stages": {
                r.name: r.summary()
                for r in (
                    self.ingest_age,
                    self.integrate,
                    self.mesh_tick,
                    self.publish,
                    self.end_to_end,
                )
            },
            "rates": {
                "depth_hz": round(self.depth_rate.value(), 1),
                "chunk_hz": round(self.chunk_rate.value(), 1),
            },
            "counters": vars(self.counters).copy(),
        }

    def render(self) -> str:
        s = self.snapshot()
        c = s["counters"]
        lines = [
            f"depth {s['rates']['depth_hz']:5.1f} Hz   "
            f"chunks {s['rates']['chunk_hz']:6.1f} /s   "
            f"sent {c['bytes_sent'] / 1e6:6.2f} MB",
            f"  drops: stale {c['frames_dropped_stale']}  "
            f"backpressure {c['frames_dropped_backpressure']}",
        ]
        for name in ("ingest_age", "integrate", "mesh_tick", "publish", "end_to_end"):
            st = s["stages"][name]
            if st["n"]:
                lines.append(
                    f"  {name:<12} p50 {st['p50']:6.1f} ms   "
                    f"p95 {st['p95']:6.1f} ms   max {st['max']:6.1f} ms"
                )
        return "\n".join(lines)


class Stopwatch:
    __slots__ = ("_t0", "_sink")

    def __init__(self, sink: Rolling):
        self._sink = sink

    def __enter__(self) -> "Stopwatch":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc) -> None:
        self._sink.add((time.perf_counter() - self._t0) * 1000.0)
