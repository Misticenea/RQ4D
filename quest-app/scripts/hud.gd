extends Node3D

## The only thing drawn on the lenses: a small status panel.
##
## It exists for two reasons. The wearer is operating a capture device with no
## visible output and needs to know it is working, and an OpenXR app that stops
## submitting frames gets throttled and then killed — this quad is the
## keepalive that costs almost nothing.
##
## The measurement that matters most is `depth achieved_hz`. Every bandwidth
## and latency figure in the plan assumes a depth rate nobody has measured yet.

@onready var label: Label3D = $Panel/Label

const PANEL_DISTANCE := 1.2

var _calibration_message := "Starting…"
var _progress := 0.0
var _hint := ""
var _warning := ""
var _fatal := ""
var _paused := false
var _link_up := false
var _link_url := ""
var _viewer_url := ""


func _ready() -> void:
	position = Vector3(0, -0.18, -PANEL_DISTANCE)


func set_fatal(message: String) -> void:
	_fatal = message


func set_warning(message: String) -> void:
	_warning = message


func set_paused(paused: bool) -> void:
	_paused = paused


func set_link(up: bool, url: String) -> void:
	_link_up = up
	_link_url = url


## Shown large and on its own line: this is the address a person reads off the
## lenses and types into a browser, so it has to survive being looked at
## through passthrough while wearing the thing.
func set_viewer_url(url: String) -> void:
	_viewer_url = url


func set_calibration(_state: int, message: String) -> void:
	_calibration_message = message


func set_progress(fraction: float, hint: String) -> void:
	_progress = fraction
	_hint = hint


func update_status(net_status: Dictionary, depth_status: Dictionary, streaming: bool) -> void:
	if label == null:
		return
	if _fatal != "":
		label.text = "RQ4D\n\nFATAL: %s" % _fatal
		label.modulate = Color(1.0, 0.4, 0.4)
		return

	var lines: Array[String] = []
	lines.append("RQ4D  %s" % ("PAUSED" if _paused else ("STREAMING" if streaming else "SETUP")))
	lines.append("")
	lines.append("%s" % _calibration_message)
	if _progress > 0.0 and _progress < 1.0:
		lines.append("%s %s" % [_bar(_progress), _hint])

	if _viewer_url != "":
		lines.append("")
		lines.append("   open on any device:")
		lines.append("   %s" % _viewer_url)

	lines.append("")
	lines.append("host    %s" % ("connected" if _link_up else "connecting…"))
	if _link_up:
		lines.append("sent    %s MB   dropped %d" % [
			net_status.get("mb_sent", 0.0), net_status.get("dropped", 0)
		])

	var achieved := float(depth_status.get("achieved_hz", 0.0))
	var target := float(depth_status.get("target_hz", 0.0))
	lines.append("depth   %.1f Hz of %.0f target" % [achieved, target])
	if depth_status.get("dropped_inflight", 0) > 0:
		lines.append("        %d skipped, readback busy" % depth_status["dropped_inflight"])

	var err := str(depth_status.get("error", ""))
	if err != "":
		lines.append("")
		lines.append("depth: %s" % err)
	elif _warning != "":
		lines.append("")
		lines.append("! %s" % _warning)

	label.text = "\n".join(lines)
	label.modulate = Color.WHITE if _link_up else Color(1.0, 0.85, 0.5)


static func _bar(fraction: float) -> String:
	var filled := int(clampf(fraction, 0.0, 1.0) * 16.0)
	return "[%s%s]" % ["=".repeat(filled), " ".repeat(16 - filled)]
