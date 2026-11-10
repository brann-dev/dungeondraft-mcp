extends SceneTree
# Runs the bridge mod's shared-history code against a stand-in for
# Dungeondraft's History, in a stock Godot 3.4 headless binary. Started by
# tools/test_mod_history.py, which puts the mod script next to this file as
# res://mod_copy.gd (see that file for why it is a copy).
#
# The stand-in keeps Dungeondraft's rules, taken from its History class:
#   - Record() cuts off the redo branch, appends, and drops the oldest record
#     past the "Max undos" limit;
#   - Undo() / Redo() run the record at once, then stay locked for three
#     frames, and a call made while locked is silently dropped;
#   - neither checks its bounds: Undo() with nothing to undo and Redo() with
#     nothing to redo leave the history broken.
# A dropped call or an out-of-bounds call is a bug in the bridge, so the
# stand-in counts both and every test ends by checking the counts are zero.


class CustomRecord extends Reference:   # Dungeondraft's CustomHistoryRecord
	var ScriptInstance = null

	func Undo():
		ScriptInstance.undo()

	func Redo():
		ScriptInstance.redo()


class OwnRecord extends Reference:      # one of Dungeondraft's own records
	var name = ""
	var trace = null

	func Undo():
		trace.append("undo " + name)

	func Redo():
		trace.append("redo " + name)


class FakeHistory extends Reference:
	var bookmark = -1
	var locked = false
	var records = []
	var max_undos = 32
	var lock_frames = 0
	var dropped = 0      # calls made while locked
	var broken = 0       # calls made out of bounds
	var refuses = false  # CreateCustomRecord fails, as a call into Dungeondraft can

	func _get(property):
		if property == "history":
			return records.duplicate()   # Dungeondraft hands GDScript a copy of its list
		return null

	func Record(record):
		if bookmark != records.size() - 1:
			records.resize(bookmark + 1)
		records.append(record)
		if records.size() > max_undos:
			records.pop_front()
		else:
			bookmark += 1

	func CreateCustomRecord(reference):
		if refuses:
			return null
		var record = CustomRecord.new()
		record.ScriptInstance = reference
		Record(record)
		return record

	func Undo():
		if locked:
			dropped += 1
			return
		if bookmark < 0:
			broken += 1
			return
		locked = true
		lock_frames = 3
		var record = records[bookmark]
		bookmark -= 1
		record.Undo()

	func Redo():
		if locked:
			dropped += 1
			return
		if bookmark + 1 >= records.size():
			broken += 1
			return
		locked = true
		lock_frames = 3
		bookmark += 1
		records[bookmark].Redo()

	func Clear():
		bookmark = -1
		records.clear()

	func frame():
		if lock_frames > 0:
			lock_frames -= 1
			if lock_frames == 0:
				locked = false


class HistoryWithoutLock extends Reference:   # a build whose History lacks what the bridge reads
	var bookmark = -1
	var records = []

	func _get(property):
		if property == "history":
			return records.duplicate()
		return null

	func CreateCustomRecord(reference):
		return null

	func Undo():
		pass

	func Redo():
		pass

	func Clear():
		pass


class FakeMaster extends Node:
	var IsBusy = false


class FakeEditor extends Node:
	var History = null
	var ActiveToolName = null


class FakeWorld extends Reference:
	var nodes = {}

	func GetNodeByID(id):
		return nodes.get(int(id))

	func HasNodeID(id):
		return nodes.has(int(id))

	func RemoveNodeID(id):
		nodes.erase(int(id))

	func SetNodeID(node, id):
		nodes[int(id)] = node


class FakePeer extends Reference:
	var status = 2   # StreamPeerTCP.STATUS_CONNECTED
	var sent = []

	func get_status():
		return status

	func put_data(bytes):
		sent.append(bytes.get_string_from_utf8())


var passed = 0
var failed = 0
var bridge_script = null
var keep = []   # nodes made for a test, freed at the end

# One test's world.
var bridge = null
var history = null
var master = null
var world = null
var trace = []


