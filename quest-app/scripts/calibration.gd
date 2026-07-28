class_name Calibration
extends Node

## Launch calibration: establish the room model and a room-fixed origin.
##
## Two things have to be true before a single depth frame is worth sending:
##
## 1. The host knows how big the room is, where the floor is, and what the
##    furniture is — so it can size its volume and give the viewer something
##    to render before live geometry arrives.
## 2. Every coordinate is expressed relative to a *persisted spatial anchor*
##    rather than to tracking space. Tracking space drifts and jumps on
##    relocalisation; data anchored to it shears over a long session, and the
##    shear is invisible until it is badly wrong.
##
## See docs/CALIBRATION.md. The flow is deliberately unskippable.

signal state_changed(state: int, message: String)
signal completed(profile: Dictionary)
signal progress(fraction: float, hint: String)

enum State {
	IDLE,
	WAITING_FOR_SCENE,
	REQUESTING_CAPTURE,
	ANCHORING,
	SETTLING,
	SWEEPING,
	DONE,
	FAILED,
}

const ANCHOR_FILE := "user://rq4d_anchor.json"
const SETTLE_SECONDS := 2.0
const SWEEP_TARGET_CELLS := 0.80
const SWEEP_TIMEOUT_S := 90.0
const COVERAGE_CELL_M := 0.35

var state: int = State.IDLE
var room_profile: Dictionary = {}
var anchor_uuid: String = ""

var _scene_manager: Node = null
var _anchor_manager: Node = null
var _origin: XROrigin3D = null
var _camera: XRCamera3D = null
var _timer: float = 0.0
var _covered: Dictionary = {}
var _expected_cells: int = 0
var _bounds_min := Vector3.ZERO
var _bounds_max := Vector3.ZERO


func setup(origin: XROrigin3D, camera: XRCamera3D, scene_manager: Node, anchor_manager: Node) -> void:
	_origin = origin
	_camera = camera
	_scene_manager = scene_manager
	_anchor_manager = anchor_manager

	if _scene_manager != null:
		if _scene_manager.has_signal("openxr_fb_scene_data_missing"):
			_scene_manager.openxr_fb_scene_data_missing.connect(_on_scene_data_missing)
		if _scene_manager.has_signal("openxr_fb_scene_capture_completed"):
			_scene_manager.openxr_fb_scene_capture_completed.connect(_on_capture_completed)


func begin() -> void:
	_set_state(State.WAITING_FOR_SCENE, "Reading room setup…")
	# Give the scene manager a frame to report missing data before assuming
	# anything: the signal is what tells us Space Setup has never been run.
	await get_tree().create_timer(0.5).timeout
	if state == State.WAITING_FOR_SCENE:
		_collect_room()


func _on_scene_data_missing() -> void:
	# The app can ask for Space Setup but cannot script it — the user walks
	# Meta's own flow, and can back out of it. Re-query on return rather than
	# assuming success.
	_set_state(State.REQUESTING_CAPTURE, "Scan your room when prompted")
	if _scene_manager != null and _scene_manager.has_method("request_scene_capture"):
		_scene_manager.request_scene_capture()
	else:
		_fail("Scene capture unavailable — run Space Setup from headset settings")


func _on_capture_completed(success: bool) -> void:
	if not success:
		_fail("Room scan was cancelled")
		return
	_collect_room()


func _collect_room() -> void:
	var planes: Array = []
	var volumes: Array = []
	var floor_y := INF
	var ceiling_y := -INF
	_bounds_min = Vector3(INF, INF, INF)
	_bounds_max = Vector3(-INF, -INF, -INF)

	for entity in _spatial_entities():
		var labels: PackedStringArray = entity.get_semantic_labels() if \
			entity.has_method("get_semantic_labels") else PackedStringArray()
		var label := labels[0] if labels.size() > 0 else "unknown"
		var xform: Transform3D = _entity_transform(entity)
		var record := {
			"label": label,
			"position": _v3(xform.origin),
			"basis": _basis(xform.basis),
		}
		if label in ["floor", "ceiling", "wall_face", "door_frame", "window_frame"]:
			planes.append(record)
			if label == "floor":
				floor_y = minf(floor_y, xform.origin.y)
			elif label == "ceiling":
				ceiling_y = maxf(ceiling_y, xform.origin.y)
		else:
			volumes.append(record)
		_bounds_min = _bounds_min.min(xform.origin)
		_bounds_max = _bounds_max.max(xform.origin)

	if planes.is_empty() and volumes.is_empty():
		_fail("No room data — run Space Setup from headset settings")
		return

	if not is_finite(floor_y):
		floor_y = _bounds_min.y
	if not is_finite(ceiling_y):
		ceiling_y = maxf(floor_y + 2.4, _bounds_max.y)

	# Pad outwards: entity origins are centres, and the surfaces they describe
	# extend beyond them.
	_bounds_min -= Vector3(1.0, 0.0, 1.0)
	_bounds_max += Vector3(1.0, 0.0, 1.0)
	_bounds_min.y = floor_y - 0.2
	_bounds_max.y = ceiling_y + 0.2

	room_profile = {
		"profile_id": "%d" % Time.get_unix_time_from_system(),
		"bounds": [_v3(_bounds_min), _v3(_bounds_max)],
		"floor_height": floor_y,
		"ceiling_height": ceiling_y,
		"planes": planes,
		"volumes": volumes,
		"device": {
			"model": OS.get_model_name(),
			"engine": "godot-%s" % Engine.get_version_info()["string"],
		},
	}
	_establish_anchor()


