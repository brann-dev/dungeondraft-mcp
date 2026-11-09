# MCP Bridge — Dungeondraft mod
#
# Opens a localhost TCP server inside Dungeondraft and speaks a tiny
# newline-delimited JSON protocol (see PROTOCOL.md) so an external MCP server
# can drive the currently-open map for AI-assisted map building.
#
# Engine: Dungeondraft runs Godot 3.4.2, so this uses the Godot 3 networking
# class names (TCP_Server / StreamPeerTCP) and the connect(sig, self, "method")
# signal form. Mod tool scripts receive a per-frame update(delta) callback but
# NOT _process / _input, so all socket polling happens in update().
#
# Element model: every element this bridge touches is referenced by an integer
# `id` == Dungeondraft's node_id (Global.World.AssignNodeID / GetNodeByID /
# DeleteNodeByID). Creation commands return the new id; query commands return
# ids you can pass back to move/modify/delete.
#
# Undo: reversible edits push an op onto the bridge's own undo/redo stacks
# (DD 3.4.2's History.CreateCustomRecord is unreliable); see _record_and_dispatch.

var script_class = "tool"

const HOST := "127.0.0.1"
const PORT := 8787
const PROTOCOL_VERSION := 29

# Commands that get wrapped in a Dungeondraft undo record (see _record_and_dispatch).
const CREATE_CMDS := [
	"place_object", "draw_wall", "draw_path", "add_light",
	"add_portal", "add_roof", "add_text", "duplicate_object", "import_image",
	"place_pattern",
]
const TRANSFORM_CMDS := ["move_element", "modify_object"]
const TERRAIN_CMDS := ["fill_terrain", "fill_region", "paint_terrain", "paint_path"]
const CAVE_CMDS := ["dig_cave", "clear_caves"]
# History keeps this many steps; past it the oldest is dropped, and a checkpoint
# older than the drop can no longer be rolled back.
const MAX_UNDO_OPS := 200
const MAX_CHECKPOINTS := 16
# Commands that settle history before they run: any open undo group is closed.
const HISTORY_CMDS := ["undo", "redo", "checkpoint", "list_checkpoints", "rollback_checkpoint"]
# The bridge's token and settings live in Dungeondraft's user folder (user://,
# the folder that holds config.ini), where the Python server reads them too.
const TOKEN_FILE := "user://mcp_bridge_token"
const SETTINGS_FILE := "user://mcp_bridge_settings.json"
# Commands that look but change nothing on the map: they still run while the
# user has paused AI edits in the MCP Bridge panel.
const READ_ONLY_CMDS := [
	"ping", "get_status", "list_asset_categories", "list_assets", "list_elements", "get_element",
	"list_levels", "list_layers", "list_asset_packs", "terrain_samples", "get_current_map",
	"screenshot", "export_map", "export_finish", "set_grid",
	"get_camera", "set_camera", "focus_element", "fit_elements", "reset_camera",
	"native_targets", "native_describe", "native_get", "ui_tree",
	"spatial_snapshot", "asset_footprint", "list_spatial_regions", "list_checkpoints",
	"get_map_style", "list_prefabs", "generator_options", "select_elements", "clear_selection",
]
# Neutral opaque tint for pattern floors when no color is given. PatternShapeTool
# has no per-texture default (and its .Color leaks across calls), so we apply a
# deterministic wood/stone-neutral tone; callers pass an explicit color to override.
const DEFAULT_PATTERN_TINT := Color(0.62, 0.5, 0.34)
# Custom color for "Colorable" objects (the red-mask ones) when the caller gives
# none: ObjectTool's initial palette color (Cache.DefaultCustomColors[0]).
const DEFAULT_OBJECT_TINT := Color("6b3834")
const CUSTOM_COLORS_MATERIAL := "res://materials/CustomColors.material"
const TAGS_FILE := "data/default.dungeondraft_tags"
# The formats DD's own file-drop handler embeds (Editor.OnFilesDropped).
const IMAGE_EXTENSIONS := ["png", "jpg", "jpeg", "webp"]
const MAX_IMAGE_SIDE := 16384
# MaterialBrush's own default layer ("Below Ground": above terrain, under floors).
const DEFAULT_MATERIAL_LAYER := -400
# The layer DD's Object, Path and Scatter tools start on ("User Layer 1"), so
# what the bridge places stacks with what is placed by hand.
const DEFAULT_LAYER := 100

# SelectTool.GetSelectableType() integer -> readable kind.
const KIND_NAMES := {
	1: "wall", 2: "wall_portal", 3: "portal", 4: "object",
	5: "path", 6: "light", 7: "pattern", 8: "roof",
}
# kind string -> the Level child collection that holds those nodes.
const COLLECTIONS := {
	"objects": "Objects", "walls": "Walls", "lights": "Lights",
	"paths": "Pathways", "portals": "Portals", "roofs": "Roofs",
	"texts": "Texts",
}

var _server : TCP_Server = null
var _conns := []         # array of { "peer": StreamPeerTCP, "buf": String }
var _undo_stack := []    # bridge-managed undo ops (see _build_op / _apply_op)
var _redo_stack := []
var _op_serial := 0      # numbers every op pushed; checkpoints remember where they stood
var _lost_serial := 0    # the newest serial dropped from history (see _push_op)
var _groups := []        # open undo groups, innermost last (begin_undo_group / end_undo_group)
var _checkpoints := {}   # label -> { "label", "after": serial, "opened": msec }
var _token := ""         # the shared secret every request must carry (see _ensure_token)
var _require_token := true
var _paused := false     # "Pause AI edits" in the panel: only READ_ONLY_CMDS run
var _panel = null
var _panel_labels := {}  # key -> Label in the panel
var _last_request := ""
var _colorable = null    # lazily built set of "Colorable"-tagged texture paths
var _export = null       # export in flight: { grid_was, cam: {anchor, pos, zoom} }; see _finish_export


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

# DD calls start() on a fresh script instance every time a map is opened or
# created, with no teardown hook for the previous one — whose TCP_Server keeps
# the port bound. So the listener is process-global: stashed in Engine meta and
# adopted by each new instance. Ownership moves to the newest instance; older
# ones notice in update() and stop polling.
const META_SERVER := "mcp_bridge_server"
const META_OWNER := "mcp_bridge_owner"

func start():
	var settings = _settings_read()
	_require_token = bool(settings.get("require_token", true))
	_paused = bool(settings.get("paused", false))   # a pause outlives a restart; the user lifts it
	_token = _ensure_token()
	_register_tool()
	var prev = Engine.get_meta(META_SERVER) if Engine.has_meta(META_SERVER) else null
	if prev != null and prev is TCP_Server and prev.is_listening():
		_server = prev
		print("[mcp-bridge] adopted listener on %s:%d (protocol v%d)" % [HOST, PORT, PROTOCOL_VERSION])
	else:
		_server = TCP_Server.new()
		var err = _server.listen(PORT, HOST)
		if err != OK:
			_server = null
			print("[mcp-bridge] FAILED to listen on %s:%d (error %d)" % [HOST, PORT, err])
			OS.alert("MCP bridge could not open port %d (error %d).\nIs it already in use?" % [PORT, err], "MCP Bridge")
			return
		Engine.set_meta(META_SERVER, _server)
		print("[mcp-bridge] listening on %s:%d (protocol v%d)" % [HOST, PORT, PROTOCOL_VERSION])
	Engine.set_meta(META_OWNER, get_instance_id())


func update(delta : float):
	if _server == null:
		return
	if Engine.get_meta(META_OWNER) != get_instance_id():
		# A newer map's instance took over the shared listener.
		_server = null
		_conns = []
		return

	if _export != null and not _is_exporting():
		_finish_export()   # Exporter.Run finished (or we were never told): un-stage

	while _server.is_connection_available():
		var peer = _server.take_connection()
		peer.set_no_delay(true)
		_conns.append({ "peer": peer, "buf": "" })

	var still_open := []
	for c in _conns:
		var peer : StreamPeerTCP = c["peer"]
		if peer.get_status() != StreamPeerTCP.STATUS_CONNECTED:
			continue
		var avail = peer.get_available_bytes()
		if avail > 0:
			var res = peer.get_data(avail)   # [err, PoolByteArray]
			if res[0] == OK:
				c["buf"] += res[1].get_string_from_utf8()
		var nl = c["buf"].find("\n")
		while nl != -1:
			var line = c["buf"].substr(0, nl).strip_edges()
			c["buf"] = c["buf"].substr(nl + 1)
			if line != "":
				_handle_line(peer, line)
			nl = c["buf"].find("\n")
		still_open.append(c)
	_conns = still_open


# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------

func _handle_line(peer : StreamPeerTCP, line : String):
	var parsed = JSON.parse(line)
	var req = parsed.result
	var resp : Dictionary
	if parsed.error != OK or typeof(req) != TYPE_DICTIONARY:
		resp = _err("invalid JSON request")
	else:
		var cmd = str(req.get("cmd", ""))
		_last_request = "%s at %s" % [cmd, _clock()]
		if cmd != "ping" and not _authenticated(req):
			resp = _err(_auth_error(req))
		elif _paused and not (cmd in READ_ONLY_CMDS):
			resp = _err("paused: the user paused AI edits in Dungeondraft's MCP Bridge panel " +
				"(Settings > MCP Bridge). Read-only commands still work; make no edits until they resume.")
		else:
			resp = _record_and_dispatch(req)
	_refresh_panel()
	peer.put_data((JSON.print(resp) + "\n").to_utf8())


# ---------------------------------------------------------------------------
# Token, settings, pause
# ---------------------------------------------------------------------------
#
# Anything that can reach 127.0.0.1:8787 could drive Dungeondraft: another
# user on the machine, a sandboxed program, a web page making a request to
# localhost. The mod writes a random token to Dungeondraft's user folder on
# first start; the Python server reads the same file and sends the token with
# every request. Something that cannot read the user's files cannot drive the
# map. It is a bearer token over localhost, not encryption: integrity against
# a process that can already read the user's files is not a goal.

func _authenticated(req : Dictionary) -> bool:
	if not _require_token:
		return true
	return _token != "" and str(req.get("token", "")) == _token


func _auth_error(req : Dictionary) -> String:
	if _token == "":
		return "this bridge could not create its token (see Dungeondraft's log); the bridge refuses every edit until it can"
	if not req.has("token"):
		return ("this bridge requires a token and the request carried none: update the MCP server " +
			"(0.4 or newer reads the token from Dungeondraft's user folder), or turn off " +
			"'Require token' in Dungeondraft's MCP Bridge panel")
	return ("bad token: the MCP server's token does not match this Dungeondraft's. Restart " +
		"Dungeondraft and reconnect the server, or turn off 'Require token' in the MCP Bridge panel")


# Read the token the last start wrote, or make a new one (32 random bytes as
# 64 hex characters). An unreadable or malformed file is replaced.
func _ensure_token() -> String:
	var f = File.new()
	if f.file_exists(TOKEN_FILE) and f.open(TOKEN_FILE, File.READ) == OK:
		var existing = f.get_as_text().strip_edges()
		f.close()
		if existing.length() == 64 and existing.is_valid_hex_number(false):
			return existing
	return _regenerate_token()


func _regenerate_token() -> String:
	if not ClassDB.can_instance("Crypto"):
		print("[mcp-bridge] this build cannot instance Crypto; no token, the bridge refuses edits")
		return ""
	var crypto = ClassDB.instance("Crypto")
	var raw = crypto.generate_random_bytes(32)
	if typeof(raw) != TYPE_RAW_ARRAY or raw.size() != 32:
		print("[mcp-bridge] Crypto.generate_random_bytes failed; no token, the bridge refuses edits")
		return ""
	var token = raw.hex_encode()
	var f = File.new()
	if f.open(TOKEN_FILE, File.WRITE) != OK:
		print("[mcp-bridge] could not write %s; no token, the bridge refuses edits" % TOKEN_FILE)
		return ""
	f.store_string(token)
	f.close()
	print("[mcp-bridge] wrote a new bridge token to %s" % ProjectSettings.globalize_path(TOKEN_FILE))
	return token


func _settings_read() -> Dictionary:
	var f = File.new()
	if not f.file_exists(SETTINGS_FILE) or f.open(SETTINGS_FILE, File.READ) != OK:
		return {}
	var parsed = JSON.parse(f.get_as_text())
	f.close()
	if parsed.error != OK or typeof(parsed.result) != TYPE_DICTIONARY:
		return {}
	return parsed.result


func _settings_write(key : String, value) -> void:
	var settings = _settings_read()
	settings[key] = value
	var f = File.new()
	if f.open(SETTINGS_FILE, File.WRITE) != OK:
		print("[mcp-bridge] could not write %s" % SETTINGS_FILE)
		return
	f.store_string(JSON.print(settings, "\t"))
	f.close()


func _clock() -> String:
	var t = OS.get_time()
	return "%02d:%02d:%02d" % [t.hour, t.minute, t.second]


# Runs a command and, if it succeeded and is a reversible map edit, pushes an op
# onto the bridge-managed undo stack. We do NOT use Dungeondraft's
# History.CreateCustomRecord — on 3.4.2 it invokes undo() unreliably and never
# round-trips redo(). Our own stacks are deterministic and fully under control.
# Pre-edit state (transform / terrain splat) is captured before the command runs.
func _record_and_dispatch(req : Dictionary) -> Dictionary:
	var cmd = req.get("cmd", "")

	var pre = null
	if cmd in TRANSFORM_CMDS and req.has("id"):
		var node = Global.World.GetNodeByID(int(req["id"]))
		if node != null:
			pre = _snapshot(node)

	var terrain_before = null
	if cmd in TERRAIN_CMDS:
		var lvl = Global.World.GetCurrentLevel()
		if lvl != null:
			terrain_before = _terrain_snapshot(lvl)

	var cave_before = null
	if cmd in CAVE_CMDS:
		cave_before = _cave_snapshot()

	# A request without the open group's token comes from a caller that stopped
	# talking (a server that died mid-batch): close that group first, so its
	# edits are not swallowed. Groups the mod opens itself (build_room) carry
	# no token and match the untokened requests made inside them.
	if not _groups.empty() and cmd != "end_undo_group":
		if str(_groups.back().get("token", "")) != str(req.get("undo_group", "")):
			_close_all_groups()
	if cmd in HISTORY_CMDS:
		_close_all_groups()

	var result = _safe_dispatch(req)
	if typeof(result) == TYPE_DICTIONARY and result.get("ok", false):
		# Handlers that know their own before/after state hand over a ready op
		# (see _ok_op); the older commands are reconstructed from the pre-state.
		var op = result.get("op")
		result.erase("op")
		if op == null:
			op = _build_op(cmd, req, result, pre, terrain_before, cave_before)
		if op != null:
			_push_op(op)
	return result


# Returns an undo op for a recordable command, or null. Ops are reversed by
# _apply_op (undo=true) and re-applied (undo=false).
func _build_op(cmd, req, result, pre, terrain_before, cave_before = null):
	if cmd in CREATE_CMDS:
		var id = result["result"].get("id", -1)
		if id != null and int(id) >= 0:
			var node = Global.World.GetNodeByID(int(id))
			if node != null:
				return _create_op(node, int(id))
	elif cmd in TRANSFORM_CMDS and pre != null:
		var node = Global.World.GetNodeByID(int(req["id"]))
		if node != null:
			return { "kind": "transform", "id": int(req["id"]), "old": pre, "new": _snapshot(node) }
	elif cmd in TERRAIN_CMDS and terrain_before != null:
		var lvl = Global.World.GetCurrentLevel()
		if lvl != null:
			return { "kind": "terrain", "before": terrain_before, "after": _terrain_snapshot(lvl) }
	elif cmd in CAVE_CMDS and cave_before != null:
		var after = _cave_snapshot()
		if after != null:
			return { "kind": "cave", "before": cave_before, "after": after }
	return null


# Record an op: into the innermost open group, or onto the undo stack with a
# serial number. A fresh edit invalidates the redo branch, and history past
# MAX_UNDO_OPS drops its oldest step.
func _push_op(op) -> void:
	if not _groups.empty():
		_groups.back()["entries"].append(op)
		return
	_op_serial += 1
	op["serial"] = _op_serial
	_undo_stack.append(op)
	_discard_redo()
	while _undo_stack.size() > MAX_UNDO_OPS:
		var dropped = _undo_stack.pop_front()
		_lost_serial = int(max(_lost_serial, int(dropped.get("serial", 0))))
		_release_op(dropped)


# Free the nodes an op still holds out of the tree (an undone create, a delete
# that was not undone) once the op can no longer be reached.
func _release_op(op) -> void:
	if typeof(op) != TYPE_DICTIONARY:
		return
	if op.get("kind") == "group":
		for entry in op.get("entries", []):
			_release_op(entry)
		return
	var node = op.get("node")
	if node != null and is_instance_valid(node) and node.get_parent() == null:
		node.queue_free()


func _discard_redo() -> void:
	for op in _redo_stack:
		_release_op(op)
	_redo_stack = []


# What undo() / redo() report about a step.
func _op_summary(op : Dictionary, out : Dictionary) -> Dictionary:
	out["kind"] = op["kind"]
	if op["kind"] == "group":
		out["steps"] = op["entries"].size()
		if str(op.get("label", "")) != "":
			out["label"] = op["label"]
	return out


func _do_undo() -> Dictionary:
	if _undo_stack.empty():
		return _ok({ "undone": false, "reason": "nothing to undo" })
	var op = _undo_stack.pop_back()
	_apply_op(op, true)
	_redo_stack.append(op)
	return _ok(_op_summary(op, { "undone": true, "undo_depth": _undo_stack.size() }))


func _do_redo() -> Dictionary:
	if _redo_stack.empty():
		return _ok({ "redone": false, "reason": "nothing to redo" })
	var op = _redo_stack.pop_back()
	_apply_op(op, false)
	_op_serial += 1
	op["serial"] = _op_serial   # the newest edit again, for checkpoints
	_undo_stack.append(op)
	return _ok(_op_summary(op, { "redone": true, "redo_depth": _redo_stack.size() }))


# --- undo groups: several edits that undo() and redo() treat as one step ----

func _begin_group(req : Dictionary) -> Dictionary:
	var label = str(req.get("label", "")).strip_edges()
	_groups.append({ "kind": "group", "label": label, "entries": [],
		"token": str(req.get("undo_group", "")) })
	return _ok({ "open_groups": _groups.size(), "label": label })


# Close the innermost group. One recorded edit is pushed as itself, several as
# one group op, none pushes nothing.
func _end_group(req : Dictionary) -> Dictionary:
	if _groups.empty():
		return _err("no undo group is open")
	if str(_groups.back().get("token", "")) != str(req.get("undo_group", "")):
		return _err("the open undo group belongs to another request")
	var steps = _close_group()
	return _ok({ "steps": steps, "open_groups": _groups.size(), "undo_depth": _undo_stack.size() })


func _close_group() -> int:
	var group = _groups.pop_back()
	var entries = group["entries"]
	if entries.size() == 1:
		_push_op(entries[0])
	elif entries.size() > 1:
		_push_op(group)
	return entries.size()


func _close_all_groups() -> void:
	while not _groups.empty():
		_close_group()


# --- checkpoints: a mark in history that rollback_checkpoint rewinds to -----

func _checkpoint(req : Dictionary) -> Dictionary:
	var label = str(req.get("label", "")).strip_edges()
	if label == "" or label.length() > 64:
		return _err("'label' is required, up to 64 characters")
	if not _checkpoints.has(label) and _checkpoints.size() >= MAX_CHECKPOINTS:
		return _err("%d checkpoints are open; roll one back or reuse a label" % MAX_CHECKPOINTS)
	_checkpoints[label] = { "label": label, "after": _op_serial, "opened": OS.get_ticks_msec() }
	return _ok({ "label": label, "undo_depth": _undo_stack.size(), "history_limit": MAX_UNDO_OPS })


func _checkpoint_view(cp : Dictionary) -> Dictionary:
	var steps := 0
	for op in _undo_stack:
		if int(op.get("serial", 0)) > int(cp["after"]):
			steps += 1
	return { "label": cp["label"], "steps_since": steps,
		"reachable": _lost_serial <= int(cp["after"]),
		"age_ms": OS.get_ticks_msec() - int(cp["opened"]) }


func _list_checkpoints() -> Dictionary:
	var out := []
	for label in _checkpoints:
		out.append(_checkpoint_view(_checkpoints[label]))
	return _ok({ "checkpoints": out })


# Undo every step recorded since the checkpoint, newest first. The checkpoint
# stays open, so the next attempt can be rolled back to the same point. Refuses,
# changing nothing, once history has dropped a step made after it.
func _rollback_checkpoint(req : Dictionary) -> Dictionary:
	var label = str(req.get("label", "")).strip_edges()
	if not _checkpoints.has(label):
		return _err("no checkpoint '%s'; open ones: %s" % [label, str(_checkpoints.keys())])
	var cp = _checkpoints[label]
	if _lost_serial > int(cp["after"]):
		return _err(("history no longer reaches checkpoint '%s': a step made after it was " +
			"dropped (history keeps %d steps). Nothing was changed; undo step by step, " +
			"or reopen the last saved map.") % [label, MAX_UNDO_OPS])
	var kinds := []
	while not _undo_stack.empty() and int(_undo_stack.back().get("serial", 0)) > int(cp["after"]):
		var res = _do_undo()
		if not res.get("ok", false) or not res["result"].get("undone", false):
			break
		kinds.append(res["result"]["kind"])
	return _ok({ "label": label, "rolled_back": kinds.size(), "kinds": kinds,
		"undo_depth": _undo_stack.size(), "redo_depth": _redo_stack.size() })


func _apply_op(op, undo : bool):
	match op["kind"]:
		"create":
			if undo:
				_detach_node(_op_node(op), int(op["id"]))
			else:
				_attach_node(_op_node(op), _op_parent(op), int(op["id"]))
		"create_saved":
			if undo:
				_delete_saved(op)
			else:
				_reload_saved(op)
		"delete":
			if undo:
				_attach_node(_op_node(op), _op_parent(op), int(op["id"]))
			else:
				_detach_node(_op_node(op), int(op["id"]))
		"delete_saved":
			if undo:
				_reload_saved(op)
			else:
				_delete_saved(op)
		"group":
			if undo:
				# Newest first: a later entry may sit on an earlier one (a door on a wall).
				for i in range(op["entries"].size() - 1, -1, -1):
					_apply_op(op["entries"][i], true)
			else:
				for entry in op["entries"]:
					_apply_op(entry, false)
		"transform":
			_apply_props(Global.World.GetNodeByID(int(op["id"])), op["old"] if undo else op["new"])
		"terrain":
			_restore_splat(op["before"] if undo else op["after"])
		"cave":
			_restore_cave(op["before"] if undo else op["after"])
		"wall":
			if Global.World.HasNodeID(int(op["id"])):
				var wall = Global.World.GetNodeByID(int(op["id"]))
				if wall != null and _is_wall(wall):
					_apply_wall_state(wall, op["old"] if undo else op["new"])
		"light":
			if Global.World.HasNodeID(int(op["id"])):
				var light = Global.World.GetNodeByID(int(op["id"]))
				if light != null and light is Light2D:
					_apply_light_state(light, op["old"] if undo else op["new"])
		"text":
			if Global.World.HasNodeID(int(op["id"])):
				var text = Global.World.GetNodeByID(int(op["id"]))
				if text != null and _is_text(text):
					_apply_text_state(text, op["old"] if undo else op["new"])
		"layer":
			if Global.World.HasNodeID(int(op["id"])):
				var node = Global.World.GetNodeByID(int(op["id"]))
				if node != null and _element_layer(node) != null:
					_apply_element_layer(node, int(op["old"] if undo else op["new"]))
		"water":
			if is_instance_valid(op["mesh"]):
				op["mesh"].Load(op["before"] if undo else op["after"])
		"material":
			if is_instance_valid(op["mesh"]):
				_set_material_bitmap(op["mesh"], op["before"] if undo else op["after"])
		"map_style":
			_restore_map_style(op["before"] if undo else op["after"])


# The parent to re-attach under: the one the op holds or, when that was a wall
# since reloaded by undo/redo (a new object carrying the old id), the wall that
# holds the id now.
# The node an op attaches or detaches: the one it holds or, when that was
# freed and reloaded under the same id (a door whose wall was reloaded), the
# node that holds the id now.
func _op_node(op):
	var node = op.get("node")
	if is_instance_valid(node):
		return node
	var id = int(op.get("id", -1))
	if id >= 0 and Global.World.HasNodeID(id):
		node = Global.World.GetNodeByID(id)
		op["node"] = node
	return node


func _op_parent(op):
	var parent = op.get("parent")
	if is_instance_valid(parent):
		return parent
	if op.has("parent_id") and Global.World.HasNodeID(int(op["parent_id"])):
		return Global.World.GetNodeByID(int(op["parent_id"]))
	return parent