func _init():
	var f = File.new()
	f.open("res://mod_copy.gd", File.READ)
	bridge_script = GDScript.new()
	bridge_script.source_code = f.get_as_text()
	f.close()
	if bridge_script.reload() != OK:
		print("HISTORY_TESTS could not compile the mod")
		quit()
		return
	for name in ["test_mode_probe", "test_one_step", "test_bounds_and_lock", "test_own_records_replaced",
			"test_own_records_adopted", "test_user_step_on_top", "test_group_is_one_record",
			"test_group_split_by_user", "test_group_after_user_undo", "test_history_limit",
			"test_checkpoint_rollback", "test_checkpoint_user_steps", "test_checkpoint_empty_history",
			"test_checkpoint_replaced", "test_checkpoint_survives_a_growing_group",
			"test_checkpoint_survives_undo_and_retry", "test_busy_and_disconnect", "test_forget_history",
			"test_released_nodes", "test_stale_step", "test_record_refused", "test_panel_undo",
			"test_unreversible_call_in_a_group"]:
		_fresh()
		call(name)
		check(name + ": no call dropped or out of bounds", history.dropped == 0 and history.broken == 0,
			[history.dropped, history.broken])
	for node in keep:
		if is_instance_valid(node):
			node.free()
	print("HISTORY_TESTS passed=%d failed=%d" % [passed, failed])
	quit()


func check(label, ok, detail = null):
	if ok:
		passed += 1
	else:
		failed += 1
		print("FAIL  %s  %s" % [label, str(detail)])


func _fresh():
	history = FakeHistory.new()
	master = FakeMaster.new()
	var editor = FakeEditor.new()
	editor.History = history
	master.add_child(editor)
	keep.append(master)
	world = FakeWorld.new()
	trace = []
	bridge = bridge_script.new()
	bridge.Global = { "Editor": editor, "World": world, "WorldUI": null }
	bridge._native = bridge._native_available()


# A node the test's ops move, and an op that moves it: what a bridge command
# leaves behind for _settle.
func _node(id):
	var node = Node2D.new()
	keep.append(node)
	world.nodes[id] = node
	return node


func _move(id, x):
	var node = world.nodes[id]
	var op = { "kind": "transform", "id": id, "old": { "position": node.position }, "new": { "position": Vector2(x, 0) } }
	node.position = Vector2(x, 0)
	return op


# A bridge command that moves node `id` to x, as _handle_line and
# _record_and_dispatch run one. `own` names the records Dungeondraft adds by
# itself while the command runs.
func _command(id, x, own = []):
	bridge._watch_checkpoints()
	var mark = bridge._history_mark()
	var op = _move(id, x)
	for name in own:
		history.Record(_own(name))
	bridge._settle(op, mark)


func _own(name):
	var record = OwnRecord.new()
	record.name = name
	record.trace = trace
	return record


# The user edits by hand: Dungeondraft records a step of its own.
func _user_edit(name):
	history.Record(_own(name))


func _user_undo():
	history.Undo()
	_frames(4)


func _frames(count):
	for _i in range(count):
		history.frame()


# Send a history request and run frames until it is answered.
func _request(cmd, params = {}):
	var peer = FakePeer.new()
	var req = { "cmd": cmd }
	for key in params:
		req[key] = params[key]
	bridge._watch_checkpoints()
	bridge._start_job(peer, req)
	var frames = 0
	while bridge._job != null and frames < 500:
		history.frame()
		bridge._job_tick()
		frames += 1
	if peer.sent.empty():
		return { "ok": false, "error": "no answer after %d frames" % frames }
	return JSON.parse(peer.sent[0]).result


func _result(cmd, params = {}):
	var resp = _request(cmd, params)
	if not resp.get("ok", false):
		return { "error": resp.get("error") }
	return resp["result"]


func _checkpoint(label):
	bridge._watch_checkpoints()
	return bridge._checkpoint({ "label": label })["result"]


