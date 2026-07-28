"""Time-boxed, priority-ordered chunk meshing.

The realtime property that matters here is a *bounded tick*. An unbounded
mesher re-meshes everything that changed, so a large scene change produces a
long stall exactly when responsiveness matters most. This scheduler instead
spends a fixed millisecond budget per tick on the highest-priority dirty
chunks and leaves the rest queued. Update rate degrades smoothly under load
rather than the pipeline hitching.

Priority is distance to the headset, offset by how long a chunk has been
waiting. Without the staleness term, chunks behind the wearer would never be
meshed while they keep looking around; with it, everything converges.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
from skimage.measure import marching_cubes

from .volume import ChunkKey, TSDFVolume
from .wire import MeshChunk


@dataclass
class MeshConfig:
    budget_ms: float = 8.0
    min_weight: float = 1.0  # voxels below this are treated as unobserved
    staleness_bonus_per_s: float = 2.0  # metres of priority per second waiting
    max_chunks_per_tick: int = 64


@dataclass(slots=True)
class _Pending:
    key: ChunkKey
    since: float


class MeshScheduler:
    def __init__(self, volume: TSDFVolume, config: MeshConfig | None = None):
        self.vol = volume
        self.cfg = config or MeshConfig()
        self._pending: dict[ChunkKey, _Pending] = {}
        self._versions: dict[ChunkKey, int] = {}
        self.stats_last_tick_ms = 0.0
        self.stats_last_meshed = 0
        self.stats_backlog = 0

    def mark_dirty(self, keys) -> None:
        now = time.perf_counter()
        for k in keys:
            if k not in self._pending:
                self._pending[k] = _Pending(k, now)

    def tick(self, camera_pos: np.ndarray, now_ns: int) -> list[MeshChunk]:
        """Mesh as many pending chunks as the budget allows, best first."""
        if not self._pending:
            self.stats_last_tick_ms = 0.0
            self.stats_last_meshed = 0
            self.stats_backlog = 0
            return []

        started = time.perf_counter()
        budget = self.cfg.budget_ms / 1000.0
        extent = self.vol.cfg.chunk_extent

        ordered = sorted(
            self._pending.values(),
            key=lambda p: _priority(p, camera_pos, extent, started, self.cfg),
        )

        out: list[MeshChunk] = []
        for pending in ordered[: self.cfg.max_chunks_per_tick]:
            if time.perf_counter() - started >= budget:
                break
            self._pending.pop(pending.key, None)
            chunk = self._mesh_one(pending.key, now_ns)
            if chunk is not None:
                out.append(chunk)

        self.stats_last_tick_ms = (time.perf_counter() - started) * 1000.0
        self.stats_last_meshed = len(out)
        self.stats_backlog = len(self._pending)
        return out

    def _mesh_one(self, key: ChunkKey, now_ns: int) -> MeshChunk | None:
        block = self.vol.chunk_block(key)
        if block is None:
            return None
        tsdf, weight = block

        # Unobserved voxels must read as empty, not as zero — zero is the
        # iso-level and would hallucinate surfaces at the frontier of the scan.
        field_ = np.where(weight >= self.cfg.min_weight, tsdf, 1.0).astype(np.float32)
        if field_.min() >= 0.0 or field_.max() <= 0.0:
            return None  # no zero crossing, nothing to extract

        vs = self.vol.cfg.voxel_size
        try:
            verts, faces, normals, _ = marching_cubes(
                field_, level=0.0, spacing=(vs, vs, vs), allow_degenerate=False
            )
        except (RuntimeError, ValueError):
            return None
        if len(verts) == 0 or len(faces) == 0:
            return None

        version = self._versions.get(key, 0) + 1
        self._versions[key] = version
        chunk = self.vol.chunks[key]

        return MeshChunk(
            key=key,
            version=version,
            origin=chunk.origin.copy(),
            voxel_size=vs,
            vertices=verts.astype(np.float32),
            normals=normals.astype(np.float32),
            indices=faces.astype(np.uint32).reshape(-1),
        )

    def drop(self, key: ChunkKey) -> None:
        self._pending.pop(key, None)
        self._versions.pop(key, None)


def _priority(
    pending: _Pending, camera_pos: np.ndarray, extent: float, now: float, cfg: MeshConfig
) -> float:
    centre = (np.array(pending.key, dtype=np.float32) + 0.5) * extent
    distance = float(np.linalg.norm(centre - camera_pos))
    waited = now - pending.since
    return distance - waited * cfg.staleness_bonus_per_s
