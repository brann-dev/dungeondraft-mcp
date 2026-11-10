#!/usr/bin/env python3
"""Live validator for the shared undo history (0.6.0).

Run with Dungeondraft open (mod enabled, a map loaded) and no unsaved work
you care about on the map's undo history: the test walks that history back
and forth. It drives the server's tool functions in-process, builds a few
throwaway elements away from the map's centre, prints PASS/FAIL per check and
removes what it made. The map file is never saved.

The user's side is played by Dungeondraft's own toolbar: ui_action presses
its Undo and Redo arrows, which is what Ctrl+Z and Ctrl+Y do. What cannot be
played from here is an edit made by hand (anything done through the bridge is
the assistant's), so "undo leaves the user's step alone" is covered offline
by tools/test_mod_history.py instead.

    python test_history.py
"""

import sys
import time

import dungeondraft_mcp.server as s
from dungeondraft_mcp.bridge_client import BridgeError

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  <- {str(detail)[:400]}"))
    if cond:
        passed += 1
    else:
        failed += 1
    return cond


def exists(element_id) -> bool:
    try:
        s.get_element(element_id)
        return True
    except BridgeError:
        return False


def toolbar_button(text):
    """The target of Dungeondraft's own Undo / Redo toolbar arrow, or None."""
    for control in s.ui_tree("Editor", search=text.lower()).get("controls", []):
        if control.get("class") == "Button" and control.get("text") == text and control.get("visible"):
            return control["target"]
    return None


def press(target, done):
    """Press a toolbar arrow and wait for the step to show.

    Dungeondraft ignores an arrow pressed within three frames of the last
    history move (its own rule, the same for a fast Ctrl+Z then Ctrl+Y), so a
    press that shows no effect is repeated once Dungeondraft has had time.
    """
    for _ in range(3):
        try:
            s.ui_action(target, "press")
        except BridgeError:
            pass   # the arrow is disabled: the earlier press landed after all
        deadline = time.time() + 2.0
        while time.time() < deadline:
            time.sleep(0.1)
            if done():
                return True
    return False


def view(label):
    return next(v for v in s.list_checkpoints()["checkpoints"] if v["label"] == label)