func _views():
	bridge._watch_checkpoints()
	var out = {}
	for view in bridge._list_checkpoints()["result"]["checkpoints"]:
		out[view["label"]] = view
	return out


func _owners():
	var out = []
	for record in history.records:
		out.append(bridge._record_owner(record))
	return out


# ---------------------------------------------------------------------------

func test_mode_probe():
	check("native mode is available", bridge._native and bridge._history_mode() == "dungeondraft")
	check("the probe leaves the bookmark alone", history.bookmark == -1)
	var editor = FakeEditor.new()
	keep.append(editor)
	editor.History = HistoryWithoutLock.new()
	var other = bridge_script.new()
	other.Global = { "Editor": editor, "World": world, "WorldUI": null }
	check("a History without `locked` means fallback", not other._native_available())
	check("fallback mode is named", other._history_mode() == "bridge")
	editor.History = null
	check("no History at all means fallback", not other._native_available())


func test_one_step():
	var node = _node(1)
	_command(1, 100)
	check("one command is one record", history.records.size() == 1 and history.bookmark == 0, history.records.size())
	var undone = _result("undo")
	check("undo answers", undone.get("undone") == true and undone.get("by") == "assistant"
		and undone.get("kind") == "transform", undone)
	check("undo depths", undone.get("undo_depth") == 0 and undone.get("redo_depth") == 1, undone)
	check("undo put the node back", node.position == Vector2(0, 0), node.position)
	var redone = _result("redo")
	check("redo answers", redone.get("redone") == true and redone.get("kind") == "transform"
		and redone.get("undo_depth") == 1 and redone.get("redo_depth") == 0, redone)
	check("redo moved the node again", node.position == Vector2(100, 0), node.position)
	# Dungeondraft's own Ctrl+Z reaches the same step.
	_frames(4)
	history.Undo()
	check("Ctrl+Z undoes the bridge's step", node.position == Vector2(0, 0), node.position)
	_frames(4)
	history.Redo()
	check("Ctrl+Y redoes it", node.position == Vector2(100, 0), node.position)
	_frames(4)


func test_bounds_and_lock():
	_node(1)
	var nothing = _result("undo")
	check("undo with nothing to undo", nothing.get("undone") == false and nothing.get("reason") == "nothing to undo", nothing)
	nothing = _result("redo")
	check("redo with nothing to redo", nothing.get("redone") == false and nothing.get("reason") == "nothing to redo", nothing)
	_command(1, 10)
	_command(1, 20)
	# Two undos with no frame between the requests: the second waits for the lock.
	var first = _result("undo")
	var second = _result("undo")
	check("back-to-back undos both land", first.get("undone") == true and second.get("undone") == true, [first, second])
	check("both steps undone", world.nodes[1].position == Vector2(0, 0) and history.bookmark == -1)
	var third = _result("undo")
	check("a third finds nothing", third.get("undone") == false, third)
	check("the answer waits for the lock to clear", not history.locked)


func test_own_records_replaced():
	_node(1)
	_command(1, 50, ["floor"])
	check("Dungeondraft's own record of the command is gone", history.records.size() == 1
		and _owners() == ["assistant"] and bridge._serial_of(history.records[0]) > 0, _owners())
	_result("undo")
	check("undo ran the bridge's step, not Dungeondraft's", trace.empty() and world.nodes[1].position == Vector2(0, 0), trace)
	# Same at the history limit, where recording drops the oldest instead of growing the list.
	_result("redo")
	history.max_undos = 2
	_user_edit("u1")
	check("history is full", history.records.size() == 2)
	_command(1, 60, ["water"])
	check("replaced at the limit too", history.records.size() == 2 and _owners() == ["user", "assistant"]
		and history.bookmark == 1, _owners())


func test_own_records_adopted():
	var mark = bridge._history_mark()
	history.Record(_own("prefab"))
	bridge._settle(null, mark)
	check("a command without an op keeps Dungeondraft's record", history.records.size() == 1)
	check("and it counts as the assistant's", _owners() == ["assistant"], _owners())
	var undone = _result("undo")
	check("undo takes it back through Dungeondraft", undone.get("undone") == true and undone.get("by") == "assistant"
		and undone.get("kind") == "dungeondraft" and trace == ["undo prefab"], [undone, trace])