func _detach_node(node, id : int):
	if node == null or not is_instance_valid(node):
		return   # nothing to take out, and the id may belong to a reloaded node
	if node.get_parent() != null:
		node.get_parent().remove_child(node)
	if Global.World.HasNodeID(id):
		Global.World.RemoveNodeID(id)


func _attach_node(node, parent, id : int):
	if not is_instance_valid(node):
		return
	if node.get_parent() == null and is_instance_valid(parent):
		if parent.has_method("InsertPortal") and node.get("WallID") != null:
			# A door goes back into its wall's portal list; InsertPortal adds the
			# child itself (next frame) and cuts the gap. Plain add_child would
			# show the door on a wall that no longer knows about it.
			parent.InsertPortal(node, true)
		else:
			parent.add_child(node)
			if parent.has_method("AddToSearchTable"):
				parent.AddToSearchTable(node, false)   # selectable again in the editor
	Global.World.SetNodeID(node, id)
	if node.has_method("RemakeLines"):
		node.RemakeLines()   # walls/paths cache geometry; refresh after re-adding


# The op that undoes the creation of `node`. Props and walls can't be detached
# and re-attached (see _saved_kind), so they mirror DD's own ObjectRecord /
# WallRecord: serialise + delete on undo, reload on redo. Everything else is
# detached and held.
func _create_op(node, id : int) -> Dictionary:
	var saved = _saved_kind(node)
	if saved != null:
		return { "kind": "create_saved", "parent": node.get_parent(), "id": id,
			"loader": saved["loader"], "save_args": saved["save_args"] }
	var op := { "kind": "create", "node": node, "parent": node.get_parent(), "id": id }
	if _is_wall(node.get_parent()):
		op["parent_id"] = _id(node.get_parent())   # a door's wall may be reloaded (see _op_parent)
	return op


# Kinds that must go through Dungeondraft's own save/load instead of being
# detached and re-attached, with the parent's loader and the Save() arguments
# (a C# default is not filled in for a call from GDScript, so Prop.Save's
# `copy` must be passed):
# - a Prop: Prop._EnterTree builds a fresh, empty Sprite on every re-entry, so
#   a re-added prop loses its texture (unselectable, saved texture-less);
# - a Wall: Wall._ExitTree deletes its mounted portals and drops its id the
#   moment it leaves the tree, so a detached wall comes back without its doors.
# Returns null for everything else.
func _saved_kind(node):
	var parent = node.get_parent()
	if parent == null:
		return null
	if parent.has_method("LoadObject"):
		return { "loader": "LoadObject", "save_args": [false] }
	if _is_wall(node) and parent.has_method("LoadWall"):
		return { "loader": "LoadWall", "save_args": [] }
	return null


# Delete a saved-kind node: snapshot it (Save keeps node_id) so it can be
# reloaded, then free it. Prop._ExitTree drops it from the select search table
# itself; Wall._ExitTree deletes the wall's portals with it.
func _delete_saved(op) -> void:
	var id = int(op["id"])
	if not Global.World.HasNodeID(id):
		return
	var node = Global.World.GetNodeByID(id)
	if node == null:
		return
	op["data"] = node.callv("Save", op["save_args"])
	op["index"] = node.get_index()
	Global.World.DeleteNodeByID(id)
	# DeleteNodeByID frees the node at the end of the frame; take it out of the
	# tree now so a request handled in the same frame no longer lists it.
	if is_instance_valid(node) and node.get_parent() != null:
		node.get_parent().remove_child(node)


# Reload a saved-kind node from its snapshot with the same id (DD's
# ObjectRecord.Create / WallRecord path): LoadObject returns the prop, LoadWall
# returns nothing, so the wall is found by the id its data carried.
func _reload_saved(op) -> void:
	if op.get("data") == null or not is_instance_valid(op["parent"]):
		return
	var id = int(op["id"])
	var node = op["parent"].call(op["loader"], op["data"])
	if node == null and Global.World.HasNodeID(id):
		node = Global.World.GetNodeByID(id)
	if node == null or not is_instance_valid(node):
		return
	op["parent"].move_child(node, min(int(op["index"]), op["parent"].get_child_count() - 1))
	# Load can register the node under a fresh id when the saved one is taken.
	# Callers hold the op's id, so make sure that is what resolves.
	var current = Global.World.GetNodeByID(id) if Global.World.HasNodeID(id) else null
	if current != node:
		var fresh = _id(node)
		if fresh != id and Global.World.HasNodeID(fresh):
			Global.World.RemoveNodeID(fresh)
		Global.World.SetNodeID(node, id)
		node.set_meta("node_id", id)


func _apply_props(node, snap):
	if node == null or snap == null:
		return
	if snap.get("position") != null:
		node.position = snap["position"]
	if snap.get("rotation") != null:
		node.rotation = snap["rotation"]
	if snap.get("scale") != null:
		node.scale = snap["scale"]
	if snap.get("shadow") != null:
		node.set("HasShadow", snap["shadow"])
	if snap.get("block_light") != null and node.has_method("SetBlockLight"):
		node.SetBlockLight(snap["block_light"])
	if snap.has("color"):
		if snap["color"] != null:
			_apply_custom_color(node, snap["color"])
		elif node.HasCustomColor():
			node.ClearCustomColor()


# Both splat weight images (the second only exists with expanded slots), like
# DD's own PaintTerrain / PaintTerrain2 history records.
func _terrain_snapshot(level) -> Dictionary:
	return {
		"splat": level.Terrain.CloneSplatImage(),
		"splat2": level.Terrain.CloneSplatImage2() if level.Terrain.ExpandedSlots else null,
	}


func _restore_splat(snap):
	if snap == null or snap["splat"] == null:
		return
	var level = Global.World.GetCurrentLevel()
	if level == null:
		return
	if snap["splat2"] != null and level.Terrain.ExpandedSlots:
		level.Terrain.RestoreSplat2(snap["splat"], snap["splat2"])
	else:
		level.Terrain.RestoreSplat(snap["splat"])
	level.Terrain.UpdateSplat()


# Deep-copy the cave BitMap for the undo stack (Resource.duplicate(true) so the
# snapshot isn't aliased to the live bitmap). Returns null if no cave is present.
func _cave_snapshot():
	var cave = _cave_mesh()
	if cave == null or not cave.has_method("get_Bitmap"):
		return null
	var bm = cave.call("get_Bitmap")
	if bm == null:
		return null
	return bm.duplicate(true)


# Restore a snapshotted cave BitMap and rebuild the mesh (mirror of _restore_splat
# for the cave layer). A duplicate(true) of the stored snapshot is pushed so the
# op's snapshot stays pristine across repeated undo/redo.
func _restore_cave(bm):
	if bm == null:
		return
	var cave = _cave_mesh()
	if cave == null or not cave.has_method("SetBitmap"):
		return
	cave.call("SetBitmap", bm.duplicate(true))
	if cave.has_method("FinalizeMeshAndBorders"):
		cave.call("FinalizeMeshAndBorders")
	cave.call("UpdateMesh")


func _snapshot(node) -> Dictionary:
	var s := {}
	if node is Node2D:
		s["position"] = node.position
		s["rotation"] = node.rotation
		s["scale"] = node.scale
	if node.get("HasShadow") != null:
		s["shadow"] = node.get("HasShadow")
	if node.has_method("SetBlockLight"):
		s["block_light"] = bool(node.get("BlockLight"))
	if node.has_method("HasCustomColor"):
		s["color"] = node.GetCustomColor() if node.HasCustomColor() else null
	return s


# Dispatch with a guard so a bad command can never take down the TCP loop.
func _safe_dispatch(req : Dictionary) -> Dictionary:
	var cmd = req.get("cmd", "")
	match cmd:
		# --- read / query ---
		"ping": return _ok({ "pong": true, "protocol": PROTOCOL_VERSION, "engine": Engine.get_version_info(),
			"auth": "token" if _require_token else "off", "authenticated": _authenticated(req),
			"paused": _paused })
		"get_status": return _get_status()
		"list_asset_categories": return _ok({ "categories": ASSET_CATEGORIES })
		"list_assets": return _list_assets(req)
		"list_elements": return _list_elements(req)
		"get_element": return _get_element(req)
		"list_levels": return _list_levels()
		"list_asset_packs": return _list_asset_packs()
		"terrain_samples": return _terrain_samples(req)
		"dig_cave": return _dig_cave(req)
		"clear_caves": return _clear_caves(req)
		# --- create ---
		"place_object": return _place_object(req)
		"draw_wall": return _draw_wall(req)
		"draw_path": return _draw_path(req)
		"add_light": return _add_light(req)
		"add_portal": return _add_portal(req)
		"add_roof": return _add_roof(req)
		"add_text": return _add_text(req)
		"place_pattern": return _place_pattern(req)
		"build_room": return _build_room(req)
		"import_image": return _import_image(req)
		"draw_water": return _draw_water(req)
		"draw_material": return _draw_material(req)
		# --- terrain ---
		"set_terrain_slot": return _set_terrain_slot(req)
		"fill_terrain": return _fill_terrain(req)
		"fill_region": return _fill_region(req)
		"paint_terrain": return _paint_terrain(req)
		"paint_path": return _paint_path(req)
		# --- modify / delete ---
		"move_element": return _move_element(req)
		"modify_object": return _modify_object(req)
		"modify_wall": return _modify_wall(req)
		"modify_light": return _modify_light(req)
		"modify_text": return _modify_text(req)
		"set_element_layer": return _set_element_layer(req)
		"duplicate_object": return _duplicate_object(req)
		"delete_element": return _delete_element(req)
		"delete_elements": return _delete_elements(req)
		# --- map lifecycle ---
		"get_current_map": return _get_current_map()
		"save_map": return _save_map()
		"open_map": return _open_map(req)
		# --- levels / layers ---
		"add_level": return _add_level(req)
		"set_level": return _set_level(req)
		"rename_level": return _rename_level(req)
		"clone_level": return _clone_level(req)
		"reorder_levels": return _reorder_levels(req)
		"compare_levels": return _compare_levels(req)
		"delete_level": return _delete_level(req)
		"list_layers": return _list_layers()
		"add_layer": return _add_layer(req)
		"configure_level": return _configure_level(req)
		"get_map_style": return _get_map_style()
		"list_prefabs": return _list_prefabs(req)
		"place_prefab": return _place_prefab(req)
		"generator_options": return _generator_options()
		"generate_dungeon": return _generate_dungeon(req)
		"set_map_style": return _set_map_style(req)
		"set_trace_image": return _set_trace_image(req)
		# --- capture ---
		"screenshot": return _screenshot(req)
		"export_map": return _export_map(req)
		"set_grid": return _set_grid(req)
		"export_finish": return _export_finish()
		# --- camera ---
		"get_camera": return _get_camera()
		"set_camera": return _set_camera(req)
		"focus_element": return _focus_element(req)
		"fit_elements": return _fit_elements(req)
		"reset_camera": return _reset_camera()
		# --- history (bridge-managed undo/redo of the model's own edits) ---
		"undo": return _do_undo()
		"redo": return _do_redo()
		"begin_undo_group": return _begin_group(req)
		"end_undo_group": return _end_group(req)
		"checkpoint": return _checkpoint(req)
		"list_checkpoints": return _list_checkpoints()
		"rollback_checkpoint": return _rollback_checkpoint(req)
		# --- selection ---
		"select_elements": return _select_elements(req)
		"clear_selection": return _clear_selection()
		# --- native access (Dungeondraft's own API and UI) ---
		"native_targets": return _native_targets()
		"native_describe": return _native_describe(req)
		"native_get": return _native_get(req)
		"native_set": return _native_set(req)
		"native_call": return _native_call(req)
		"ui_tree": return _ui_tree(req)
		"ui_action": return _ui_action(req)
		# --- spatial facts (the checks themselves run in the server) ---
		"spatial_snapshot": return _spatial_snapshot(req)
		"asset_footprint": return _asset_footprint(req)
		"list_spatial_regions": return _list_spatial_regions()
		"set_spatial_region": return _set_spatial_region(req)
		"remove_spatial_region": return _remove_spatial_region(req)
		_: return _err("unknown cmd: " + str(cmd))


const ASSET_CATEGORIES := [
	"Objects", "Walls", "Paths", "Terrain", "Lights", "Portals", "Roofs",
	"Patterns", "Patterns Colorable", "Caves", "Materials",
	"Simple Tiles", "Smart Tiles", "Smart Tiles Double",
]


# ---------------------------------------------------------------------------
# Read / query
# ---------------------------------------------------------------------------

func _get_status() -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null:
		return _ok({ "map_open": false })
	var counts := {}
	for kind in COLLECTIONS:
		counts[kind] = _collection(level, kind).get_child_count()
	return _ok({
		"map_open": true,
		"level_id": level.ID,
		"level_index": Global.World.CurrentLevelId,
		"level_count": Global.World.levels.size(),
		"map_size_woxels": [Global.World.WoxelDimensions.x, Global.World.WoxelDimensions.y],
		"map_center": [Global.World.WoxelDimensions.x * 0.5, Global.World.WoxelDimensions.y * 0.5],
		"terrain_slots": 8 if level.Terrain.ExpandedSlots else 4,
		"terrain_visible": level.Terrain.visible,
		"counts": counts,
		"active_tool": Global.Editor.ActiveToolName,
		"paused": _paused,
	})


func _list_assets(req : Dictionary) -> Dictionary:
	var category = req.get("category", "Objects")
	var search = str(req.get("search", "")).to_lower()
	var limit = int(req.get("limit", 100))
	var all = Script.GetAssetList(category)
	if all == null:
		return _err("unknown asset category: " + str(category))
	var out := []
	var matched := 0
	for path in all:
		if search != "" and str(path).to_lower().find(search) == -1:
			continue
		matched += 1
		if out.size() < limit:
			out.append(path)
	return _ok({ "category": category, "total": all.size(), "matched": matched, "returned": out.size(), "assets": out })


# Resolve the cave editing target: enable the CaveBrush (the UI path that wires
# the mesh to the level) and return its CaveMesh, or null if unavailable.
func _cave_mesh():
	if not Global.Editor.Tools.has("CaveBrush"):
		return null
	var brush = Global.Editor.Tools["CaveBrush"]
	brush.call("Enable")
	if not brush.has_method("get_Mesh"):
		return null
	return brush.call("get_Mesh")


# Woxel position -> cave-bitmap cell (the bitmap is the woxel grid at CellSize
# woxels/cell plus a MapEdgeBuffer border).
func _cave_cell(cave, world : Vector2) -> Vector2:
	var cs = cave.call("get_CellSize")
	var buf = int(cave.get("MapEdgeBuffer"))
	return Vector2(int(floor(world.x / cs)) + buf, int(floor(world.y / cs)) + buf)