def main() -> int:
    pg = s.ping()
    print(f"bridge protocol {pg['protocol']}, history: {pg.get('history')}")
    st = s.get_status()
    if not st.get("map_open"):
        print("no map open")
        return 1
    if pg.get("paused"):
        print("AI edits are paused in Dungeondraft's MCP Bridge panel: resume them and run again")
        return 1
    shared = pg.get("history") == "dungeondraft"
    check("ping and get_status agree on the history mode", st.get("history") == pg.get("history")
          and pg.get("history") in ("dungeondraft", "bridge"), (pg, st))
    if not shared:
        print("  NOTE  this Dungeondraft keeps the bridge's undo separate (fallback): the shared-timeline checks are skipped")
    cx, cy = st["map_center"]
    asset = s.list_assets("Objects", limit=1)["assets"][0]
    made = []

    # ---- one step, both directions ----
    a = s.place_object(asset, cx + 2000, cy - 2000)["id"]
    made.append(a)
    u = s.undo()
    check("undo answers with who and what", u.get("undone") is True and u.get("by") == "assistant"
          and u.get("kind") == "create_saved" and u.get("redo_depth", 0) >= 1, u)
    check("the object is gone", not exists(a))
    r = s.redo()
    check("redo brings it back with its id", r.get("redone") is True and exists(a), r)

    # ---- every request lands, and the ends are refused cleanly ----
    b = s.place_object(asset, cx + 2300, cy - 2000)["id"]
    c = s.place_object(asset, cx + 2600, cy - 2000)["id"]
    made += [b, c]
    undone = [s.undo(), s.undo(), s.undo()]
    check("three undos in a row all land", all(x.get("undone") for x in undone)
          and not exists(a) and not exists(b) and not exists(c), undone)
    redone = [s.redo(), s.redo(), s.redo()]
    check("three redos in a row all land", all(x.get("redone") for x in redone)
          and exists(a) and exists(b) and exists(c), redone)
    top = s.redo()
    check("redo at the top is refused, not attempted", top.get("redone") is False and top.get("reason") == "nothing to redo", top)
    u = s.undo()
    check("history still works after that", u.get("undone") is True and not exists(c), u)
    s.redo()

    # ---- Dungeondraft's own Undo / Redo reach the assistant's steps ----
    if shared:
        undo_arrow, redo_arrow = toolbar_button("Undo"), toolbar_button("Redo")
        if undo_arrow and redo_arrow:
            check("Dungeondraft's Undo takes the assistant's step back", press(undo_arrow, lambda: not exists(c)))
            check("Dungeondraft's Redo brings it back", press(redo_arrow, lambda: exists(c)))
        else:
            print("  SKIP  toolbar arrows not found (a translated interface names them differently)")

    # ---- commands Dungeondraft records by itself are still one step each ----
    cp = s.checkpoint("history-test")
    check("checkpoint reports the history it marks", cp.get("history") == pg.get("history")
          and cp.get("history_limit", 0) >= 1, cp)
    tile = s.list_assets("Simple Tiles", limit=1)["assets"][0]
    floor = s.place_pattern(tile, rect=[cx + 2000, cy - 1500, 512, 512], category="Simple Tiles")["id"]
    portal = s.add_portal(s.list_assets("Portals", limit=1)["assets"][0], cx + 2800, cy - 1300, mount="free")["id"]
    s.draw_water(rect=[cx + 2000, cy - 800, 512, 384])
    check("a pattern floor, a free portal and water are three steps", view("history-test")["steps_since"] == 3, view("history-test"))

    # ---- a batch and a room are one step each ----
    out = s.batch([
        {"tool": "place_object", "args": {"asset": asset, "x": cx + 2000, "y": cy + 400}},
        {"tool": "place_pattern", "args": {"asset": tile, "rect": [cx + 2300, cy + 300, 256, 256], "category": "Simple Tiles"}},
        {"tool": "add_light", "args": {"x": cx + 2100, "y": cy + 400}},
    ])
    batch_ids = [entry["result"]["id"] for entry in out["results"]]
    room = s.build_room(rect=[cx + 2000, cy + 800, 768, 768], floor_asset=tile)
    v = view("history-test")
    check("a batch and a room add one step each", out["failed"] == 0 and v["steps_since"] == 5
          and v["user_steps"] == 0 and v["reachable"], (out, v))
    u = s.undo()
    check("undo takes the whole room", u.get("kind") == "group" and u.get("label") == "build_room"
          and u.get("steps") == 2 and not exists(room["wall_id"]), u)
    u = s.undo()
    check("undo takes the whole batch", u.get("kind") == "group" and u.get("label") == "batch"
          and u.get("steps") == 3 and not any(exists(i) for i in batch_ids), u)
    s.redo()
    s.redo()

    # ---- terrain steps stay on the level they were painted on ----
    where = [cx + 2600, cy + 300, 256, 256]
    plain = s.get_terrain(rect=where, samples=2)
    s.fill_region(slot=2, rect=where)
    painted = s.get_terrain(rect=where, samples=2)
    upper = s.add_level("history-test level")
    levels = s.list_levels()["levels"]
    upper_index = next(l["index"] for l in levels if l["id"] == upper["id"])
    s.set_level(upper_index)
    upper_plain = s.get_terrain(rect=where, samples=2)
    s.undo()   # while the other level is showing
    check("undo on another level leaves that level's terrain alone", s.get_terrain(rect=where, samples=2) == upper_plain)
    base_index = next(l["index"] for l in s.list_levels()["levels"] if l["id"] != upper["id"])
    s.set_level(base_index)
    check("and takes the paint back where it was made", painted != plain and s.get_terrain(rect=where, samples=2) == plain)

    # ---- rollback ----
    rolled = s.rollback_checkpoint("history-test")
    check("rollback undoes everything since the checkpoint", rolled.get("rolled_back") == 5
          and rolled.get("user_steps") == 0 and "stopped" not in rolled
          and not exists(floor) and not exists(portal) and not exists(room["wall_id"]), rolled)
    check("what was there before stays", exists(a) and exists(b) and exists(c))
    v = view("history-test")
    check("the checkpoint stays open", v["steps_since"] == 0 and v["reachable"], v)

    # ---- Dungeondraft's History is not for native_call ----
    try:
        s.native_call("Editor.History", "Undo", [])
        check("native_call cannot move the history", False)
    except BridgeError as exc:
        check("native_call cannot move the history", "History" in str(exc), exc)

    # ---- clean up: the level, then the three objects ----
    s.delete_level(upper_index)
    try:
        s.delete_elements([i for i in made if exists(i)])
    except BridgeError as exc:
        print("  NOTE  cleanup:", exc)
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
