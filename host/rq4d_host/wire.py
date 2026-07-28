"""Binary wire codec for the RQ4D protocol.

Framing (little-endian) — every message on every transport:

    u32 length          payload bytes following the header
    u8  type            MsgType
    u8  flags           bit0 = zstd, bit1 = keyframe
    u16 reserved
    u64 timestamp_ns    capture time, device monotonic clock

Bulk payloads (depth blobs, vertex arrays) are fixed-layout binary so they can
be wrapped in a numpy view without a parse step. Control messages are JSON —
they are rare and small, and readability is worth more than microseconds there.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from enum import IntEnum

import numpy as np

HEADER = struct.Struct("<IBBHQ")
HEADER_SIZE = HEADER.size  # 16
PROTOCOL_VERSION = "0.1"


class MsgType(IntEnum):
    HELLO = 0x01
    HELLO_ACK = 0x02
    ROOM_PROFILE = 0x03
    ANCHOR_UPDATE = 0x04

    POSE_FRAME = 0x10
    DEPTH_FRAME = 0x11
    COLOR_FRAME = 0x12

    MESH_CHUNK_UPDATE = 0x20
    CHUNK_REMOVED = 0x21

    CONTROL = 0x30
    STATS = 0x31
    HEARTBEAT = 0x3F


class Flags(IntEnum):
    NONE = 0
    ZSTD = 1 << 0
    KEYFRAME = 1 << 1


class Tracking(IntEnum):
    TRACKED = 0
    LIMITED = 1
    LOST = 2


# JSON-payload message types. Everything else is fixed-layout binary.
JSON_TYPES = frozenset(
    {
        MsgType.HELLO,
        MsgType.HELLO_ACK,
        MsgType.ROOM_PROFILE,
        MsgType.ANCHOR_UPDATE,
        MsgType.CHUNK_REMOVED,
        MsgType.CONTROL,
        MsgType.STATS,
        MsgType.HEARTBEAT,
    }
)


def encode(msg_type: MsgType, payload: bytes, timestamp_ns: int, flags: int = 0) -> bytes:
    return HEADER.pack(len(payload), int(msg_type), flags, 0, timestamp_ns) + payload


def encode_json(msg_type: MsgType, obj: dict, timestamp_ns: int) -> bytes:
    return encode(msg_type, json.dumps(obj, separators=(",", ":")).encode(), timestamp_ns)


@dataclass(slots=True)
class Frame:
    """A decoded envelope. `payload` is a memoryview into the source buffer."""

    type: MsgType
    flags: int
    timestamp_ns: int
    payload: memoryview

    def json(self) -> dict:
        return json.loads(bytes(self.payload))


def decode(buf: bytes | memoryview) -> Frame:
    view = memoryview(buf)
    length, mtype, flags, _, ts = HEADER.unpack_from(view, 0)
    body = view[HEADER_SIZE : HEADER_SIZE + length]
    if len(body) != length:
        raise ValueError(f"truncated frame: want {length} bytes, got {len(body)}")
    return Frame(MsgType(mtype), flags, ts, body)


# --------------------------------------------------------------------------
# Pose
# --------------------------------------------------------------------------

_POSE = struct.Struct("<7f")  # px py pz qx qy qz qw
_FOV = struct.Struct("<4f")  # angle_left angle_right angle_up angle_down


@dataclass(slots=True)
class Pose:
    position: np.ndarray  # (3,) float32, ANCHOR frame
    rotation: np.ndarray  # (4,) float32 quaternion xyzw

    def pack(self) -> bytes:
        return _POSE.pack(*self.position, *self.rotation)

    @staticmethod
    def unpack_from(view: memoryview, offset: int) -> tuple["Pose", int]:
        v = _POSE.unpack_from(view, offset)
        return (
            Pose(np.array(v[0:3], np.float32), np.array(v[3:7], np.float32)),
            offset + _POSE.size,
        )

    def matrix(self) -> np.ndarray:
        """4x4 camera-to-world transform."""
        x, y, z, w = self.rotation
        m = np.eye(4, dtype=np.float32)
        m[:3, :3] = np.array(
            [
                [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
            ],
            dtype=np.float32,
        )
        m[:3, 3] = self.position
        return m


_POSE_FRAME_HEAD = struct.Struct("<B")


@dataclass(slots=True)
class PoseFrame:
    head: Pose
    views: list[Pose]
    fovs: list[tuple[float, float, float, float]]
    tracking: Tracking = Tracking.TRACKED

    def pack(self) -> bytes:
        out = [_POSE_FRAME_HEAD.pack(int(self.tracking)), self.head.pack()]
        for pose, fov in zip(self.views, self.fovs):
            out.append(pose.pack())
            out.append(_FOV.pack(*fov))
        return b"".join(out)

    @staticmethod
    def unpack(view: memoryview) -> "PoseFrame":
        (tracking,) = _POSE_FRAME_HEAD.unpack_from(view, 0)
        off = _POSE_FRAME_HEAD.size
        head, off = Pose.unpack_from(view, off)
        poses, fovs = [], []
        while off + _POSE.size + _FOV.size <= len(view):
            pose, off = Pose.unpack_from(view, off)
            fovs.append(_FOV.unpack_from(view, off))
            off += _FOV.size
            poses.append(pose)
        return PoseFrame(head, poses, fovs, Tracking(tracking))


# --------------------------------------------------------------------------
# Depth
# --------------------------------------------------------------------------

# view_index, encoding, hands_removed, pad, width, height, depth_scale, near, far
_DEPTH_HEAD = struct.Struct("<BBBBHHfff")


class DepthEncoding(IntEnum):
    RAW16 = 0  # uint16, metric = value * depth_scale
    ZSTD16 = 1  # same, zstd-compressed
    RAW_F32 = 2  # float32 metric metres, straight from the source image
    RAW_F32_NDC = 3  # float32 clip-space depth; needs inv_proj to linearise

    def is_float(self) -> bool:
        return self in (DepthEncoding.RAW_F32, DepthEncoding.RAW_F32_NDC)


@dataclass(slots=True)
class DepthFrame:
    """A single depth image plus the pose it was captured at.

    The pose travels *with* the frame. Nothing downstream is ever allowed to
    pair an image with "the most recent pose" — that is the single most common
    source of smeared reconstruction.
    """

    view_index: int
    width: int
    height: int
    pose: Pose
    fov: tuple[float, float, float, float]
    depth: np.ndarray  # (h, w) uint16
    depth_scale: float = 1.0 / 4000.0  # uint16 -> metres
    near: float = 0.2
    far: float = 6.0
    hands_removed: bool = False
    encoding: DepthEncoding = DepthEncoding.RAW16

    inv_proj: np.ndarray | None = None  # (4,4) inverse projection*view, NDC only

    def pack(self) -> bytes:
        head = _DEPTH_HEAD.pack(
            self.view_index,
            int(self.encoding),
            int(self.hands_removed),
            0,
            self.width,
            self.height,
            self.depth_scale,
            self.near,
            self.far,
        )
        inv = np.ascontiguousarray(
            self.inv_proj if self.inv_proj is not None else np.zeros((4, 4)), np.float32
        )
        dtype = np.float32 if self.encoding.is_float() else np.uint16
        blob = np.ascontiguousarray(self.depth, dtype=dtype).tobytes()
        return head + self.pose.pack() + _FOV.pack(*self.fov) + inv.tobytes() + blob

    @staticmethod
    def unpack(view: memoryview) -> "DepthFrame":
        (vi, enc, hands, _, w, h, scale, near, far) = _DEPTH_HEAD.unpack_from(view, 0)
        encoding = DepthEncoding(enc)
        off = _DEPTH_HEAD.size
        pose, off = Pose.unpack_from(view, off)
        fov = _FOV.unpack_from(view, off)
        off += _FOV.size
        inv = np.frombuffer(view, np.float32, 16, off).reshape(4, 4)
        off += 64
        dtype = np.float32 if encoding.is_float() else np.uint16
        depth = np.frombuffer(view, dtype, w * h, off).reshape(h, w)
        return DepthFrame(
            vi, w, h, pose, fov, depth, scale, near, far, bool(hands), encoding,
            inv if encoding == DepthEncoding.RAW_F32_NDC else None,
        )

    def metric(self) -> np.ndarray:
        """Depth in metres along -Z as float32. Zero means no measurement.

        Three source shapes, because the capture side does not get to choose:
        Meta hands back a depth texture whose units depend on the path used to
        read it, and forcing a conversion on the headset would burn CPU there
        to save none here.
        """
        if self.encoding == DepthEncoding.RAW_F32:
            # Scaled, not passed through. WebXR reports depth as raw values
            # plus a `rawValueToMeters` factor, which arrives here as
            # depth_scale; ignoring it silently doubled or halved the whole
            # reconstruction depending on the device's chosen units.
            return np.ascontiguousarray(self.depth, np.float32) * self.depth_scale
        if self.encoding == DepthEncoding.RAW_F32_NDC:
            return self._linearise()
        return self.depth.astype(np.float32) * self.depth_scale

    def _linearise(self) -> np.ndarray:
        """Clip-space depth -> metric range along -Z, via the inverse matrix.

        Unprojecting with the API's own matrix avoids decomposing it into a
        pose and a field of view first — that decomposition is where sign and
        handedness conventions silently disagree between engines.
        """
        if self.inv_proj is None:
            raise ValueError("RAW_F32_NDC depth requires inv_proj")
        h, w = self.depth.shape
        u = (np.arange(w, dtype=np.float32) + 0.5) / w * 2.0 - 1.0
        v = 1.0 - (np.arange(h, dtype=np.float32) + 0.5) / h * 2.0
        uu, vv = np.meshgrid(u, v)
        d = np.ascontiguousarray(self.depth, np.float32)

        pts = np.stack([uu, vv, d, np.ones_like(d)], -1).reshape(-1, 4)
        world = pts @ self.inv_proj.T
        w_comp = world[:, 3:4]
        np.divide(world, w_comp, out=world, where=np.abs(w_comp) > 1e-9)

        rel = world[:, :3] - self.pose.position[None, :]
        forward = self.pose.matrix()[:3, 2]  # camera -Z is forward, so -basis_z
        z = -(rel @ forward)
        z[np.abs(w_comp[:, 0]) <= 1e-9] = 0.0
        return z.reshape(h, w).astype(np.float32)


# --------------------------------------------------------------------------
# Mesh chunks
# --------------------------------------------------------------------------

# key xyz, lod, vertex_encoding, pad, version, origin xyz, voxel_size, counts
_CHUNK_HEAD = struct.Struct("<3iBBHI3ffII")


@dataclass(slots=True)
class MeshChunk:
    key: tuple[int, int, int]
    version: int
    origin: np.ndarray  # (3,) chunk origin in ANCHOR frame
    voxel_size: float
    vertices: np.ndarray  # (n, 3) float32, chunk-local metres
    normals: np.ndarray  # (n, 3) float32
    indices: np.ndarray  # (m,) uint32
    lod: int = 0
    vertex_encoding: int = 0  # 0 = float32 pos + float32 normal

    def pack(self) -> bytes:
        v = np.ascontiguousarray(self.vertices, np.float32)
        n = np.ascontiguousarray(self.normals, np.float32)
        i = np.ascontiguousarray(self.indices, np.uint32)
        head = _CHUNK_HEAD.pack(
            *self.key,
            self.lod,
            self.vertex_encoding,
            0,
            self.version,
            *self.origin,
            self.voxel_size,
            len(v),
            len(i),
        )
        return head + v.tobytes() + n.tobytes() + i.tobytes()

    @staticmethod
    def unpack(view: memoryview) -> "MeshChunk":
        (kx, ky, kz, lod, venc, _, ver, ox, oy, oz, vs, nv, ni) = _CHUNK_HEAD.unpack_from(view, 0)
        off = _CHUNK_HEAD.size
        verts = np.frombuffer(view, np.float32, nv * 3, off).reshape(nv, 3)
        off += nv * 3 * 4
        norms = np.frombuffer(view, np.float32, nv * 3, off).reshape(nv, 3)
        off += nv * 3 * 4
        idx = np.frombuffer(view, np.uint32, ni, off)
        return MeshChunk(
            (kx, ky, kz),
            ver,
            np.array([ox, oy, oz], np.float32),
            vs,
            verts,
            norms,
            idx,
            lod,
            venc,
        )

    def nbytes(self) -> int:
        return _CHUNK_HEAD.size + self.vertices.nbytes + self.normals.nbytes + self.indices.nbytes


# --------------------------------------------------------------------------
# Stream reassembly
# --------------------------------------------------------------------------


@dataclass
class FrameReader:
    """Incremental framing for stream transports. Not needed on WebSocket,
    which preserves message boundaries, but required for raw TCP and USB."""

    _buf: bytearray = field(default_factory=bytearray)

    def feed(self, data: bytes) -> list[Frame]:
        self._buf.extend(data)
        out = []
        while len(self._buf) >= HEADER_SIZE:
            length = HEADER.unpack_from(self._buf, 0)[0]
            total = HEADER_SIZE + length
            if len(self._buf) < total:
                break
            out.append(decode(bytes(self._buf[:total])))
            del self._buf[:total]
        return out
