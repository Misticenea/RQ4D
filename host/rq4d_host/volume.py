"""Chunked TSDF volume with ray-band integration and free-space carving.

Two design choices carry the realtime behaviour:

**Ray-band integration.** The naive projective TSDF walks every voxel in the
camera frustum and projects it into the depth image — cost scales with room
volume, which is exactly backwards. Here we unproject the depth pixels instead
and touch only a band of +-`trunc` voxels around each measured surface point.
Cost scales with observed pixels, so it is bounded by the sensor rather than by
how big the room is.

**Carving is integration, not a special case.** Samples between the camera and
the surface are integrated at +1 with a reduced weight. A surface that stops
being observed is progressively erased by the free space now measured through
it, which is what makes a moved chair actually move instead of leaving a ghost.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .wire import DepthFrame

ChunkKey = tuple[int, int, int]

# Chunk coordinates are signed; bias them into a positive range so three of
# them pack into one int64 *alongside* the local voxel index. The widths are
# tight on purpose: three 14-bit fields (42 bits) plus a 15-bit local index
# is 57 bits, leaving headroom in a signed int64. Wider fields overflow once
# the local index is shifted in, which silently writes geometry to wrong
# coordinates rather than failing.
_KEY_BITS = 14
_KEY_BIAS = 1 << (_KEY_BITS - 1)  # +-8192 chunks, ~+-5 km at 0.64 m
_KEY_MASK = (1 << _KEY_BITS) - 1


@dataclass(slots=True)
class Chunk:
    key: ChunkKey
    origin: np.ndarray  # world-space corner, ANCHOR frame
    tsdf: np.ndarray  # (C,C,C) float32 in [-1, 1]
    weight: np.ndarray  # (C,C,C) float32
    version: int = 0
    dirty: bool = False
    last_update_ns: int = 0
    # Flat indices of voxels that could hold a stale surface. Cached because
    # most chunks in view are untouched on any given frame, and recomputing
    # the mask over every voxel of every visible chunk is the dominant cost
    # of the carve pass. Invalidated by any write to the chunk.
    solid_idx: np.ndarray | None = None


@dataclass
class VolumeConfig:
    voxel_size: float = 0.02
    chunk_voxels: int = 32  # -> 0.64 m chunks at 2 cm
    trunc_voxels: float = 3.0  # truncation band, in voxels

    # Weight saturation is a responsiveness ceiling, not just a memory bound:
    # the higher it is, the longer the volume takes to accept that the room
    # changed. 12 keeps noise averaging useful while staying reactive.
    max_weight: float = 12.0
    contradiction_decay: float = 0.35  # history discount on a sign disagreement
    contradiction_margin: float = 0.2  # below this, disagreement is just noise

    # Surface band. `depth_stride` subsamples the depth image; the jittered
    # offset means successive frames cover different pixels, so detail is
    # preserved across time rather than lost per frame. This is the main knob
    # for keeping cost flat as sensor resolution rises.
    depth_stride: int = 1

    # Carving is projective: currently-solid voxels are projected into the
    # depth image and erased where the measurement lies behind them. Cost
    # scales with reconstructed surface area, so it can afford to run often.
    carve_weight: float = 0.4
    carve_candidate_max: float = 0.5  # only voxels at/behind a surface qualify
    carve_every_n: int = 2

    # A chunk is re-meshed only when a voxel near the iso-surface moved by
    # more than this. Writes deep in free space change nothing extractable.
    dirty_epsilon: float = 0.02
    dirty_band: float = 0.95

    min_depth: float = 0.25
    max_depth: float = 5.0

    @property
    def chunk_extent(self) -> float:
        return self.voxel_size * self.chunk_voxels

    @property
    def trunc(self) -> float:
        return self.voxel_size * self.trunc_voxels


class TSDFVolume:
    """Sparse voxel-hashed TSDF. Chunks are allocated on first observation."""

    def __init__(self, config: VolumeConfig | None = None, bounds: np.ndarray | None = None):
        self.cfg = config or VolumeConfig()
        c = self.cfg.chunk_voxels
        if c & (c - 1):
            raise ValueError(f"chunk_voxels must be a power of two, got {c}")
        self.chunks: dict[ChunkKey, Chunk] = {}
        self.dirty: set[ChunkKey] = set()
        self.bounds = bounds  # optional (2,3) AABB clamp, from the RoomProfile
        self._frame_counter = 0
        self._rng = np.random.default_rng(1234)
        self._c = self.cfg.chunk_voxels
        self._shift = c.bit_length() - 1
        self._local_shape = (self._c, self._c, self._c)
        self._grid: np.ndarray | None = None
        self._table: tuple[list[ChunkKey], np.ndarray] = ([], np.empty((0, 3), np.float32))
        self._table_n = -1
        self.stats_last_samples = 0
        self.stats_last_chunks = 0

    # -- allocation ---------------------------------------------------------

    def _chunk(self, key: ChunkKey) -> Chunk:
        c = self.chunks.get(key)
        if c is None:
            origin = np.array(key, dtype=np.float32) * self.cfg.chunk_extent
            c = Chunk(
                key=key,
                origin=origin,
                tsdf=np.ones(self._local_shape, np.float32),
                weight=np.zeros(self._local_shape, np.float32),
            )
            self.chunks[key] = c
        return c

    # -- integration --------------------------------------------------------

    def integrate(self, frame: DepthFrame, now_ns: int) -> int:
        """Fuse one depth frame. Returns the number of chunks touched."""
        self._frame_counter += 1
        cfg = self.cfg

        depth = frame.metric()
        valid = (depth > cfg.min_depth) & (depth < cfg.max_depth)
        if not valid.any():
            return 0

        rays_cam = _ray_directions(frame)  # (h, w, 3), unit -Z, camera space
        cam_to_world = frame.pose.matrix()
        rot = cam_to_world[:3, :3]
        cam_pos = cam_to_world[:3, 3]

        band_sel = valid
        if cfg.depth_stride > 1:
            band_sel = valid & _stride_mask(depth.shape, cfg.depth_stride, self._rng)
            if not band_sel.any():
                return 0

        d = depth[band_sel].astype(np.float32)  # (N,)
        dirs = rays_cam[band_sel] @ rot.T  # (N,3) world-space rays

        # Surface band: samples at d + k*voxel_size for k in [-B, B].
        b = int(round(cfg.trunc_voxels))
        offsets = np.arange(-b, b + 1, dtype=np.float32) * cfg.voxel_size
        sdf_norm = np.clip(-offsets / cfg.trunc, -1.0, 1.0)  # sdf at each k

        # (K, N) distances -> (K, N, 3) world points, flattened for scatter.
        dist = d[None, :] + offsets[:, None]
        pts = cam_pos[None, None, :] + dirs[None, :, :] * dist[:, :, None]
        sdf = np.repeat(sdf_norm, d.size).astype(np.float32)
        wts = np.ones_like(sdf)

        pts = pts.reshape(-1, 3)

        n_samples = pts.shape[0]
        touched = self._scatter(pts, sdf, wts, now_ns, allocate=True)

        if self._frame_counter % cfg.carve_every_n == 0:
            n_carved = self._carve_projective(frame, depth, valid, cam_to_world, now_ns)
            n_samples += n_carved

        self.stats_last_samples = n_samples
        self.stats_last_chunks = touched
        return touched

    def _carve_projective(
        self,
        frame: DepthFrame,
        depth: np.ndarray,
        valid: np.ndarray,
        cam_to_world: np.ndarray,
        now_ns: int,
    ) -> int:
        """Erase voxels the camera can now see straight through.

        Carving must be dense in *voxel* space, and ray marching is dense in
        *image* space — which is not the same thing at all. Rays diverge, so
        at 1.25 m a 6-pixel stride puts neighbouring samples six voxels apart
        and most voxels are simply never touched, however many samples are
        thrown at them. Measured: zero hits on a ghost voxel over eight
        frames, and a ghost that survived 100% intact.

        Projecting the voxels instead inverts the loop. Every currently-solid
        voxel in view is tested exactly once, so coverage is complete by
        construction, and the cost tracks reconstructed surface area rather
        than swept volume. Only solid voxels are candidates — empty space
        cannot hold a ghost, and skipping it is what keeps this affordable.
        """
        cfg = self.cfg
        if not self.chunks:
            return 0

        world_to_cam = np.linalg.inv(cam_to_world)
        rot = world_to_cam[:3, :3]
        trans = world_to_cam[:3, 3]

        al, ar, au, ad = frame.fov
        tl, tr = np.tan(al), np.tan(ar)
        tu, td = np.tan(au), np.tan(ad)
        h, w = depth.shape

        local = self._local_grid()
        carved = 0

        # Cull whole chunks against the frustum before touching any voxel.
        # Without this the pass costs 500 chunks x 32768 voxels per frame
        # regardless of where the wearer is looking — measured at 94 ms, which
        # dropped the achieved rate from 15 Hz to 8. Nearly all of that work is
        # on chunks behind the head.
        for key in self._visible_chunks(rot, trans, frame.fov):
            chunk = self.chunks[key]
            if chunk.solid_idx is None:
                mask = (chunk.weight > 0.5) & (chunk.tsdf < cfg.carve_candidate_max)
                chunk.solid_idx = np.flatnonzero(mask.reshape(-1)).astype(np.intp)
            sel = chunk.solid_idx
            if sel.size == 0:
                continue

            pts = chunk.origin + local[sel]  # world space
            cam = pts @ rot.T + trans
            z = -cam[:, 2]

            ok = (z > cfg.min_depth) & (z < cfg.max_depth)
            if not ok.any():
                continue

            u = (cam[:, 0] / z - tl) / (tr - tl)
            v = (cam[:, 1] / z - tu) / (td - tu)
            px = (u * w).astype(np.int32)
            py = (v * h).astype(np.int32)
            ok &= (px >= 0) & (px < w) & (py >= 0) & (py < h)
            if not ok.any():
                continue

            idx = np.flatnonzero(ok)
            measured = depth[py[idx], px[idx]]
            seen = valid[py[idx], px[idx]]

            # Free space: the surface actually measured along this pixel lies
            # well behind the voxel, so nothing can be occupying it.
            free = seen & (measured > z[idx] + cfg.trunc)
            if not free.any():
                continue

            li = sel[idx[free]].astype(np.intp)
            wt = np.full(li.size, cfg.carve_weight, np.float32)
            self._apply(chunk, key, li, wt, wt, now_ns)  # sdf = +1, so s = w
            carved += int(li.size)

        return carved

    def _visible_chunks(
        self, rot: np.ndarray, trans: np.ndarray, fov: tuple[float, float, float, float]
    ) -> list[ChunkKey]:
        """Chunk keys whose bounding sphere intersects the view frustum."""
        keys, centres = self._chunk_table()
        if not len(keys):
            return []

        cam = centres @ rot.T + trans
        z = -cam[:, 2]
        r = self.cfg.chunk_extent * 0.8661  # half diagonal of a cube

        al, ar, au, ad = fov
        keep = (z > self.cfg.min_depth - r) & (z < self.cfg.max_depth + r)
        # Side planes, normalised so the test is a true distance in metres.
        for nx, ny, nz in (
            (1.0, 0.0, np.tan(al)),
            (-1.0, 0.0, -np.tan(ar)),
            (0.0, -1.0, -np.tan(au)),
            (0.0, 1.0, np.tan(ad)),
        ):
            n = np.array([nx, ny, nz], np.float32)
            n /= np.linalg.norm(n)
            keep &= (cam @ n) > -r
            if not keep.any():
                return []
        return [keys[i] for i in np.flatnonzero(keep)]

    def _chunk_table(self) -> tuple[list[ChunkKey], np.ndarray]:
        """Chunk keys and centres, rebuilt only when the chunk set changes."""
        if self._table_n != len(self.chunks):
            keys = list(self.chunks.keys())
            centres = (
                np.array(keys, np.float32) + 0.5
            ) * self.cfg.chunk_extent if keys else np.empty((0, 3), np.float32)
            self._table = (keys, centres)
            self._table_n = len(self.chunks)
        return self._table

    def _local_grid(self) -> np.ndarray:
        """Voxel centre offsets within a chunk, (C^3, 3). Built once."""
        if self._grid is None:
            c = self._c
            a = (np.arange(c, dtype=np.float32) + 0.5) * self.cfg.voxel_size
            gx, gy, gz = np.meshgrid(a, a, a, indexing="ij")
            self._grid = np.stack([gx, gy, gz], -1).reshape(-1, 3)
        return self._grid

    def _scatter(
        self,
        pts: np.ndarray,
        sdf: np.ndarray,
        wts: np.ndarray,
        now_ns: int,
        allocate: bool = True,
    ) -> int:
        """Accumulate weighted samples into chunks.

        The whole frame collapses in one pass. Each sample gets a single int64
        key laid out as (chunk << 15) | local_voxel, so `np.unique` both folds
        duplicate hits on the same voxel *and* returns them grouped by chunk —
        one sort does the work of both, and the per-chunk update then touches
        only the voxels actually hit instead of all 32768.

        The earlier shape of this — a bincount over the full chunk per chunk
        per frame — measured 72 ms of the 95 ms spent here. Same arithmetic,
        different data layout.
        """
        cfg = self.cfg
        c = self._c
        shift = (c * c * c - 1).bit_length()  # bits needed for the local index

        if self.bounds is not None:
            inside = np.all((pts >= self.bounds[0]) & (pts <= self.bounds[1]), axis=1)
            if not inside.all():
                pts, sdf, wts = pts[inside], sdf[inside], wts[inside]
                if pts.size == 0:
                    return 0

        vidx = np.floor(pts / cfg.voxel_size).astype(np.int64)  # global voxel coords
        ckey = vidx >> self._shift  # chunk coords; arithmetic shift floors correctly
        local = vidx - (ckey << self._shift)  # 0..c-1

        packed_chunk = (
            (ckey[:, 0] + _KEY_BIAS)
            | ((ckey[:, 1] + _KEY_BIAS) << _KEY_BITS)
            | ((ckey[:, 2] + _KEY_BIAS) << (2 * _KEY_BITS))
        )
        flat_local = (local[:, 0] * c + local[:, 1]) * c + local[:, 2]
        key = (packed_chunk << shift) | flat_local

        uniq, inverse = np.unique(key, return_inverse=True)
        w_new = np.bincount(inverse, weights=wts).astype(np.float32)
        s_new = np.bincount(inverse, weights=sdf * wts).astype(np.float32)

        uniq_chunk = uniq >> shift
        uniq_local = (uniq & ((1 << shift) - 1)).astype(np.intp)

        # uniq is sorted and chunk occupies the high bits, so chunk groups are
        # already contiguous — no second grouping pass needed.
        starts = np.flatnonzero(np.diff(uniq_chunk)) + 1
        starts = np.concatenate([[0], starts, [uniq.size]])
        n_touched = 0

        for i in range(starts.size - 1):
            lo, hi = int(starts[i]), int(starts[i + 1])
            k = int(uniq_chunk[lo])
            ckey_t = (
                (k & _KEY_MASK) - _KEY_BIAS,
                ((k >> _KEY_BITS) & _KEY_MASK) - _KEY_BIAS,
                ((k >> (2 * _KEY_BITS)) & _KEY_MASK) - _KEY_BIAS,
            )
            chunk = self.chunks.get(ckey_t)
            if chunk is None:
                if not allocate:
                    continue
                chunk = self._chunk(ckey_t)

            self._apply(chunk, ckey_t, uniq_local[lo:hi], s_new[lo:hi], w_new[lo:hi], now_ns)
            n_touched += 1

        return n_touched

    def _apply(
        self,
        chunk: Chunk,
        key: ChunkKey,
        li: np.ndarray,
        s_inc: np.ndarray,
        w_inc: np.ndarray,
        now_ns: int,
    ) -> None:
        """Fold weighted observations into a chunk's voxels.

        Shared by surface integration and carving so both obey the same
        fusion rule — the update policy lives in exactly one place.
        """
        cfg = self.cfg
        t_flat = chunk.tsdf.reshape(-1)
        w_flat = chunk.weight.reshape(-1)
        t_old = t_flat[li]
        w_old = w_flat[li]

        # Plain weighted averaging cannot react to a changing room. Once a
        # surface has been observed for a few seconds its weight saturates,
        # and free-space evidence then needs dozens of hits to overcome it —
        # the chair moves and its ghost stays.
        #
        # A sign disagreement is not noise to be averaged down, it is evidence
        # that the history is wrong. Discounting the old weight on
        # contradiction resolves it in a few observations, while agreeing
        # measurements still accumulate normally.
        s_mean = np.divide(s_inc, w_inc, out=np.zeros_like(s_inc), where=w_inc > 0)
        contradicts = (
            (t_old * s_mean < 0)
            & (np.abs(t_old) > cfg.contradiction_margin)
            & (np.abs(s_mean) > cfg.contradiction_margin)
        )
        w_eff = np.where(contradicts, w_old * cfg.contradiction_decay, w_old)

        total = w_eff + w_inc
        t_upd = (t_old * w_eff + s_inc) / total
        t_flat[li] = t_upd
        w_flat[li] = np.minimum(total, cfg.max_weight)
        chunk.last_update_ns = now_ns
        chunk.solid_idx = None  # the occupancy set may have changed

        # "Dirty" must mean the extracted surface would actually differ, not
        # merely that a voxel was written. Writes deep in free space change
        # nothing extractable; treating them as dirty re-meshes the scene
        # every frame and the mesher never catches up.
        if not chunk.dirty:
            moved = np.abs(t_upd - t_old) > cfg.dirty_epsilon
            if np.any(moved & (np.abs(t_upd) < cfg.dirty_band)):
                chunk.dirty = True
                self.dirty.add(key)

    # -- extraction support -------------------------------------------------

    def chunk_block(self, key: ChunkKey) -> tuple[np.ndarray, np.ndarray] | None:
        """TSDF and weight for a chunk, padded by one voxel on the +x/+y/+z
        sides from neighbouring chunks.

        Marching cubes on a bare chunk leaves a one-voxel crack at every chunk
        boundary. The overlap costs a little copying and removes the seams.
        """
        chunk = self.chunks.get(key)
        if chunk is None:
            return None
        c = self._c
        tsdf = np.ones((c + 1, c + 1, c + 1), np.float32)
        weight = np.zeros((c + 1, c + 1, c + 1), np.float32)
        tsdf[:c, :c, :c] = chunk.tsdf
        weight[:c, :c, :c] = chunk.weight

        kx, ky, kz = key
        for dx, dy, dz in ((1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0), (1, 0, 1), (0, 1, 1), (1, 1, 1)):
            nb = self.chunks.get((kx + dx, ky + dy, kz + dz))
            if nb is None:
                continue
            sx = slice(c, c + 1) if dx else slice(0, c)
            sy = slice(c, c + 1) if dy else slice(0, c)
            sz = slice(c, c + 1) if dz else slice(0, c)
            nx = slice(0, 1) if dx else slice(0, c)
            ny = slice(0, 1) if dy else slice(0, c)
            nz = slice(0, 1) if dz else slice(0, c)
            tsdf[sx, sy, sz] = nb.tsdf[nx, ny, nz]
            weight[sx, sy, sz] = nb.weight[nx, ny, nz]
        return tsdf, weight

    def take_dirty(self) -> set[ChunkKey]:
        d, self.dirty = self.dirty, set()
        for key in d:
            chunk = self.chunks.get(key)
            if chunk is not None:
                chunk.dirty = False  # re-arm; the next real change re-adds it
        return d

    def memory_bytes(self) -> int:
        per = self._c**3 * 4 * 2
        return len(self.chunks) * per


def _stride_mask(shape: tuple[int, int], stride: int, rng: np.random.Generator) -> np.ndarray:
    """A jittered every-Nth-pixel mask.

    The random offset matters: a fixed grid would always sample the same
    pixels and permanently ignore the rest. Jittering makes the subsampling
    an integration over frames rather than a fixed loss of resolution.
    """
    h, w = shape
    oy, ox = int(rng.integers(stride)), int(rng.integers(stride))
    mask = np.zeros(shape, bool)
    mask[oy::stride, ox::stride] = True
    return mask


def _ray_directions(frame: DepthFrame) -> np.ndarray:
    """Unit ray directions per pixel in camera space (OpenXR: -Z forward).

    Cached per (fov, width, height) since it is identical for every frame from
    a given view — recomputing it per frame is pure waste at 30 Hz.
    """
    key = (frame.fov, frame.width, frame.height)
    cached = _RAY_CACHE.get(key)
    if cached is not None:
        return cached

    al, ar, au, ad = frame.fov
    tl, tr = np.tan(al), np.tan(ar)
    tu, td = np.tan(au), np.tan(ad)

    u = (np.arange(frame.width, dtype=np.float32) + 0.5) / frame.width
    v = (np.arange(frame.height, dtype=np.float32) + 0.5) / frame.height
    x = tl + u * (tr - tl)
    y = tu + v * (td - tu)  # row 0 is the top of the image
    xx, yy = np.meshgrid(x, y)

    # Depth from the Quest Depth API is distance along -Z, not euclidean range.
    # Leaving the rays at unit -Z means `cam_pos + dir * depth` lands on the
    # measured surface directly, with no per-pixel range conversion.
    dirs = np.stack([xx, yy, -np.ones_like(xx)], axis=-1).astype(np.float32)

    _RAY_CACHE[key] = dirs
    return dirs


_RAY_CACHE: dict[tuple, np.ndarray] = {}
