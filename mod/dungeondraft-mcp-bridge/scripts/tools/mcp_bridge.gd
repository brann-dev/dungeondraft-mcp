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
const PROTOCOL_VERSION := 15

# Commands that get wrapped in a Dungeondraft undo record (see _record_and_dispatch).
const CREATE_CMDS := [
	"place_object", "draw_wall", "draw_path", "add_light",
	"add_portal", "add_roof", "add_text", "duplicate_object",
]
const TRANSFORM_CMDS := ["move_element", "modify_object"]
const TERRAIN_CMDS := ["fill_terrain", "fill_region", "paint_terrain", "paint_path"]
const CAVE_CMDS := ["dig_cave", "clear_caves"]
# Neutral opaque tint for pattern floors when no color is given. PatternShapeTool
# has no per-texture default (and its .Color leaks across calls), so we apply a
# deterministic wood/stone-neutral tone; callers pass an explicit color to override.
const DEFAULT_PATTERN_TINT := Color(0.62, 0.5, 0.34)

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


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

func start():
	_register_tool()
	_server = TCP_Server.new()
	var err = _server.listen(PORT, HOST)
	if err == OK:
		print("[mcp-bridge] listening on %s:%d (protocol v%d)" % [HOST, PORT, PROTOCOL_VERSION])
	else:
		_server = null
		print("[mcp-bridge] FAILED to listen on %s:%d (error %d)" % [HOST, PORT, err])
		OS.alert("MCP bridge could not open port %d (error %d).\nIs it already in use?" % [PORT, err], "MCP Bridge")


func update(delta : float):
	if _server == null:
		return

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
		resp = _record_and_dispatch(req)
	peer.put_data((JSON.print(resp) + "\n").to_utf8())


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
			terrain_before = lvl.Terrain.CloneSplatImage()

	var cave_before = null
	if cmd in CAVE_CMDS:
		cave_before = _cave_snapshot()

	var result = _safe_dispatch(req)
	if typeof(result) == TYPE_DICTIONARY and result.get("ok", false):
		var op = _build_op(cmd, req, result, pre, terrain_before, cave_before)
		if op != null:
			_undo_stack.append(op)
			_redo_stack = []   # a fresh edit invalidates the redo branch
	return result


# Returns an undo op for a recordable command, or null. Ops are reversed by
# _apply_op (undo=true) and re-applied (undo=false).
func _build_op(cmd, req, result, pre, terrain_before, cave_before = null):
	if cmd in CREATE_CMDS:
		var id = result["result"].get("id", -1)
		if id != null and int(id) >= 0:
			var node = Global.World.GetNodeByID(int(id))
			if node != null:
				return { "kind": "create", "node": node, "parent": node.get_parent(), "id": int(id) }
	elif cmd in TRANSFORM_CMDS and pre != null:
		var node = Global.World.GetNodeByID(int(req["id"]))
		if node != null:
			return { "kind": "transform", "id": int(req["id"]), "old": pre, "new": _snapshot(node) }
	elif cmd in TERRAIN_CMDS and terrain_before != null:
		var lvl = Global.World.GetCurrentLevel()
		if lvl != null:
			return { "kind": "terrain", "before": terrain_before, "after": lvl.Terrain.CloneSplatImage() }
	elif cmd in CAVE_CMDS and cave_before != null:
		var after = _cave_snapshot()
		if after != null:
			return { "kind": "cave", "before": cave_before, "after": after }
	return null


func _do_undo() -> Dictionary:
	if _undo_stack.empty():
		return _ok({ "undone": false, "reason": "nothing to undo" })
	var op = _undo_stack.pop_back()
	_apply_op(op, true)
	_redo_stack.append(op)
	return _ok({ "undone": true, "kind": op["kind"], "undo_depth": _undo_stack.size() })


func _do_redo() -> Dictionary:
	if _redo_stack.empty():
		return _ok({ "redone": false, "reason": "nothing to redo" })
	var op = _redo_stack.pop_back()
	_apply_op(op, false)
	_undo_stack.append(op)
	return _ok({ "redone": true, "kind": op["kind"], "redo_depth": _redo_stack.size() })


func _apply_op(op, undo : bool):
	match op["kind"]:
		"create":
			if undo:
				_detach_node(op["node"], int(op["id"]))
			else:
				_attach_node(op["node"], op["parent"], int(op["id"]))
		"transform":
			_apply_props(Global.World.GetNodeByID(int(op["id"])), op["old"] if undo else op["new"])
		"terrain":
			_restore_splat(op["before"] if undo else op["after"])
		"cave":
			_restore_cave(op["before"] if undo else op["after"])


