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

var script_class = "tool"

const HOST := "127.0.0.1"
const PORT := 8787
const PROTOCOL_VERSION := 3

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
var _conns := []   # array of { "peer": StreamPeerTCP, "buf": String }


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
		resp = _safe_dispatch(req)
		if typeof(req) == TYPE_DICTIONARY and req.has("id"):
			resp["id"] = req["id"]
	peer.put_data((JSON.print(resp) + "\n").to_utf8())


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
		# --- create ---
		"place_object": return _place_object(req)
		"draw_wall": return _draw_wall(req)
		"draw_path": return _draw_path(req)
		"add_light": return _add_light(req)
		"add_portal": return _add_portal(req)
		"add_roof": return _add_roof(req)
		"add_text": return _add_text(req)
		# --- terrain ---
		"set_terrain_slot": return _set_terrain_slot(req)
		"fill_terrain": return _fill_terrain(req)
		"paint_terrain": return _paint_terrain(req)
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


func _list_elements(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null:
		return _err("no map open")
	var kind = req.get("kind", "objects")
	if not COLLECTIONS.has(kind):
		return _err("unknown kind '%s' (one of: %s)" % [kind, COLLECTIONS.keys()])
	var limit = int(req.get("limit", 200))
	var out := []
	for node in _collection(level, kind).get_children():
		if out.size() >= limit:
			break
		out.append(_describe(node))
	return _ok({ "kind": kind, "count": out.size(), "elements": out })


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
		pts, tex, _color(req.get("color", ""), Color(1, 1, 1)),
		bool(req.get("loop", false)), bool(req.get("shadow", true)),
		int(req.get("type", 0)), int(req.get("joint", 1)), true)
	if wall == null: return _err("AddWall returned null (bad asset/points?)")
	return _ok({ "id": _id(wall), "point_count": pts.size() })


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


func _add_portal(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var tex = _asset_tex("Portals", req.get("asset", ""))
	if tex == null: return _err("could not load portal asset: " + str(req.get("asset")))
	var pos = _xy(req, Global.World.WoxelDimensions * 0.5)
	level.CreateFreestandingPortal(
		tex, pos, bool(req.get("closed", false)),
		float(req.get("radius", 64.0)), deg2rad(float(req.get("rotation", 0.0))))
	# CreateFreestandingPortal returns void; the new portal is the last child.
	var kids = level.Portals.get_children()
	if kids.empty(): return _err("portal was not created")
	return _ok({ "id": _id(kids[kids.size() - 1]), "position": _vec(pos) })


func _add_roof(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var pts = _points(req.get("points", []))
	if pts.size() < 2: return _err("'points' needs >= 2 [x,y] pairs")
	var tex = _asset_tex("Roofs", req.get("asset", ""))
	if tex == null: return _err("could not load roof asset: " + str(req.get("asset")))
	var roof = level.Roofs.CreateRoof(int(req.get("sorting", 0)))
	roof.Set(pts, float(req.get("width", 256.0)), int(req.get("type", 0)))  # type: 0 gable,1 hip,2 dormer
	roof.SetTileTexture(tex)
	return _ok({ "id": _id(roof), "point_count": pts.size() })


# A Dungeondraft Text extends Godot LineEdit, so the string is the inherited
# `.text` property. SetFont takes (name, size); SetFontSize/SetFontColor are setters.
func _add_text(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var text = level.Texts.CreateText()
	text.position = _xy(req, Global.World.WoxelDimensions * 0.5)
	text.text = str(req.get("text", ""))
	var size = int(req.get("size", 32))
	if req.has("size"):
		text.SetFontSize(size)
	if req.has("color"):
		text.SetFontColor(_color(req["color"], Color(1, 1, 1)))
	if req.has("font"):
		text.SetFont(str(req["font"]), size)
	if Global.Editor.Tools.has("TextTool"):
		Global.Editor.Tools["TextTool"].UpdateText(text)
	return _ok({ "id": _id(text) })


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


# Experimental: brush footprint / offset semantics inferred, verify visually.
func _paint_terrain(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null: return _err("no map open")
	var slot = int(req.get("slot", 0))
	var radius = float(req.get("radius", 64.0))
	var rate = float(req.get("rate", 1.0))
	var brush = _circle_brush(radius)
	var world = _xy(req, Global.World.WoxelDimensions * 0.5)
	var tex_pos = level.Terrain.WorldToTexture(world)
	var offset = Vector2(-brush.get_width() * 0.5, -brush.get_height() * 0.5)
	level.Terrain.Paint(slot, brush, offset, tex_pos, rate)
	level.Terrain.UpdateSplat()
	return _ok({ "painted_slot": slot, "experimental": true })


# ---------------------------------------------------------------------------
# Modify / delete
# ---------------------------------------------------------------------------

func _move_element(req : Dictionary) -> Dictionary:
	var node = _resolve(req)
	if node == null: return _err("no element with id " + str(req.get("id")))
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


func _describe(node) -> Dictionary:
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


func _circle_brush(radius : float) -> Image:
	var size = int(max(2.0, radius * 2.0))
	var img = Image.new()
	img.create(size, size, false, Image.FORMAT_RGBA8)
	img.lock()
	var c = Vector2(size, size) * 0.5
	var r = size * 0.5
	for y in range(size):
		for x in range(size):
			var a = clamp(1.0 - (Vector2(x, y).distance_to(c) / r), 0.0, 1.0)
			img.set_pixel(x, y, Color(1, 1, 1, a))
	img.unlock()
	return img


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
