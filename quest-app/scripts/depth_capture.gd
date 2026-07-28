class_name DepthCapture
extends Node

## Reads the Meta environment depth map and hands it to the transport.
##
## THE OPEN QUESTION OF THIS PROJECT.
##
## Godot's documentation for `get_environment_depth_map_async` says requests
## should happen "approximately every 1-2 seconds, not per-frame". If that is a
## hard ceiling rather than a caution about cost, streaming depth at 15 Hz is
## not possible through this API and the architecture needs its fallback. The
## whole design leans on a rate nobody here can measure without a headset.
##
## So this class does not assume. It requests at a configurable target rate,
## keeps at most one request in flight, and reports the rate it actually
## achieved. The first thing to do on a real device is run it and read
## `achieved_hz` off the HUD — every bandwidth and latency figure in the plan
## depends on that number.
##
## If the achieved rate turns out to be ~1 Hz:
##   - the static room model from calibration still works unchanged;
##   - dynamic updates degrade to "slow" rather than absent;
##   - the escape hatch is a GDExtension reading the depth swapchain directly,
##     which is a bounded piece of C++ and disturbs nothing else.

signal depth_ready(payload: PackedByteArray)
signal rate_measured(hz: float)

const MAX_TRACKED_SAMPLES := 32

@export var target_hz: float = 15.0
@export var enabled: bool = true
@export var hand_removal: bool = true
@export var view_index: int = 0  ## 0 = left eye only; both views double the bitrate

var achieved_hz: float = 0.0
var last_error: String = ""
var frames_captured: int = 0
var frames_dropped_inflight: int = 0

var _ext: Object = null
var _supported := false
var _started := false
var _request_in_flight := false
var _next_request_us: int = 0
var _pending_pose := Transform3D()
var _recent_us: Array[int] = []
var _camera: XRCamera3D = null
var _to_anchor: Callable = Callable()


## `to_anchor` supplies the world-to-anchor transform each frame rather than a
## fixed node, because the anchor may only start tracking after capture begins.
func setup(to_anchor: Callable, camera: XRCamera3D) -> bool:
	_to_anchor = to_anchor
	_camera = camera

	if not ClassDB.class_exists("OpenXRMetaEnvironmentDepthExtension"):
		last_error = "OpenXR vendors plugin missing (OpenXRMetaEnvironmentDepthExtension)"
		return false

	_ext = ClassDB.instantiate("OpenXRMetaEnvironmentDepthExtension")
	if _ext == null or not _ext.has_method("is_environment_depth_supported"):
		last_error = "environment depth extension unavailable"
		return false

	_supported = _ext.is_environment_depth_supported()
	if not _supported:
		last_error = "environment depth not supported on this device"
		return false

	_ext.start_environment_depth()
	if _ext.has_method("set_hand_removal_enabled"):
		_ext.set_hand_removal_enabled(hand_removal)
	_started = true
	return true


func stop() -> void:
	if _started and _ext != null:
		_ext.stop_environment_depth()
	_started = false


func _process(_delta: float) -> void:
	if not (enabled and _started):
		return
	var now := Time.get_ticks_usec()
	if now < _next_request_us:
		return
	if _request_in_flight:
		# Never queue a second request. A backlog of readbacks would report a
		# healthy request rate while delivering frames that are already stale.
		frames_dropped_inflight += 1
		return

	_next_request_us = now + int(1_000_000.0 / maxf(target_hz, 0.1))
	_request_in_flight = true
	# Pose is sampled at request time, not at callback time. The depth image
	# describes the moment the request was made; pairing it with a later pose
	# smears geometry along whatever direction the head was turning.
	_pending_pose = _anchor_relative(_camera.global_transform)
	_ext.get_environment_depth_map_async(_on_depth_map)


func _on_depth_map(result) -> void:
	_request_in_flight = false
	if result == null or not (result is Array) or (result as Array).is_empty():
		last_error = "empty depth result"
		return

	var views := result as Array
	var idx: int = mini(view_index, views.size() - 1)
	var entry = views[idx]
	if not (entry is Dictionary) or not entry.has("image"):
		last_error = "depth result missing 'image'"
		return

	var image: Image = entry["image"]
	if image == null or image.get_width() == 0:
		last_error = "depth image empty"
		return

	var inv_proj: Projection = entry.get("depth_inverse_projection_view", Projection())
	var proj: Projection = entry.get("depth_projection_view", Projection())

	match image.get_format():
		Image.FORMAT_RF:
			pass  # already float32
		Image.FORMAT_RH:
			image.convert(Image.FORMAT_RF)
		_:
			last_error = "unexpected depth format %d" % image.get_format()
			return

	# Sent verbatim as clip-space float32. Linearising per pixel in GDScript
	# would cost far more on the headset than the bytes it would save, and the
	# host has the inverse matrix it needs to do it in one numpy operation.
	var payload := Wire.encode_depth_frame(
		idx,
		image.get_width(),
		image.get_height(),
		_pending_pose,
		Wire.fov_from_projection(proj),
		image.get_data(),
		Wire.DepthEncoding.RAW_F32_NDC,
		1.0,
		0.2,
		6.0,
		hand_removal,
		inv_proj,
		Time.get_ticks_usec() * 1000
	)

	_note_rate()
	frames_captured += 1
	depth_ready.emit(payload)


func _note_rate() -> void:
	var now := Time.get_ticks_usec()
	_recent_us.append(now)
	if _recent_us.size() > MAX_TRACKED_SAMPLES:
		_recent_us.remove_at(0)
	if _recent_us.size() >= 2:
		var span := float(_recent_us[-1] - _recent_us[0]) / 1_000_000.0
		if span > 0.0:
			achieved_hz = float(_recent_us.size() - 1) / span
			rate_measured.emit(achieved_hz)


## Everything on the wire is expressed relative to the calibration anchor, so a
## tracking-space jump never reaches the host.
func _anchor_relative(world: Transform3D) -> Transform3D:
	if not _to_anchor.is_valid():
		return world
	var to_anchor: Transform3D = _to_anchor.call()
	return to_anchor * world


func status() -> Dictionary:
	return {
		"supported": _supported,
		"started": _started,
		"target_hz": target_hz,
		"achieved_hz": achieved_hz,
		"captured": frames_captured,
		"dropped_inflight": frames_dropped_inflight,
		"error": last_error,
	}