# Dig (or fill) a cave along a polyline brush stroke. The cave is a MeshInstance2D
# whose open/rock state is a Godot BitMap (true = open cave floor); editing it
# then SetBitmap + UpdateMesh rebuilds the floor, rocky wall border and debris
# (exactly what the UI's Cave Brush does). Params:
#   points:[[x,y]...] (>=1) woxel path; single point = one dab.
#   radius (woxels, default 256 = 1 tile), value/dig (true=dig open, false=fill),
#   ground_color / wall_color (optional tints), texture (optional Caves asset).
# A multi-point path is rasterized as a constant-width ribbon so it digs a smooth
# tunnel (like paint_path, but into the cave bitmap).
func _dig_cave(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var cave = _cave_mesh()
	if cave == null: return _err("cave brush/mesh unavailable")
	if not cave.has_method("get_Bitmap") or not cave.has_method("SetBitmap"):
		return _err("cave mesh missing BitMap API")

	# Optional cave tints / floor texture (apply before the mesh rebuild).
	if req.has("ground_color") and str(req.get("ground_color", "")) != "":
		var gc = _color(req["ground_color"], Color(0.5, 0.5, 0.45))
		if cave.has_method("SetGroundColor"): cave.call("SetGroundColor", gc)
	if req.has("wall_color") and str(req.get("wall_color", "")) != "":
		var wc = _color(req["wall_color"], Color(0.5, 0.5, 0.45))
		if cave.has_method("SetWallColor"): cave.call("SetWallColor", wc)
	if req.has("texture") and str(req.get("texture", "")) != "":
		var tex = _asset_tex("Caves", req["texture"])
		if tex != null and cave.has_method("SetFloorTexture"):
			cave.call("SetFloorTexture", tex)

	# Path -> cell-space points.
	var pts := []
	if req.has("points"):
		for p in req["points"]:
			pts.append(_cave_cell(cave, Vector2(float(p[0]), float(p[1]))))
	else:
		pts.append(_cave_cell(cave, _xy(req, Global.World.WoxelDimensions * 0.5)))
	if pts.empty(): return _err("provide 'points':[[x,y]...] or x/y")

	var value = bool(req.get("value", req.get("dig", true)))
	var cs = cave.call("get_CellSize")
	var rad_cells = max(int(round(float(req.get("radius", 256.0)) / cs)), 1)

	var bm = cave.call("get_Bitmap")
	if bm == null: return _err("cave bitmap is null")
	var size = bm.call("get_size")
	var bw = int(size.x)
	var bh = int(size.y)
	# Rasterize the stroke: dab each point, then thicken the segments between them.
	var n := 0
	for cell in pts:
		n += _cave_stamp(bm, cell, rad_cells, value, bw, bh)
	for i in range(pts.size() - 1):
		_cave_stroke_segment(bm, pts[i], pts[i + 1], rad_cells, value, bw, bh)

	cave.call("SetBitmap", bm)
	if cave.has_method("FinalizeMeshAndBorders"):
		cave.call("FinalizeMeshAndBorders")
	cave.call("UpdateMesh")
	return _ok({
		"dug": value, "cells_painted": n, "radius_cells": rad_cells,
		"points": pts.size(), "bitmap_size": [bw, bh],
	})


# Stamp a filled circle of cells into the BitMap. Returns the count set.
func _cave_stamp(bm, c : Vector2, rad : int, value : bool, bw : int, bh : int) -> int:
	var n := 0
	var x0 = max(int(c.x) - rad, 0)
	var y0 = max(int(c.y) - rad, 0)
	var x1 = min(int(c.x) + rad, bw - 1)
	var y1 = min(int(c.y) + rad, bh - 1)
	for iy in range(y0, y1 + 1):
		for ix in range(x0, x1 + 1):
			if Vector2(ix, iy).distance_to(c) <= rad:
				bm.call("set_bit", Vector2(ix, iy), value)
				n += 1
	return n


# Stamp a thick line of cells between two cell-space points (no gaps between dabs).
func _cave_stroke_segment(bm, a : Vector2, b : Vector2, rad : int, value : bool, bw : int, bh : int) -> void:
	var steps = int(ceil(a.distance_to(b)))
	if steps <= 0:
		return
	for s in range(steps + 1):
		_cave_stamp(bm, a.linear_interpolate(b, float(s) / steps), rad, value, bw, bh)


# Wipe the entire cave layer back to solid rock. Uses the native Clear() then
# rebuilds the mesh; if Clear isn't present, falls back to zeroing the BitMap
# (set every bit false) and pushing it back — both render via UpdateMesh. This is
# a CAVE_CMD, so it's BitMap-snapshotted for undo like dig_cave.
func _clear_caves(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var cave = _cave_mesh()
	if cave == null: return _err("cave brush/mesh unavailable")
	var method = ""
	if cave.has_method("Clear"):
		cave.call("Clear")
		method = "Clear"
	elif cave.has_method("get_Bitmap") and cave.has_method("SetBitmap"):
		var bm = cave.call("get_Bitmap")
		if bm != null:
			var size = bm.call("get_size")
			for iy in range(int(size.y)):
				for ix in range(int(size.x)):
					bm.call("set_bit", Vector2(ix, iy), false)
			cave.call("SetBitmap", bm)
			method = "zero_bitmap"
	else:
		return _err("cave mesh missing Clear/BitMap API")
	if cave.has_method("FinalizeMeshAndBorders"):
		cave.call("FinalizeMeshAndBorders")
	cave.call("UpdateMesh")
	return _ok({ "cleared": true, "method": method })


func _list_elements(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null:
		return _err("no map open")
	var kind = req.get("kind", "objects")
	if not COLLECTIONS.has(kind):
		return _err("unknown kind '%s' (one of: %s)" % [kind, COLLECTIONS.keys()])
	var limit = int(req.get("limit", 200))
	var out := []
	# Wall-mounted portals (doors/windows) live inside each wall's `Portals`
	# array, NOT in level.Portals — so listing "portals" must also walk the
	# walls, or hand-placed doors are invisible to query.
	if kind == "portals":
		for wall in level.Walls.get_children():
			var wp = wall.get("Portals")
			if wp == null:
				continue
			for portal in wp:
				if out.size() >= limit:
					break
				out.append(_describe_wall_portal(portal, wall))
	for node in _collection(level, kind).get_children():
		if out.size() >= limit:
			break
		out.append(_describe(node))
	return _ok({ "kind": kind, "count": out.size(), "elements": out })


# Describe a wall-mounted portal (a door/window living in a wall's Portals
# array). Reports its world `position`, the door's half-width `radius`, and an
# outward `normal` (unit vector pointing OUT of the building) plus `facing` in
# degrees. The normal is `Direction` (the along-wall tangent) rotated 90°,
# oriented to point away from the wall's centroid — so a caller can route a road
# to the door and offset along `normal` to stop at the threshold, with no
# knowledge of how the door was placed (it's pure geometry from the data).
func _describe_wall_portal(portal, wall) -> Dictionary:
	var pos = portal.get("position")
	if pos == null or not (pos is Vector2):
		pos = Vector2()
	var tangent = portal.get("Direction")
	if tangent == null or not (tangent is Vector2) or tangent.length() < 0.001:
		tangent = Vector2(1, 0)
	else:
		tangent = tangent.normalized()
	# Outward normal = tangent rotated 90 degrees, flipped to face away from the
	# wall's centroid (which is inside the enclosed room for a building loop).
	var normal = Vector2(-tangent.y, tangent.x)
	var centroid = _wall_centroid(wall)
	if centroid != null and (pos - centroid).dot(normal) < 0.0:
		normal = -normal
	var d := {
		"id": _id(portal), "kind": "wall_portal", "wall_id": _id(wall),
		"position": _vec(pos), "normal": _vec(normal),
		"facing": rad2deg(normal.angle()),
		"closed": bool(portal.get("Closed")),
	}
	var r = portal.get("Radius")
	if r != null:
		d["radius"] = float(r)
	var tex = portal.get("Texture")
	if tex != null and tex is Texture:
		d["asset"] = tex.resource_path
	return d


# Mean of a wall's points — used to orient a portal's normal outward. Returns
# null for a wall with no usable points.
func _wall_centroid(wall):
	var pts = wall.get("Points")
	if pts == null or pts.size() == 0:
		return null
	var sum = Vector2()
	for p in pts:
		sum += p
	return sum / pts.size()


func _get_element(req : Dictionary) -> Dictionary:
	var node = _resolve(req)
	if node == null:
		return _err("no element with id " + str(req.get("id")))
	if _is_wall(node):
		return _ok(_describe_wall(node))
	return _ok(_describe(node))


func _list_levels() -> Dictionary:
	var out := []
	var levels = Global.World.levels
	for i in range(levels.size()):
		var lv = levels[i]
		out.append({ "index": i, "id": lv.ID, "label": lv.Label })
	return _ok({
		"current_index": Global.World.CurrentLevelId,
		"current_id": Global.World.GetCurrentLevel().ID,
		"levels": out,
	})


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

func _place_object(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var tex = _asset_tex("Objects", req.get("asset", ""))
	if tex == null: return _err("could not load object asset: " + str(req.get("asset")))
	var layer = int(req.get("layer", DEFAULT_LAYER))
	var bad = _layer_error(level, layer)
	if bad != "": return _err(bad)
	var prop = level.Objects.CreateObject(int(req.get("sorting", 0)))
	prop.z_index = layer
	prop.SetTexture(tex)
	prop.position = _xy(req, Global.World.WoxelDimensions * 0.5)
	var s = float(req.get("scale", 1.0))
	prop.scale = Vector2(s, s)
	prop.rotation = deg2rad(float(req.get("rotation", 0.0)))
	var result := { "id": _id(prop), "position": _vec(prop.position) }
	_color_new_prop(prop, req, result)
	_register_prop(level, prop, int(req.get("sorting", 0)))
	return _ok(result)


# Custom color, the way ObjectTool.PromoteCustomColor does it: only "Colorable"-
# tagged textures get one, and the CustomColors shader material must be on the
# Sprite BEFORE SetCustomColor — Prop.SetCustomColor only sets the shader's
# tint_r param if a ShaderMaterial is already there, so on a bare CreateObject()
# prop it records the color but renders nothing (the raw red mask shows).
# Colorable + no color -> DEFAULT_OBJECT_TINT. Non-colorable + color -> ignored
# with a `warning` (DD's own tools ignore it too).
func _color_new_prop(prop, req : Dictionary, result : Dictionary) -> void:
	var wants = req.has("color") and str(req.get("color", "")) != ""
	if _is_colorable(prop):
		var c = _color(req["color"], DEFAULT_OBJECT_TINT) if wants else DEFAULT_OBJECT_TINT
		_apply_custom_color(prop, c)
		result["color"] = "#" + c.to_html(false)
	elif wants:
		result["warning"] = "asset is not Colorable; color ignored"


func _apply_custom_color(prop, c : Color) -> void:
	var spr = prop.get("Sprite")
	if spr != null and not (spr.material is ShaderMaterial):
		spr.material = load(CUSTOM_COLORS_MATERIAL).duplicate(false)
	prop.SetCustomColor(c)
	# Prop.Load clears the texture flags of a custom-colored prop (no filter /
	# mipmaps, which would bleed the mask edge); mirror it.
	var tex = prop.get("Texture")
	if tex != null and tex.flags != 0:
		tex.flags = 0


func _is_colorable(prop) -> bool:
	if not prop.has_method("SetCustomColor"):
		return false
	var tex = prop.get("Texture")
	if tex == null or not (tex is Texture):
		return false
	var path = tex.resource_path
	return path.find("colorable") != -1 or _colorable_paths().has(path)


# DD's Master.Tags isn't reachable from GDScript (static C#), so rebuild the
# "Colorable" set from the same files Master.LoadTags reads: the built-in tags,
# each asset pack's (entries relative to the pack dir) and the user override.
func _colorable_paths() -> Dictionary:
	if _colorable != null:
		return _colorable
	_colorable = {}
	_load_colorable_tags("res://" + TAGS_FILE, "")
	var dir = Directory.new()
	if dir.open("res://packs") == OK:
		dir.list_dir_begin(true, true)
		var name = dir.get_next()
		while name != "":
			if dir.current_is_dir():
				var pack = "res://packs/" + name + "/"
				_load_colorable_tags(pack + TAGS_FILE, pack)
			name = dir.get_next()
		dir.list_dir_end()
	_load_colorable_tags(OS.get_user_data_dir() + "/override.dungeondraft_tags", "")
	return _colorable


func _load_colorable_tags(path : String, prefix : String) -> void:
	var f = File.new()
	if not f.file_exists(path) or f.open(path, File.READ) != OK:
		return
	var parsed = JSON.parse(f.get_as_text())
	f.close()
	if parsed.error != OK or typeof(parsed.result) != TYPE_DICTIONARY:
		return
	var tags = parsed.result.get("tags")
	if typeof(tags) != TYPE_DICTIONARY or typeof(tags.get("Colorable")) != TYPE_ARRAY:
		return
	for p in tags["Colorable"]:
		_colorable[prefix + str(p)] = true


# A bare CreateObject() skips two things DD's own placement paths do
# (ObjectTool.Record / Objects.LoadObject), and both bite later:
#  - the "preview" meta: Objects/Pathways/Lights.Save() read it with a hard
#    (bool) cast. Missing on a path or light, the cast throws, the whole save
#    aborts (no file written) and DD stays "busy" until restart; missing on an
#    object, the save silently DROPS that object.
#  - Objects.AddToSearchTable: SelectTool hit-tests props only through
#    QuickSearchBuckets, so an unregistered prop can never be clicked/selected.
# Register after the final z/sorting is set (the bucket is keyed by z_index).
func _register_prop(level, prop, sorting : int) -> void:
	prop.set_meta("preview", false)
	level.Objects.AddToSearchTable(prop, sorting == 1)


func _draw_wall(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var pts = _points(req.get("points", []))
	if pts.size() < 2: return _err("'points' needs >= 2 [x,y] pairs")
	var tex = _asset_tex("Walls", req.get("asset", ""))
	var wall = level.Walls.AddWall(
		pts, tex, _wall_color(req, tex),
		bool(req.get("loop", false)), bool(req.get("shadow", true)),
		int(req.get("type", 0)), int(req.get("joint", 1)), true)
	if wall == null: return _err("AddWall returned null (bad asset/points?)")
	return _ok({ "id": _id(wall), "point_count": pts.size() })


# Wall tint: use the caller's `color` if given, else the texture's own default
# (WallTool.GetWallColor) so stone/wood walls render with their natural tint
# instead of a bleached white (Color(1,1,1) = no tint = washed-out base).
func _wall_color(req : Dictionary, tex) -> Color:
	if req.has("color") and str(req.get("color", "")) != "":
		return _color(req["color"], Color(1, 1, 1))
	if tex != null and Global.Editor.Tools.has("WallTool"):
		var wt = Global.Editor.Tools["WallTool"]
		if wt.has_method("GetWallColor"):
			var c = wt.GetWallColor(tex)
			if c != null and c is Color:
				return c
	return Color(1, 1, 1)


func _draw_path(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var pts = _points(req.get("points", []))
	if pts.size() < 2: return _err("'points' needs >= 2 [x,y] pairs")
	var tex = _asset_tex("Paths", req.get("asset", ""))
	if tex == null: return _err("could not load path asset: " + str(req.get("asset")))
	var layer = int(req.get("layer", DEFAULT_LAYER))
	var bad = _layer_error(level, layer)
	if bad != "": return _err(bad)
	var path = level.Pathways.CreatePath(
		tex, layer, int(req.get("sorting", 0)),
		bool(req.get("fade_in", false)), bool(req.get("fade_out", false)),
		bool(req.get("grow", false)), bool(req.get("shrink", false)))
	path.SetEditPoints(pts)
	if req.has("smoothness"):
		path.Smoothness = float(req["smoothness"])
		path.Smooth()
	if req.has("width"):
		path.SetWidthScale(float(req["width"]))
	path.UpdateGradient()
	path.set_meta("preview", false)   # Pathways.Save() hard-casts it (see _register_prop)
	return _ok({ "id": _id(path), "point_count": pts.size() })


func _add_light(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var range_value = float(req.get("range", 2.0))
	if range_value <= 0.0: return _err("'range' must be > 0")
	var tex = _asset_tex("Lights", req.get("asset", ""))
	if str(req.get("asset", "")) != "" and tex == null:
		return _err("could not load light asset: " + str(req.get("asset")))
	var light = level.Lights.CreateLight(false)
	light.position = _xy(req, Global.World.WoxelDimensions * 0.5)
	light.color = _color(req.get("color", ""), Color(1, 0.9, 0.7))
	light.energy = float(req.get("energy", 1.0))
	light.shadow_enabled = bool(req.get("shadows", true))
	if tex != null:
		light.texture = tex
	light.texture_scale = _light_scale(light, range_value)
	light.set_meta("preview", false)   # Lights.Save() hard-casts it (see _register_prop)
	return _ok({ "id": _id(light), "position": _vec(light.position), "range": _light_range(light) })


# Dungeondraft's Range, the number its Light tool shows and its saved map and
# Universal VTT export carry, is the Light2D texture scale times
# texture_width / 512 (LightTool: TextureScale = Range * 512 / width). The
# built-in light textures are 1024 px, so Range is twice the scale. The bridge
# takes and reports Range everywhere.
func _light_width(light) -> float:
	if light.texture != null and light.texture.get_width() > 0:
		return float(light.texture.get_width())
	return 1024.0


func _light_range(light) -> float:
	return light.texture_scale * _light_width(light) / 512.0


func _light_scale(light, range_value : float) -> float:
	return range_value * 512.0 / _light_width(light)


# Find the wall segment nearest to `pos`. Returns
# { wall, point_index, closest, direction, distance } or null if no walls.
# A wall is a polyline of `Points`; segment i goes Points[i] -> Points[i+1]
# (plus the closing segment when Loop). We pick the segment whose nearest
# point to `pos` is closest overall, and report that segment's unit tangent.
func _nearest_wall_segment(level, pos : Vector2):
	var best = null
	for wall in level.Walls.get_children():
		var pts = wall.get("Points")
		if pts == null or pts.size() < 2:
			continue
		var seg_count = pts.size() - 1
		if wall.get("Loop"):
			seg_count = pts.size()
		for i in range(seg_count):
			var a = pts[i]
			var b = pts[(i + 1) % pts.size()]
			var closest = _closest_point_on_segment(pos, a, b)
			var dist = pos.distance_to(closest)
			if best == null or dist < best.distance:
				var dir = (b - a)
				if dir.length() > 0.001:
					dir = dir.normalized()
				best = {
					"wall": wall, "point_index": i, "closest": closest,
					"direction": dir, "distance": dist,
				}
	return best


func _closest_point_on_segment(p : Vector2, a : Vector2, b : Vector2) -> Vector2:
	var ab = b - a
	var len2 = ab.dot(ab)
	if len2 < 0.0001:
		return a
	var t = clamp((p - a).dot(ab) / len2, 0.0, 1.0)
	return a + ab * t


func _add_portal(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var tex = _asset_tex("Portals", req.get("asset", ""))
	if tex == null: return _err("could not load portal asset: " + str(req.get("asset")))
	var pos = _xy(req, Global.World.WoxelDimensions * 0.5)
	var closed = bool(req.get("closed", false))
	var radius = float(req.get("radius", 64.0))
	# mount: "wall" (default) snaps onto the nearest wall and cuts a gap;
	# "free" forces a freestanding portal. snap_max caps how far (woxels) a
	# wall may be and still capture the portal.
	var mount = str(req.get("mount", "wall"))
	var snap_max = float(req.get("snap_max", 256.0))
	if mount != "free":
		var seg = _nearest_wall_segment(level, pos)
		if seg != null and seg.distance <= snap_max:
			# Mount onto the wall: snap to the segment, face along it, and let
			# the wall remake its lines so the portal cuts a gap.
			var flip = bool(req.get("flip", false))
			var portal = seg.wall.AddPortal(
				tex, closed, seg.closest, seg.direction,
				seg.point_index, radius, flip)
			seg.wall.RemakeLines()
			if portal == null: return _err("Wall.AddPortal returned null")
			return _ok({
				"id": _id(portal), "kind": "wall_portal",
				"position": _vec(seg.closest), "wall_id": _id(seg.wall),
				"snapped": _vec(seg.closest), "snap_distance": seg.distance,
			})
		if mount == "wall" and not req.get("fallback_free", true):
			return _err("no wall within snap_max (%d) of portal position" % int(snap_max))
	# Freestanding fallback (no wall nearby, or mount == "free").
	level.CreateFreestandingPortal(
		tex, pos, closed, radius, deg2rad(float(req.get("rotation", 0.0))))
	# CreateFreestandingPortal returns void; the new portal is the last child.
	var kids = level.Portals.get_children()
	if kids.empty(): return _err("portal was not created")
	return _ok({ "id": _id(kids[kids.size() - 1]), "kind": "portal", "position": _vec(pos) })


# TEMP DEBUG: dump AddPolygon/AddHip/Set arg signatures so we can find the proper
# CLOSED-roof entry point (Set duplicates the first vertex when we close it,
# which malforms the top-left hip face). Then build a test roof via AddPolygon.
# Add a roof. KEY MODEL (learned by probing): Roof.Set(points, width, type) takes
# the roof's RIDGE LINE (the peak), NOT a footprint to trace — it builds a
# complete, self-closing roof that slopes down `width` woxels perpendicular to
# each side of the ridge, with hips/gables off the ridge ends. So:
#   - 2 points = one clean roof (a short ridge -> a near-pyramid hip).
#   - the footprint covered = the ridge bounding box expanded by `width` on all
#     sides. For a building W x H, put the ridge along the LONG axis, centered,
#     and set width = half the SHORT dimension so the eaves reach the walls.
# (Earlier code wrongly treated points as a footprint and "closed" the loop,
# which duplicated a vertex and malformed the seam corner — removed.)
func _add_roof(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var pts = _points(req.get("points", []))
	if pts.size() < 2: return _err("'points' needs >= 2 [x,y] ridge points")
	var tex = _asset_tex("Roofs", req.get("asset", ""))
	if tex == null: return _err("could not load roof asset: " + str(req.get("asset")))
	var roof = level.Roofs.CreateRoof(int(req.get("sorting", 0)))
	roof.Set(pts, float(req.get("width", 256.0)), int(req.get("type", 0)))  # type: 0 gable,1 hip,2 dormer
	roof.SetTileTexture(tex)
	return _ok({ "id": _id(roof), "ridge_points": pts.size() })


# Place a tiled floor/pattern shape (the "Floor" / Pattern Shape Tool in the UI).
# The shape's texture is set on the PatternShapeTool, then DrawRect/DrawPolygon
# rasterizes the shape into the pattern layer. Pass `rect:[x,y,w,h]` OR
# `points:[[x,y]...]`. `category` selects the asset bank ("Patterns",
# "Patterns Colorable", "Materials", "Simple Tiles", "Smart Tiles").
func _place_pattern(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	if not Global.Editor.Tools.has("PatternShapeTool"):
		return _err("PatternShapeTool not available")
	var category = str(req.get("category", "Patterns"))
	var tex = _asset_tex(category, req.get("asset", ""))
	if tex == null: return _err("could not load pattern asset: " + str(req.get("asset")))

	var shapes = level.PatternShapes
	var tool = Global.Editor.Tools["PatternShapeTool"]
	tool.Texture = tex
	# NOTE: layer switching is intentionally not exposed. tool.SetLayer() on an
	# index DD hasn't created hard-crashes the mod (GDScript has no try/catch)
	# and the valid index range is undocumented. New shapes go to the tool's
	# current layer.
	#
	# Color. CRITICAL: PatternShapeTool.Color is PERSISTENT TOOL STATE that leaks
	# across calls — it holds whatever the previous place_pattern set, NOT a
	# per-texture default (unlike walls, which have WallTool.GetWallColor(tex);
	# patterns have no such per-texture lookup). Reading it back as a "default"
	# made every no-color call inherit the last call's tint, and if it was ever
	# left transparent (alpha~0) every subsequent floor rendered INVISIBLE. So we
	# never trust tool.Color: an explicit `color` is used verbatim; with no color
	# we set a deterministic OPAQUE neutral tint so each call is independent and
	# always renders. (Pass an explicit color for an exact look.)
	var color
	var used_default = false
	if req.has("color") and str(req.get("color", "")) != "":
		color = _color(req["color"], DEFAULT_PATTERN_TINT)
	else:
		color = DEFAULT_PATTERN_TINT
		used_default = true
	if color.a < 0.05:  # guard: never paint an invisible floor
		color = Color(color.r, color.g, color.b, 1.0)
	tool.Color = color  # always set, so we don't inherit/leave leaked state
	var rotation = float(req.get("rotation", 0.0))
	if tool.get("Rotation") != null:
		tool.Rotation.value = rotation

	var before := shapes.GetShapes().size()
	var kind : String
	if req.has("rect"):
		var r = req["rect"]
		if typeof(r) != TYPE_ARRAY or r.size() < 4:
			return _err("'rect' must be [x, y, w, h]")
		shapes.DrawRect(Rect2(float(r[0]), float(r[1]), float(r[2]), float(r[3])), false)
		kind = "rect"
	elif req.has("points"):
		var pts = _points(req["points"])
		if pts.size() < 3:
			return _err("'points' needs >= 3 [x,y] pairs for a polygon")
		shapes.DrawPolygon(pts, false)
		kind = "polygon"
	else:
		return _err("provide 'rect':[x,y,w,h] or 'points':[[x,y]...]")

	# DrawRect/DrawPolygon create the shape but don't apply the texture, so set
	# it on the new shape directly via SetOptions(texture, color, rotation).
	#
	# Z-ORDER: new shapes land in the tool's active layer, by default DD's
	# "Layer 100" node (z 100, ABOVE Objects at z 0), so a floor would cover the
	# furniture. Move the shape with PatternShape.SetLayer(z): PatternShapes
	# keeps one Node2D per layer (GetOrMakeLayer creates it, ZIndex = layer),
	# the layer is what Save()/Load() persist, and it stays relative to the
	# level — so it survives save/reload and the export staging that shifts the
	# whole level to z -2000. (An earlier absolute-z hack did neither: reloaded
	# floors came back on layer 100 and exports drew them over everything.)
	# -100 is DD's own "below objects" layer (hotkey Shift+1).
	var all = shapes.GetShapes()
	var result := { "shape": kind, "category": category, "shape_count": all.size() }
	if color is Color:
		result["color"] = _hex(color)
	if used_default:
		# Signal we applied the neutral default (no per-texture tint exists for
		# patterns; pass an explicit `color` for an exact look).
		result["used_default_tint"] = true
	if all.size() > before and all.size() > 0:
		var shape = all[all.size() - 1]
		if shape.has_method("SetOptions"):
			shape.SetOptions(tex, color, rotation)
		var layer = int(req.get("z", -100))
		if shape.has_method("SetLayer"):
			shape.SetLayer(layer)
		result["id"] = _id(shape)
		result["layer"] = shape.GetLayer() if shape.has_method("GetLayer") else layer
	return _ok(result)


# Build a room in one call: a looped wall AND a floor along the SAME boundary
# path, so the floor meets the wall exactly (the wall covers the floor's outer
# edge) — the way the UI's combined wall+floor trace works. Pass `rect:[x,y,w,h]`
# or `points:[[x,y]...]`. Floor is a pattern by default (`floor:"pattern"`,
# floor_asset + floor_category) or terrain (`floor:"terrain"`, floor_asset +
# floor_slot). Pass `floor:"none"` for walls only. Reuses _draw_wall /
# _place_pattern / _fill_region so behavior matches those tools exactly.
func _build_room(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")

	# Normalize the boundary to a list of [x,y] points (rect -> 4 corners).
	var pts_raw := []
	if req.has("rect"):
		var r = req["rect"]
		if typeof(r) != TYPE_ARRAY or r.size() < 4:
			return _err("'rect' must be [x, y, w, h]")
		var x = float(r[0]); var y = float(r[1]); var w = float(r[2]); var h = float(r[3])
		pts_raw = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
	elif req.has("points"):
		if typeof(req["points"]) != TYPE_ARRAY or req["points"].size() < 3:
			return _err("'points' needs >= 3 [x,y] pairs")
		pts_raw = req["points"]
	else:
		return _err("provide 'rect':[x,y,w,h] or 'points':[[x,y]...]")

	var result := {}

	# The wall and the floor are recorded inside one undo group, so undo() takes
	# the whole room back in one step.
	_groups.append({ "kind": "group", "label": "build_room", "entries": [] })

	# 1) Wall loop along the boundary.
	var wall_req := {
		"cmd": "draw_wall", "points": pts_raw, "loop": true,
		"asset": req.get("wall_asset", ""),
		"type": req.get("wall_type", 0), "joint": req.get("wall_joint", 1),
		"shadow": req.get("wall_shadow", true),
	}
	var wall_res = _record_and_dispatch(wall_req)
	if not wall_res.get("ok", false):
		_close_group()
		return wall_res
	result["wall_id"] = wall_res["result"].get("id")

	# 2) Floor along the SAME boundary (no inset — the wall covers the seam).
	var floor_kind = str(req.get("floor", "pattern"))
	if floor_kind == "pattern":
		var fr := {
			"cmd": "place_pattern", "points": pts_raw,
			"asset": req.get("floor_asset", ""),
			"category": req.get("floor_category", "Simple Tiles"),
		}
		if req.has("floor_color"): fr["color"] = req["floor_color"]
		if req.has("floor_z"): fr["z"] = req["floor_z"]
		var fres = _record_and_dispatch(fr)
		if fres.get("ok", false):
			result["floor_id"] = fres["result"].get("id")
		else:
			result["floor_error"] = fres.get("error")
	elif floor_kind == "terrain":
		var tr := {
			"cmd": "fill_region", "points": pts_raw, "slot": int(req.get("floor_slot", 1)),
		}
		if req.has("floor_asset") and str(req.get("floor_asset", "")) != "":
			tr["asset"] = req["floor_asset"]
		var tres = _record_and_dispatch(tr)
		if tres.get("ok", false):
			result["floor_pixels"] = tres["result"].get("pixels")
		else:
			result["floor_error"] = tres.get("error")
	# floor_kind == "none" -> walls only

	_close_group()
	result["points"] = pts_raw
	return _ok(result)


# A Dungeondraft Text extends Godot LineEdit: the string is the inherited
# `.text`, position is `rect_position`, and size/color must be applied through
# the TextTool (see below) because UpdateText repaints from the tool's settings.
func _add_text(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var text = level.Texts.CreateText()
	# A Text extends LineEdit (a Control): place it via rect_position, not the
	# Node2D `.position` (which silently does nothing on a Control).
	text.rect_position = _xy(req, Global.World.WoxelDimensions * 0.5)
	text.text = str(req.get("text", ""))
	# Font/size/color: TextTool.UpdateText() repaints the focused Text from the
	# TOOL's settings (FontSize/FontColor), overwriting anything set directly on
	# the node. So drive it through the tool: stash the tool's values, set ours,
	# repaint, then restore the tool so the user's UI state is unchanged.
	# A fresh Text reports fontSize 0 until repainted, so default to 32 (DD's
	# standard) rather than the node's pre-paint value when no size is given.
	var size = int(req.get("size", 32))
	if size <= 0:
		size = 32
	var col = _color(req.get("color", ""), Color(0, 0, 0, 1))
	var font_name = str(req.get("font", text.fontName))
	if req.has("font"):
		text.SetFont(font_name, size)  # font name has no tool-member path
	if Global.Editor.Tools.has("TextTool"):
		var tt = Global.Editor.Tools["TextTool"]
		var saved_size = tt.FontSize
		var saved_color = tt.FontColor
		tt.FontSize = size
		tt.FontColor = col
		tt.focus = text
		tt.UpdateText(text)
		tt.FontSize = saved_size
		tt.FontColor = saved_color
	else:
		# No tool available: best-effort direct set.
		text.fontSize = size
		text.fontColor = col
		text.SetFont(font_name, size)
		text.SetFontColor(col)
	return _ok({ "id": _id(text), "size": text.fontSize, "color": "#" + text.fontColor.to_html(false) })


# Embed an image file from disk as an object, the way dropping the file on DD's
# window does (ObjectTool.EmbedObject): the pixels are stored inside the map
# file, so the map no longer needs the source image. DD keys embedded images by
# path, so importing the same path again reuses the stored copy (even if the
# file changed since).
func _import_image(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var path = str(req.get("path", ""))
	if path == "": return _err("missing 'path'")
	if not (path.get_extension().to_lower() in IMAGE_EXTENSIONS):
		return _err("unsupported image type (use png, jpg or webp): " + path)
	if not File.new().file_exists(path): return _err("file not found: " + path)
	if not Global.Editor.Tools.has("ObjectTool"): return _err("ObjectTool unavailable")
	var s = float(req.get("scale", 1.0))
	if s <= 0.0: return _err("'scale' must be > 0")
	var layer = int(req.get("layer", DEFAULT_LAYER))
	var bad = _layer_error(level, layer)
	if bad != "": return _err(bad)
	# EmbedObject dereferences the texture it loads without a null check, so a
	# file that fails to decode would throw inside DD. Decode it here first.
	var img = Image.new()
	if img.load(path) != OK: return _err("could not read image: " + path)
	if max(img.get_width(), img.get_height()) > MAX_IMAGE_SIDE:
		return _err("image is larger than %d px on a side" % MAX_IMAGE_SIDE)

	var count = level.Objects.get_child_count()
	Global.Editor.Tools["ObjectTool"].EmbedObject(path)
	if level.Objects.get_child_count() <= count: return _err("EmbedObject created no object")
	var prop = level.Objects.get_child(level.Objects.get_child_count() - 1)
	# EmbedObject leaves the prop at the cursor, on the Object tool's layer, with
	# the tool's shadow setting, and indexed for selection under that layer. Give
	# it the caller's layer and transform, then re-index it (the search table
	# is keyed by z_index; see _register_prop).
	var sorting = int(req.get("sorting", 0))
	level.Objects.RemoveFromSearchTable(prop)
	prop.z_index = layer
	if sorting == 1:
		level.Objects.move_child(prop, 0)
	prop.position = _xy(req, Global.World.WoxelDimensions * 0.5)
	prop.scale = Vector2(s, s)
	prop.rotation = deg2rad(float(req.get("rotation", 0.0)))
	prop.set("HasShadow", bool(req.get("shadow", true)))
	level.Objects.AddToSearchTable(prop, sorting == 1)
	return _ok({
		"id": _id(prop), "position": _vec(prop.position),
		"image_size": [img.get_width(), img.get_height()],
		"asset": prop.Texture.resource_path,
	})


# Draw (or `erase`) water over a rect/polygon. This is the Water Brush's shape
# mode (WaterMesh.DrawPolygon): the shape is unioned into the level's water, or
# cut out of it, and DD rebuilds the depth gradient and shoreline. A new or
# reshaped body of water takes the mesh's current colors, so the optional
# deep_color / shallow_color / blend_distance are set for this call only and
# the Water Brush's own settings are put back afterwards.
func _draw_water(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var water = level.get("WaterMesh")
	if water == null: return _err("water mesh unavailable")
	var pts = _shape_points(req)
	if pts.size() < 3: return _err("provide 'rect':[x,y,w,h] or 'points':[[x,y]...] (>= 3 points, with some area)")
	var erase = bool(req.get("erase", false))
	var deep = water.DeepColor
	var shallow = water.ShallowColor
	var blend = water.BlendDistance
	var use_deep = _color(req.get("deep_color", ""), deep)
	var use_shallow = _color(req.get("shallow_color", ""), shallow)
	var use_blend = float(req.get("blend_distance", blend))
	# The mesh builder divides by the blend distance; its gradient stops at 4 tiles.
	if use_blend <= 0.0 or use_blend > 4.0:
		return _err("'blend_distance' must be > 0 and <= 4 (tiles)")

	var before = water.Save()
	water.DeepColor = use_deep
	water.ShallowColor = use_shallow
	water.BlendDistance = use_blend
	water.DrawPolygon(pts, erase)
	water.DeepColor = deep
	water.ShallowColor = shallow
	water.BlendDistance = blend
	var after = water.Save()
	if not after.has("tree"): return _err("water mesh did not update")
	if not before.has("tree"):
		# No water before this call: the mesh had no polygon tree to save, and
		# Load ignores a state without one. Undo to a childless root instead.
		before = after.duplicate(true)
		before["tree"]["children"] = []
	var result := { "erase": erase, "point_count": pts.size() }
	if not erase:
		result["deep_color"] = "#" + use_deep.to_html(false)
		result["shallow_color"] = "#" + use_shallow.to_html(false)
		result["blend_distance"] = use_blend
	return _ok_op(result, { "kind": "water", "mesh": water, "before": before, "after": after })


# Paint (or `erase`) a ground material over a rect/polygon: the Material
# Brush's textures, each with its own blended border. Like the cave layer, a
# material is a marching-squares mesh (one per layer + texture) driven by a
# BitMap of grid corners CellSize apart (128 woxels, half a tile); a set corner
# fills out to halfway toward its unset neighbours. Setting every corner inside
# the shape therefore covers it, running up to a quarter tile past its edge.
func _draw_material(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var pts = _shape_points(req)
	if pts.size() < 3: return _err("provide 'rect':[x,y,w,h] or 'points':[[x,y]...] (>= 3 points, with some area)")
	var tex = _asset_tex("Materials", req.get("asset", ""))
	if tex == null: return _err("could not load material asset: " + str(req.get("asset")))
	# Only the level's own drawing layers: GetOrMakeMaterialMesh would happily
	# create one the editor's layer menus know nothing about.
	if not level.has_method("SaveLayers"): return _err("level layers unavailable")
	var layer = int(req.get("layer", DEFAULT_MATERIAL_LAYER))
	var layers = level.SaveLayers()
	if not layers.has(layer):
		return _err("no layer %d on this level (layers: %s)" % [layer, str(layers.keys())])
	var erase = bool(req.get("erase", false))
	if erase and level.IsMeshEmpty(layer, tex):
		return _ok({ "erase": true, "layer": layer, "asset": tex.resource_path, "cells": 0 })
	var smooth = bool(req.get("smooth", true))
	var mesh = level.GetOrMakeMaterialMesh(layer, tex, smooth)
	if mesh == null or not mesh.has_method("get_Bitmap") or not mesh.has_method("SetBitmap"):
		return _err("material mesh missing BitMap API")
	if req.has("smooth"):
		mesh.set("Smooth", smooth)

	var before = mesh.call("get_Bitmap").duplicate(true)
	var bm = before.duplicate(true)
	var cs = float(mesh.call("get_CellSize"))
	var buf = int(mesh.get("MapEdgeBuffer"))
	var size = bm.get_size()
	var poly := []
	var mn = pts[0]
	var mx = pts[0]
	for p in pts:
		poly.append(p)
		mn.x = min(mn.x, p.x); mn.y = min(mn.y, p.y)
		mx.x = max(mx.x, p.x); mx.y = max(mx.y, p.y)
	# Corner (ix, iy) sits at woxel ((ix - buf) * cs, (iy - buf) * cs).
	var x0 = max(int(ceil(mn.x / cs - 0.001)) + buf, 0)
	var y0 = max(int(ceil(mn.y / cs - 0.001)) + buf, 0)
	var x1 = min(int(floor(mx.x / cs + 0.001)) + buf, int(size.x) - 1)
	var y1 = min(int(floor(mx.y / cs + 0.001)) + buf, int(size.y) - 1)
	var cells := 0
	for iy in range(y0, y1 + 1):
		for ix in range(x0, x1 + 1):
			if _point_in_or_on_poly(Vector2((ix - buf) * cs, (iy - buf) * cs), poly):
				bm.set_bit(Vector2(ix, iy), not erase)
				cells += 1
	if cells == 0:
		return _err("shape is too small: no material grid corner (%d woxels apart) falls inside it" % int(cs))
	_set_material_bitmap(mesh, bm)
	return _ok_op(
		{ "erase": erase, "layer": layer, "asset": tex.resource_path, "cells": cells, "cell_size": cs },
		{ "kind": "material", "mesh": mesh, "before": before, "after": bm })


# Push a BitMap into a material mesh and rebuild its fill and border (shared by
# draw_material and its undo). SetBitmap takes a copy, so `bm` stays the caller's.
func _set_material_bitmap(mesh, bm) -> void:
	mesh.call("SetBitmap", bm)
	if mesh.has_method("FinalizeMeshAndBorders"):
		mesh.call("FinalizeMeshAndBorders")


# ---------------------------------------------------------------------------
# Terrain
# ---------------------------------------------------------------------------

func _set_terrain_slot(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var tex = _asset_tex("Terrain", req.get("asset", ""))
	if tex == null: return _err("could not load terrain asset: " + str(req.get("asset")))
	var slot = int(req.get("slot", 0))
	if slot >= 4 and slot <= 7 and bool(req.get("expand", false)):
		_expand_terrain_slots(level)
	var bad = _terrain_slot_error(level, slot)
	if bad != "": return _err(bad)
	level.Terrain.SetTexture(tex, slot)
	level.Terrain.UpdateSplat()
	return _ok({ "slot": slot, "terrain_slots": 8 if level.Terrain.ExpandedSlots else 4 })


# Slots 0-3 are weighted in the first splat image; 4-7 in a second one that
# only exists once the level's "Expand Slots" option is on. Without it
# SetTexture ignores them, Fill paints the wrong slot and CloneSplatImage2
# throws inside DD, so refuse those slots here. Returns "" when `slot` is usable.
func _terrain_slot_error(level, slot : int) -> String:
	if slot < 0 or slot > 7:
		return "terrain slot must be 0-7, got " + str(slot)
	if slot >= 4 and not level.Terrain.ExpandedSlots:
		return "terrain slot %d needs expanded slots: call set_terrain_slot with 'expand': true (or turn on Expand Slots in the Terrain Brush)" % slot
	return ""


# Turn on the level's 8-slot terrain mode. While the Terrain Brush panel is
# built, go through the brush so its slot list follows (it also gives the new
# slots DD's default textures); otherwise the panel syncs when next opened.
func _expand_terrain_slots(level) -> void:
	if level.Terrain.ExpandedSlots:
		return
	if Global.Editor.Tools.has("TerrainBrush"):
		var brush = Global.Editor.Tools["TerrainBrush"]
		if brush.get("terrainList") != null:
			brush.ExpandSlots(true)
			return
	level.Terrain.ExpandSlots(true)


# Shared front half of the paint/fill commands: check `slot`, then assign the
# optional `asset` to it. Returns "" or the error.
func _prepare_terrain_slot(level, req : Dictionary, slot : int) -> String:
	var bad = _terrain_slot_error(level, slot)
	if bad != "":
		return bad
	if req.has("asset"):
		var tex = _asset_tex("Terrain", req["asset"])
		if tex == null:
			return "could not load terrain asset: " + str(req["asset"])
		level.Terrain.SetTexture(tex, slot)
	elif slot >= 4 and level.Terrain.GetTexture(slot) == null:
		return "terrain slot %d has no texture yet: pass 'asset' or call set_terrain_slot first" % slot
	return ""


func _fill_terrain(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var slot = int(req.get("slot", 0))
	var bad = _prepare_terrain_slot(level, req, slot)
	if bad != "": return _err(bad)
	level.Terrain.Fill(slot)
	level.Terrain.UpdateSplat()
	return _ok({ "filled_slot": slot })


# Paint a soft circular brush of a terrain slot at a woxel position. Like
# fill_region, this edits the splat weight image directly (Terrain.Paint is a
# no-op from a mod context). The brush has a smooth radial falloff so strokes
# blend; `rate` scales the peak strength at the center.
func _paint_terrain(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var slot = int(req.get("slot", 0))
	var radius = float(req.get("radius", 64.0))
	var rate = clamp(float(req.get("rate", 1.0)), 0.0, 1.0)
	var bad = _prepare_terrain_slot(level, req, slot)
	if bad != "": return _err(bad)
	var world = _xy(req, Global.World.WoxelDimensions * 0.5)
	# Convert the brush center and radius into texture space (radius scales by
	# the woxel->texture ratio along x).
	var center = level.Terrain.WorldToTexture(world)
	var tscale = float(level.Terrain.width) / max(Global.World.WoxelDimensions.x, 1.0)
	var trad = max(radius * tscale, 0.5)

	var sp = _open_splat(level, slot)
	if sp == null: return _err("could not read splat image for slot " + str(slot))
	var iw = sp.img.get_width()
	var ih = sp.img.get_height()
	var x0 = int(floor(center.x - trad))
	var y0 = int(floor(center.y - trad))
	var x1 = int(ceil(center.x + trad))
	var y1 = int(ceil(center.y + trad))
	var painted := 0
	for iy in range(max(y0, 0), min(y1 + 1, ih)):
		for ix in range(max(x0, 0), min(x1 + 1, iw)):
			var d = Vector2(ix + 0.5, iy + 0.5).distance_to(center)
			if d > trad:
				continue
			# Smooth falloff: full strength in the inner half, easing to 0 at the rim.
			var falloff = clamp(1.0 - (d / trad), 0.0, 1.0)
			falloff = falloff * falloff * (3.0 - 2.0 * falloff)  # smoothstep
			var w = rate * falloff
			if w <= 0.0:
				continue
			_splat_paint_pixel(sp, ix, iy, w)
			painted += 1
	_close_splat(level, sp)
	return _ok({ "painted_slot": slot, "pixels": painted })


# Paint a continuous terrain stroke along a polyline (a road/trail) in one call.
# Unlike stamping many `paint_terrain` dabs (whose overlaps double-paint and whose
# spacing the caller has to eyeball), this rasterizes a uniform ribbon: for each
# pixel near the line it measures the distance to the NEAREST segment and applies
# the soft falloff once, so the whole route has constant width and clean edges.
# `points` is a woxel polyline (>= 2 [x,y] pairs); `radius` is the half-width.
func _paint_path(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var slot = int(req.get("slot", 0))
	var radius = float(req.get("radius", 96.0))
	var rate = clamp(float(req.get("rate", 1.0)), 0.0, 1.0)
	if not req.has("points"):
		return _err("provide 'points':[[x,y]...] (>= 2 points)")
	var pts = req["points"]
	if typeof(pts) != TYPE_ARRAY or pts.size() < 2:
		return _err("'points' needs >= 2 [x,y] pairs for a path")
	var bad = _prepare_terrain_slot(level, req, slot)
	if bad != "": return _err(bad)

	# Map the polyline into texture space; radius scales by the woxel->texture
	# ratio (same conversion paint_terrain uses for a single dab).
	var tscale = float(level.Terrain.width) / max(Global.World.WoxelDimensions.x, 1.0)
	var trad = max(radius * tscale, 0.5)
	var tpts := []
	var mn = Vector2(INF, INF)
	var mx = Vector2(-INF, -INF)
	for p in pts:
		var tp = level.Terrain.WorldToTexture(Vector2(float(p[0]), float(p[1])))
		tpts.append(tp)
		mn.x = min(mn.x, tp.x); mn.y = min(mn.y, tp.y)
		mx.x = max(mx.x, tp.x); mx.y = max(mx.y, tp.y)

	var sp = _open_splat(level, slot)
	if sp == null: return _err("could not read splat image for slot " + str(slot))
	var iw = sp.img.get_width()
	var ih = sp.img.get_height()
	# Bounding box of the whole stroke, padded by the brush radius.
	var x0 = max(int(floor(mn.x - trad)), 0)
	var y0 = max(int(floor(mn.y - trad)), 0)
	var x1 = min(int(ceil(mx.x + trad)), iw - 1)
	var y1 = min(int(ceil(mx.y + trad)), ih - 1)
	var painted := 0
	for iy in range(y0, y1 + 1):
		for ix in range(x0, x1 + 1):
			var pix = Vector2(ix + 0.5, iy + 0.5)
			# Distance to the closest segment of the polyline.
			var d = INF
			for i in range(tpts.size() - 1):
				var sd = _dist_point_segment(pix, tpts[i], tpts[i + 1])
				if sd < d:
					d = sd
				if d <= 0.0:
					break
			if d > trad:
				continue
			# Same smoothstep falloff as paint_terrain, applied once per pixel.
			var falloff = clamp(1.0 - (d / trad), 0.0, 1.0)
			falloff = falloff * falloff * (3.0 - 2.0 * falloff)
			var w = rate * falloff
			if w <= 0.0:
				continue
			_splat_paint_pixel(sp, ix, iy, w)
			painted += 1
	_close_splat(level, sp)
	return _ok({ "painted_slot": slot, "segments": tpts.size() - 1, "pixels": painted })


# Shortest distance from point p to segment a-b (all in texture space).
func _dist_point_segment(p : Vector2, a : Vector2, b : Vector2) -> float:
	var ab = b - a
	var len2 = ab.x * ab.x + ab.y * ab.y
	if len2 <= 0.0000001:
		return p.distance_to(a)
	var t = clamp((p - a).dot(ab) / len2, 0.0, 1.0)
	return p.distance_to(a + ab * t)


# Fill a region (rectangle or polygon) with a terrain slot, in woxel coords.
# Unlike fill_terrain (whole level), this paints only inside the shape, so you
# can floor a single room. Pass `rect:[x,y,w,h]` OR `points:[[x,y]...]` (a
# polygon, >= 3 points). The shape is mapped to texture space and rasterized
# directly into the splat weight image (see below), not via Terrain.Paint.
func _fill_region(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var slot = int(req.get("slot", 0))
	var rate = float(req.get("rate", 1.0))
	var bad = _prepare_terrain_slot(level, req, slot)
	if bad != "": return _err(bad)

	# Gather the shape's world-space polygon (rect -> 4 corners).
	var poly := []
	if req.has("rect"):
		var r = req["rect"]
		if typeof(r) != TYPE_ARRAY or r.size() < 4:
			return _err("'rect' must be [x, y, w, h]")
		var x = float(r[0]); var y = float(r[1]); var w = float(r[2]); var h = float(r[3])
		poly = [Vector2(x, y), Vector2(x + w, y), Vector2(x + w, y + h), Vector2(x, y + h)]
	elif req.has("points"):
		for p in req["points"]:
			poly.append(Vector2(float(p[0]), float(p[1])))
		if poly.size() < 3:
			return _err("'points' needs >= 3 [x,y] pairs for a polygon")
	else:
		return _err("provide 'rect':[x,y,w,h] or 'points':[[x,y]...]")

	# Map the polygon into texture space and find its pixel bounding box.
	var tpoly := []
	var mn = Vector2(INF, INF)
	var mx = Vector2(-INF, -INF)
	for wp in poly:
		var tp = level.Terrain.WorldToTexture(wp)
		tpoly.append(tp)
		mn.x = min(mn.x, tp.x); mn.y = min(mn.y, tp.y)
		mx.x = max(mx.x, tp.x); mx.y = max(mx.y, tp.y)
	var origin = Vector2(floor(mn.x), floor(mn.y))
	var bw = int(ceil(mx.x - origin.x))
	var bh = int(ceil(mx.y - origin.y))
	if bw < 1 or bh < 1:
		return _err("region is too small in texture space")

	# Edit the splat weight image directly (Terrain.Paint is a no-op from a mod
	# context). For each pixel inside the polygon, drive the target slot's
	# channel toward 1 by `rate`; _open/_close_splat handle clone + restore.
	var local := []
	for tp in tpoly:
		local.append(tp - origin)
	rate = clamp(rate, 0.0, 1.0)
	var sp = _open_splat(level, slot)
	if sp == null: return _err("could not read splat image for slot " + str(slot))
	var iw = sp.img.get_width()
	var ih = sp.img.get_height()
	var painted := 0
	for py in range(bh):
		var iy = int(origin.y) + py
		if iy < 0 or iy >= ih:
			continue
		for px in range(bw):
			var ix = int(origin.x) + px
			if ix < 0 or ix >= iw:
				continue
			if not _point_in_poly(Vector2(px + 0.5, py + 0.5), local):
				continue
			_splat_paint_pixel(sp, ix, iy, rate)
			painted += 1
	_close_splat(level, sp)

	return _ok({
		"filled_slot": slot, "shape": ("rect" if req.has("rect") else "polygon"),
		"texture_bbox": [_vec(origin), [origin.x + bw, origin.y + bh]],
		"pixels": painted,
	})


# Clone the splat weight images for direct editing of `slot` (callers validate
# it with _terrain_slot_error first). Returns { img, other, ch, which }, locked:
# img holds the slot in RGBA channel ch (0..3) and is splat image `which` (0 =
# slots 0-3, 1 = slots 4-7); other is the second image when the level has
# expanded slots (its weights must shrink as the slot's grow), else null.
# Returns null if an image isn't available. Pair with _close_splat.
func _open_splat(level, slot : int):
	var which = 0 if slot < 4 else 1
	var first = level.Terrain.CloneSplatImage()
	var second = level.Terrain.CloneSplatImage2() if level.Terrain.ExpandedSlots else null
	var img = first if which == 0 else second
	var other = second if which == 0 else first
	if img == null:
		return null
	img.lock()
	if other != null:
		other.lock()
	return { "img": img, "other": other, "ch": slot % 4, "which": which }


# Push an _open_splat edit back into the terrain. RestoreSplat2 takes BOTH
# images (first, second), as DD's PaintTerrain2 record passes them.
func _close_splat(level, sp : Dictionary) -> void:
	sp.img.unlock()
	if sp.other == null:
		level.Terrain.RestoreSplat(sp.img)
	else:
		sp.other.unlock()
		if sp.which == 0:
			level.Terrain.RestoreSplat2(sp.img, sp.other)
		else:
			level.Terrain.RestoreSplat2(sp.other, sp.img)
	level.Terrain.UpdateSplat()


# Push the open slot's weight at one pixel toward 1 by `rate`, scaling the
# remaining slots (all 8 with expanded slots) down so the weights still sum to ~1.
func _splat_paint_pixel(sp : Dictionary, x : int, y : int, rate : float) -> void:
	var c = sp.img.get_pixel(x, y)
	var w = [c.r, c.g, c.b, c.a]
	if sp.other != null:
		var o = sp.other.get_pixel(x, y)
		w += [o.r, o.g, o.b, o.a]
	var ch = sp.ch
	var target = w[ch] + (1.0 - w[ch]) * rate
	var rest = 1.0 - target
	var others = -w[ch]
	for v in w:
		others += v
	for i in range(w.size()):
		if i == ch:
			w[i] = target
		elif others > 0.0001:
			w[i] = w[i] / others * rest
		else:
			w[i] = 0.0
	sp.img.set_pixel(x, y, Color(w[0], w[1], w[2], w[3]))
	if sp.other != null:
		sp.other.set_pixel(x, y, Color(w[4], w[5], w[6], w[7]))


# Even-odd point-in-polygon test (ray cast). `poly` is an array of Vector2.
func _point_in_poly(pt : Vector2, poly : Array) -> bool:
	var inside = false
	var n = poly.size()
	var j = n - 1
	for i in range(n):
		var a = poly[i]
		var b = poly[j]
		if ((a.y > pt.y) != (b.y > pt.y)) and \
				(pt.x < (b.x - a.x) * (pt.y - a.y) / (b.y - a.y) + a.x):
			inside = not inside
		j = i
	return inside


# _point_in_poly, but a point on the outline counts as inside (the ray cast
# alone is a coin toss there, and grid-aligned shapes put many points on it).
func _point_in_or_on_poly(pt : Vector2, poly : Array) -> bool:
	if _point_in_poly(pt, poly):
		return true
	for i in range(poly.size()):
		if _dist_point_segment(pt, poly[i], poly[(i + 1) % poly.size()]) <= 0.5:
			return true
	return false


# ---------------------------------------------------------------------------
# Modify / delete
# ---------------------------------------------------------------------------

func _move_element(req : Dictionary) -> Dictionary:
	var node = _resolve(req)
	if node == null: return _err("no element with id " + str(req.get("id")))
	if _is_text(node):
		node.rect_position = _xy(req, node.rect_position)
		return _ok({ "id": req.get("id"), "position": _vec(node.rect_position) })
	if not (node is Node2D): return _err("element is not movable")
	node.position = _xy(req, node.position)
	return _ok({ "id": req.get("id"), "position": _vec(node.position) })


func _modify_object(req : Dictionary) -> Dictionary:
	var node = _resolve(req)
	if node == null: return _err("no element with id " + str(req.get("id")))
	if req.has("scale"):
		# A mirrored prop is one with a negative x scale (Prop.Mirror): keep the sign.
		var s = abs(float(req["scale"]))
		node.scale = Vector2(-s if node.scale.x < 0 else s, s)
	if req.has("rotation"):
		node.rotation = deg2rad(float(req["rotation"]))
	if req.has("mirror") and node is Node2D:
		node.scale = Vector2(abs(node.scale.x) * (-1.0 if bool(req["mirror"]) else 1.0), node.scale.y)
	if req.has("block_light") and node.has_method("SetBlockLight"):
		node.SetBlockLight(bool(req["block_light"]))
	var warning = ""
	if req.has("color") and str(req.get("color", "")) != "":
		if _is_colorable(node):
			_apply_custom_color(node, _color(req["color"], DEFAULT_OBJECT_TINT))
		else:
			warning = "element is not a Colorable object; color ignored"
	if req.has("shadow"):
		node.set("HasShadow", bool(req["shadow"]))
	var d = _describe(node)
	if warning != "":
		d["warning"] = warning
	return _ok(d)


# What modify_light changes on a light, and its undo puts back. `scale` is the
# Light2D's texture_scale; callers speak in Dungeondraft's Range (_light_range).
func _light_state(light) -> Dictionary:
	return {
		"color": light.color, "energy": light.energy, "scale": light.texture_scale,
		"shadows": light.shadow_enabled, "rotation": light.rotation, "texture": light.texture,
	}


func _apply_light_state(light, s : Dictionary) -> void:
	light.color = s["color"]
	light.energy = s["energy"]
	light.shadow_enabled = s["shadows"]
	light.rotation = s["rotation"]
	if s["texture"] != null:
		light.texture = s["texture"]
	light.texture_scale = s["scale"]


func _modify_light(req : Dictionary) -> Dictionary:
	var light = _resolve(req)
	if light == null or not (light is Light2D): return _err("no light with id " + str(req.get("id")))
	var old = _light_state(light)
	var want = old.duplicate()
	if req.has("color") and str(req.get("color", "")) != "":
		want["color"] = _color(req["color"], old["color"])
	if req.has("energy"):
		want["energy"] = float(req["energy"])
		if want["energy"] < 0.0: return _err("'energy' must be >= 0")
	if req.has("shadows"): want["shadows"] = bool(req["shadows"])
	if req.has("rotation"): want["rotation"] = deg2rad(float(req["rotation"]))
	if req.has("asset") and str(req.get("asset", "")) != "":
		var tex = _asset_tex("Lights", req["asset"])
		if tex == null: return _err("could not load light asset: " + str(req["asset"]))
		want["texture"] = tex
	# Range is converted against the texture the light will have, and a
	# texture change alone keeps the Range the light showed before.
	var width = float(want["texture"].get_width()) if want["texture"] != null else 1024.0
	if req.has("range"):
		var range_value = float(req["range"])
		if range_value <= 0.0: return _err("'range' must be > 0")
		want["scale"] = range_value * 512.0 / width
	elif want["texture"] != old["texture"]:
		want["scale"] = _light_range(light) * 512.0 / width
	_apply_light_state(light, want)
	return _ok_op(_describe(light), { "kind": "light", "id": _id(light), "old": old, "new": want })


func _text_state(text) -> Dictionary:
	return { "text": text.text, "font": str(text.fontName), "size": int(text.fontSize), "color": text.fontColor }


# Text.SetFont builds the font for a name + size and refits the label to its
# text, so it is called for any change; the color is separate.
func _apply_text_state(text, s : Dictionary) -> void:
	text.text = s["text"]
	text.SetFont(s["font"], s["size"])
	text.SetFontColor(s["color"])


func _modify_text(req : Dictionary) -> Dictionary:
	var text = _resolve(req)
	if text == null or not _is_text(text): return _err("no text with id " + str(req.get("id")))
	var old = _text_state(text)
	var want = old.duplicate()
	if req.has("text"): want["text"] = str(req["text"])
	if req.has("font") and str(req.get("font", "")) != "": want["font"] = str(req["font"])
	if req.has("size"):
		want["size"] = int(req["size"])
		if want["size"] <= 0: return _err("'size' must be > 0")
	if req.has("color") and str(req.get("color", "")) != "":
		want["color"] = _color(req["color"], old["color"])
	_apply_text_state(text, want)
	return _ok_op(_describe(text), { "kind": "text", "id": _id(text), "old": old, "new": want })


# Edit an existing wall by id: texture (`asset`), tint (`color`), `shadow`
# and/or geometry (`points`, `loop`, `type`, `joint`). Anything omitted is kept.
func _modify_wall(req : Dictionary) -> Dictionary:
	var wall = _resolve(req)
	if wall == null or not _is_wall(wall): return _err("no wall with id " + str(req.get("id")))
	var old = _wall_state(wall)
	var want = old.duplicate()
	if req.has("points"): want["points"] = _points(req["points"])
	if req.has("loop"): want["loop"] = bool(req["loop"])
	if req.has("type"): want["type"] = int(req["type"])
	if req.has("joint"): want["joint"] = int(req["joint"])
	if req.has("shadow"): want["shadow"] = bool(req["shadow"])
	if req.has("asset"):
		var tex = _asset_tex("Walls", req["asset"])
		if tex == null: return _err("could not load wall asset: " + str(req["asset"]))
		want["texture"] = tex
		# A new texture brings its own default tint, as in draw_wall.
		want["color"] = _wall_color(req, tex)
	elif req.has("color") and str(req.get("color", "")) != "":
		want["color"] = _color(req["color"], old["color"])
	var bad = _apply_wall_state(wall, want)
	if bad != "": return _err(bad)
	return _ok_op(_describe_wall(wall), { "kind": "wall", "id": _id(wall), "old": old, "new": want })


# A wall's editable state: what modify_wall changes and its undo puts back.
func _wall_state(wall) -> Dictionary:
	return {
		"points": wall.Points, "loop": wall.Loop,
		"type": int(wall.Type), "joint": int(wall.Joint),
		"texture": wall.Texture, "color": wall.Color, "shadow": wall.HasShadow,
	}


# Apply a _wall_state to a wall. Returns "" on success, or why it was refused
# (nothing has changed then).
#
# Wall.Set, the only call that replaces a wall's shape wholesale, frees every
# mounted portal (its Clear() does), and ModifyPoint deletes the ones next to
# the moved vertex. So a wall with doors/windows is reshaped in place instead:
# same point count / loop / type / joint, each portal carried along at the same
# fraction of its segment and turned with it, then re-keyed with MovePortal
# (what SelectTool does when a door is dragged along its wall).
func _apply_wall_state(wall, s : Dictionary) -> String:
	var pts : PoolVector2Array = s["points"]
	var n = pts.size()
	if n < 2:
		return "'points' needs >= 2 [x,y] pairs"
	if not (int(s["type"]) in [0, 1, 2]) or not (int(s["joint"]) in [0, 1, 2]):
		return "'type' and 'joint' must each be 0, 1 or 2"
	var cur : PoolVector2Array = wall.Points
	var same_shape = n == cur.size() and s["loop"] == wall.Loop \
			and int(s["type"]) == int(wall.Type) and int(s["joint"]) == int(wall.Joint)
	var moved = not same_shape
	if same_shape:
		for i in range(n):
			if pts[i] != cur[i]:
				moved = true
				break

	var anchors := []
	if moved and wall.Portals.size() > 0:
		if not same_shape:
			return "wall has mounted doors/windows, so only its existing points can be moved (same point count, loop, type and joint); delete the portals first to reshape it"
		for portal in wall.Portals:
			var i = int(portal.WallPointIndex)
			var j = (i + 1) % n
			if i < 0 or i >= n or (j == 0 and not wall.Loop):
				return "portal %d has an invalid wall anchor" % _id(portal)
			var old_edge = cur[j] - cur[i]
			var new_edge = pts[j] - pts[i]
			if old_edge.length() < 0.001 or new_edge.length() < 0.001:
				return "a wall segment holding a door/window can't have zero length"
			var t = clamp((portal.position - cur[i]).dot(old_edge) / old_edge.length_squared(), 0.0, 1.0)
			# Room on the shorter side of the portal's centre, before and after:
			# it must still fit (or at least fit no worse than it already did).
			var room_was = min(t, 1.0 - t) * old_edge.length()
			var room_now = min(t, 1.0 - t) * new_edge.length()
			if room_now + 0.5 < min(float(portal.Radius), room_was):
				return "segment %d would be too short for its door/window (portal %d)" % [i, _id(portal)]
			anchors.append({
				"portal": portal, "index": i, "position": pts[i] + new_edge * t,
				"turn": new_edge.angle() - old_edge.angle(),
			})

	if moved and anchors.empty():
		wall.Set(pts, s["texture"], s["color"], s["loop"], s["shadow"],
			int(s["type"]), int(s["joint"]), wall.NormalizeUV)
	else:
		if moved:
			wall.Points = pts
			if wall.Loop:
				# Set() keeps a closed copy of a loop for hit-testing; refresh it too.
				var closed = pts
				closed.append(pts[0])
				wall.set("pointsClosed", closed)
			for a in anchors:
				var portal = a["portal"]
				portal.position = a["position"]
				portal.rotation += a["turn"]
				portal.Direction = portal.Direction.rotated(a["turn"])
				wall.MovePortal(portal, a["index"])
		if s["texture"] != wall.Texture:
			wall.UpdateTexture(s["texture"])
		wall.SetColor(s["color"])
		wall.HasShadow = s["shadow"]
	# Set() leaves the previous shadow lines behind, and the setters above only
	# store values: RemakeLines rebuilds lines, shadows and occluders from them.
	wall.RemakeLines()
	return ""


func _duplicate_object(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var src = _resolve(req)
	if src == null: return _err("no element with id " + str(req.get("id")))
	if src.get("Texture") == null: return _err("element has no Texture to duplicate")
	var prop = level.Objects.CreateObject(0)
	prop.z_index = src.z_index
	prop.SetTexture(src.Texture)
	prop.position = src.position + Vector2(float(req.get("dx", 64.0)), float(req.get("dy", 0.0)))
	prop.scale = src.scale
	prop.rotation = src.rotation
	if src.has_method("HasCustomColor") and src.HasCustomColor():
		_apply_custom_color(prop, src.GetCustomColor())
	if bool(src.get("BlockLight")):
		prop.SetBlockLight(true)
	_register_prop(level, prop, 0)
	return _ok({ "id": _id(prop), "position": _vec(prop.position) })


# Delete, keeping the element for undo: a prop is serialised and freed (as
# Dungeondraft's own ObjectRecord does, see _build_op), anything else is
# detached from the tree and held by the op.
func _delete_element(req : Dictionary) -> Dictionary:
	var ident = req.get("id")
	if ident == null: return _err("missing 'id'")
	var id = int(ident)
	var node = Global.World.GetNodeByID(id)
	if node == null:
		return _err("no element with id %d" % id)
	var op = _delete_op(node, id)
	return _ok_op({ "deleted": true, "id": id, "undoable": true }, op)


func _delete_op(node, id : int) -> Dictionary:
	var parent = node.get_parent()
	var saved = _saved_kind(node)
	if saved != null:
		var op := { "kind": "delete_saved", "parent": parent, "id": id,
			"loader": saved["loader"], "save_args": saved["save_args"] }
		_delete_saved(op)
		return op
	_detach_node(node, id)
	var op := { "kind": "delete", "node": node, "parent": parent, "id": id }
	if _is_wall(parent):
		op["parent_id"] = _id(parent)
	return op


# Delete several elements as ONE undo step. Every id is checked first, so an
# unknown id deletes nothing.
func _delete_elements(req : Dictionary) -> Dictionary:
	var ids = req.get("ids", [])
	if typeof(ids) != TYPE_ARRAY or ids.empty():
		return _err("'ids' must be a non-empty array of element ids")
	if ids.size() > 100:
		return _err("'ids' is capped at 100 per call")
	var nodes := {}
	var unknown := []
	for raw in ids:
		if not (typeof(raw) in [TYPE_INT, TYPE_REAL]):
			return _err("every id must be an integer, got: " + str(raw))
		var id = int(raw)
		if nodes.has(id):
			continue
		var node = Global.World.GetNodeByID(id)
		if node == null:
			unknown.append(id)
		else:
			nodes[id] = node
	if not unknown.empty():
		return _err("no element with id %s; nothing was deleted" % str(unknown))
	var entries := []
	for id in nodes:
		entries.append(_delete_op(nodes[id], int(id)))
	var op := { "kind": "group", "label": "delete_elements", "entries": entries }
	return _ok_op({ "deleted": entries.size(), "ids": nodes.keys(), "undoable": true }, op)


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------

# World.CreateLevel puts the new level FIRST in the list (index 0 is the top
# floor) and leaves the current level current, so every index below shifts by 1.
# Like DD's own New Level (LevelSettings.CreateLevel), the new level's terrain
# is hidden unless asked for: an upper floor has no ground, and an opaque
# terrain would hide the level shown beneath it by compare_levels.
func _add_level(req : Dictionary) -> Dictionary:
	var lv = Global.World.CreateLevel(str(req.get("label", "Level")))
	lv.Terrain.visible = bool(req.get("terrain", false))
	Global.Editor.UpdateLevelOptions()   # the level dropdown
	return _ok({
		"id": lv.ID, "label": lv.Label, "index": 0,
		"current_index": Global.World.CurrentLevelId, "terrain_visible": lv.Terrain.visible,
	})


# World.SetLevel is (int level, bool loading = false), and a C# default is not
# filled in for a call from GDScript: the one-argument call found no method and
# did nothing, with no error. Pass both, then confirm the switch.
func _set_level(req : Dictionary) -> Dictionary:
	var idx = int(req.get("index", 0))
	if idx < 0 or idx >= Global.World.levels.size():
		return _err("level index out of range: " + str(idx))
	Global.World.SetLevel(idx, false)
	if Global.World.CurrentLevelId != idx:
		return _err("Dungeondraft did not switch to level " + str(idx))
	return _ok({ "current_index": idx, "current_id": Global.World.GetCurrentLevel().ID })


func _rename_level(req : Dictionary) -> Dictionary:
	var level = Global.World.TryGetLevel(int(req.get("index", -1)))
	if level == null: return _err("level index out of range: " + str(req.get("index")))
	var label = str(req.get("label", "")).strip_edges()
	if label == "": return _err("missing 'label'")
	level.Label = label
	Global.Editor.UpdateLevelOptions()
	return _list_levels()


# Copy a whole level (World.CloneLevel, as the New Level window's "clone" option
# does). Like add_level, the copy lands at index 0 and is not switched to; its
# elements get fresh ids, and it fills in over the next frames.
func _clone_level(req : Dictionary) -> Dictionary:
	var src = Global.World.TryGetLevel(int(req.get("index", -1)))
	if src == null: return _err("level index out of range: " + str(req.get("index")))
	var label = str(req.get("label", "")).strip_edges()
	if label == "":
		label = str(src.Label) + " copy"
	var clone = Global.World.CloneLevel(src, label)
	if clone == null: return _err("CloneLevel returned null")
	Global.Editor.UpdateLevelOptions()
	return _ok({ "id": clone.ID, "label": clone.Label, "index": 0, "current_index": Global.World.CurrentLevelId })


# Reorder the levels: `ids` lists every level id once, top floor first.
func _reorder_levels(req : Dictionary) -> Dictionary:
	var ids = req.get("ids")
	var levels = Global.World.levels
	if typeof(ids) != TYPE_ARRAY or ids.size() != levels.size():
		return _err("'ids' must list each of the %d level ids exactly once" % levels.size())
	var by_id := {}
	for lv in levels:
		by_id[int(lv.ID)] = lv
	var order := []
	for id in ids:
		if not by_id.has(int(id)):
			return _err("unknown or repeated level id: " + str(id))
		order.append(by_id[int(id)])
		by_id.erase(int(id))
	Global.World.SetNewLevelOrder(order)
	return _list_levels()


# Show another level faintly beneath the current one, for lining floors up
# (the Compare Levels window). `index` -1 turns it off. The setting belongs to
# the current level and DD re-applies it whenever that level is shown.
func _compare_levels(req : Dictionary) -> Dictionary:
	var idx = int(req.get("index", -1))
	var toggle = Global.Editor.get("CompareToggle")
	if idx == -1:
		Global.World.DisableCompareLevels()
		_set_toggle_quietly(toggle, false)
		return _ok({ "comparing": false })
	var ref = Global.World.TryGetLevel(idx)
	if ref == null: return _err("level index out of range: " + str(idx))
	if ref == Global.World.GetCurrentLevel(): return _err("pick a level other than the current one")
	var ref_opacity = clamp(float(req.get("reference_opacity", 0.35)), 0.0, 1.0)
	var cur_opacity = clamp(float(req.get("current_opacity", 1.0)), 0.0, 1.0)
	Global.World.SetCompareLevels(ref, ref_opacity, cur_opacity, true)
	_set_toggle_quietly(toggle, true)
	return _ok({
		"comparing": true, "reference_index": idx, "reference_id": ref.ID,
		"reference_opacity": ref_opacity, "current_opacity": cur_opacity,
	})


# Delete a level (floor) by index. Not undoable: Dungeondraft frees the level
# and everything on it. Refuses to delete the last level; if the level is the
# current one, the editor is switched to a neighbour first.
func _delete_level(req : Dictionary) -> Dictionary:
	var idx = int(req.get("index", -1))
	var level = Global.World.TryGetLevel(idx)
	if level == null: return _err("level index out of range: " + str(req.get("index")))
	if Global.World.levels.size() <= 1:
		return _err("refusing to delete the only level: a map must keep at least one")
	var label = str(level.Label)
	var id = level.ID
	if Global.World.CurrentLevelId == idx:
		Global.World.SetLevel(idx - 1 if idx > 0 else idx + 1, false)
	Global.World.DeleteLevel(level)
	Global.Editor.UpdateLevelOptions()
	if Global.World.GetCurrentLevel() == null:
		Global.World.SetLevel(0, false)
	var rows = _list_levels()["result"]
	rows["deleted_id"] = id
	rows["deleted_label"] = label
	return _ok(rows)


# Tick a toggle button without firing its handler (which would act a second time).
func _set_toggle_quietly(button, pressed : bool) -> void:
	if button != null and button is BaseButton:
		button.set_pressed_no_signal(pressed)


# Level-wide settings DD keeps on tool panels: the ambient light (Environment),
# lighting on/off (the Lighting checkbox, which is map-wide), the terrain's
# visibility and smooth blending (Terrain Brush) and the water's shoreline
# (Water Brush). Sets what is given, then reports all of them.
func _configure_level(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	if req.has("ambient_light") and str(req.get("ambient_light", "")) != "":
		var c = _color(req["ambient_light"], level.LightPassRender.color)
		level.LightPassRender.color = c
		if Global.Editor.Tools.has("Environment"):
			var env = Global.Editor.Tools["Environment"]
			env.SetAmbientLight(c)
			var picker = env.get("Controls").get("AmbientLight")
			if picker != null and picker is ColorPickerButton:
				picker.color = c
	if req.has("lighting"):
		# Not Editor.ToggleLighting: outside an export it dereferences
		# World.SourceLevel, which is null. This is what its checkbox does.
		for lv in Global.World.levels:
			lv.ToggleLighting(bool(req["lighting"]))
		_set_toggle_quietly(Global.Editor.get("LightingToggle"), bool(req["lighting"]))
	if req.has("terrain_visible"):
		level.Terrain.visible = bool(req["terrain_visible"])
	if req.has("smooth_terrain"):
		level.Terrain.SetSmoothBlending(bool(req["smooth_terrain"]))
	if req.has("water_shoreline"):
		level.WaterMesh.DisableBorder(not bool(req["water_shoreline"]))
	return _ok({
		"level_id": level.ID,
		"ambient_light": _hex(level.LightPassRender.color),
		"lighting": level.Lights.visible,
		"terrain_visible": level.Terrain.visible,
		"smooth_terrain": level.Terrain.SmoothBlending,
		"water_shoreline": not bool(level.WaterMesh.get("disableBorder")),
	})


# Show an image over the map to trace from (World.AddTraceImage, the Trace
# Image tool). It is an editing aid: DD hides it while exporting. An empty
# `path` removes it.
func _set_trace_image(req : Dictionary) -> Dictionary:
	var path = str(req.get("path", ""))
	if path == "":
		Global.World.RemoveTraceImage()
		return _ok({ "trace_image": false })
	if not (path.get_extension().to_lower() in IMAGE_EXTENSIONS):
		return _err("unsupported image type (use png, jpg or webp): " + path)
	var img = Image.new()
	if not File.new().file_exists(path) or img.load(path) != OK:
		return _err("could not read image: " + path)
	var scale = float(req.get("scale", 1.0))
	if scale <= 0.0: return _err("'scale' must be > 0")
	var opacity = clamp(float(req.get("opacity", 0.5)), 0.0, 1.0)
	Global.World.TraceImageVisible = true
	Global.World.AddTraceImage(path, scale, opacity)
	var sprite = Global.World.TraceImage
	if sprite == null: return _err("AddTraceImage created no image")
	sprite.position = _xy(req, Global.World.WoxelDimensions * 0.5)
	return _ok({
		"trace_image": true, "path": path, "position": _vec(sprite.position),
		"scale": scale, "opacity": opacity, "image_size": [img.get_width(), img.get_height()],
	})


# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------

# DD's built-in layers (Level.CreateDefaultLockedLayers). They are what the
# level's own nodes draw on; the editor never lets an element be put on one.
const LOCKED_LAYERS := {
	-500: "Terrain", -300: "Caves", -200: "Floor", 0: "Water",
	500: "Portals", 600: "Walls", 800: "Roofs",
}
const LAYER_MIN := -4096   # CanvasItem z-index range
const LAYER_MAX := 4096


# The level's drawing layers { z: label } without the locked ones. Level.Layers
# itself (a SortedDictionary) can't cross into GDScript; SaveLayers() can.
func _user_layers(level) -> Dictionary:
	return level.SaveLayers()


# Layers an element may be put on: the level's drawing layers, as in DD's own
# layer menus (the locked ones are greyed out there). Returns "" or the error.
func _layer_error(level, layer : int) -> String:
	var layers = _user_layers(level)
	if layers.has(layer):
		return ""
	var keys = layers.keys()
	keys.sort()
	return "no layer %d on this level (layers: %s)" % [layer, str(keys)]


func _list_layers() -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var labels = LOCKED_LAYERS.duplicate()
	var user = _user_layers(level)
	for z in user:
		labels[int(z)] = str(user[z])
	var keys = labels.keys()
	keys.sort()
	var out := []
	for z in keys:
		out.append({ "layer": z, "label": labels[z], "locked": not user.has(z) })
	return _ok({ "level_id": level.ID, "layers": out })


# Add a drawing layer to the current level. DD keeps layers as a { z: label }
# table per level (saved with the map; the layer menus list it) but ships no
# way to add to it, so this goes through the loader the map file uses.
func _add_layer(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	if not req.has("layer"): return _err("missing 'layer'")
	var layer = int(req["layer"])
	if layer < LAYER_MIN or layer > LAYER_MAX:
		return _err("'layer' must be %d..%d" % [LAYER_MIN, LAYER_MAX])
	if LOCKED_LAYERS.has(layer) or _user_layers(level).has(layer):
		return _err("layer %d already exists" % layer)
	var label = str(req.get("label", "")).strip_edges()
	if label == "":
		label = "Layer %d" % layer
	level.LoadLayers({ layer: label })
	return _list_layers()


# The layer an element is on, or null for kinds that have none (walls, portals,
# roofs, lights and texts draw on fixed layers).
func _element_layer(node):
	if node.has_method("GetLayer") and node.has_method("SetLayer"):
		return int(node.GetLayer())                    # pattern floor
	if node is Node2D and (node.has_method("SetCustomColor") or node.has_method("SetEditPoints")):
		return int(node.z_index)                       # object / path
	return null


# Move an element to a layer the way SelectTool's layer menu does: a pattern
# floor is re-parented (SetLayer), an object or path takes the layer as its
# z_index, and an object is re-filed in the selection index, keyed by z_index.
func _apply_element_layer(node, layer : int) -> void:
	if node.has_method("SetLayer"):
		node.SetLayer(layer)
		return
	var objects = node.get_parent()
	var indexed = objects != null and objects.has_method("RemoveFromSearchTable") and node.has_method("SetCustomColor")
	if indexed:
		objects.RemoveFromSearchTable(node)
	node.z_index = layer
	if indexed:
		objects.AddToSearchTable(node, false)


func _set_element_layer(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var node = _resolve(req)
	if node == null: return _err("no element with id " + str(req.get("id")))
	if not req.has("layer"): return _err("missing 'layer'")
	var old = _element_layer(node)
	if old == null: return _err("element has no layer: only objects, paths and pattern floors do")
	var layer = int(req["layer"])
	var bad = _layer_error(level, layer)
	if bad != "": return _err(bad)
	_apply_element_layer(node, layer)
	return _ok_op(
		{ "id": _id(node), "layer": _element_layer(node), "previous": old },
		{ "kind": "layer", "id": _id(node), "old": old, "new": layer })


# ---------------------------------------------------------------------------
# Map lifecycle
# ---------------------------------------------------------------------------
#
# Global.Editor.CurrentMapFile is the open map's full path ("" if never saved).
# Master.IsSaving (Master = the Editor's parent node, /root/Master) is true
# while a save runs. DD exposes no dirty/unsaved flag, so `dirty` is null.

func _current_map_file() -> String:
	var f = Global.Editor.get("CurrentMapFile")
	if typeof(f) != TYPE_STRING:
		return ""
	return f


func _is_saving() -> bool:
	var master = Global.Editor.get_parent()
	if master == null:
		return false
	var v = master.get("IsSaving")
	return typeof(v) == TYPE_BOOL and v


func _get_current_map() -> Dictionary:
	return _ok({
		"map_open": Global.World.GetCurrentLevel() != null,
		"path": _current_map_file(),
		"dirty": null,
		"saving": _is_saving(),
	})


# Saves through the Save button's own handler, exactly as a user's click does.
# (Editor.SaveMap is SaveMap(bool saveAs, action): a one-argument
# SaveMap(false) wrote nothing in testing.) The guards above mean the
# handler always has a file to save to, so it never opens a Save As dialog.
func _save_map() -> Dictionary:
	if Global.World.GetCurrentLevel() == null:
		return _err("no map open")
	var path = _current_map_file()
	if path == "":
		return _err("map has never been saved")
	if _is_saving():
		return _err("save already in progress")
	if not Global.Editor.has_method("_on_SaveButton_pressed"):
		return _err("save button handler not available")
	Global.Editor._on_SaveButton_pressed()
	# `saved` only means the save was started: it may still be writing (see
	# PROTOCOL.md). `saving` is read right after the handler call.
	return _ok({ "saved": true, "path": path, "saving": _is_saving() })


# Replies first, switches after: ForceOpenMap tears down this map (and DD
# starts a fresh bridge instance), so the reply must be on the wire before it.
# Refuses while a save is running: switching would cut the save off and lose
# the user's work.
func _open_map(req : Dictionary) -> Dictionary:
	var path = req.get("path")
	if typeof(path) != TYPE_STRING or path == "":
		return _err("missing 'path'")
	var f = File.new()
	if not f.file_exists(path):
		return _err("file not found: " + path)
	if _is_saving():
		return _err("save in progress")
	call_deferred("_do_open_map", path)
	return _ok({ "opening": path })


const OPEN_SAVE_WAIT_MS := 30000

# This script has no `extends` (no get_tree of its own), so yield on World's
# tree. A save can start between the reply and this frame, so keep waiting
# while one runs; if it never finishes, stay on the current map rather than
# discard it (the caller's get_current_map poll then times out visibly).
func _do_open_map(path : String):
	var tree = Global.World.get_tree()
	yield(tree, "idle_frame")
	var deadline = OS.get_ticks_msec() + OPEN_SAVE_WAIT_MS
	while _is_saving():
		if OS.get_ticks_msec() > deadline:
			print("[mcp-bridge] open_map: a save was still running after %d ms; not opening %s" % [OPEN_SAVE_WAIT_MS, path])
			return
		yield(tree, "idle_frame")
	Global.Editor.ForceOpenMap(path)


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

# Grab the current window (what's on screen) to a PNG. Synchronous: the texture
# holds the last drawn frame, so no yield is needed inside update().
func _screenshot(req : Dictionary) -> Dictionary:
	var path = str(req.get("path", ""))
	if path == "": return _err("missing 'path'")
	var img = Global.World.get_viewport().get_texture().get_data()
	if img == null: return _err("viewport capture returned null")
	img.flip_y()  # viewport textures come back vertically flipped
	var err = img.save_png(path)
	if err != OK: return _err("save_png failed (err %d): %s" % [err, path])
	return _ok({ "path": path, "width": img.get_width(), "height": img.get_height() })


# Exporter.Start(mode, ppi, path) modes, per the modding API reference:
# 0 = PNG, 1 = JPEG, 2 = WEBP, 3 = Universal VTT, 4 = Mod. Universal VTT is the
# .dd2vtt JSON (base64 image + walls / portals / lights) that Foundry and Roll20
# importers read. Start() uses `path` verbatim, so the caller picks the extension.
const EXPORT_MODES := { "png": 0, "jpg": 1, "jpeg": 1, "webp": 2, "uvtt": 3, "dd2vtt": 3 }

# Render the whole map (no UI) to a file, PNG by default. Asynchronous:
# Exporter.Start runs on a separate thread, so the caller (MCP server) polls
# the path for the file.
func _export_map(req : Dictionary) -> Dictionary:
	var path = str(req.get("path", ""))
	if path == "": return _err("missing 'path'")
	var ppi = int(req.get("ppi", 40))
	var format = str(req.get("format", "png")).to_lower()
	if not EXPORT_MODES.has(format):
		return _err("unknown export format '%s' (png, jpg, webp, uvtt)" % format)
	var toggle = Global.Editor.get("GridToggle")
	if toggle == null: return _err("grid toggle not available")
	var cam = _camera()
	if cam == null: return _err("camera not available")
	if _export != null or _is_exporting():
		return _err("an export is already running")
	if req.has("quality"):
		var q = int(req["quality"])
		if q < 1 or q > 100: return _err("quality must be 1..100")
		Global.Exporter.Quality = q   # JPEG / WEBP only; PNG and UVTT ignore it
	# DD's export has no grid option: it renders whatever the editor shows, and
	# the Export window's Grid checkbox just calls Editor.ToggleGrid. So set it
	# before Start; _finish_export puts it back once the render is done (the
	# render spans later frames). `grid_was` is still reported for callers.
	var grid_was = toggle.pressed
	var grid = bool(req["grid"]) if req.has("grid") else grid_was
	Global.Editor.ToggleGrid(grid)
	# Stage the current level the way ExportWindow does when it opens
	# (SetSourceLevel + no overlay). The Exporter's VTT helpers read
	# World.SourceLevel; with it null the UVTT bake throws inside Run — a C#
	# async Task that Start() doesn't await — so the exception is swallowed, no
	# .dd2vtt is written, "<path>.png" is left behind and Run's own camera /
	# viewport restore never happens. Snapshot the camera so _finish_export can
	# put it back even then.
	_export = { "grid_was": grid_was,
		"cam": { "anchor": cam.anchor_mode, "pos": cam.global_position, "zoom": cam.zoom.x } }
	Global.World.SetSourceLevel(int(Global.World.CurrentLevelId))
	Global.World.SetOverlayLevel(-1, 0.0)
	Global.Exporter.Start(EXPORT_MODES[format], ppi, path)
	return _ok({ "path": path, "ppi": ppi, "format": format, "async": true, "grid": grid, "grid_was": grid_was })


# ExportWindow calls World.RestoreLevelsPostExport() when it closes; we do it
# once Exporter.Run is done. Master.IsExporting (static, but readable through
# the Master node like IsSaving) goes true before Run's first await and false
# on its last line, after the file is written and the camera restored, so
# update() polls it. If Run threw, the flag stays true and nothing was
# restored: the caller's `export_finish` (sent once the file is complete, or on
# timeout) is the fallback that un-stages anyway.
func _finish_export() -> void:
	if _export == null:
		return
	var snap = _export
	_export = null
	if Global.World.has_method("RestoreLevelsPostExport"):
		Global.World.RestoreLevelsPostExport()
	Global.Editor.ToggleGrid(bool(snap["grid_was"]))
	var cam = _camera()
	if cam != null and snap.has("cam"):
		_restore_camera(cam, snap["cam"])


func _restore_camera(cam, c : Dictionary) -> void:
	if cam.has_method("SetAnchorMode"):
		cam.SetAnchorMode(int(c["anchor"]))   # DD's setter also notifies FloorTileCamera
	else:
		cam.anchor_mode = int(c["anchor"])
	cam.global_position = c["pos"]
	_apply_zoom(cam, float(c["zoom"]))


func _is_exporting() -> bool:
	var master = Global.Editor.get_parent()
	if master == null:
		return false
	var v = master.get("IsExporting")
	return typeof(v) == TYPE_BOOL and v


func _export_finish() -> Dictionary:
	var had = _export != null
	_finish_export()
	var stuck = _is_exporting()
	if stuck:
		# Run died before its last line; clear the flag so the next export isn't refused.
		var master = Global.Editor.get_parent()
		if master != null:
			master.set("IsExporting", false)
		stuck = _is_exporting()
	return _ok({ "restored": had, "exporting": stuck })


# Show/hide the editor grid (same as the bottom-bar Grid toggle). It also
# decides whether export_map renders the grid.
func _set_grid(req : Dictionary) -> Dictionary:
	var toggle = Global.Editor.get("GridToggle")
	if toggle == null: return _err("grid toggle not available")
	if req.has("visible"):
		Global.Editor.ToggleGrid(bool(req["visible"]))
	return _ok({ "grid": toggle.pressed })


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------
#
# The editor camera is a Camera2D at Global.Camera. Its world center is the
# inherited `global_position`; `zoom` is a Vector2 where LARGER = zoomed OUT
# (a Camera2D scales the view by `zoom`, so zoom 0.5 shows half as much = 2x
# magnification). We expose zoom as a single float = zoom.x and pan by setting
# global_position directly (unambiguous), then nudge DD's zoom dropdown to
# match via SetZoomOptionByRaw so the bottom bar stays in sync.

func _camera():
	return Global.get("Camera")


func _viewport_size() -> Vector2:
	return Global.World.get_viewport().get_visible_rect().size


func _apply_zoom(cam, z : float) -> void:
	z = max(0.01, z)
	cam.zoom = Vector2(z, z)
	# Keep DD's bottom-bar zoom dropdown in sync with the raw zoom value.
	if Global.Editor.has_method("SetZoomOptionByRaw"):
		Global.Editor.SetZoomOptionByRaw(z)


func _camera_state(cam) -> Dictionary:
	return {
		"position": _vec(cam.global_position),
		"zoom": cam.zoom.x,
		"viewport_size": _vec(_viewport_size()),
	}


func _get_camera() -> Dictionary:
	var cam = _camera()
	if cam == null: return _err("camera not available")
	return _ok(_camera_state(cam))


func _set_camera(req : Dictionary) -> Dictionary:
	var cam = _camera()
	if cam == null: return _err("camera not available")
	if req.has("x") or req.has("y"):
		cam.global_position = _xy(req, cam.global_position)
	if req.has("zoom"):
		_apply_zoom(cam, float(req["zoom"]))
	return _ok(_camera_state(cam))


# Put the view back to a known state: default anchor (drag-center), map center,
# 100% zoom. Rescues a view left zoomed out / off-map by an aborted export.
func _reset_camera() -> Dictionary:
	if Global.World.GetCurrentLevel() == null: return _err("no map open")
	var cam = _camera()
	if cam == null: return _err("camera not available")
	_restore_camera(cam, { "anchor": Camera2D.ANCHOR_MODE_DRAG_CENTER,
		"pos": Global.World.WoxelDimensions * 0.5, "zoom": 1.0 })
	return _ok(_camera_state(cam))


# Center the camera on a single element (any kind, incl. text). Optional zoom.
func _focus_element(req : Dictionary) -> Dictionary:
	var cam = _camera()
	if cam == null: return _err("camera not available")
	var node = _resolve(req)
	if node == null: return _err("no element with id " + str(req.get("id")))
	var pos = _element_position(node)
	if pos == null: return _err("element has no position to focus")
	cam.global_position = pos
	if req.has("zoom"):
		_apply_zoom(cam, float(req["zoom"]))
	return _ok({ "id": req.get("id"), "focused": _vec(pos), "camera": _camera_state(cam) })


# Frame a set of elements: center on the UNION of their world-space bounding
# rects and zoom so it fits the viewport with `pad` (fraction of extra margin,
# default 0.15). Ids that don't resolve or lack bounds are skipped and reported
# in `missing`. Using real rects (not anchor points) keeps wall loops and large
# props correctly centered.
func _fit_elements(req : Dictionary) -> Dictionary:
	var cam = _camera()
	if cam == null: return _err("camera not available")
	var ids = req.get("ids", [])
	var mn = Vector2(INF, INF)
	var mx = Vector2(-INF, -INF)
	var used := 0
	var missing := []
	for ident in ids:
		var node = Global.World.GetNodeByID(int(ident))
		var rect = null
		if node != null:
			rect = _element_rect(node)
		if rect == null:
			missing.append(ident)
			continue
		mn.x = min(mn.x, rect.position.x); mn.y = min(mn.y, rect.position.y)
		mx.x = max(mx.x, rect.end.x); mx.y = max(mx.y, rect.end.y)
		used += 1
	if used == 0:
		return _err("no elements with bounds to fit")
	var center = (mn + mx) * 0.5
	cam.global_position = center
	# Zoom so the box fits: zoom (Camera2D) = world_span / viewport_span.
	var pad = 1.0 + float(req.get("pad", 0.15))
	var raw = mx - mn
	var vp = _viewport_size()
	var z
	if raw.length() < 1.0:
		# Degenerate box (one point, no derivable bounds): use a sane close zoom
		# instead of slamming to the minimum and burying the camera in a pixel.
		z = 1.0
	else:
		var span = raw * pad
		z = max(span.x / max(vp.x, 1.0), span.y / max(vp.y, 1.0))
	_apply_zoom(cam, z)
	return _ok({
		"fit": used, "missing": missing,
		"center": _vec(center), "bounds": [_vec(mn), _vec(mx)],
		"camera": _camera_state(cam),
	})


# A representative world point for any element kind: the center of its bounding
# rect (so walls/large props focus on their middle, not their anchor).
func _element_position(node):
	var rect = _element_rect(node)
	if rect != null:
		return rect.position + rect.size * 0.5
	return null


# A world-space Rect2 enclosing an element's visual extent, or null if none can
# be determined. Prefers the engine's own bounds (GlobalRect on walls/paths,
# get_global_rect on the LineEdit-based Text), then a prop's Rect, then a wall/
# path's Points, finally a zero-size rect at the node position.
func _element_rect(node):
	if _is_text(node):
		return node.get_global_rect()  # Control: world-space rect
	var grect = node.get("GlobalRect")
	if grect != null and grect is Rect2 and grect.size.length() > 0.0:
		return grect
	var prect = node.get("Rect")
	if prect != null and prect is Rect2 and prect.size.length() > 0.0:
		return prect
	# A prop's Rect can be empty until DD computes it; derive bounds from the
	# Sprite's texture size * node scale, centered on the node position.
	if node is Node2D:
		var spr = node.get("Sprite")
		if spr != null and spr.has_method("get_texture") and spr.get_texture() != null:
			var tsize = spr.get_texture().get_size() * node.scale
			if tsize.length() > 0.0:
				return Rect2(node.position - tsize * 0.5, tsize)
	var pts = node.get("Points")
	if pts != null and pts is PoolVector2Array and pts.size() > 0:
		var mn = Vector2(INF, INF)
		var mx = Vector2(-INF, -INF)
		for p in pts:
			mn.x = min(mn.x, p.x); mn.y = min(mn.y, p.y)
			mx.x = max(mx.x, p.x); mx.y = max(mx.y, p.y)
		return Rect2(mn, mx - mn)
	if node is Node2D:
		return Rect2(node.position, Vector2())
	return null


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

func _select_elements(req : Dictionary) -> Dictionary:
	var stool = Global.Editor.Tools["SelectTool"]
	stool.DeselectAll()
	var n := 0
	for ident in req.get("ids", []):
		var node = Global.World.GetNodeByID(int(ident))
		if node != null:
			stool.SelectThing(node, true)
			n += 1
	stool.EnableTransformBox(true)
	return _ok({ "selected": n })


func _clear_selection() -> Dictionary:
	Global.Editor.Tools["SelectTool"].DeselectAll()
	return _ok({ "cleared": true })


# ---------------------------------------------------------------------------
# Native access: Dungeondraft's own API and UI, for what no command covers
# ---------------------------------------------------------------------------
#
# A `target` names a live object: a root (World, Editor, Level = the current
# level, Camera, Exporter, Header), "Tool:<name>", "Window:<name>",
# "Element:<id>", "Level:<id>" or "Object:<instance id>" (how objects are
# handed back by these commands), optionally followed by ".Member" steps
# ("Level.Terrain", "Tool:TerrainBrush.Controls.SmoothBlending").
#
# Only DD's own members are reachable for calls: its C# methods are PascalCase
# and Godot's are snake_case, so the name test in _native_callable keeps
# engine methods (free, call, set_script, ...) out. Nothing here can be undone.
# A DD method that throws is logged by DD as an unhandled exception and returns
# null to the script; there is no way to see the failure from here.

const NATIVE_ROOTS := ["World", "Editor", "Level", "Camera", "Exporter", "Header"]
const NATIVE_MAX_ITEMS := 200    # per array / dictionary / listing in a reply
const NATIVE_MAX_DEPTH := 4

const TYPE_NAMES := {
	TYPE_NIL: "Variant", TYPE_BOOL: "bool", TYPE_INT: "int", TYPE_REAL: "float",
	TYPE_STRING: "String", TYPE_VECTOR2: "Vector2", TYPE_RECT2: "Rect2",
	TYPE_VECTOR3: "Vector3", TYPE_TRANSFORM2D: "Transform2D", TYPE_COLOR: "Color",
	TYPE_NODE_PATH: "NodePath", TYPE_OBJECT: "Object", TYPE_DICTIONARY: "Dictionary",
	TYPE_ARRAY: "Array", TYPE_RAW_ARRAY: "bytes", TYPE_INT_ARRAY: "IntArray",
	TYPE_REAL_ARRAY: "FloatArray", TYPE_STRING_ARRAY: "StringArray",
	TYPE_VECTOR2_ARRAY: "Vector2Array", TYPE_COLOR_ARRAY: "ColorArray",
}


func _type_name(t : int) -> String:
	return TYPE_NAMES.get(t, "type %d" % t)


func _native_root(name : String):
	match name:
		"World": return Global.World
		"Editor": return Global.Editor
		"Level": return Global.World.GetCurrentLevel()
		"Camera": return Global.Camera
		"Exporter": return Global.Exporter
		"Header": return Global.Header
	var colon = name.find(":")
	if colon == -1:
		return null
	var key = name.substr(colon + 1)
	match name.substr(0, colon):
		"Tool":
			return Global.Editor.Tools[key] if Global.Editor.Tools.has(key) else null
		"Window":
			return Global.Editor.Windows[key] if Global.Editor.Windows.has(key) else null
		"Element":
			if key.is_valid_integer() and Global.World.HasNodeID(int(key)):
				return Global.World.GetNodeByID(int(key))
		"Level":
			for lv in Global.World.levels:
				if str(lv.ID) == key:
					return lv
		"Object":
			if key.is_valid_integer():
				return instance_from_id(int(key))
	return null


# One ".Member" step: a dictionary key, an array index, or an object's member
# (a C# property through its get_X accessor, else a field / Godot property).
func _native_member(obj, name : String):
	if name == "" or name.begins_with("_") or name == "script":
		return null
	match typeof(obj):
		TYPE_DICTIONARY:
			if obj.has(name):
				return obj[name]
			return obj.get(int(name)) if name.is_valid_integer() else null
		TYPE_ARRAY:
			if name.is_valid_integer() and int(name) >= 0 and int(name) < obj.size():
				return obj[int(name)]
		TYPE_OBJECT:
			if is_instance_valid(obj):
				return _native_read(obj, name)
	return null


func _native_read(obj, name : String):
	if obj.has_method("get_" + name):
		return obj.call("get_" + name)
	return obj.get(name)


func _native_target(path : String):
	var parts = path.split(".")
	var obj = _native_root(parts[0])
	for i in range(1, parts.size()):
		if obj == null:
			return null
		obj = _native_member(obj, parts[i])
	return obj


# _native_target, but only a live object will do. Returns null otherwise.
func _native_object(path : String):
	if path.begins_with("Tool:mcp_bridge"):
		return null   # the bridge's own panel is the user's, not the assistant's
	var obj = _native_target(path)
	if typeof(obj) == TYPE_OBJECT and is_instance_valid(obj):
		if _inside_panel(obj):
			return null
		return obj
	return null


# DD's own members: PascalCase methods, and the get_X / set_X accessors C#
# gives its properties. Compiler-generated names carry "<" or ".".
func _native_callable(name : String) -> bool:
	if name == "" or name.find("<") != -1 or name.find(".") != -1:
		return false
	var first = name.substr(4, 1) if (name.begins_with("get_") or name.begins_with("set_")) else name.substr(0, 1)
	return first != "" and first != "_" and first == first.to_upper() and first != first.to_lower()


func _native_class(obj) -> String:
	var script = obj.get_script()
	if script != null and str(script.resource_path) != "":
		return str(script.resource_path).get_file().get_basename()
	return obj.get_class()


# A value as JSON. Godot types become { "$type", "value" }, objects a
# { "$target": "Object:<id>" } that can be passed back as a target or argument.
func _native_pack(v, depth : int = 0):
	match typeof(v):
		TYPE_NIL, TYPE_BOOL, TYPE_INT, TYPE_REAL, TYPE_STRING:
			return v
		TYPE_VECTOR2:
			return { "$type": "Vector2", "value": [v.x, v.y] }
		TYPE_VECTOR3:
			return { "$type": "Vector3", "value": [v.x, v.y, v.z] }
		TYPE_RECT2:
			return { "$type": "Rect2", "value": [v.position.x, v.position.y, v.size.x, v.size.y] }
		TYPE_COLOR:
			return { "$type": "Color", "value": _hex(v) }
		TYPE_TRANSFORM2D:
			return { "$type": "Transform2D", "value": [_vec(v.x), _vec(v.y), _vec(v.origin)] }
		TYPE_NODE_PATH:
			return str(v)
		TYPE_RAW_ARRAY:
			return { "$type": "bytes", "size": v.size() }
		TYPE_OBJECT:
			if not is_instance_valid(v):
				return null
			var o := { "$target": "Object:%d" % v.get_instance_id(), "class": _native_class(v) }
			if v is Node:
				o["name"] = str(v.name)
			elif v is Resource and v.resource_path != "":
				o["path"] = v.resource_path
			return o
		TYPE_DICTIONARY:
			if depth >= NATIVE_MAX_DEPTH:
				return { "$type": "Dictionary", "size": v.size() }
			var d := {}
			for key in v:
				if d.size() >= NATIVE_MAX_ITEMS:
					d["$truncated"] = v.size() - NATIVE_MAX_ITEMS
					break
				d[str(key)] = _native_pack(v[key], depth + 1)
			return d
		TYPE_ARRAY, TYPE_INT_ARRAY, TYPE_REAL_ARRAY, TYPE_STRING_ARRAY, TYPE_VECTOR2_ARRAY, TYPE_COLOR_ARRAY:
			if depth >= NATIVE_MAX_DEPTH:
				return { "$type": _type_name(typeof(v)), "size": v.size() }
			var a := []
			for item in v:
				if a.size() >= NATIVE_MAX_ITEMS:
					a.append({ "$truncated": v.size() - NATIVE_MAX_ITEMS })
					break
				a.append(_native_pack(item, depth + 1))
			return a
	return str(v)


# JSON back to a value (the reverse of _native_pack, plus two ways to make a
# resource argument). Returns _ok(value) / _err(why).
func _native_unpack(v) -> Dictionary:
	if typeof(v) == TYPE_ARRAY:
		var a := []
		for item in v:
			var u = _native_unpack(item)
			if not u.ok: return u
			a.append(u.result)
		return _ok(a)
	if typeof(v) != TYPE_DICTIONARY:
		return _ok(v)
	if v.has("$target"):
		var obj = _native_object(str(v["$target"]))
		if obj == null: return _err("no live object at " + str(v["$target"]))
		return _ok(obj)
	if not v.has("$type"):
		var d := {}
		for key in v:
			var u = _native_unpack(v[key])
			if not u.ok: return u
			d[key] = u.result
		return _ok(d)
	var val = v.get("value")
	match str(v["$type"]):
		"Texture":
			var tex = _asset_tex(str(v.get("category", "Objects")), v.get("asset", ""))
			if tex == null: return _err("no such %s asset: %s" % [str(v.get("category", "Objects")), str(v.get("asset"))])
			return _ok(tex)
		"Image":
			var img = Image.new()
			if not File.new().file_exists(str(v.get("path", ""))) or img.load(str(v["path"])) != OK:
				return _err("could not read image: " + str(v.get("path")))
			return _ok(img)
		"Vector2":
			return _native_coerce(val, TYPE_VECTOR2)
		"Vector3":
			if _is_numbers(val, 3): return _ok(Vector3(val[0], val[1], val[2]))
		"Rect2":
			return _native_coerce(val, TYPE_RECT2)
		"Color":
			return _native_coerce(val, TYPE_COLOR)
		"Vector2Array":
			return _native_coerce(val, TYPE_VECTOR2_ARRAY)
		"StringArray":
			return _native_coerce(val, TYPE_STRING_ARRAY)
		"IntArray":
			return _native_coerce(val, TYPE_INT_ARRAY)
		"FloatArray":
			return _native_coerce(val, TYPE_REAL_ARRAY)
	return _err("bad or unsupported tagged value: " + JSON.print(v))


func _is_numbers(v, count : int) -> bool:
	if typeof(v) != TYPE_ARRAY or v.size() != count:
		return false
	for n in v:
		if not (typeof(n) in [TYPE_INT, TYPE_REAL]):
			return false
	return true


# Fit a decoded value to the type DD declares for a parameter or property, so
# plain JSON is enough wherever the type is known: every JSON number arrives as
# a float, [x, y] can be a Vector2, "#rrggbb" a Color, a list of pairs a
# Vector2Array. Returns _ok(value) / _err(why).
func _native_coerce(val, want : int) -> Dictionary:
	var have = typeof(val)
	if want == TYPE_NIL or have == want:
		return _ok(val)
	match want:
		TYPE_INT:
			if have == TYPE_REAL and val == floor(val): return _ok(int(val))
		TYPE_REAL:
			if have == TYPE_INT: return _ok(float(val))
		TYPE_OBJECT:
			if have == TYPE_NIL: return _ok(null)
		TYPE_VECTOR2:
			if _is_numbers(val, 2): return _ok(Vector2(val[0], val[1]))
		TYPE_RECT2:
			if _is_numbers(val, 4): return _ok(Rect2(val[0], val[1], val[2], val[3]))
		TYPE_COLOR:
			if have == TYPE_STRING and val.trim_prefix("#").is_valid_hex_number(false) and val.trim_prefix("#").length() in [6, 8]:
				return _ok(_color(val, Color()))
			if (_is_numbers(val, 3) or _is_numbers(val, 4)): return _ok(_color(val, Color()))
		TYPE_VECTOR2_ARRAY:
			if have == TYPE_ARRAY:
				var pts = _points(val)
				if pts.size() == val.size(): return _ok(pts)
		TYPE_STRING_ARRAY:
			if have == TYPE_ARRAY:
				for item in val:
					if typeof(item) != TYPE_STRING: return _err("expected a list of strings")
				return _ok(PoolStringArray(val))
		TYPE_INT_ARRAY:
			if _is_numbers(val, val.size() if have == TYPE_ARRAY else -1): return _ok(PoolIntArray(val))
		TYPE_REAL_ARRAY:
			if _is_numbers(val, val.size() if have == TYPE_ARRAY else -1): return _ok(PoolRealArray(val))
	return _err("expected %s, got %s" % [_type_name(want), _type_name(have)])


func _native_targets() -> Dictionary:
	var out := []
	for name in NATIVE_ROOTS:
		out.append(name)
	for key in Global.Editor.Tools:
		out.append("Tool:" + str(key))
	for key in Global.Editor.Windows:
		out.append("Window:" + str(key))
	return _ok({ "targets": out })


# List an object's DD methods (name, typed args, return type) and properties.
# C# properties show up through their get_X / set_X accessors; plain C# fields
# (World.levels, Terrain.width) are not listed anywhere but can still be read.
func _native_describe(req : Dictionary) -> Dictionary:
	var target = str(req.get("target", "Level"))
	var obj = _native_object(target)
	if obj == null: return _err("no live object at target: " + target)
	var search = str(req.get("search", "")).to_lower()
	var methods := []
	var props := {}     # name -> { name, type, settable }
	for m in obj.get_method_list():
		var name = str(m.name)
		if not _native_callable(name):
			continue
		if name.begins_with("get_") or name.begins_with("set_"):
			var p = props.get(name.substr(4), { "name": name.substr(4), "type": "Variant", "settable": false })
			if name.begins_with("get_"):
				p["type"] = _type_name(int(m["return"].type))
			else:
				p["settable"] = true
				if p["type"] == "Variant" and m.args.size() == 1:
					p["type"] = _type_name(int(m.args[0].type))
			props[p["name"]] = p
			continue
		if search != "" and name.to_lower().find(search) == -1:
			continue
		var args := []
		for a in m.args:
			args.append({ "name": str(a.name), "type": _type_name(int(a.type)) })
		var entry := { "name": name, "args": args }
		if int(m["return"].type) != TYPE_NIL:
			entry["returns"] = _type_name(int(m["return"].type))
		methods.append(entry)
	for info in obj.get_property_list():
		var name = str(info.name)
		# Skip category / group headers and anything already found as a C# property.
		if name.begins_with("_") or name.find("<") != -1 or name == "script" or props.has(name) \
				or (int(info.usage) & (PROPERTY_USAGE_CATEGORY | PROPERTY_USAGE_GROUP)) != 0:
			continue
		props[name] = { "name": name, "type": _type_name(int(info.type)), "settable": true }
	var properties := []
	for name in props:
		if search == "" or name.to_lower().find(search) != -1:
			properties.append(props[name])
	return _ok({
		"target": target, "class": _native_class(obj),
		"method_count": methods.size(), "methods": methods.slice(0, NATIVE_MAX_ITEMS - 1),
		"property_count": properties.size(), "properties": properties.slice(0, NATIVE_MAX_ITEMS - 1),
	})


func _native_get(req : Dictionary) -> Dictionary:
	var target = str(req.get("target", "Level"))
	var obj = _native_object(target)
	if obj == null: return _err("no live object at target: " + target)
	var names = req.get("properties", [])
	if typeof(names) != TYPE_ARRAY or names.empty(): return _err("'properties' must be a list of names")
	var out := {}
	for name in names:
		if typeof(name) != TYPE_STRING or name == "" or name.begins_with("_") or name == "script":
			return _err("bad property name: " + str(name))
		out[name] = _native_pack(_native_read(obj, name))
	return _ok({ "target": target, "values": out })


# The Variant type a property takes: from its C# setter, else Godot's property
# list, else whatever it currently holds (TYPE_NIL = unknown, pass as given).
func _native_property_type(obj, prop : String) -> int:
	for m in obj.get_method_list():
		if str(m.name) == "set_" + prop and m.args.size() == 1:
			return int(m.args[0].type)
	for info in obj.get_property_list():
		if str(info.name) == prop:
			return int(info.type)
	return typeof(_native_read(obj, prop))


func _native_set(req : Dictionary) -> Dictionary:
	var target = str(req.get("target", "Level"))
	var obj = _native_object(target)
	if obj == null: return _err("no live object at target: " + target)
	var prop = str(req.get("property", ""))
	if prop == "" or prop.begins_with("_") or prop == "script" or prop.find("<") != -1:
		return _err("bad property name: " + prop)
	if obj.has_method("get_" + prop) and not obj.has_method("set_" + prop):
		return _err("property '%s' is read-only (it has no setter)" % prop)
	var u = _native_unpack(req.get("value"))
	if not u.ok: return u
	var c = _native_coerce(u.result, _native_property_type(obj, prop))
	if not c.ok: return _err("'%s': %s" % [prop, c.error])
	if obj.has_method("set_" + prop):
		obj.call("set_" + prop, c.result)
	else:
		obj.set(prop, c.result)
	return _ok({ "target": target, "property": prop, "value": _native_pack(_native_read(obj, prop)) })


func _native_call(req : Dictionary) -> Dictionary:
	var target = str(req.get("target", "Level"))
	var obj = _native_object(target)
	if obj == null: return _err("no live object at target: " + target)
	var method = str(req.get("method", ""))
	if not _native_callable(method):
		return _err("only Dungeondraft's own methods (PascalCase) can be called: " + method)
	var args = req.get("args", [])
	if typeof(args) != TYPE_ARRAY: return _err("'args' must be a list")
	# A C# default argument is NOT filled in for a call from here, and a call
	# with the wrong count silently does nothing, so match the count exactly.
	var info = null
	var counts := []
	for m in obj.get_method_list():
		if str(m.name) == method:
			counts.append(m.args.size())
			if m.args.size() == args.size():
				info = m
	if counts.empty(): return _err("%s has no method %s" % [_native_class(obj), method])
	if info == null:
		return _err("%s takes %s argument(s), got %d (pass every argument, including ones with defaults)" % [method, str(counts), args.size()])
	var values := []
	for i in range(args.size()):
		var u = _native_unpack(args[i])
		if u.ok:
			u = _native_coerce(u.result, int(info.args[i].type))
		if not u.ok:
			return _err("argument %d (%s): %s" % [i, str(info.args[i].name), u.error])
		values.append(u.result)
	return _ok({ "target": target, "method": method, "value": _native_pack(obj.callv(method, values)) })


# The controls under a target, flattened: buttons, toggles, sliders, text
# fields, option lists, color pickers and labels, each with its state and an
# "Object:<id>" target for ui_action. A tool ("Tool:<name>") is not a node; its
# controls are in its side panel, and the ones it keeps by name report it as
# `control`.
func _ui_tree(req : Dictionary) -> Dictionary:
	var target = str(req.get("target", "Editor"))
	var root = _native_object(target)
	if root == null: return _err("no live object at target: " + target)
	var named := {}
	if not (root is Node):
		var controls = root.get("Controls")
		if not target.begins_with("Tool:") or target.find(".") != -1 or typeof(controls) != TYPE_DICTIONARY:
			return _err("target has no UI: use Editor, a Tool:<name>, a Window:<name> or a control")
		for key in controls:
			if typeof(controls[key]) == TYPE_OBJECT and is_instance_valid(controls[key]):
				named[controls[key].get_instance_id()] = str(key)
		root = Global.Editor.Toolset.GetToolPanel(target.substr(5))
		if root == null: return _err("tool has no panel: " + target)
	var search = str(req.get("search", "")).to_lower()
	var limit = int(clamp(int(req.get("limit", NATIVE_MAX_ITEMS)), 1, 1000))
	var out := []
	var matched := 0
	var stack := [root]
	while not stack.empty():
		var node = stack.pop_back()
		var kids = node.get_children()
		for i in range(kids.size() - 1, -1, -1):    # reversed, so they pop in tree order
			stack.append(kids[i])
		if not (node is Control) or not (named.has(node.get_instance_id()) or _ui_is_widget(node)):
			continue
		var entry = _ui_entry(node, named)
		if search != "" and JSON.print(entry).to_lower().find(search) == -1:
			continue
		matched += 1
		if out.size() < limit:
			out.append(entry)
	return _ok({ "target": target, "matched": matched, "returned": out.size(), "controls": out })


func _ui_is_widget(node) -> bool:
	if node is ScrollBar or node.get_parent() is SpinBox:
		return false     # a list's own scrollbar; a spin box's inner text field
	return node is BaseButton or node is Range or node is LineEdit or node is TextEdit \
			or node is ItemList or (node is Label and node.text != "")


func _ui_entry(node, named : Dictionary) -> Dictionary:
	var e := {
		"target": "Object:%d" % node.get_instance_id(), "class": node.get_class(),
		"visible": node.is_visible_in_tree(),
	}
	if named.has(node.get_instance_id()):
		e["control"] = named[node.get_instance_id()]
	if node.hint_tooltip != "":
		e["tooltip"] = node.hint_tooltip
	if node is Label or node is Button:
		e["text"] = node.tr(node.text)    # captions are translation keys until drawn
	elif node is LineEdit or node is TextEdit:
		e["text"] = node.text
	if node is BaseButton:
		e["disabled"] = node.disabled
		if node.toggle_mode:
			e["pressed"] = node.pressed
	if node is OptionButton:
		e["selected"] = node.selected
		var items := []
		for i in range(node.get_item_count()):
			items.append(node.get_item_text(i))
		e["items"] = items
	if node is ColorPickerButton:
		e["color"] = _hex(node.color)
	if node is Range:
		e["value"] = node.value
		e["min"] = node.min_value
		e["max"] = node.max_value
		e["step"] = node.step
	if node is ItemList:
		var items := []
		for i in range(node.get_item_count()):
			items.append(node.get_item_text(i))
		e["items"] = items
		e["selected_items"] = Array(node.get_selected_items())
	return e


# Operate a control found with ui_tree. `press` clicks a button; `set` gives a
# toggle, slider, text field, option list or color picker a value and fires the
# signal DD listens to, so its own handler runs as if the user had done it.
func _ui_action(req : Dictionary) -> Dictionary:
	var target = str(req.get("target", ""))
	var node = _native_object(target)
	if node == null or not (node is Control):
		return _err("target is not a live UI control: use a 'target' from ui_tree")
	match str(req.get("action", "")):
		"press":
			if not (node is BaseButton): return _err("only buttons can be pressed")
			if node.disabled: return _err("button is disabled")
			# Deferred: the handler (which may open a dialog or swap the map)
			# then runs after this reply is out, not inside the mod's update.
			if node.toggle_mode:
				node.call_deferred("set_pressed", not node.pressed)
			node.call_deferred("emit_signal", "pressed")
			return _ok({ "target": target, "pressed": true })
		"set":
			var bad = _ui_set(node, req.get("value"))
			if bad != "": return _err(bad)
			return _ok(_ui_entry(node, {}))
		"show":
			if node is Popup:
				node.call_deferred("popup_centered")
			else:
				node.show()
			return _ok({ "target": target, "shown": true })
		"hide":
			node.hide()
			return _ok({ "target": target, "shown": false })
	return _err("'action' must be press, set, show or hide")


# Returns "" or why the value doesn't fit the control.
func _ui_set(node, value) -> String:
	var is_number = typeof(value) in [TYPE_INT, TYPE_REAL]
	if node is ColorPickerButton:
		var c = _native_coerce(value, TYPE_COLOR)
		if not c.ok: return "a color picker takes '#rrggbb' or [r,g,b]"
		node.color = c.result
		node.emit_signal("color_changed", node.color)
	elif node is OptionButton or node is ItemList:
		var index = int(value) if is_number else -1
		if typeof(value) == TYPE_STRING:
			for i in range(node.get_item_count()):
				if node.get_item_text(i).to_lower() == value.to_lower():
					index = i
		if index < 0 or index >= node.get_item_count() or node.is_item_disabled(index):
			return "not an available item (give its index or exact text)"
		node.select(index)
		node.emit_signal("item_selected", index)
	elif node is BaseButton:
		if not node.toggle_mode or typeof(value) != TYPE_BOOL: return "only a toggle can be set, to true or false; use 'press' for a button"
		if node.disabled: return "control is disabled"
		node.pressed = value
	elif node is Range:
		if not is_number or value < node.min_value or value > node.max_value:
			return "expected a number from %s to %s" % [str(node.min_value), str(node.max_value)]
		node.value = float(value)
	elif node is LineEdit:
		if typeof(value) != TYPE_STRING: return "a text field takes a string"
		node.text = value
		node.emit_signal("text_changed", value)
		node.emit_signal("text_entered", value)
	elif node is TextEdit:
		if typeof(value) != TYPE_STRING: return "a text field takes a string"
		node.text = value
		node.emit_signal("text_changed")
	else:
		return "this kind of control (%s) can't be set" % node.get_class()
	return ""


# ---------------------------------------------------------------------------
# Spatial facts: footprints, walls, doors and regions for the layout checks
# ---------------------------------------------------------------------------
#
# The mod only reports geometry; the checks (does this table cross a wall,
# block a door, overlap the bed?) are done by the server's spatial.py from one
# snapshot. A footprint is the box around a sprite's opaque pixels, carried
# through the object's own rotation and scale: four corners, in woxels.

const META_USED_RECT := "mcp_bridge_used_rect"
const META_REGIONS := "mcp_bridge_regions"
const REGION_KINDS := ["room", "clearance", "protected"]
const DEFAULT_WALL_HALF_WIDTH := 32.0


# The part of a texture that isn't fully transparent, in its own pixels. Read
# back once per texture and kept on the texture itself (DD's asset textures are
# shared, cached instances). The whole image when the pixels can't be read.
func _texture_used_rect(tex) -> Rect2:
	if tex.has_meta(META_USED_RECT):
		return tex.get_meta(META_USED_RECT)
	var rect = Rect2(Vector2(), tex.get_size())
	var img = tex.get_data()
	if img != null and not img.is_empty() and (not img.is_compressed() or img.decompress() == OK):
		var used = img.get_used_rect()
		if used.size.x > 0 and used.size.y > 0:
			rect = used
	tex.set_meta(META_USED_RECT, rect)
	return rect


func _rect_corners(r : Rect2) -> Array:
	return [r.position, Vector2(r.end.x, r.position.y), r.end, Vector2(r.position.x, r.end.y)]


func _vecs(points : Array) -> Array:
	var out := []
	for p in points:
		out.append(_vec(p))
	return out


# An object's footprint, or null when it has no readable sprite. `local` is in
# the object's own space (before its position / rotation / scale), so the
# server can work out where it would land after a move, turn or resize;
# `polygon` is where it is now.
func _object_footprint(prop):
	var sprite = prop.get("Sprite")
	var tex = prop.get("Texture")
	if sprite == null or not (sprite is Sprite) or tex == null or not (tex is Texture):
		return null
	var size = tex.get_size()
	if size.x <= 0 or size.y <= 0:
		return null
	var used = _texture_used_rect(tex)
	if sprite.flip_h: used.position.x = size.x - used.end.x
	if sprite.flip_v: used.position.y = size.y - used.end.y
	var rect = sprite.get_rect()
	var local := []
	var world := []
	for corner in _rect_corners(Rect2(rect.position + used.position * rect.size / size, used.size * rect.size / size)):
		var p = sprite.transform.xform(corner)
		local.append(_vec(p))
		world.append(_vec(prop.global_transform.xform(p)))
	return {
		"id": _id(prop), "asset": tex.resource_path, "layer": prop.z_index,
		"position": _vec(prop.position), "scale": _vec(prop.scale),
		"rotation": rad2deg(prop.rotation), "local": local, "polygon": world,
	}


# The footprint an asset (or an image file about to be imported) would have as
# a new object at scale 1: its size, its opaque box, and that box's corners
# around the sprite's center. Returns _ok({...}) / _err(why).
func _footprint_of(req : Dictionary) -> Dictionary:
	var size : Vector2
	var used : Rect2
	var name = str(req.get("asset", ""))
	if req.has("path"):
		name = str(req["path"])
		var img = Image.new()
		if not File.new().file_exists(name) or img.load(name) != OK:
			return _err("could not read image: " + name)
		size = img.get_size()
		used = img.get_used_rect()
		if used.size.x <= 0 or used.size.y <= 0:
			used = Rect2(Vector2(), size)
	else:
		var category = str(req.get("category", "Objects"))
		var tex = _asset_tex(category, name)
		if tex == null: return _err("could not load %s asset: %s" % [category, name])
		size = tex.get_size()
		used = _texture_used_rect(tex)
	return _ok({
		"asset": name, "size": _vec(size),
		"visible": [used.position.x, used.position.y, used.size.x, used.size.y],
		"local": _vecs(_rect_corners(Rect2(used.position - size * 0.5, used.size))),
	})


func _asset_footprint(req : Dictionary) -> Dictionary:
	return _footprint_of(req)


# Everything the layout checks need about the current level, in one reply.
# With `asset` (+ `category`) or `path`, also the `footprint` of that candidate.
func _spatial_snapshot(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var result := {
		"level_id": level.ID,
		"map_size": _vec(Global.World.WoxelDimensions),
		"regions": _regions(level),
	}
	if req.has("asset") or req.has("path"):
		var f = _footprint_of(req)
		if not f.ok: return f
		result["footprint"] = f.result

	var objects := []
	var unreadable := []
	for prop in level.Objects.get_children():
		if prop.has_meta("preview") and prop.get_meta("preview"):
			continue     # the Object tool's ghost under the cursor
		var f = _object_footprint(prop)
		if f == null:
			unreadable.append(_id(prop))
		else:
			objects.append(f)
	result["objects"] = objects
	result["unreadable"] = unreadable

	var walls := []
	var portals := []
	for wall in level.Walls.get_children():
		var pts = wall.get("Points")
		if pts == null or pts.size() < 2:
			continue
		var route := []
		for p in pts:
			route.append(_vec(wall.global_transform.xform(p)))
		var tex = wall.get("Texture")
		var half = DEFAULT_WALL_HALF_WIDTH
		if tex != null and tex is Texture:
			half = tex.get_height() * 0.5     # a wall is a Line2D as wide as its texture is tall
		walls.append({
			"id": _id(wall), "points": route, "loop": bool(wall.get("Loop")),
			"half_width": half * max(abs(wall.scale.x), abs(wall.scale.y)),
		})
		for portal in wall.Portals:
			var along = portal.Direction
			along = along.normalized() if along.length() > 0.001 else Vector2(1, 0)
			var asset = portal.Texture.resource_path if portal.Texture != null else ""
			portals.append({
				"id": _id(portal), "wall_id": _id(wall), "asset": asset,
				"position": _vec(portal.global_position), "direction": _vec(along),
				"radius": float(portal.Radius),
				# The only thing telling a window from a door is its asset's name.
				"window": asset.get_file().to_lower().find("window") != -1,
			})
	result["walls"] = walls
	result["portals"] = portals
	return _ok(result)


# Regions are named polygons the layout checks treat specially (see the
# server's set_spatial_region). They live on the level node for the session:
# DD's map format has nowhere to keep them, so they are gone after a reload.
func _regions(level) -> Array:
	return level.get_meta(META_REGIONS) if level.has_meta(META_REGIONS) else []


func _list_spatial_regions() -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	return _ok({ "level_id": level.ID, "regions": _regions(level) })


func _set_spatial_region(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var name = str(req.get("name", "")).strip_edges()
	if name == "" or name.length() > 100: return _err("'name' must be 1-100 characters")
	var kind = str(req.get("kind", "clearance"))
	if not (kind in REGION_KINDS): return _err("'kind' must be one of " + str(REGION_KINDS))
	var pts = _shape_points(req)
	if pts.size() < 3: return _err("provide 'rect':[x,y,w,h] or 'points':[[x,y]...] (>= 3 points, with some area)")
	var regions := []
	for r in _regions(level):
		if r["name"] != name:
			regions.append(r)
	regions.append({ "name": name, "kind": kind, "points": _vecs(Array(pts)) })
	level.set_meta(META_REGIONS, regions)
	return _ok({ "level_id": level.ID, "regions": regions })


func _remove_spatial_region(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var name = str(req.get("name", "")).strip_edges()
	var regions := []
	var removed := false
	for r in _regions(level):
		if r["name"] == name:
			removed = true
		else:
			regions.append(r)
	if not removed: return _err("no region named '%s' on this level" % name)
	level.set_meta(META_REGIONS, regions)
	return _ok({ "level_id": level.ID, "regions": regions })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

func _collection(level, kind : String) -> Node:
	return level.get(COLLECTIONS[kind])


# Stable id for a node: reuse its node_id meta, else allocate+register one.
# Returns -1 if the node type can't be registered (e.g. Text on some versions).
func _id(node) -> int:
	if node.has_meta("node_id"):
		return int(node.get_meta("node_id"))
	var nid = Global.World.AssignNodeID(node)
	if nid != null:
		return int(nid)
	if node.has_meta("node_id"):
		return int(node.get_meta("node_id"))
	return -1


func _resolve(req : Dictionary):
	if not req.has("id"):
		return null
	return Global.World.GetNodeByID(int(req["id"]))


# A DD Text node extends LineEdit (a Control), so GetSelectableType doesn't
# classify it and it has no Node2D `.position` — special-case it up front.
func _is_text(node) -> bool:
	return (node is Control) and node.has_method("SetFontSize")


func _describe(node) -> Dictionary:
	# A wall-mounted portal carries a WallID script var and lives under its wall;
	# describe it richly (position + outward normal) like list_elements does.
	if node.get("WallID") != null and node.get("Direction") != null:
		var parent = node.get_parent()
		if parent != null:
			return _describe_wall_portal(node, parent)
	if _is_text(node):
		# DD Text extends LineEdit (Control): position is rect_position, the
		# string is the inherited `.text`, and size/color are the `fontSize` /
		# `fontColor` members. LineEdit has no rotation, so none is reported.
		var td := {
			"id": _id(node), "kind": "text",
			"position": _vec(node.rect_position),
			"text": node.text,
		}
		var fsize = node.get("fontSize")
		if fsize != null:
			td["size"] = int(fsize)
		var fcolor = node.get("fontColor")
		if fcolor != null and fcolor is Color:
			td["color"] = "#" + fcolor.to_html(false)
		var fname = node.get("fontName")
		if fname != null and str(fname) != "":
			td["font"] = str(fname)
		return td
	var stool = Global.Editor.Tools["SelectTool"]
	var t = stool.GetSelectableType(node)
	var d := { "id": _id(node), "kind": KIND_NAMES.get(t, "unknown") }
	if node is Node2D:
		d["position"] = _vec(node.position)
		d["rotation"] = rad2deg(node.rotation)
		d["scale"] = abs(node.scale.x)
	var tex_path = _texture_path(node, t)
	if tex_path != "":
		d["asset"] = tex_path
	var layer = _element_layer(node)
	if layer != null:
		d["layer"] = layer
	if node is Light2D:
		d["color"] = _hex(node.color)
		d["energy"] = node.energy
		d["range"] = _light_range(node)
		d["shadows"] = node.shadow_enabled
	if node.has_method("SetBlockLight"):       # objects and paths
		d["block_light"] = bool(node.get("BlockLight"))
	if node.has_method("SetCustomColor"):      # objects only
		d["mirror"] = node.scale.x < 0
	if t == 4 and node.has_method("HasCustomColor"):
		d["colorable"] = _is_colorable(node)
		if node.HasCustomColor():
			d["color"] = "#" + node.GetCustomColor().to_html(false)
	return d


# Wall-only API as the test: SelectTool.GetSelectableType can't be asked about
# every node (see _is_text).
func _is_wall(node) -> bool:
	return (node is Node2D) and node.has_method("AddPortal") and node.has_method("RemakeLines")


# _describe plus a wall's geometry, for get_element and modify_wall: `points`
# is what modify_wall's `points` replaces (list_elements stays compact).
func _describe_wall(wall) -> Dictionary:
	var d = _describe(wall)
	var pts := []
	for p in wall.Points:
		pts.append(_vec(p))
	d["points"] = pts
	d["loop"] = wall.Loop
	d["type"] = int(wall.Type)
	d["joint"] = int(wall.Joint)
	d["portal_count"] = wall.Portals.size()
	return d


func _texture_path(node, kind : int) -> String:
	var tex = null
	if kind in [1, 2, 3, 4]:
		tex = node.get("Texture")
	elif kind in [5, 6]:
		if node.has_method("get_texture"):
			tex = node.get_texture()
	elif kind == 8:
		tex = node.get("TilesTexture")
	if tex != null and tex is Texture:
		return tex.resource_path
	return ""


func _asset_tex(category : String, asset):
	if typeof(asset) != TYPE_STRING or asset == "":
		return null
	return Script.GetAssetTexture(category, asset)


func _xy(req : Dictionary, fallback : Vector2) -> Vector2:
	if req.has("x") and req.has("y"):
		return Vector2(float(req["x"]), float(req["y"]))
	return fallback


func _vec(v : Vector2) -> Array:
	return [v.x, v.y]


func _points(raw) -> PoolVector2Array:
	var pts := PoolVector2Array()
	if typeof(raw) != TYPE_ARRAY:
		return pts
	for p in raw:
		if typeof(p) == TYPE_ARRAY and p.size() >= 2:
			pts.append(Vector2(float(p[0]), float(p[1])))
	return pts


# Shape params shared by draw_water / draw_material: `rect:[x,y,w,h]` or
# `points:[[x,y]...]` -> the polygon's corners in woxels. Empty when neither
# gives a usable shape (fewer than 3 points, or no area to fill).
func _shape_points(req : Dictionary) -> PoolVector2Array:
	var pts := PoolVector2Array()
	if req.has("rect"):
		var r = req["rect"]
		if typeof(r) != TYPE_ARRAY or r.size() < 4:
			return pts
		var x = float(r[0]); var y = float(r[1]); var w = float(r[2]); var h = float(r[3])
		pts = PoolVector2Array([Vector2(x, y), Vector2(x + w, y), Vector2(x + w, y + h), Vector2(x, y + h)])
	else:
		pts = _points(req.get("points", []))
	if pts.size() < 3:
		return PoolVector2Array()
	var area := 0.0   # shoelace, doubled
	for i in range(pts.size()):
		var a = pts[i]
		var b = pts[(i + 1) % pts.size()]
		area += a.x * b.y - b.x * a.y
	if abs(area) < 2.0:
		return PoolVector2Array()
	return pts


# Accepts "#rrggbb" / "#rrggbbaa" (the "#" is optional), or [r,g,b] / [r,g,b,a]
# floats 0..1. Godot 3's Color(String) reads eight digits as AARRGGBB, so the
# alpha pair is moved to the front first: "ff000080" is half-transparent red
# here, not opaque blue.
func _color(v, fallback : Color) -> Color:
	if typeof(v) == TYPE_STRING and v != "":
		var hex = v.trim_prefix("#")
		if hex.length() == 8 and hex.is_valid_hex_number(false):
			return Color(hex.substr(6, 2) + hex.substr(0, 6))
		return Color(v)
	if typeof(v) == TYPE_ARRAY and v.size() >= 3:
		var a = 1.0
		if v.size() >= 4:
			a = float(v[3])
		return Color(float(v[0]), float(v[1]), float(v[2]), a)
	return fallback


# A color as the bridge reports it: "#rrggbb", or "#rrggbbaa" when it is not
# opaque (Color.to_html(true) would put the alpha first; see _color).
func _hex(c : Color) -> String:
	var hex = "#" + c.to_html(false)
	if c.a < 1.0:
		hex += "%02x" % int(round(c.a * 255.0))
	return hex


# ---------------------------------------------------------------------------
# Terrain read-back
# ---------------------------------------------------------------------------

const SAMPLES_MAX := 64

# Sample the painted terrain on a regular grid: each point's splat weight per
# slot, and whether a pattern floor or dug cave covers it (the Floor tool's
# shapes are Clipper paths with no safe accessor, so they do not count). The
# server's get_terrain and validate_scene read this.
func _terrain_samples(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var img = level.Terrain.CloneSplatImage()
	if img == null: return _err("terrain splat unavailable")
	var img2 = level.Terrain.CloneSplatImage2() if level.Terrain.ExpandedSlots else null
	var dims = Global.World.WoxelDimensions
	var x0 := 0.0
	var y0 := 0.0
	var w := float(dims.x)
	var h := float(dims.y)
	if req.has("rect"):
		var r = req["rect"]
		if typeof(r) != TYPE_ARRAY or r.size() < 4: return _err("'rect' must be [x, y, w, h] in woxels")
		x0 = float(r[0]); y0 = float(r[1]); w = float(r[2]); h = float(r[3])
		if w <= 0.0 or h <= 0.0: return _err("rect width and height must be positive")
	var n = int(clamp(int(req.get("samples", 32)), 2, SAMPLES_MAX))
	var slots := []
	for i in range(8 if level.Terrain.ExpandedSlots else 4):
		var tex = level.Terrain.GetTexture(i)
		slots.append(tex.resource_path if tex != null else null)
	var covers = _cover_polygons(level)
	# Read-only: the level's own CaveMesh (null when nothing was ever dug),
	# not _cave_mesh(), which enables the Cave Brush to create one.
	var cave = level.get("CaveMesh")
	var bm = null
	if cave != null and is_instance_valid(cave) and cave.has_method("get_Bitmap"):
		bm = cave.call("get_Bitmap")
	var bm_size = bm.get_size() if bm != null else Vector2.ZERO
	var iw = img.get_width()
	var ih = img.get_height()
	img.lock()
	if img2 != null: img2.lock()
	var samples := []
	for j in range(n):
		for i in range(n):
			var wx = x0 + w * (float(i) / float(n - 1))
			var wy = y0 + h * (float(j) / float(n - 1))
			var px = int(clamp(wx / float(dims.x) * float(iw), 0, iw - 1))
			var py = int(clamp(wy / float(dims.y) * float(ih), 0, ih - 1))
			var c = img.get_pixel(px, py)
			var weights := [stepify(c.r, 0.01), stepify(c.g, 0.01), stepify(c.b, 0.01), stepify(c.a, 0.01)]
			if img2 != null:
				var c2 = img2.get_pixel(px, py)
				weights += [stepify(c2.r, 0.01), stepify(c2.g, 0.01), stepify(c2.b, 0.01), stepify(c2.a, 0.01)]
			var point = Vector2(wx, wy)
			var covered := false
			for poly in covers:
				if Geometry.is_point_in_polygon(point, poly):
					covered = true
					break
			if not covered and bm != null:
				# cell = floor(woxel / 64) + 1 (the mesh's edge buffer); a set bit is open cave floor
				var cell = Vector2(floor(wx / 64.0) + 1, floor(wy / 64.0) + 1)
				if cell.x >= 0 and cell.y >= 0 and cell.x < bm_size.x and cell.y < bm_size.y and bm.get_bit(cell):
					covered = true
			samples.append({ "x": wx, "y": wy, "weights": weights, "covered": covered })
	img.unlock()
	if img2 != null: img2.unlock()
	return _ok({ "level_id": level.ID, "map_size": [dims.x, dims.y], "rect": [x0, y0, w, h],
		"samples_per_side": n, "slots": slots, "smooth_blending": bool(level.Terrain.SmoothBlending),
		"samples": samples })


# World-space polygons of the level's pattern floors.
func _cover_polygons(level) -> Array:
	var out := []
	var shapes = level.PatternShapes
	if shapes == null or not shapes.has_method("GetShapes"):
		return out
	for shape in shapes.GetShapes():
		if not is_instance_valid(shape):
			continue
		var poly = shape.get("GlobalPolygon")
		if poly == null and shape is Polygon2D:
			poly = shape.get_global_transform().xform(shape.polygon)
		if poly != null and poly.size() >= 3:
			out.append(poly)
	return out


# ---------------------------------------------------------------------------
# Map style: building wear and grid line style (the Map Settings tool)
# ---------------------------------------------------------------------------

# MapSettings.GRID_TEXTURES, in order.
const GRID_STYLE_NAMES := ["dashes", "dotted", "narrow_line", "thick_line"]
const GRID_STYLE_FILES := ["dashes.png", "dotted_line.png", "narrow_line.png", "thick_line.png"]

func _map_settings_tool():
	if not Global.Editor.Tools.has("MapSettings"):
		return null
	return Global.Editor.Tools["MapSettings"]


func _wear_name(tex) -> String:
	if tex == null:
		return "none"
	return str(tex.resource_path).get_file().get_basename()


func _map_style_snapshot() -> Dictionary:
	var grid = Global.World.get("GridMesh")
	return { "wear": Global.World.get("BuildingWear"), "grid": grid.texture if grid != null else null }


func _get_map_style() -> Dictionary:
	var settings = _map_settings_tool()
	if settings == null: return _err("this build has no Map Settings tool")
	var snap = _map_style_snapshot()
	var options := []
	var controls = settings.get("Controls")
	var wear_list = controls.get("BuildingWear") if typeof(controls) == TYPE_DICTIONARY else null
	if wear_list != null and wear_list.has_method("get_item_count"):
		for i in range(wear_list.get_item_count()):
			var icon = wear_list.get_item_icon(i)
			options.append({ "index": i, "name": _wear_name(icon) if i > 0 else "none",
				"texture": str(icon.resource_path) if icon != null else "" })
	var grid_path = str(snap["grid"].resource_path) if snap["grid"] != null else ""
	var grid_style = null
	for i in range(GRID_STYLE_FILES.size()):
		if grid_path.ends_with("/" + GRID_STYLE_FILES[i]):
			grid_style = GRID_STYLE_NAMES[i]
	return _ok({
		"building_wear": _wear_name(snap["wear"]),
		"building_wear_texture": str(snap["wear"].resource_path) if snap["wear"] != null else "",
		"building_wear_options": options,
		"grid_style": grid_style, "grid_texture": grid_path, "grid_style_options": GRID_STYLE_NAMES,
	})


# Apply a building wear (by option name or index) and/or a grid line style
# through the Map Settings tool's own setters, as its panel would; the panel's
# dropdown is re-read from the world afterwards. One undo step.
func _set_map_style(req : Dictionary) -> Dictionary:
	var settings = _map_settings_tool()
	if settings == null: return _err("this build has no Map Settings tool")
	if not settings.has_method("SetBuildingWear") or not settings.has_method("SetGridStyle"):
		return _err("this build's Map Settings tool cannot set the style")
	if not req.has("building_wear") and not req.has("grid_style"):
		return _err("give building_wear and/or grid_style")
	var current = _get_map_style()
	if not current.get("ok", false): return current
	var wear_index := -1
	if req.has("building_wear"):
		var want = str(req["building_wear"]).to_lower()
		for opt in current["result"]["building_wear_options"]:
			if str(opt["name"]).to_lower() == want or str(opt["index"]) == want:
				wear_index = int(opt["index"])
		if wear_index < 0:
			var names := []
			for opt in current["result"]["building_wear_options"]: names.append(opt["name"])
			return _err("building_wear must be one of: " + str(names))
	var grid_index := -1
	if req.has("grid_style"):
		grid_index = GRID_STYLE_NAMES.find(str(req["grid_style"]))
		if grid_index < 0: return _err("grid_style must be one of: " + str(GRID_STYLE_NAMES))
	var before = _map_style_snapshot()
	if wear_index >= 0: settings.SetBuildingWear(wear_index)
	if grid_index >= 0: settings.SetGridStyle(grid_index)
	if settings.has_method("UpdateStyleFromWorld"): settings.UpdateStyleFromWorld()
	var after = _map_style_snapshot()
	var out = _get_map_style()
	if not out.get("ok", false): return out
	return _ok_op(out["result"], { "kind": "map_style", "before": before, "after": after })


func _restore_map_style(snap : Dictionary) -> void:
	Global.World.SetBuildingWear(snap["wear"])
	var grid = Global.World.get("GridMesh")
	if grid != null:
		grid.texture = snap["grid"]
	var settings = _map_settings_tool()
	if settings != null and settings.has_method("UpdateStyleFromWorld"):
		settings.UpdateStyleFromWorld()


# ---------------------------------------------------------------------------
# Prefabs (groups saved with the Prefab settings) and the Map Wizard generator
# ---------------------------------------------------------------------------

const WIZARD_DESIGNS := ["Dungeon", "Cave"]

func _prefab_tool():
	if not Global.Editor.Tools.has("PrefabTool"):
		return null
	var editor_tool = Global.Editor.Tools["PrefabTool"]
	var controls = editor_tool.get("Controls")
	if typeof(controls) != TYPE_DICTIONARY or not controls.has("Set") or not controls.has("List"):
		return null
	return { "tool": editor_tool, "set": controls["Set"], "list": controls["List"] }


# Enable a tool so its panel and state are live, remembering whether it was
# the active one; _tool_done puts it back. The tool the UI points at is left
# enabled: disabling that one is what crashes the editor.
func _tool_take(tool_name : String, editor_tool) -> bool:
	var was_active = str(Global.Editor.ActiveToolName) == tool_name
	if not was_active and editor_tool.has_method("Enable"):
		editor_tool.Enable()
	return was_active


func _tool_done(editor_tool, was_active : bool) -> void:
	if not was_active and editor_tool.has_method("Disable"):
		editor_tool.Disable()


func _prefab_set_names(setbtn) -> Array:
	var names := []
	for i in range(setbtn.get_item_count()):
		names.append(str(setbtn.get_item_text(i)))
	return names


func _select_prefab_set(c : Dictionary, want : String) -> String:
	var setbtn = c["set"]
	for i in range(setbtn.get_item_count()):
		if str(setbtn.get_item_text(i)) == want:
			setbtn.select(i)
			setbtn.emit_signal("item_selected", i)   # the tool's SetSet refills the list
			return ""
	return "no prefab set named '%s'; available: %s" % [want, str(_prefab_set_names(setbtn))]


func _list_prefabs(req : Dictionary) -> Dictionary:
	var c = _prefab_tool()
	if c == null: return _err("the Prefab tool is not available")
	var was_active = _tool_take("PrefabTool", c["tool"])
	if req.has("set") and str(req["set"]) != "":
		var bad = _select_prefab_set(c, str(req["set"]))
		if bad != "":
			_tool_done(c["tool"], was_active)
			return _err(bad)
	var setbtn = c["set"]
	var sel = setbtn.get_selected()
	var prefabs := []
	var lst = c["list"]
	for i in range(lst.get_item_count()):
		prefabs.append(str(lst.get_item_text(i)))
	_tool_done(c["tool"], was_active)
	return _ok({ "sets": _prefab_set_names(setbtn),
		"set": str(setbtn.get_item_text(sel)) if sel >= 0 else "", "prefabs": prefabs })


# Every node of the level's collections (and pattern floors) by instance id;
# what a prefab adds is whatever appears.
func _level_children(level) -> Dictionary:
	var seen := {}
	for kind in COLLECTIONS:
		var coll = _collection(level, kind)
		if coll == null: continue
		for child in coll.get_children():
			seen[child.get_instance_id()] = child
	var shapes = level.PatternShapes
	if shapes != null and shapes.has_method("GetShapes"):
		for shape in shapes.GetShapes():
			seen[shape.get_instance_id()] = shape
	return seen


# A node the prefab placed that can be shifted by changing its position:
# props, lights, free portals and texts. Walls, paths, roofs and pattern
# floors carry absolute points and stay where the prefab was saved.
func _prefab_movable(node) -> bool:
	if _is_text(node):
		return true
	return node is Node2D and not (node is Polygon2D) and node.get("Points") == null


func _place_prefab(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var c = _prefab_tool()
	if c == null: return _err("the Prefab tool is not available")
	var name = str(req.get("name", ""))
	if name == "": return _err("'name' is required; list_prefabs shows what there is")
	var editor_tool = c["tool"]
	var was_active = _tool_take("PrefabTool", editor_tool)
	if req.has("set") and str(req["set"]) != "":
		var bad = _select_prefab_set(c, str(req["set"]))
		if bad != "":
			_tool_done(editor_tool, was_active)
			return _err(bad)
	var lst = c["list"]
	var idx := -1
	for i in range(lst.get_item_count()):
		if str(lst.get_item_text(i)) == name:
			idx = i
			break
	if idx < 0:
		_tool_done(editor_tool, was_active)
		return _err("no prefab named '%s' in this set; list_prefabs shows what there is" % name)
	var before = _level_children(level)
	lst.select(idx)
	editor_tool.OnSelectPrefab(idx)   # PrefabTool.Instance(): deserialises the prefab into the level
	var created := []
	var now = _level_children(level)
	for iid in now:
		if not before.has(iid):
			created.append(now[iid])
	# Shift the movable pieces so their centre lands on (x, y).
	var centre := Vector2.ZERO
	var movable := []
	var fixed := []
	for node in created:
		if _prefab_movable(node):
			centre += node.rect_position if _is_text(node) else node.position
			movable.append(node)
		else:
			fixed.append(node)
	if not movable.empty():
		centre /= movable.size()
	if not movable.empty() and (req.has("x") or req.has("y")):
		var delta = Vector2(float(req.get("x", centre.x)), float(req.get("y", centre.y))) - centre
		for node in movable:
			if _is_text(node):
				node.rect_position += delta
			else:
				node.position += delta
		centre += delta
	editor_tool.Confirm()   # hands the group to Dungeondraft's own history and forgets the preview
	_tool_done(editor_tool, was_active)
	var ids := []
	var entries := []
	for node in created:
		var id = _id(node)
		ids.append(id)
		entries.append(_create_op(node, id))
	var result := { "prefab": name, "ids": ids, "count": created.size(), "centre": _vec(centre) }
	if not fixed.empty():
		var unmoved := []
		for node in fixed: unmoved.append(_id(node))
		result["unmoved"] = unmoved
		result["note"] = ("walls, paths, roofs and pattern floors in a prefab keep their saved " +
			"position; move them with modify_wall or move_element")
	if entries.empty():
		return _ok(result)
	return _ok_op(result, { "kind": "group", "label": "place_prefab", "entries": entries })


func _generator_tool():
	if not Global.Editor.Tools.has("Generator"):
		return null
	return Global.Editor.Tools["Generator"]


func _generator_dials(gen) -> Dictionary:
	var dials := {}
	for name in ["Boundary", "Complexity", "Density"]:
		var ctl = gen.get(name)
		if ctl != null and ctl.get("value") != null:
			dials[name.to_lower()] = { "value": ctl.value, "min": ctl.min_value, "max": ctl.max_value,
				"step": ctl.step }
	return dials


func _generator_options() -> Dictionary:
	var gen = _generator_tool()
	if gen == null: return _err("this build has no Generator tool")
	return _ok({ "design": str(gen.get("Design")), "designs": WIZARD_DESIGNS, "dials": _generator_dials(gen) })


# Run Dungeondraft's own Map Wizard generator on the current level. Not
# undoable through the bridge: a layout is thousands of nodes.
func _generate_dungeon(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var gen = _generator_tool()
	if gen == null: return _err("this build has no Generator tool")
	if not gen.has_method("Generate"): return _err("the Generator tool exposes no Generate")
	var design = str(req.get("design", ""))
	if design != "" and not (design in WIZARD_DESIGNS):
		return _err("'design' must be one of " + str(WIZARD_DESIGNS))
	var dials = _generator_dials(gen)
	for name in ["boundary", "complexity", "density"]:
		if req.has(name):
			if not dials.has(name): return _err("no '%s' dial on this build" % name)
			var v = float(req[name])
			if v < float(dials[name]["min"]) or v > float(dials[name]["max"]):
				return _err("'%s' must be between %s and %s" % [name, str(dials[name]["min"]), str(dials[name]["max"])])
	var before := {}
	for kind in COLLECTIONS:
		before[kind] = _collection(level, kind).get_child_count()
	var was_active = _tool_take("Generator", gen)
	if design != "":
		gen.Design = design
	for name in ["boundary", "complexity", "density"]:
		if req.has(name):
			gen.get(name.capitalize()).value = float(req[name])
	gen.Generate()
	_tool_done(gen, was_active)
	var after := {}
	for kind in COLLECTIONS:
		after[kind] = _collection(level, kind).get_child_count()
	return _ok({ "design": str(gen.get("Design")), "dials": _generator_dials(gen),
		"counts_before": before, "counts_after": after, "level_id": level.ID,
		"note": "not undoable through the bridge; the layout was added to the current level" })


# ---------------------------------------------------------------------------
# Asset packs
# ---------------------------------------------------------------------------

const PACKS_DIR := "res://packs"

# Which asset packs are mounted, and which the open map includes. A map sees
# only the packs in its header's manifest (chosen when it was made); the rest
# are mounted but invisible to list_assets, and an asset placed from one would
# not survive a reload.
func _list_asset_packs() -> Dictionary:
	var installed := []
	var dir = Directory.new()
	if dir.open(PACKS_DIR) == OK and dir.list_dir_begin(true, true) == OK:
		var entry = dir.get_next()
		while entry != "":
			if dir.current_is_dir():
				installed.append(_read_pack_json(entry))
			entry = dir.get_next()
		dir.list_dir_end()
	var included := []
	var header = Global.Header
	if header != null and header.get("AssetManifest") != null:
		for pack in header.AssetManifest:
			if pack == null:
				continue
			included.append({ "id": str(pack.get("ID")), "name": str(pack.get("Name")),
				"version": str(pack.get("Version")), "author": str(pack.get("Author")) })
	var included_ids := {}
	for pack in included:
		included_ids[pack["id"]] = true
	var unused := []
	for pack in installed:
		if not included_ids.has(pack["id"]):
			unused.append(pack)
	return _ok({
		"installed": installed, "included_in_this_map": included,
		"installed_but_not_in_this_map": unused,
		"uses_default_assets": bool(header.get("UsesDefaultAssets")) if header != null else null,
	})


func _read_pack_json(pack_id : String) -> Dictionary:
	var out := { "id": pack_id, "name": "", "version": "", "author": "" }
	var f = File.new()
	if f.open("%s/%s/pack.json" % [PACKS_DIR, pack_id], File.READ) != OK:
		return out
	var parsed = JSON.parse(f.get_as_text())
	f.close()
	if parsed.error != OK or typeof(parsed.result) != TYPE_DICTIONARY:
		return out
	for key in ["name", "version", "author", "keywords", "allow_3rd_party_mapping_software_to_read"]:
		if parsed.result.has(key):
			out[key] = parsed.result[key]
	return out


func _ok(result) -> Dictionary:
	return { "ok": true, "result": result }


# _ok for a handler that builds its own undo op: _record_and_dispatch takes
# "op" off the response and pushes it onto the undo stack.
func _ok_op(result, op : Dictionary) -> Dictionary:
	return { "ok": true, "result": result, "op": op }


func _err(msg) -> Dictionary:
	return { "ok": false, "error": msg }


# The MCP Bridge panel under Settings: what the bridge is doing, and the
# controls that belong to the user alone (the assistant cannot reach them: see
# _native_object). Labels are created with a marker text and found again, since
# CreateLabel returns nothing.
func _register_tool():
	var icon = _ensure_icon()
	var panel = Global.Editor.Toolset.CreateModTool(self, "Settings", "mcp_bridge", "MCP Bridge", icon)
	if panel == null:
		return
	_panel = panel
	_panel_labels = {}
	_panel_label("listening", "Listening on %s:%d, protocol %d" % [HOST, PORT, PROTOCOL_VERSION])
	_panel_label("token", "")
	_panel_label("last", "No requests yet")
	_panel_label("history", "Assistant history: 0 steps")
	panel.CreateSeparator()
	var pause = panel.CreateCheckButton("Pause AI edits", "mcp_pause", _paused)
	if pause != null:
		pause.connect("toggled", self, "_on_panel_pause")
	var undo = panel.CreateButton("Undo the assistant's last step", icon)
	if undo != null:
		undo.connect("pressed", self, "_on_panel_undo")
	panel.CreateSeparator()
	var require = panel.CreateCheckButton("Require token", "mcp_require_token", _require_token)
	if require != null:
		require.connect("toggled", self, "_on_panel_require_token")
	var regen = panel.CreateButton("New token", icon)
	if regen != null:
		regen.connect("pressed", self, "_on_panel_new_token")
	panel.CreateNote("Pause and the token are yours: the assistant can read them but cannot " +
		"change them. The MCP server reads the token from Dungeondraft's user folder; " +
		"after a new token, reconnect it.")
	_refresh_panel()


func _panel_label(key : String, text : String) -> void:
	var marker = "__mcp_%s__" % key
	_panel.CreateLabel(marker)
	var label = _find_label(_panel, marker, 0)
	if label != null:
		label.text = text
		_panel_labels[key] = label


func _find_label(node, text : String, depth : int):
	if node == null or depth > 6:
		return null
	for child in node.get_children():
		if child is Label and child.text == text:
			return child
		var deeper = _find_label(child, text, depth + 1)
		if deeper != null:
			return deeper
	return null


func _set_panel_text(key : String, text : String) -> void:
	var label = _panel_labels.get(key)
	if label != null and is_instance_valid(label):
		label.text = text


func _refresh_panel() -> void:
	if _panel == null:
		return
	var token_text = "Token: required" if _require_token else "Token: not required"
	if _require_token and _token == "":
		token_text = "Token: could not be created, edits refused (see the log)"
	_set_panel_text("token", token_text)
	_set_panel_text("last", "Last request: " + (_last_request if _last_request != "" else "none yet"))
	_set_panel_text("history", "Assistant history: %d of %d steps%s" % [_undo_stack.size(), MAX_UNDO_OPS,
		" (paused)" if _paused else ""])


func _on_panel_pause(pressed : bool) -> void:
	_paused = pressed
	_settings_write("paused", pressed)
	print("[mcp-bridge] AI edits %s from the panel" % ("paused" if pressed else "resumed"))
	_refresh_panel()


func _on_panel_undo() -> void:
	_close_all_groups()
	var result = _do_undo()
	if result.get("ok", false) and result["result"].get("undone", false):
		_last_request = "undone from the panel at %s (%s)" % [_clock(), str(result["result"].get("kind", ""))]
	elif result.get("ok", false):
		_last_request = "nothing of the assistant's to undo"
	else:
		_last_request = "could not undo: " + str(result.get("error", ""))
	_refresh_panel()


func _on_panel_require_token(pressed : bool) -> void:
	_require_token = pressed
	_settings_write("require_token", pressed)
	print("[mcp-bridge] token %s from the panel" % ("required" if pressed else "not required"))
	_refresh_panel()


func _on_panel_new_token() -> void:
	_token = _regenerate_token()
	_last_request = "new token written at %s; reconnect the MCP server" % _clock()
	_refresh_panel()


# True for the bridge's own panel or anything in it: the assistant must not
# operate the user's controls through ui_action or native_*.
func _inside_panel(node) -> bool:
	if _panel == null or node == null or not (node is Node):
		return false
	var cursor = node
	var hops := 0
	while cursor != null and hops < 32:
		if cursor == _panel:
			return true
		cursor = cursor.get_parent()
		hops += 1
	return false


func _ensure_icon() -> String:
	var dir = Directory.new()
	if not dir.dir_exists(Global.Root + "icons"):
		dir.make_dir(Global.Root + "icons")
	var path = Global.Root + "icons/mcp_bridge.png"
	var f = File.new()
	if not f.file_exists(path):
		var img = Image.new()
		img.create(32, 32, false, Image.FORMAT_RGBA8)
		img.fill(Color(0.18, 0.55, 0.95))
		img.save_png(path)
	return path