func test_user_step_on_top():
	_node(1)
	_command(1, 10)
	_user_edit("wall")
	var refused = _result("undo")
	check("undo leaves the user's step alone", refused.get("undone") == false and refused.get("by") == "user"
		and trace.empty() and history.bookmark == 1, refused)
	var taken = _result("undo", { "include_user": true })
	check("include_user undoes it", taken.get("undone") == true and taken.get("by") == "user"
		and taken.get("kind") == "dungeondraft" and trace == ["undo wall"], [taken, trace])
	refused = _result("redo")
	check("redo leaves the user's step alone", refused.get("redone") == false and refused.get("by") == "user", refused)
	taken = _result("redo", { "include_user": true })
	check("include_user redoes it", taken.get("redone") == true and trace == ["undo wall", "redo wall"], [taken, trace])


func test_group_is_one_record():
	_node(1)
	_node(2)
	_node(3)
	_user_edit("before")
	bridge._begin_group({ "label": "batch", "undo_group": "t" })
	_command(1, 10)
	_command(2, 20, ["floor"])
	_command(3, 30)
	check("a group is one record", history.records.size() == 2 and _owners() == ["user", "assistant"], _owners())
	var closed = bridge._end_group({ "undo_group": "t" })["result"]
	check("the group reports its edits", closed.get("steps") == 3 and closed.get("undo_depth") == 2, closed)
	var undone = _result("undo")
	check("undo takes the whole group", undone.get("kind") == "group" and undone.get("steps") == 3
		and undone.get("label") == "batch", undone)
	check("every node is back", world.nodes[1].position.x == 0 and world.nodes[2].position.x == 0
		and world.nodes[3].position.x == 0)
	_result("redo")
	check("redo replays the whole group", world.nodes[1].position.x == 10 and world.nodes[2].position.x == 20
		and world.nodes[3].position.x == 30)
	# An edit after the group is a step of its own.
	_command(1, 11)
	check("the group is closed", history.records.size() == 3)
	# A group with a single edit reports that edit, not a group.
	bridge._begin_group({ "label": "batch", "undo_group": "t2" })
	_command(2, 21)
	bridge._end_group({ "undo_group": "t2" })
	check("a lone edit in a group keeps its kind", _result("undo").get("kind") == "transform")


func test_group_split_by_user():
	_node(1)
	_node(2)
	bridge._begin_group({ "label": "batch", "undo_group": "t" })
	_command(1, 10)
	_user_edit("by hand")
	_command(2, 20)
	_command(2, 25)
	bridge._end_group({ "undo_group": "t" })
	check("the user's edit splits the group in two records", _owners() == ["assistant", "user", "assistant"], _owners())
	var undone = _result("undo")
	check("the second part undoes together", undone.get("kind") == "group" and undone.get("steps") == 2
		and world.nodes[2].position.x == 0 and world.nodes[1].position.x == 10, undone)


func test_group_after_user_undo():
	_node(1)
	_node(2)
	bridge._begin_group({ "label": "batch", "undo_group": "t" })
	_command(1, 10)
	_user_undo()   # Ctrl+Z in the middle of the batch
	check("the user's Ctrl+Z undid the first edit", world.nodes[1].position.x == 0)
	_command(2, 20)
	bridge._end_group({ "undo_group": "t" })
	check("the rest of the group is a new record and the undone one is gone", history.records.size() == 1
		and history.bookmark == 0 and bridge._steps.size() == 1, [history.records.size(), bridge._steps.size()])
	_result("undo")
	check("undo touches only the part that was done", world.nodes[2].position.x == 0 and world.nodes[1].position.x == 0)