func _detach_node(node, id : int):
	if is_instance_valid(node) and node.get_parent() != null:
		node.get_parent().remove_child(node)
	if Global.World.HasNodeID(id):
		Global.World.RemoveNodeID(id)


func _attach_node(node, parent, id : int):
	if not is_instance_valid(node):
		return
	if node.get_parent() == null and is_instance_valid(parent):
		parent.add_child(node)
	Global.World.SetNodeID(node, id)
	if node.has_method("RemakeLines"):
		node.RemakeLines()   # walls/paths cache geometry; refresh after re-adding


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
	if snap.get("color") != null and node.has_method("SetCustomColor"):
		node.SetCustomColor(snap["color"])


func _restore_splat(img):
	if img == null:
		return
	var level = Global.World.GetCurrentLevel()
	if level == null:
		return
	level.Terrain.RestoreSplat(img)
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
	if node.get("customColor") != null:
		s["color"] = node.get("customColor")
	return s


# Dispatch with a guard so a bad command can never take down the TCP loop.
func _safe_dispatch(req : Dictionary) -> Dictionary:
	var cmd = req.get("cmd", "")
	match cmd:
		# --- read / query ---
		"ping": return _ok({ "pong": true, "protocol": PROTOCOL_VERSION, "engine": Engine.get_version_info() })
		"get_status": return _get_status()
		"list_asset_categories": return _ok({ "categories": ASSET_CATEGORIES })
		"list_assets": return _list_assets(req)
		"list_elements": return _list_elements(req)
		"get_element": return _get_element(req)
		"list_levels": return _list_levels()
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
		# --- terrain ---
		"set_terrain_slot": return _set_terrain_slot(req)
		"fill_terrain": return _fill_terrain(req)
		"fill_region": return _fill_region(req)
		"paint_terrain": return _paint_terrain(req)
		"paint_path": return _paint_path(req)
		# --- modify / delete ---
		"move_element": return _move_element(req)
		"modify_object": return _modify_object(req)
		"duplicate_object": return _duplicate_object(req)
		"delete_element": return _delete_element(req)
		# --- levels ---
		"add_level": return _add_level(req)
		"set_level": return _set_level(req)
		# --- capture ---
		"screenshot": return _screenshot(req)
		"export_map": return _export_map(req)
		# --- camera ---
		"get_camera": return _get_camera()
		"set_camera": return _set_camera(req)
		"focus_element": return _focus_element(req)
		"fit_elements": return _fit_elements(req)
		# --- history (bridge-managed undo/redo of the model's own edits) ---
		"undo": return _do_undo()
		"redo": return _do_redo()
		# --- selection ---
		"select_elements": return _select_elements(req)
		"clear_selection": return _clear_selection()
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
		"level_id": Global.World.CurrentLevelId,
		"level_count": Global.World.levels.size(),
		"map_size_woxels": [Global.World.WoxelDimensions.x, Global.World.WoxelDimensions.y],
		"map_center": [Global.World.WoxelDimensions.x * 0.5, Global.World.WoxelDimensions.y * 0.5],
		"counts": counts,
		"active_tool": Global.Editor.ActiveToolName,
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
	return _ok(_describe(node))


func _list_levels() -> Dictionary:
	var out := []
	var levels = Global.World.levels
	for i in range(levels.size()):
		var lv = levels[i]
		out.append({ "index": i, "id": lv.ID, "label": lv.Label })
	return _ok({ "current_index": Global.World.CurrentLevelId, "levels": out })


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

