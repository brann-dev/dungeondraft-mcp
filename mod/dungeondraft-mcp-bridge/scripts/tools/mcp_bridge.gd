# MCP Bridge — Dungeondraft mod
#
# Opens a localhost TCP server inside Dungeondraft and speaks a tiny
# newline-delimited JSON protocol (see PROTOCOL.md) so an external MCP server
# can drive the currently-open map.
#
# Engine: Dungeondraft runs Godot 3.4.2, so this uses the Godot 3 networking
# class names (TCP_Server / StreamPeerTCP) and the connect(sig, self, "method")
# signal form. Mod tool scripts receive a per-frame update(delta) callback but
# NOT _process / _input, so all socket polling happens in update().

var script_class = "tool"

const HOST := "127.0.0.1"
const PORT := 8787
const PROTOCOL_VERSION := 1

var _server : TCP_Server = null
var _conns := []   # array of { "peer": StreamPeerTCP, "buf": String }


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

func start():
	# A registered tool guarantees the lifecycle callbacks (incl. update) fire.
	_register_tool()

	_server = TCP_Server.new()
	var err = _server.listen(PORT, HOST)
	if err == OK:
		print("[mcp-bridge] listening on %s:%d (protocol v%d)" % [HOST, PORT, PROTOCOL_VERSION])
	else:
		_server = null
		print("[mcp-bridge] FAILED to listen on %s:%d (error %d)" % [HOST, PORT, err])
		# If this alert fires, networking IS allowed but the port is taken.
		# If you never see the "listening" line AND never this alert, the
		# sandbox blocked TCP_Server — see PROTOCOL.md fallback notes.
		OS.alert("MCP bridge could not open port %d (error %d).\nIs it already in use?" % [PORT, err], "MCP Bridge")


func update(delta : float):
	if _server == null:
		return

	# Accept any pending connections.
	while _server.is_connection_available():
		var peer = _server.take_connection()
		peer.set_no_delay(true)
		_conns.append({ "peer": peer, "buf": "" })

	# Service open connections; drop dead ones.
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

		# Process every complete newline-delimited request in the buffer.
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
		resp = _dispatch(req)
		if typeof(req) == TYPE_DICTIONARY and req.has("id"):
			resp["id"] = req["id"]   # echo correlation id if the caller sent one

	var out = JSON.print(resp) + "\n"
	peer.put_data(out.to_utf8())


func _dispatch(req : Dictionary) -> Dictionary:
	var cmd = req.get("cmd", "")
	match cmd:
		"ping":
			return _ok({
				"pong": true,
				"protocol": PROTOCOL_VERSION,
				"engine": Engine.get_version_info(),
			})
		"get_status":
			return _get_status()
		"list_assets":
			return _list_assets(req)
		"place_object":
			return _place_object(req)
		"draw_wall":
			return _draw_wall(req)
		_:
			return _err("unknown cmd: " + str(cmd))


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

func _get_status() -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null:
		return _ok({ "map_open": false })
	return _ok({
		"map_open": true,
		"level_id": Global.World.CurrentLevelId,
		"level_count": Global.World.levels.size(),
		"map_size_woxels": [Global.World.WoxelDimensions.x, Global.World.WoxelDimensions.y],
		"object_count": level.Objects.get_children().size(),
		"active_tool": Global.Editor.ActiveToolName,
	})


func _list_assets(req : Dictionary) -> Dictionary:
	var category = req.get("category", "Objects")
	var limit = int(req.get("limit", 100))
	var all = Script.GetAssetList(category)
	if all == null:
		return _err("unknown asset category: " + str(category))
	var out := []
	var count = min(limit, all.size())
	for i in range(count):
		out.append(all[i])
	return _ok({ "category": category, "total": all.size(), "returned": out.size(), "assets": out })


func _place_object(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null:
		return _err("no map open")
	var asset = req.get("asset", "")
	if asset == "":
		return _err("missing 'asset' (call list_assets to find one)")

	var tex = Script.GetAssetTexture("Objects", asset)
	if tex == null:
		return _err("could not load object texture: " + str(asset))

	var prop = level.Objects.CreateObject(int(req.get("sorting", 0)))  # 0=over, 1=under
	prop.SetTexture(tex)
	var pos = _xy(req, Global.World.WoxelDimensions * 0.5)
	prop.position = pos
	var s = float(req.get("scale", 1.0))
	prop.scale = Vector2(s, s)
	prop.rotation = deg2rad(float(req.get("rotation", 0.0)))

	var nid = null
	if prop.has_meta("node_id"):
		nid = prop.get_meta("node_id")
	return _ok({ "placed": true, "position": [pos.x, pos.y], "node_id": nid })


# Experimental: coordinate space and wall texture category are inferred, not
# documented. Verify visually and adjust before relying on this.
func _draw_wall(req : Dictionary) -> Dictionary:
	var level = Global.World.GetCurrentLevel()
	if level == null:
		return _err("no map open")
	var raw = req.get("points", [])
	if typeof(raw) != TYPE_ARRAY or raw.size() < 2:
		return _err("'points' must be an array of [x,y] pairs, length >= 2")

	var pts := PoolVector2Array()
	for p in raw:
		pts.append(Vector2(float(p[0]), float(p[1])))

	var tex = null
	var asset = req.get("asset", "")
	if asset != "":
		tex = Script.GetAssetTexture("Walls", asset)

	var wall = level.Walls.AddWall(
		pts,
		tex,
		Color(1, 1, 1),
		bool(req.get("loop", false)),
		bool(req.get("shadow", true)),
		int(req.get("type", 0)),    # 0=auto, 1=manual, 2=cave
		int(req.get("joint", 1)),   # 0=sharp, 1=bevel, 2=round
		true)
	return _ok({ "drawn": wall != null, "point_count": pts.size() })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

func _xy(req : Dictionary, fallback : Vector2) -> Vector2:
	if req.has("x") and req.has("y"):
		return Vector2(float(req["x"]), float(req["y"]))
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


# CreateModTool needs an icon path. Generate a tiny one at runtime so the mod
# ships without a binary asset. Works for unpacked (dev) mods where Global.Root
# is writable.
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