func _establish_anchor() -> void:
	_set_state(State.ANCHORING, "Setting the room origin…")
	if _anchor_manager == null or not _anchor_manager.has_method("create_anchor"):
		# Without an anchor the session still runs, it just accumulates drift.
		# Say so rather than pretending the data is room-fixed.
		push_warning("no spatial anchor manager; poses will be in tracking space")
		_begin_settle()
		return

	var uuids := _load_saved_anchor()
	if uuids.is_empty():
		# Deterministic placement: floor level, under the room centre, axes
		# aligned to the tracking frame. Relaunching in the same room lands on
		# the same origin without asking the user to do anything.
		var centre := (_bounds_min + _bounds_max) * 0.5
		centre.y = room_profile.get("floor_height", 0.0)
		_anchor_manager.create_anchor(Transform3D(Basis(), centre), {"role": "rq4d_origin"})
	_begin_settle()


func _load_saved_anchor() -> Array:
	if not FileAccess.file_exists(ANCHOR_FILE):
		return []
	var file := FileAccess.open(ANCHOR_FILE, FileAccess.READ)
	if file == null:
		return []
	var parsed = JSON.parse_string(file.get_as_text())
	if not (parsed is Dictionary) or (parsed as Dictionary).is_empty():
		return []
	var data := parsed as Dictionary
	if _anchor_manager.has_method("load_anchors"):
		_anchor_manager.load_anchors(data.keys(), data, 0, true)
	anchor_uuid = str(data.keys()[0])
	return data.keys()


func save_anchor() -> void:
	if _anchor_manager == null or not _anchor_manager.has_method("get_anchor_uuids"):
		return
	var data := {}
	for uuid in _anchor_manager.get_anchor_uuids():
		data[uuid] = {"role": "rq4d_origin"}
		anchor_uuid = str(uuid)
	var file := FileAccess.open(ANCHOR_FILE, FileAccess.WRITE)
	if file != null:
		file.store_string(JSON.stringify(data))


func _begin_settle() -> void:
	_set_state(State.SETTLING, "Hold still…")
	_timer = 0.0


func _begin_sweep() -> void:
	_set_state(State.SWEEPING, "Look slowly around the room")
	_timer = 0.0
	_covered.clear()
	var span := _bounds_max - _bounds_min
	_expected_cells = maxi(1, int(
		ceil(span.x / COVERAGE_CELL_M) * ceil(span.z / COVERAGE_CELL_M)
	))


func _process(delta: float) -> void:
	match state:
		State.SETTLING:
			_timer += delta
			if _timer >= SETTLE_SECONDS:
				_begin_sweep()
		State.SWEEPING:
			_timer += delta
			_accumulate_coverage()
			var fraction := float(_covered.size()) / float(_expected_cells)
			progress.emit(minf(fraction / SWEEP_TARGET_CELLS, 1.0), _sweep_hint())
			if fraction >= SWEEP_TARGET_CELLS or _timer >= SWEEP_TIMEOUT_S:
				_finish(fraction)


## Coverage is tracked from where the wearer has actually looked. The wearer is
## the scanner: nothing gets captured that nobody pointed their head at, so the
## progress bar has to measure gaze coverage rather than elapsed time.
func _accumulate_coverage() -> void:
	if _camera == null:
		return
	var xform := _camera.global_transform
	var forward := -xform.basis.z
	for distance in [1.0, 2.0, 3.0]:
		var p := xform.origin + forward * distance
		var cell := Vector2i(
			int(floor(p.x / COVERAGE_CELL_M)), int(floor(p.z / COVERAGE_CELL_M))
		)
		_covered[cell] = true


func _sweep_hint() -> String:
	if _camera == null:
		return "Look around"
	var yaw := _camera.global_transform.basis.get_euler().y
	return "Keep turning — %d%% covered" % int(
		100.0 * float(_covered.size()) / float(_expected_cells)
	) if absf(yaw) < TAU else "Look around"


func _finish(coverage: float) -> void:
	save_anchor()
	room_profile["coverage_pct"] = snappedf(coverage * 100.0, 0.1)
	room_profile["anchor_uuid"] = anchor_uuid
	_set_state(State.DONE, "Calibrated")
	completed.emit(room_profile)


func _fail(message: String) -> void:
	_set_state(State.FAILED, message)


func _set_state(next: int, message: String) -> void:
	state = next
	state_changed.emit(next, message)


func _spatial_entities() -> Array:
	if _scene_manager == null:
		return []
	for getter in ["get_spatial_entities", "get_anchors", "get_entities"]:
		if _scene_manager.has_method(getter):
			var result = _scene_manager.call(getter)
			if result is Array:
				return result
	return []


func _entity_transform(entity) -> Transform3D:
	for getter in ["get_transform", "get_global_transform"]:
		if entity.has_method(getter):
			return entity.call(getter)
	if entity is Node3D:
		return (entity as Node3D).global_transform
	return Transform3D()


static func _v3(v: Vector3) -> Array:
	return [v.x, v.y, v.z]


static func _basis(b: Basis) -> Array:
	var q := b.get_rotation_quaternion()
	return [q.x, q.y, q.z, q.w]
