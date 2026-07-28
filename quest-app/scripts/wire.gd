class_name Wire
extends RefCounted

## Binary wire codec — the GDScript half of the RQ4D protocol.
##
## Byte layout must match host/rq4d_host/wire.py exactly. That file is the
## reference; this one follows it. Offsets are stated in comments so a
## mismatch is caught by reading rather than by debugging a garbled mesh.
##
## Framing (little-endian):
##   u32 length          payload bytes after the header
##   u8  type
##   u8  flags
##   u16 reserved
##   u64 timestamp_ns    device monotonic clock

const HEADER_SIZE := 16
const PROTOCOL_VERSION := "0.1"

enum Msg {
	HELLO = 0x01,
	HELLO_ACK = 0x02,
	ROOM_PROFILE = 0x03,
	ANCHOR_UPDATE = 0x04,
	POSE_FRAME = 0x10,
	DEPTH_FRAME = 0x11,
	COLOR_FRAME = 0x12,
	MESH_CHUNK_UPDATE = 0x20,
	CHUNK_REMOVED = 0x21,
	CONTROL = 0x30,
	STATS = 0x31,
	HEARTBEAT = 0x3F,
}

enum DepthEncoding {
	RAW16 = 0,      ## uint16, metric = value * depth_scale
	ZSTD16 = 1,
	RAW_F32 = 2,    ## float32 metric metres
	RAW_F32_NDC = 3 ## float32 clip-space depth, needs inv_proj
}

enum Tracking { TRACKED = 0, LIMITED = 1, LOST = 2 }


static func _frame(type: int, payload: PackedByteArray, timestamp_ns: int) -> PackedByteArray:
	var buf := StreamPeerBuffer.new()
	buf.big_endian = false
	buf.put_u32(payload.size())
	buf.put_u8(type)
	buf.put_u8(0)  # flags
	buf.put_u16(0)  # reserved
	buf.put_u64(timestamp_ns)
	buf.put_data(payload)
	return buf.data_array


static func encode_json(type: int, obj: Dictionary, timestamp_ns: int) -> PackedByteArray:
	return _frame(type, JSON.stringify(obj).to_utf8_buffer(), timestamp_ns)


## Position (3 floats) then rotation quaternion xyzw (4 floats), ANCHOR frame.
static func _put_pose(buf: StreamPeerBuffer, xform: Transform3D) -> void:
	var q := xform.basis.get_rotation_quaternion()
	buf.put_float(xform.origin.x)
	buf.put_float(xform.origin.y)
	buf.put_float(xform.origin.z)
	buf.put_float(q.x)
	buf.put_float(q.y)
	buf.put_float(q.z)
	buf.put_float(q.w)


## OpenXR field-of-view half-angles in radians: left, right, up, down.
static func _put_fov(buf: StreamPeerBuffer, fov: PackedFloat32Array) -> void:
	for i in 4:
		buf.put_float(fov[i])


static func encode_pose_frame(
	head: Transform3D,
	views: Array[Transform3D],
	fovs: Array,
	tracking: int,
	timestamp_ns: int
) -> PackedByteArray:
	var buf := StreamPeerBuffer.new()
	buf.big_endian = false
	buf.put_u8(tracking)
	_put_pose(buf, head)
	for i in views.size():
		_put_pose(buf, views[i])
		_put_fov(buf, fovs[i])
	return _frame(Msg.POSE_FRAME, buf.data_array, timestamp_ns)


## Depth payload layout, matching struct "<BBBBHHfff" + pose + fov + mat4 + blob:
##   0  u8  view_index      1  u8  encoding
##   2  u8  hands_removed   3  u8  pad
##   4  u16 width           6  u16 height
##   8  f32 depth_scale    12  f32 near        16 f32 far
##  20  pose (7 f32)       48  fov (4 f32)
##  64  inv_proj (16 f32) 128  depth blob
static func encode_depth_frame(
	view_index: int,
	width: int,
	height: int,
	pose: Transform3D,
	fov: PackedFloat32Array,
	blob: PackedByteArray,
	encoding: int,
	depth_scale: float,
	near: float,
	far: float,
	hands_removed: bool,
	inv_proj: Projection,
	timestamp_ns: int
) -> PackedByteArray:
	var buf := StreamPeerBuffer.new()
	buf.big_endian = false
	buf.put_u8(view_index)
	buf.put_u8(encoding)
	buf.put_u8(1 if hands_removed else 0)
	buf.put_u8(0)
	buf.put_u16(width)
	buf.put_u16(height)
	buf.put_float(depth_scale)
	buf.put_float(near)
	buf.put_float(far)
	_put_pose(buf, pose)
	_put_fov(buf, fov)
	# Row-major on the wire: numpy reads it as [row][col], so emit each of the
	# four Projection columns as a row of four floats in that order.
	for c in 4:
		# Explicitly typed: indexing a Projection yields Variant, which `:=`
		# refuses to infer from.
		var col: Vector4 = inv_proj[c]
		buf.put_float(col.x)
		buf.put_float(col.y)
		buf.put_float(col.z)
		buf.put_float(col.w)
	buf.put_data(blob)
	return _frame(Msg.DEPTH_FRAME, buf.data_array, timestamp_ns)


## Decode a frame header. Returns {type, flags, timestamp_ns, payload}.
static func decode(data: PackedByteArray) -> Dictionary:
	if data.size() < HEADER_SIZE:
		return {}
	var length := data.decode_u32(0)
	if data.size() < HEADER_SIZE + length:
		return {}
	return {
		"type": data.decode_u8(4),
		"flags": data.decode_u8(5),
		"timestamp_ns": data.decode_u64(8),
		"payload": data.slice(HEADER_SIZE, HEADER_SIZE + length),
	}


static func decode_json(frame: Dictionary) -> Dictionary:
	if frame.is_empty():
		return {}
	var parsed = JSON.parse_string((frame["payload"] as PackedByteArray).get_string_from_utf8())
	return parsed if parsed is Dictionary else {}


## Field-of-view half-angles from a projection matrix.
##
## Godot exposes the per-eye projection rather than the raw OpenXR angles, so
## recover the frustum tangents from it and convert. Asymmetric frusta are the
## norm on Quest — assuming a symmetric one puts every reconstructed point on a
## slight diagonal offset that is easy to mistake for tracking drift.
static func fov_from_projection(p: Projection) -> PackedFloat32Array:
	var sx: float = p[0][0]
	var sy: float = p[1][1]
	var ox: float = p[2][0]
	var oy: float = p[2][1]
	var tan_left := (ox - 1.0) / sx
	var tan_right := (ox + 1.0) / sx
	var tan_down := (oy - 1.0) / sy
	var tan_up := (oy + 1.0) / sy
	return PackedFloat32Array([
		atan(tan_left), atan(tan_right), atan(tan_up), atan(tan_down)
	])