func test_history_limit():
	_node(1)
	history.max_undos = 3
	for i in range(5):
		_command(1, (i + 1) * 10)
	check("history holds the limit", history.records.size() == 3 and history.bookmark == 2)
	check("the bridge forgot the steps Dungeondraft dropped", bridge._steps.size() == 3, bridge._steps.size())
	for _i in range(3):
		check("undo within the limit", _result("undo").get("undone") == true)
	check("and no further", _result("undo").get("undone") == false)
	check("the node is where the oldest kept step found it", world.nodes[1].position.x == 20, world.nodes[1].position)


func test_checkpoint_rollback():
	_node(1)
	_command(1, 10)
	var made = _checkpoint("a")
	check("checkpoint reports the shared history", made.get("history") == "dungeondraft"
		and made.get("undo_depth") == 1 and made.get("history_limit") == 32, made)
	_command(1, 20)
	_command(1, 30)
	var view = _views()["a"]
	check("two steps since", view["steps_since"] == 2 and view["user_steps"] == 0 and view["reachable"], view)
	var rolled = _result("rollback_checkpoint", { "label": "a" })
	check("rollback undoes both", rolled.get("rolled_back") == 2 and rolled.get("kinds") == ["transform", "transform"]
		and rolled.get("user_steps") == 0 and rolled.get("undo_depth") == 1 and rolled.get("redo_depth") == 2
		and not rolled.has("stopped"), rolled)
	check("the map is at the checkpoint", world.nodes[1].position.x == 10)
	check("the checkpoint stays open", _views()["a"]["steps_since"] == 0 and _views()["a"]["reachable"])
	var again = _result("rollback_checkpoint", { "label": "a" })
	check("a second rollback has nothing to do", again.get("rolled_back") == 0, again)
	check("an unknown label is an error", _result("rollback_checkpoint", { "label": "nope" }).has("error"))
	# Undone past the checkpoint by hand: nothing since, and still reachable.
	_user_undo()
	view = _views()["a"]
	check("undone past the mark", view["steps_since"] == 0 and view["reachable"], view)


func test_checkpoint_user_steps():
	_node(1)
	_command(1, 10)
	_checkpoint("a")
	_command(1, 20)
	_user_edit("by hand")
	_command(1, 30)
	var view = _views()["a"]
	check("the user's step is counted", view["steps_since"] == 3 and view["user_steps"] == 1, view)
	var refused = _result("rollback_checkpoint", { "label": "a" })
	check("rollback refuses to undo the user's step", refused.has("error") and history.bookmark == 3
		and world.nodes[1].position.x == 30, refused)
	var rolled = _result("rollback_checkpoint", { "label": "a", "include_user": true })
	check("include_user rolls it all back", rolled.get("rolled_back") == 3 and rolled.get("user_steps") == 1
		and rolled.get("kinds") == ["transform", "dungeondraft", "transform"] and trace == ["undo by hand"]
		and world.nodes[1].position.x == 10, [rolled, trace])


func test_checkpoint_empty_history():
	_node(1)
	var made = _checkpoint("start")
	check("a checkpoint on an empty history", made.get("undo_depth") == 0)
	check("nothing since yet", _views()["start"]["steps_since"] == 0 and _views()["start"]["reachable"])
	_command(1, 10)
	_command(1, 20)
	var view = _views()["start"]
	check("both steps since", view["steps_since"] == 2 and view["reachable"], view)
	var rolled = _result("rollback_checkpoint", { "label": "start" })
	check("rollback empties the history", rolled.get("rolled_back") == 2 and history.bookmark == -1
		and world.nodes[1].position.x == 0, rolled)
	# Past the limit the oldest step since the checkpoint drops off: no way back.
	history.max_undos = 3
	for i in range(5):
		_command(1, (i + 1) * 10)
	view = _views()["start"]
	check("unreachable once a step since it was dropped", not view["reachable"] and view["steps_since"] == 0, view)
	var refused = _result("rollback_checkpoint", { "label": "start" })
	check("rollback refuses, changing nothing", refused.has("error") and history.bookmark == 2, refused)


