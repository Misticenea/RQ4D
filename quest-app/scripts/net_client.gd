class_name NetClient
extends Node

## WebSocket transport to the reconstruction host.
##
## The send queue is bounded with an explicit drop policy, mirroring the host
## side. Under a congested link an unbounded queue turns into latency and keeps
## converting until the stream is useless; a bounded one sheds the frames that
## had already stopped being worth anything.
##
##   depth    drop oldest — a late depth frame describes a moment already gone
##   pose     drop oldest — cheap and continuous, gaps are invisible
##   control  never dropped — room profile and commands are cumulative state

signal connected()
signal disconnected()
signal control_received(command: Dictionary)

const MAX_DEPTH_QUEUE := 2
const MAX_POSE_QUEUE := 4
const RECONNECT_DELAY_S := 2.0

@export var url: String = "ws://192.168.1.10:8787"
@export var auto_reconnect: bool = true

var bytes_sent: int = 0
var frames_dropped: int = 0
var is_connected: bool = false

var _socket := WebSocketPeer.new()
var _want_connection := false
var _retry_at_us: int = 0
var _depth_queue: Array[PackedByteArray] = []
var _pose_queue: Array[PackedByteArray] = []
var _control_queue: Array[PackedByteArray] = []


func start(target_url: String = "") -> void:
	if target_url != "":
		url = target_url
	_want_connection = true
	_retry_at_us = 0


func stop() -> void:
	_want_connection = false
	_socket.close()


func send_hello(device: String, depth_size: Vector2i) -> void:
	_control_queue.append(Wire.encode_json(Wire.Msg.HELLO, {
		"protocol_version": Wire.PROTOCOL_VERSION,
		"role": "producer",
		"device": device,
		"engine": "godot-%s" % Engine.get_version_info()["string"],
		"depth": {"width": depth_size.x, "height": depth_size.y},
		# The host uses this to relate our monotonic clock to its own, so
		# reported frame ages are real rather than an artefact of two
		# unrelated clock origins.
		"clock_ns": Time.get_ticks_usec() * 1000,
	}, Time.get_ticks_usec() * 1000))


func send_room_profile(profile: Dictionary) -> void:
	_control_queue.append(
		Wire.encode_json(Wire.Msg.ROOM_PROFILE, profile, Time.get_ticks_usec() * 1000)
	)


func send_depth(payload: PackedByteArray) -> void:
	_depth_queue.append(payload)
	while _depth_queue.size() > MAX_DEPTH_QUEUE:
		_depth_queue.remove_at(0)
		frames_dropped += 1


func send_pose(payload: PackedByteArray) -> void:
	_pose_queue.append(payload)
	while _pose_queue.size() > MAX_POSE_QUEUE:
		_pose_queue.remove_at(0)


func _process(_delta: float) -> void:
	if not _want_connection:
		return

	var state := _socket.get_ready_state()

	if state == WebSocketPeer.STATE_CLOSED:
		if is_connected:
			is_connected = false
			disconnected.emit()
		if auto_reconnect and Time.get_ticks_usec() >= _retry_at_us:
			_retry_at_us = Time.get_ticks_usec() + int(RECONNECT_DELAY_S * 1_000_000)
			_socket.connect_to_url(url)
		return

	_socket.poll()
	state = _socket.get_ready_state()

	if state == WebSocketPeer.STATE_OPEN:
		if not is_connected:
			is_connected = true
			connected.emit()
		_drain_incoming()
		_flush()


func _drain_incoming() -> void:
	while _socket.get_available_packet_count() > 0:
		var frame := Wire.decode(_socket.get_packet())
		if frame.is_empty():
			continue
		if frame["type"] == Wire.Msg.CONTROL:
			control_received.emit(Wire.decode_json(frame))


func _flush() -> void:
	# Control first and unconditionally: the host cannot interpret any depth
	# frame until it has the room profile that defines the coordinate frame.
	for payload in _control_queue:
		_put(payload)
	_control_queue.clear()
	for payload in _pose_queue:
		_put(payload)
	_pose_queue.clear()
	for payload in _depth_queue:
		_put(payload)
	_depth_queue.clear()


func _put(payload: PackedByteArray) -> void:
	if _socket.send(payload) == OK:
		bytes_sent += payload.size()


func status() -> Dictionary:
	return {
		"connected": is_connected,
		"url": url,
		"mb_sent": snappedf(bytes_sent / 1e6, 0.01),
		"dropped": frames_dropped,
	}