func _place_object(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var tex = _asset_tex("Objects", req.get("asset", ""))
	if tex == null: return _err("could not load object asset: " + str(req.get("asset")))
	var prop = level.Objects.CreateObject(int(req.get("sorting", 0)))
	prop.SetTexture(tex)
	prop.position = _xy(req, Global.World.WoxelDimensions * 0.5)
	var s = float(req.get("scale", 1.0))
	prop.scale = Vector2(s, s)
	prop.rotation = deg2rad(float(req.get("rotation", 0.0)))
	if req.has("color") and prop.has_method("SetCustomColor"):
		prop.SetCustomColor(_color(req["color"], Color(1, 1, 1)))
	return _ok({ "id": _id(prop), "position": _vec(prop.position) })


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
	var path = level.Pathways.CreatePath(
		tex, int(req.get("layer", 0)), int(req.get("sorting", 0)),
		bool(req.get("fade_in", false)), bool(req.get("fade_out", false)),
		bool(req.get("grow", false)), bool(req.get("shrink", false)))
	path.SetEditPoints(pts)
	if req.has("smoothness"):
		path.Smoothness = float(req["smoothness"])
		path.Smooth()
	if req.has("width"):
		path.SetWidthScale(float(req["width"]))
	path.UpdateGradient()
	return _ok({ "id": _id(path), "point_count": pts.size() })


func _add_light(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var light = level.Lights.CreateLight(false)
	light.position = _xy(req, Global.World.WoxelDimensions * 0.5)
	light.color = _color(req.get("color", ""), Color(1, 0.9, 0.7))
	light.energy = float(req.get("energy", 1.0))
	light.texture_scale = float(req.get("range", 1.0))
	light.shadow_enabled = bool(req.get("shadows", true))
	var tex = _asset_tex("Lights", req.get("asset", ""))
	if tex != null:
		light.texture = tex
	return _ok({ "id": _id(light), "position": _vec(light.position) })


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


func _add_roof(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var pts = _points(req.get("points", []))
	if pts.size() < 2: return _err("'points' needs >= 2 [x,y] pairs")
	var tex = _asset_tex("Roofs", req.get("asset", ""))
	if tex == null: return _err("could not load roof asset: " + str(req.get("asset")))
	# A roof over a building is a CLOSED footprint, but Roof.Set connects the
	# points as given without closing the loop (an open polygon renders as a
	# "C" shape with one side missing). Close it by repeating the first point,
	# unless the caller wants an open ridge (`closed:false`, e.g. a lean-to).
	var closed = bool(req.get("closed", true))
	if closed and pts.size() >= 3 and pts[0].distance_to(pts[pts.size() - 1]) > 0.5:
		pts.append(pts[0])
	var roof = level.Roofs.CreateRoof(int(req.get("sorting", 0)))
	roof.Set(pts, float(req.get("width", 256.0)), int(req.get("type", 0)))  # type: 0 gable,1 hip,2 dormer
	roof.SetTileTexture(tex)
	return _ok({ "id": _id(roof), "point_count": pts.size(), "closed": closed })


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
	# Z-ORDER: new shapes land in the tool's default "Layer 100" node, whose
	# z_index is 100 — ABOVE the Objects node (z 0), so the floor would cover
	# furniture. The shape's own z_index is z_as_relative, i.e. an offset from
	# that 100. To sit below objects we use an ABSOLUTE z: set z_as_relative=false
	# and z_index=`z` (default -100, between FloorShapes at -200 and Objects at 0).
	var all = shapes.GetShapes()
	var result := { "shape": kind, "category": category, "shape_count": all.size() }
	if color is Color:
		result["color"] = "#" + color.to_html(true)
	if used_default:
		# Signal we applied the neutral default (no per-texture tint exists for
		# patterns; pass an explicit `color` for an exact look).
		result["used_default_tint"] = true
	if all.size() > before and all.size() > 0:
		var shape = all[all.size() - 1]
		if shape.has_method("SetOptions"):
			shape.SetOptions(tex, color, rotation)
		shape.z_as_relative = false
		shape.z_index = int(req.get("z", -100))
		result["id"] = _id(shape)
		result["z_index"] = shape.z_index
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

	# 1) Wall loop along the boundary.
	var wall_req := {
		"points": pts_raw, "loop": true,
		"asset": req.get("wall_asset", ""),
		"type": req.get("wall_type", 0), "joint": req.get("wall_joint", 1),
		"shadow": req.get("wall_shadow", true),
	}
	var wall_res = _draw_wall(wall_req)
	if not wall_res.get("ok", false):
		return wall_res
	result["wall_id"] = wall_res["result"].get("id")

	# 2) Floor along the SAME boundary (no inset — the wall covers the seam).
	var floor_kind = str(req.get("floor", "pattern"))
	if floor_kind == "pattern":
		var fr := {
			"points": pts_raw,
			"asset": req.get("floor_asset", ""),
			"category": req.get("floor_category", "Simple Tiles"),
		}
		if req.has("floor_color"): fr["color"] = req["floor_color"]
		if req.has("floor_z"): fr["z"] = req["floor_z"]
		var fres = _place_pattern(fr)
		if fres.get("ok", false):
			result["floor_id"] = fres["result"].get("id")
		else:
			result["floor_error"] = fres.get("error")
	elif floor_kind == "terrain":
		var tr := {
			"points": pts_raw, "slot": int(req.get("floor_slot", 1)),
		}
		if req.has("floor_asset") and str(req.get("floor_asset", "")) != "":
			tr["asset"] = req["floor_asset"]
		var tres = _fill_region(tr)
		if tres.get("ok", false):
			result["floor_pixels"] = tres["result"].get("pixels")
		else:
			result["floor_error"] = tres.get("error")
	# floor_kind == "none" -> walls only

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


# ---------------------------------------------------------------------------
# Terrain
# ---------------------------------------------------------------------------

func _set_terrain_slot(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var tex = _asset_tex("Terrain", req.get("asset", ""))
	if tex == null: return _err("could not load terrain asset: " + str(req.get("asset")))
	var slot = int(req.get("slot", 0))
	level.Terrain.SetTexture(tex, slot)
	level.Terrain.UpdateSplat()
	return _ok({ "slot": slot })


func _fill_terrain(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var slot = int(req.get("slot", 0))
	if req.has("asset"):
		var tex = _asset_tex("Terrain", req["asset"])
		if tex == null: return _err("could not load terrain asset: " + str(req["asset"]))
		level.Terrain.SetTexture(tex, slot)
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
	if req.has("asset"):
		var tex = _asset_tex("Terrain", req["asset"])
		if tex == null: return _err("could not load terrain asset: " + str(req["asset"]))
		level.Terrain.SetTexture(tex, slot)
	var world = _xy(req, Global.World.WoxelDimensions * 0.5)
	# Convert the brush center and radius into texture space (radius scales by
	# the woxel->texture ratio along x).
	var center = level.Terrain.WorldToTexture(world)
	var tscale = float(level.Terrain.width) / max(Global.World.WoxelDimensions.x, 1.0)
	var trad = max(radius * tscale, 0.5)

	var sp = _open_splat(level, slot)
	if sp == null: return _err("could not read splat image for slot " + str(slot))
	var img = sp.img
	var ch = sp.ch
	var iw = img.get_width()
	var ih = img.get_height()
	var x0 = int(floor(center.x - trad))
	var y0 = int(floor(center.y - trad))
	var x1 = int(ceil(center.x + trad))
	var y1 = int(ceil(center.y + trad))
	var painted := 0
	img.lock()
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
			img.set_pixel(ix, iy, _splat_set_channel(img.get_pixel(ix, iy), ch, w))
			painted += 1
	img.unlock()
	_close_splat(level, sp.which, img)
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
	if req.has("asset"):
		var tex = _asset_tex("Terrain", req["asset"])
		if tex == null: return _err("could not load terrain asset: " + str(req["asset"]))
		level.Terrain.SetTexture(tex, slot)

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
	var img = sp.img
	var ch = sp.ch
	var iw = img.get_width()
	var ih = img.get_height()
	# Bounding box of the whole stroke, padded by the brush radius.
	var x0 = max(int(floor(mn.x - trad)), 0)
	var y0 = max(int(floor(mn.y - trad)), 0)
	var x1 = min(int(ceil(mx.x + trad)), iw - 1)
	var y1 = min(int(ceil(mx.y + trad)), ih - 1)
	var painted := 0
	img.lock()
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
			img.set_pixel(ix, iy, _splat_set_channel(img.get_pixel(ix, iy), ch, w))
			painted += 1
	img.unlock()
	_close_splat(level, sp.which, img)
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
	if req.has("asset"):
		var tex = _asset_tex("Terrain", req["asset"])
		if tex == null: return _err("could not load terrain asset: " + str(req["asset"]))
		level.Terrain.SetTexture(tex, slot)

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
	var img = sp.img
	var iw = img.get_width()
	var ih = img.get_height()
	var painted := 0
	img.lock()
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
			img.set_pixel(ix, iy, _splat_set_channel(img.get_pixel(ix, iy), sp.ch, rate))
			painted += 1
	img.unlock()
	_close_splat(level, sp.which, img)

	return _ok({
		"filled_slot": slot, "shape": ("rect" if req.has("rect") else "polygon"),
		"texture_bbox": [_vec(origin), [origin.x + bw, origin.y + bh]],
		"pixels": painted,
	})


# Clone the splat weight image holding `slot` for direct editing. Returns
# { img, ch, which } where ch is the RGBA channel (0..3) for that slot and
# which is 0 (slots 0-3 -> splatImage) or 1 (slots 4-7 -> splatImage2), or null
# if the image isn't available. Pair with _close_splat to push edits back.
func _open_splat(level, slot : int):
	var which = 0 if slot < 4 else 1
	var img = level.Terrain.CloneSplatImage() if which == 0 else level.Terrain.CloneSplatImage2()
	if img == null:
		return null
	return { "img": img, "ch": slot % 4, "which": which }


func _close_splat(level, which : int, img) -> void:
	if which == 0:
		level.Terrain.RestoreSplat(img)
	else:
		level.Terrain.RestoreSplat2(img)
	level.Terrain.UpdateSplat()


# Push channel `ch` (0=R,1=G,2=B,3=A) of an RGBA splat weight toward 1 by `rate`,
# scaling the remaining channels down so the four weights still sum to ~1.
func _splat_set_channel(c : Color, ch : int, rate : float) -> Color:
	var w = [c.r, c.g, c.b, c.a]
	var target = w[ch] + (1.0 - w[ch]) * rate
	var rest = 1.0 - target
	var others = (w[0] + w[1] + w[2] + w[3]) - w[ch]
	for i in range(4):
		if i == ch:
			w[i] = target
		elif others > 0.0001:
			w[i] = w[i] / others * rest
		else:
			w[i] = 0.0
	return Color(w[0], w[1], w[2], w[3])


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
		var s = float(req["scale"]); node.scale = Vector2(s, s)
	if req.has("rotation"):
		node.rotation = deg2rad(float(req["rotation"]))
	if req.has("color") and node.has_method("SetCustomColor"):
		node.SetCustomColor(_color(req["color"], Color(1, 1, 1)))
	if req.has("shadow"):
		node.set("HasShadow", bool(req["shadow"]))
	return _ok(_describe(node))


func _duplicate_object(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var src = _resolve(req)
	if src == null: return _err("no element with id " + str(req.get("id")))
	if src.get("Texture") == null: return _err("element has no Texture to duplicate")
	var prop = level.Objects.CreateObject(0)
	prop.SetTexture(src.Texture)
	prop.position = src.position + Vector2(float(req.get("dx", 64.0)), float(req.get("dy", 0.0)))
	prop.scale = src.scale
	prop.rotation = src.rotation
	return _ok({ "id": _id(prop), "position": _vec(prop.position) })


func _delete_element(req : Dictionary) -> Dictionary:
	var ident = req.get("id")
	if ident == null: return _err("missing 'id'")
	var ok = Global.World.DeleteNodeByID(int(ident))
	return _ok({ "deleted": ok, "id": ident })


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------

func _add_level(req : Dictionary) -> Dictionary:
	var lv = Global.World.CreateLevel(str(req.get("label", "Level")))
	return _ok({ "id": lv.ID, "label": lv.Label })


func _set_level(req : Dictionary) -> Dictionary:
	var idx = int(req.get("index", 0))
	if idx < 0 or idx >= Global.World.levels.size():
		return _err("level index out of range: " + str(idx))
	Global.World.SetLevel(idx)
	return _ok({ "current_index": idx })


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


# Render the whole map (no UI) to a clean PNG. Asynchronous: Exporter.Start runs
# on a separate thread, so the caller (MCP server) polls the path for the file.
func _export_map(req : Dictionary) -> Dictionary:
	var path = str(req.get("path", ""))
	if path == "": return _err("missing 'path'")
	var ppi = int(req.get("ppi", 40))
	Global.Exporter.Start(0, ppi, path)  # mode 0 = PNG
	return _ok({ "path": path, "ppi": ppi, "async": true })


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
		d["scale"] = node.scale.x
	var tex_path = _texture_path(node, t)
	if tex_path != "":
		d["asset"] = tex_path
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


# Accepts "#rrggbb" / "rrggbb" string, or [r,g,b] / [r,g,b,a] floats 0..1.
func _color(v, fallback : Color) -> Color:
	if typeof(v) == TYPE_STRING and v != "":
		return Color(v)
	if typeof(v) == TYPE_ARRAY and v.size() >= 3:
		var a = 1.0
		if v.size() >= 4:
			a = float(v[3])
		return Color(float(v[0]), float(v[1]), float(v[2]), a)
	return fallback


func _ok(result) -> Dictionary:
	return { "ok": true, "result": result }


func _err(msg) -> Dictionary:
	return { "ok": false, "error": msg }


func _register_tool():
	var icon = _ensure_icon()
	var panel = Global.Editor.Toolset.CreateModTool(self, "Settings", "mcp_bridge", "MCP Bridge", icon)
	panel.CreateLabel("Listening on")
	panel.CreateLabel("%s:%d" % [HOST, PORT])


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
