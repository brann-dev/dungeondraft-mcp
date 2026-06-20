extends Reference
# Undo/redo record created by the MCP bridge and handed to
# Global.Editor.History.CreateCustomRecord(). Dungeondraft calls undo() on Ctrl+Z
# and redo() on Ctrl+Y. `state["action"]` selects the behaviour:
#
#   create:    { action, node, parent, id }   undo detaches the node; redo re-adds
#   transform: { action, id, old, new }        undo/redo set captured props
#   terrain:   { action, before, after }       undo/redo restore a splat image
#
# Create undo uses remove_child / add_child (pure scene-graph ops) rather than
# World.DeleteNodeByID(), because that is a history-aware delete: calling it from
# undo() clears Dungeondraft's redo stack, so redo would have nothing to restore.
# The record holds a reference to the detached node, keeping it alive and keeping
# its id stable across undo/redo.
#
# NOTE: `extends` MUST be the first line in Godot 3.4 GDScript ("extends must be
# used before anything else") — a leading comment block makes Dungeondraft abort
# loading the whole mod. This file has no `script_class`, so DD does not load it
# as a tool; it is only instantiated via Script.InstanceReference() by the bridge.

var state = {}


func undo():
	match state.get("action", ""):
		"create":
			_detach(state["node"], int(state["id"]))
		"transform":
			_apply(Global.World.GetNodeByID(int(state["id"])), state["old"])
		"terrain":
			_restore_terrain(state["before"])


func redo():
	match state.get("action", ""):
		"create":
			_attach(state["node"], state["parent"], int(state["id"]))
		"transform":
			_apply(Global.World.GetNodeByID(int(state["id"])), state["new"])
		"terrain":
			_restore_terrain(state["after"])


func _detach(node, id):
	if is_instance_valid(node) and node.get_parent() != null:
		node.get_parent().remove_child(node)
	if Global.World.HasNodeID(id):
		Global.World.RemoveNodeID(id)


func _attach(node, parent, id):
	if not is_instance_valid(node):
		return
	if node.get_parent() == null and is_instance_valid(parent):
		parent.add_child(node)
	Global.World.SetNodeID(node, id)
	if node.has_method("RemakeLines"):
		node.RemakeLines()   # walls/paths cache geometry; refresh after re-adding


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
