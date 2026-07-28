// Binary wire decoder — the browser half of the RQ4D protocol.
//
// Byte layout must match host/rq4d_host/wire.py, which is the reference.
// Offsets are stated so a mismatch is caught by reading rather than by
// debugging a garbled mesh.
//
// Framing (little-endian):
//   u32 length | u8 type | u8 flags | u16 reserved | u64 timestamp_ns

export const HEADER_SIZE = 16;
export const PROTOCOL_VERSION = '0.1';

export const Msg = {
  HELLO: 0x01,
  HELLO_ACK: 0x02,
  ROOM_PROFILE: 0x03,
  ANCHOR_UPDATE: 0x04,
  POSE_FRAME: 0x10,
  DEPTH_FRAME: 0x11,
  COLOR_FRAME: 0x12,
  MESH_CHUNK_UPDATE: 0x20,
  CHUNK_REMOVED: 0x21,
  CONTROL: 0x30,
  STATS: 0x31,
  HEARTBEAT: 0x3f,
};

const JSON_TYPES = new Set([
  Msg.HELLO, Msg.HELLO_ACK, Msg.ROOM_PROFILE, Msg.ANCHOR_UPDATE,
  Msg.CHUNK_REMOVED, Msg.CONTROL, Msg.STATS, Msg.HEARTBEAT,
]);

const decoder = new TextDecoder();

export function decodeFrame(buffer) {
  if (buffer.byteLength < HEADER_SIZE) return null;
  const view = new DataView(buffer);
  const length = view.getUint32(0, true);
  if (buffer.byteLength < HEADER_SIZE + length) return null;

  const type = view.getUint8(4);
  // BigInt only where it is actually needed; Number is plenty for a
  // nanosecond clock over any session length a person will sit through.
  const timestampNs = Number(view.getBigUint64(8, true));
  const payload = buffer.slice(HEADER_SIZE, HEADER_SIZE + length);

  if (JSON_TYPES.has(type)) {
    let json = null;
    try {
      json = JSON.parse(decoder.decode(payload));
    } catch (err) {
      console.warn('malformed JSON payload', type, err);
    }
    return { type, timestampNs, json };
  }
  return { type, timestampNs, payload };
}

export function encodeJson(type, obj, timestampNs = 0) {
  const body = new TextEncoder().encode(JSON.stringify(obj));
  const out = new ArrayBuffer(HEADER_SIZE + body.byteLength);
  const view = new DataView(out);
  view.setUint32(0, body.byteLength, true);
  view.setUint8(4, type);
  view.setUint8(5, 0);
  view.setUint16(6, 0, true);
  view.setBigUint64(8, BigInt(timestampNs), true);
  new Uint8Array(out, HEADER_SIZE).set(body);
  return out;
}

// MeshChunkUpdate payload, struct "<3iBBHI3ffII" then three arrays:
//   0  key x,y,z (3 x i32)      12 lod u8      13 vertex_encoding u8
//  14  pad u16                  16 version u32
//  20  origin (3 x f32)         32 voxel_size f32
//  36  vertex_count u32         40 index_count u32
//  44  vertices | normals | indices
export function decodeMeshChunk(payload) {
  const view = new DataView(payload);
  const vertexCount = view.getUint32(36, true);
  const indexCount = view.getUint32(40, true);

  let off = 44;
  // Copy rather than view: the payload is a slice of the socket buffer, and
  // three.js keeps these alive for the lifetime of the geometry.
  const vertices = new Float32Array(payload.slice(off, off + vertexCount * 12));
  off += vertexCount * 12;
  const normals = new Float32Array(payload.slice(off, off + vertexCount * 12));
  off += vertexCount * 12;
  const indices = new Uint32Array(payload.slice(off, off + indexCount * 4));

  return {
    key: [view.getInt32(0, true), view.getInt32(4, true), view.getInt32(8, true)],
    lod: view.getUint8(12),
    version: view.getUint32(16, true),
    origin: [view.getFloat32(20, true), view.getFloat32(24, true), view.getFloat32(28, true)],
    voxelSize: view.getFloat32(32, true),
    vertices,
    normals,
    indices,
  };
}

// PoseFrame: u8 tracking, head pose (7 x f32), then per view (7 + 4) x f32.
export function decodePoseFrame(payload) {
  const view = new DataView(payload);
  const tracking = view.getUint8(0);
  let off = 1;

  const readPose = () => {
    const p = {
      position: [
        view.getFloat32(off, true),
        view.getFloat32(off + 4, true),
        view.getFloat32(off + 8, true),
      ],
      quaternion: [
        view.getFloat32(off + 12, true),
        view.getFloat32(off + 16, true),
        view.getFloat32(off + 20, true),
        view.getFloat32(off + 24, true),
      ],
    };
    off += 28;
    return p;
  };

  const head = readPose();
  const views = [];
  while (off + 28 + 16 <= payload.byteLength) {
    const pose = readPose();
    const fov = [
      view.getFloat32(off, true),
      view.getFloat32(off + 4, true),
      view.getFloat32(off + 8, true),
      view.getFloat32(off + 12, true),
    ];
    off += 16;
    views.push({ pose, fov });
  }
  return { tracking, head, views };
}

export const TRACKING_LABELS = ['tracked', 'limited', 'lost'];