func test_checkpoint_replaced():
	_node(1)
	_command(1, 10)
	_command(1, 20)
	_checkpoint("a")
	_user_undo()            # the user undoes the step the checkpoint was placed after...
	_user_edit("instead")   # ...and makes a different edit
	var view = _views()["a"]
	check("a replaced mark is unreachable", not view["reachable"], view)
	check("rollback refuses", _result("rollback_checkpoint", { "label": "a" }).has("error"))


func test_busy_and_disconnect():
	_node(1)
	_command(1, 10)
	master.IsBusy = true   # saving or exporting
	var peer = FakePeer.new()
	bridge._start_job(peer, { "cmd": "undo" })
	for _i in range(20):
		history.frame()
		bridge._job_tick()
	check("no move while Dungeondraft is busy", bridge._job != null and peer.sent.empty() and history.bookmark == 0)
	master.IsBusy = false
	for _i in range(10):
		history.frame()
		if bridge._job != null:
			bridge._job_tick()
	check("the move lands once it is free", bridge._job == null and peer.sent.size() == 1 and history.bookmark == -1)
	# A caller that hangs up is not served any further.
	_frames(4)
	master.IsBusy = true
	peer = FakePeer.new()
	bridge._start_job(peer, { "cmd": "redo" })
	peer.status = 0
	bridge._job_tick()
	master.IsBusy = false
	check("a job whose caller left is dropped", bridge._job == null and history.bookmark == -1 and peer.sent.empty())


func test_forget_history():
	_node(1)
	_command(1, 10)
	_checkpoint("a")
	_command(1, 20)
	_user_edit("by hand")
	bridge._forget_history()
	check("history is cleared", history.records.empty() and history.bookmark == -1 and bridge._steps.empty())
	check("checkpoints are out of reach", not _views()["a"]["reachable"])
	check("nothing to undo afterwards", _result("undo").get("undone") == false)


func test_released_nodes():
	# A created node the step holds out of the tree after its undo is freed
	# once a new edit cuts that step off.
	var parent = Node2D.new()
	keep.append(parent)
	var made = Node2D.new()
	keep.append(made)
	parent.add_child(made)
	world.nodes[7] = made
	_node(1)
	var mark = bridge._history_mark()
	bridge._settle({ "kind": "create", "node": made, "parent": parent, "id": 7 }, mark)
	_result("undo")
	check("undo took the node out", made.get_parent() == null and not world.nodes.has(7))
	_result("redo")
	check("redo put it back", made.get_parent() == parent and world.nodes.has(7))
	_result("undo")
	check("held, not freed, while the step can be redone", not made.is_queued_for_deletion())
	_command(1, 10)
	check("freed when a new edit drops the step", made.is_queued_for_deletion() and bridge._steps.size() == 1)


func test_stale_step():
	# A record whose bridge is gone (Reload Mods) does nothing and breaks nothing.
	_node(1)
	_command(1, 10)
	var shell = history.records[0].ScriptInstance
	shell.bridge_id = 0
	history.Undo()
	_frames(4)
	check("a stale record is a no-op", world.nodes[1].position.x == 10 and history.bookmark == -1)
	check("it still counts as the assistant's", _owners() == ["assistant"], _owners())


func test_checkpoint_survives_a_growing_group():
	# A group's step is recorded again in place when a later edit brings one of
	# Dungeondraft's own records with it: a new record object for the same
	# step. A checkpoint that holds on to that step must still find it.
	_node(1)
	_node(2)
	_checkpoint("room")   # on an empty history
	bridge._begin_group({ "label": "build_room", "undo_group": "t" })
	_command(1, 10)
	_command(2, 20, ["floor"])
	bridge._end_group({ "undo_group": "t" })
	var view = _views()["room"]
	check("still reachable after the step was recorded again", view["reachable"] and view["steps_since"] == 1, view)
	var rolled = _result("rollback_checkpoint", { "label": "room" })
	check("and it rolls back", rolled.get("rolled_back") == 1 and world.nodes[1].position.x == 0
		and world.nodes[2].position.x == 0, rolled)
	# The same with a step to hold on to: the checkpoint sits on a group's step.
	_fresh()
	_node(1)
	_node(2)
	bridge._begin_group({ "label": "batch", "undo_group": "t" })
	_command(1, 10)
	bridge._watch_checkpoints()
	bridge._checkpoints["mid"] = { "label": "mid", "anchor": bridge._record_key(history.records[0]),
		"first": 0, "lost": false, "opened": 0 }
	_command(2, 20, ["floor"])
	bridge._end_group({ "undo_group": "t" })
	check("a mark on a step that is recorded again keeps its hold", _views()["mid"]["reachable"], _views()["mid"])


