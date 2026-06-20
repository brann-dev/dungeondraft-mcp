# Undo/redo record created by the MCP bridge and handed to
# Global.Editor.History.CreateCustomRecord(). Dungeondraft calls undo() on Ctrl+Z
# and redo() on Ctrl+Y. `state["action"]` selects the behaviour:
#
#   create:    { action, bridge, req, id }   undo deletes the node; redo replays
#   transform: { action, id, old, new }       undo/redo set captured props
#   terrain:   { action, before, after }       undo/redo restore a splat image
#
# This file has no `script_class`, so Dungeondraft does not load it as a tool;
# it is only instantiated via Script.InstanceReference() by the bridge.
extends Reference

var state = {}


func undo():
	match state.get("action", ""):
		"create":
			Global.World.DeleteNodeByID(int(state["id"]))
		"transform":
			_apply(Global.World.GetNodeByID(int(state["id"])), state["old"])
		"terrain":
			_restore_terrain(state["before"])


func redo():
	match state.get("action", ""):
		"create":
			# Re-run the original command through the bridge's pure dispatcher
			# (no recording happens there), then track the new node's id.
			var r = state["bridge"]._safe_dispatch(state["req"])
			if typeof(r) == TYPE_DICTIONARY and r.get("ok", false):
				state["id"] = r["result"].get("id", state["id"])
		"transform":
			_apply(Global.World.GetNodeByID(int(state["id"])), state["new"])
		"terrain":
			_restore_terrain(state["after"])


func _apply(node, snap):
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


func _restore_terrain(img):
	if img == null:
		return
	var level = Global.World.GetCurrentLevel()
	if level == null:
		return
	level.Terrain.RestoreSplat(img)
	level.Terrain.UpdateSplat()
