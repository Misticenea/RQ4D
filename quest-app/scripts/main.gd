extends Node3D

## RQ4D capture app — the headset is a sensor, not a display.
##
## Nothing of the reconstruction is drawn on the lenses. The wearer sees plain
## passthrough plus a small status panel; the 3D model exists only on the
## receiving client. That constraint is an asset rather than a limitation: an
## app that renders no scene content submits a passthrough layer and one quad
## per frame, so the GPU stays near idle and the power budget goes to the
## sensors, the encoder and the radio instead.
##
## It must still submit a frame every tick — an OpenXR app that stops
## submitting gets throttled and then killed. The HUD quad is that keepalive.

@onready var xr_origin: XROrigin3D = $XROrigin3D
@onready var xr_camera: XRCamera3D = $XROrigin3D/XRCamera3D
@onready var hud: Node = $XROrigin3D/XRCamera3D/HUD
@onready var depth: DepthCapture = $DepthCapture
@onready var net: NetClient = $NetClient
@onready var calibration: Calibration = $Calibration

const POSE_HZ := 30.0
const HOST_URL_FILE := "user://rq4d_host.txt"
const DEFAULT_HOST := "ws://192.168.1.10:8787/ws"

var _xr: XRInterface = null
var _streaming := false
var _pose_accum := 0.0
var _scene_manager: Node = null
var _anchor_manager: Node = null


func _ready() -> void:
	if not _init_xr():
		hud.set_fatal("OpenXR unavailable")
		return

	_attach_meta_nodes()
	calibration.setup(xr_origin, xr_camera, _scene_manager, _anchor_manager)
	calibration.state_changed.connect(_on_calibration_state)
	calibration.progress.connect(_on_calibration_progress)
	calibration.completed.connect(_on_calibration_done)

	net.connected.connect(_on_connected)
	net.disconnected.connect(func(): hud.set_link(false, ""))
	net.control_received.connect(_on_control)
	net.viewer_url_received.connect(hud.set_viewer_url)
	net.start(_load_host_url())

	if not depth.setup(calibration.to_anchor, xr_camera):
		# Not fatal: calibration still produces a usable static room model, and
		# saying exactly what is missing beats a silent stream of nothing.
		hud.set_warning(depth.last_error)
	depth.depth_ready.connect(_on_depth_ready)

	calibration.begin()


func _init_xr() -> bool:
	_xr = XRServer.find_interface("OpenXR")
	if _xr == null or not _xr.is_initialized():
		return false
	get_viewport().use_xr = true
	# No scene content to keep smooth, so take the lower refresh rate and give
	# the headroom to the capture path and to thermals.
	Engine.max_fps = 72
	DisplayServer.window_set_vsync_mode(DisplayServer.VSYNC_DISABLED)
	if _xr.has_method("set_environment_blend_mode"):
		_xr.set_environment_blend_mode(XRInterface.XR_ENV_BLEND_MODE_ALPHA_BLEND)
	return true


## The Meta nodes come from the OpenXR vendors plugin, which is a separate
## install. Instantiate by name so a missing plugin degrades to a clear message
## instead of a load-time crash on a headset with no way to read the log.
func _attach_meta_nodes() -> void:
	if ClassDB.class_exists("OpenXRFbSceneManager"):
		_scene_manager = ClassDB.instantiate("OpenXRFbSceneManager")
		xr_origin.add_child(_scene_manager)
	else:
		push_warning("OpenXRFbSceneManager missing — room capture unavailable")

	if ClassDB.class_exists("OpenXRFbSpatialAnchorManager"):
		_anchor_manager = ClassDB.instantiate("OpenXRFbSpatialAnchorManager")
		xr_origin.add_child(_anchor_manager)
	else:
		push_warning("OpenXRFbSpatialAnchorManager missing — poses will drift")


func _process(delta: float) -> void:
	hud.set_anchored(calibration.has_anchor())
	hud.update_status(net.status(), depth.status(), _streaming)
	if not _streaming:
		return

	_pose_accum += delta
	if _pose_accum >= 1.0 / POSE_HZ:
		_pose_accum = 0.0
		_send_pose()


func _send_pose() -> void:
	var to_anchor := calibration.to_anchor()
	var head := to_anchor * xr_camera.global_transform
	var views: Array[Transform3D] = []
	var fovs: Array = []
	for i in _xr.get_view_count():
		# get_transform_for_view returns the eye relative to the XR origin, so
		# lift it to world space before expressing it in the anchor frame.
		var eye := xr_origin.global_transform * _xr.get_transform_for_view(i, Transform3D())
		views.append(to_anchor * eye)
		fovs.append(Wire.fov_from_projection(
			_xr.get_projection_for_view(i, 1.0, 0.1, 100.0)
		))
	net.send_pose(Wire.encode_pose_frame(
		head, views, fovs, _tracking_state(), Time.get_ticks_usec() * 1000
	))


func _tracking_state() -> int:
	if _xr == null:
		return Wire.Tracking.LOST
	match _xr.get_tracking_status():
		XRInterface.XR_NORMAL_TRACKING:
			return Wire.Tracking.TRACKED
		XRInterface.XR_NOT_TRACKING:
			return Wire.Tracking.LOST
		_:
			return Wire.Tracking.LIMITED


func _on_depth_ready(payload: PackedByteArray) -> void:
	if _streaming:
		net.send_depth(payload)


func _on_connected() -> void:
	hud.set_link(true, net.url)
	net.send_hello(OS.get_model_name(), Vector2i(0, 0))
	if calibration.state == Calibration.State.DONE:
		# Reconnect after the room was already mapped: the host starts empty,
		# so it needs the profile again before any depth frame means anything.
		net.send_room_profile(calibration.room_profile)


func _on_calibration_state(state: int, message: String) -> void:
	hud.set_calibration(state, message)
	if state == Calibration.State.FAILED:
		_streaming = false


func _on_calibration_progress(fraction: float, hint: String) -> void:
	hud.set_progress(fraction, hint)


func _on_calibration_done(profile: Dictionary) -> void:
	net.send_room_profile(profile)
	_streaming = true
	hud.set_progress(1.0, "Streaming")


func _on_control(command: Dictionary) -> void:
	match command.get("command", ""):
		"Recalibrate":
			_streaming = false
			calibration.begin()
		"Pause":
			_streaming = false
		"Resume":
			_streaming = calibration.state == Calibration.State.DONE
		"SetQuality":
			var quality = command.get("quality", {})
			if quality is Dictionary and quality.has("depth_hz"):
				depth.target_hz = float(quality["depth_hz"])


func _load_host_url() -> String:
	if FileAccess.file_exists(HOST_URL_FILE):
		var file := FileAccess.open(HOST_URL_FILE, FileAccess.READ)
		if file != null:
			var saved := file.get_as_text().strip_edges()
			if saved != "":
				return saved
	return DEFAULT_HOST


func _notification(what: int) -> void:
	# Horizon OS does not permit background capture. Taking the headset off or
	# opening the system menu pauses the app; treat that as a state to show,
	# not an error, and resume without recalibrating.
	match what:
		NOTIFICATION_APPLICATION_PAUSED:
			_streaming = false
			hud.set_paused(true)
		NOTIFICATION_APPLICATION_RESUMED:
			hud.set_paused(false)
			_streaming = calibration.state == Calibration.State.DONE
		NOTIFICATION_WM_CLOSE_REQUEST:
			depth.stop()
			net.stop()