func test_checkpoint_survives_undo_and_retry():
	# checkpoint, try something, undo it, try something else: undoing the
	# second try still leads back to the checkpoint.
	_node(1)
	_checkpoint("c")
	_command(1, 10)
	check("first try undone", _result("undo").get("undone") == true)
	_command(1, 20)
	var view = _views()["c"]
	check("reachable after undo and a new edit", view["reachable"] and view["steps_since"] == 1, view)
	var rolled = _result("rollback_checkpoint", { "label": "c" })
	check("rollback undoes the second try", rolled.get("rolled_back") == 1 and world.nodes[1].position.x == 0, rolled)
	# Redone instead of replaced: the first step is the one held on to again.
	_result("redo")
	check("redo is a step since the checkpoint", _views()["c"]["steps_since"] == 1 and _views()["c"]["reachable"])


func test_record_refused():
	# A call into Dungeondraft can fail without a word. If the step is not
	# recorded, the bookmark must be back where Dungeondraft left it.
	_node(1)
	_user_edit("u1")
	history.refuses = true
	_command(1, 10, ["floor"])
	check("the bookmark is back on Dungeondraft's own record", history.bookmark == 1 and history.records.size() == 2,
		[history.bookmark, history.records.size()])
	check("no step is kept for an edit that was not recorded", bridge._steps.empty(), bridge._steps.size())
	history.refuses = false
	_command(1, 20)
	check("the next edit records normally", history.records.size() == 3 and history.bookmark == 2
		and bridge._steps.size() == 1)
	_result("undo")
	check("and undoes", world.nodes[1].position.x == 10)


func test_panel_undo():
	_node(1)
	check("nothing to undo from the panel", bridge._panel_undo_shared() == "nothing to undo")
	_command(1, 10)
	master.IsBusy = true
	check("the panel waits while Dungeondraft is busy", bridge._panel_undo_shared().begins_with("Dungeondraft is busy")
		and history.bookmark == 0)
	master.IsBusy = false
	check("the panel undoes the assistant's step", bridge._panel_undo_shared().begins_with("undone from the panel")
		and world.nodes[1].position.x == 0)
	check("and not again inside the lock", bridge._panel_undo_shared().begins_with("Dungeondraft is busy"))
	_frames(4)
	_user_edit("by hand")
	check("the panel leaves the user's step alone", bridge._panel_undo_shared().find("your own") != -1
		and history.bookmark == 0 and trace.empty(), trace)


func test_unreversible_call_in_a_group():
	# A call the bridge has no op for, but Dungeondraft records, is a step of
	# its own even inside a group, and the group goes on in a second record.
	_node(1)
	_node(2)
	bridge._begin_group({ "label": "batch", "undo_group": "t" })
	_command(1, 10)
	var mark = bridge._history_mark()
	history.Record(_own("generated"))
	bridge._settle(null, mark)
	_command(2, 20)
	bridge._end_group({ "undo_group": "t" })
	check("three records, all the assistant's", _owners() == ["assistant", "assistant", "assistant"], _owners())
	check("undo takes the last part first", _result("undo").get("kind") == "transform" and world.nodes[2].position.x == 0)
	check("then Dungeondraft's own record", _result("undo").get("kind") == "dungeondraft" and trace == ["undo generated"], trace)
