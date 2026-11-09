#!/usr/bin/env python3
"""Live validator for the history, arrangement, analysis, pack, import and
generator tools: the features added in 0.3.0. No MCP client involved.

Run with Dungeondraft open (mod enabled, a map loaded). It drives the server's
tool functions in-process, builds a small throwaway scene away from the map's
centre, prints PASS/FAIL per check, and deletes or undoes what it created. The
map file is never saved unless DD_MCP_TEST_CROP=1 (a backup is kept). Optional: pass two saved map files to exercise
inspect_map_file's comparison; otherwise that check uses the open map's own file.

    python test_features.py [map_a.dungeondraft_map map_b.dungeondraft_map]
"""

import json
import os
import sys

import dungeondraft_mcp.server as s

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


def main() -> int:
    print(f"bridge protocol {s.ping()['protocol']}")
    st = s.get_status()
    if not st.get("map_open"):
        print("no map open"); return 1
    cx, cy = st["map_center"]
    current = s.get_current_map().get("path") or ""
    MAP_A = sys.argv[1] if len(sys.argv) > 2 else current
    MAP_B = sys.argv[2] if len(sys.argv) > 2 else current

    # ---- token ----
    import json
    import socket

    def raw(**req):
        with socket.create_connection((s.HOST, s.PORT), timeout=5) as sock:
            sock.sendall((json.dumps(req) + "\n").encode()); buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf.split(b"\n")[0])
    pg = s.ping()
    check("server is authenticated", pg.get("authenticated") is True, pg)
    if pg.get("auth") == "token":
        bare = raw(cmd="ping")
        check("ping without a token answers unauthenticated", bare["ok"] and bare["result"]["authenticated"] is False, bare)
        wrong = raw(cmd="get_status", token="0" * 64)
        check("a wrong token is refused", not wrong["ok"] and wrong["error"].startswith("bad token"), wrong)
    guarded = []
    for target in ("Tool:mcp_bridge", "Editor.Tools.mcp_bridge", "Editor.Tools.mcp_bridge.ScriptInstance"):
        try:
            s.native_describe(target); guarded.append(None)
        except Exception as exc:
            guarded.append(str(exc))
    check("the bridge's own panel and script are unreachable", all(g is not None for g in guarded), guarded)
    if pg.get("paused"):
        print("AI edits are paused in Dungeondraft's MCP Bridge panel: resume them and run again")
        return 1

    # ---- history ----


    cx, cy = st["map_center"]
    objs = s.list_assets("Objects", "barrel", 5)["assets"]
    asset = objs[0]
    print("asset", asset)
    start_ids = {e["id"] for e in s.list_elements("objects", 1000)["elements"]}
    n0 = len(start_ids)

    # 1. undoable delete of a prop
    a = s.place_object(asset, cx, cy)["id"]
    d = s.delete_element(a)
    check("delete_element reports undoable", d.get("undoable") is True, d)
    gone = any(e["id"] == a for e in s.list_elements("objects", 500)["elements"])
    check("deleted prop is gone", not gone)
    u = s.undo()
    back = any(e["id"] == a for e in s.list_elements("objects", 500)["elements"])
    check("undo restores the prop with its id", back and u.get("kind") == "delete_saved", (u, back))
    r = s.redo()
    gone2 = not any(e["id"] == a for e in s.list_elements("objects", 500)["elements"])
    check("redo deletes it again", gone2 and r.get("redone"), (r, gone2))
    s.undo()   # keep it for the wall test below; clean later

    # 2. undoable delete of a wall (non-prop) with a mounted door
    w = s.draw_wall([[cx-600, cy-600], [cx+600, cy-600], [cx+600, cy+600], [cx-600, cy+600]], loop=True)["id"]
    door = s.list_assets("Portals", "door", 1)["assets"][0]
    p = s.add_portal(door, x=cx, y=cy-600)
    pid = p["id"]
    dw = s.delete_element(w)
    check("wall delete undoable", dw.get("undoable") is True, dw)
    walls = [e["id"] for e in s.list_elements("walls", 500)["elements"]]
    check("wall gone", w not in walls)
    uw = s.undo()
    walls = [e["id"] for e in s.list_elements("walls", 500)["elements"]]
    check("wall back after undo", w in walls and uw.get("kind") == "delete_saved", (uw, w in walls))
    el = s.get_element(w)
    check("wall keeps its portal", el.get("portal_count", 0) >= 1, el)
    # a wall created, given a door, then undone past its creation and redone keeps the door
    w2 = s.draw_wall([[cx+1600, cy-1600], [cx+2400, cy-1600], [cx+2400, cy-1000]], loop=False)["id"]
    s.add_portal(door, x=cx+2000, y=cy-1600)
    s.undo()   # door
    s.undo()   # wall
    walls = [e["id"] for e in s.list_elements("walls", 500)["elements"]]
    check("wall create undone", w2 not in walls)
    s.redo()   # wall
    s.redo()   # door
    el2 = s.get_element(w2)
    check("wall create redone with its door", el2.get("portal_count", 0) == 1, el2)
    s.delete_element(w2)

    # 3. delete_elements as one step
    b = s.place_object(asset, cx+300, cy)["id"]
    c = s.place_object(asset, cx-300, cy)["id"]
    de = s.delete_elements([b, c])
    check("delete_elements deleted 2", de.get("deleted") == 2, de)
    bad = None
    try:
        s.delete_elements([b, 99999999])
    except Exception as exc:
        bad = str(exc)
    check("delete_elements refuses unknown id", bad is not None and "99999999" in bad, bad)
    ug = s.undo()
    ids = [e["id"] for e in s.list_elements("objects", 500)["elements"]]
    check("one undo restores both", b in ids and c in ids and ug.get("kind") == "group" and ug.get("steps") == 2, (ug,))

    # 4. batch single undo
    bt = s.batch([{"tool": "place_object", "args": {"asset": asset, "x": cx+500, "y": cy+500}},
                  {"tool": "add_light", "args": {"x": cx+500, "y": cy+500, "range": 2}}])
    check("batch ran 2", bt["ran"] == 2 and bt["failed"] == 0, bt)
    ub = s.undo()
    check("batch is one undo step", ub.get("kind") == "group" and ub.get("steps") == 2 and ub.get("label") == "batch", ub)
    light_ids = [e["id"] for e in s.list_elements("lights", 500)["elements"]]
    check("batch light undone", bt["results"][1]["result"]["id"] not in light_ids)
    rb = s.redo()
    check("batch redo", rb.get("redone") and rb.get("steps") == 2, rb)
    s.undo()

    # 5. build_room one undo step
    tile = s.list_assets("Simple Tiles", "", 1)["assets"][0]
    room = s.build_room(rect=[cx+800, cy+800, 768, 512], floor_asset=tile)
    ur = s.undo()
    check("build_room is one undo step", ur.get("kind") == "group" and ur.get("label") == "build_room" and ur.get("steps") == 2, (room, ur))
    walls = [e["id"] for e in s.list_elements("walls", 500)["elements"]]
    check("room wall undone", room["wall_id"] not in walls)
    rr = s.redo()
    walls = [e["id"] for e in s.list_elements("walls", 500)["elements"]]
    check("room redo brings the wall back", room["wall_id"] in walls, rr)
    s.undo()

    # 6. scatter one step
    sc = s.scatter_objects([asset], rect=[cx-1500, cy-1500, 600, 600], count=5, seed=3, allow_overlap=True)
    us = s.undo()
    check("scatter is one undo step", us.get("kind") in ("group", "create_saved") and us.get("steps", 1) == sc["placed"], (sc, us))

    # 7. checkpoint / rollback
    cp = s.checkpoint("test")
    x1 = s.place_object(asset, cx-700, cy-700)["id"]
    x2 = s.place_object(asset, cx-700, cy+700)["id"]
    s.move_elements([x1, x2], 50, 0)
    lc = s.list_checkpoints()
    check("checkpoint sees 3 steps", lc["checkpoints"][0]["steps_since"] == 3 and lc["checkpoints"][0]["reachable"], lc)
    rbk = s.rollback_checkpoint("test")
    ids = [e["id"] for e in s.list_elements("objects", 500)["elements"]]
    check("rollback undid 3 and removed the objects", rbk["rolled_back"] == 3 and x1 not in ids and x2 not in ids, rbk)

    # 8. place_objects
    po = s.place_objects([{"asset": asset, "x": cx+1200, "y": cy}, {"asset": asset, "x": cx+1400, "y": cy}])
    check("place_objects placed 2", po["ran"] == 2 and po["failed"] == 0, po)
    upo = s.undo()
    check("place_objects one undo", upo.get("kind") == "group" and upo.get("steps") == 2, upo)

    # 9. delete_level
    lv = s.add_level("tmp level")
    levels = s.list_levels()
    idx = [l["index"] for l in levels["levels"] if l["id"] == lv["id"]][0]
    dl = s.delete_level(idx)
    check("delete_level removed it", dl.get("deleted_id") == lv["id"] and all(l["id"] != lv["id"] for l in dl["levels"]), dl)
    only = None
    try:
        if len(s.list_levels()["levels"]) == 1:
            s.delete_level(0)
    except Exception as exc:
        only = str(exc)
    check("refuses to delete the only level", only is None or "only level" in only, only)

    # cleanup: remove everything this run added
    extra = [e["id"] for e in s.list_elements("objects", 1000)["elements"] if e["id"] not in start_ids]
    if extra: s.delete_elements(extra)
    walls_now = [e["id"] for e in s.list_elements("walls", 1000)["elements"]]
    if w in walls_now: s.delete_element(w)
    n1 = len(s.list_elements("objects", 1000)["elements"])
    check("object count back to start", n1 == n0, (n0, n1))
    # ---- features ----


    start_ids = {e["id"] for e in s.list_elements("objects", 1000)["elements"]}
    start_walls = {e["id"] for e in s.list_elements("walls", 1000)["elements"]}

    # 1. terrain read-back
    t = s.get_terrain(samples=8)
    check("get_terrain returns 64 samples with slots", len(t["samples"]) == 64 and len(t["slots"]) in (4, 8), {k: v for k, v in t.items() if k != "samples"})
    w0 = t["samples"][0]["weights"]
    check("weights sum to ~1", abs(sum(w0) - 1) < 0.05, w0)

    # 2. floorplan on a fresh two-room build with a door between
    door = s.list_assets("Portals", "door", 1)["assets"][0]
    tile = s.list_assets("Simple Tiles", "", 1)["assets"][0]
    ox, oy = cx + 3000, cy + 3000
    r1 = s.build_room(rect=[ox, oy, 1024, 1024], floor_asset=tile)
    r2 = s.build_room(rect=[ox + 1024, oy, 1024, 1024], floor_asset=tile)
    check("second room shares the first's wall", r2.get("shared_wall_ids") == [r1["wall_id"]] and len(r2.get("wall_ids", [])) >= 1, r2)
    p1 = s.add_portal(door, x=ox + 1024, y=oy + 512)       # door in the shared wall
    p2 = s.add_portal(door, x=ox + 512, y=oy)              # entrance from outside into room 1
    fp = s.validate_floorplan()
    rooms = fp["rooms"]
    mine = [r for r in rooms if r["bounds"][0] >= ox - 64 and r["bounds"][1] >= oy - 64 and r["bounds"][2] <= ox + 2048 + 64 and r["bounds"][3] <= oy + 1024 + 64]
    check("floorplan finds my 2 rooms", len(mine) == 2, {k: fp[k] for k in ("rooms", "findings", "notes")})
    check("both rooms reachable from outside", all(r.get("reachable_from_outside") for r in rooms), rooms)
    check("no floorplan findings", fp["ok"] and not fp["findings"], fp["findings"])
    # seal room 2's door and check again
    s.delete_element(p1["id"])
    fp2 = s.validate_floorplan()
    codes = [f["code"] for f in fp2["findings"]]
    check("sealed room is reported", "sealed_room" in codes or "unreachable_room" in codes, fp2["findings"])
    s.undo()   # door back

    # 3. scene checks: a light with no cause, then a candle next to it
    lid = s.add_light(x=ox + 300, y=oy + 300, range=1.5)["id"]
    sc = s.validate_scene(samples=16)
    unexplained = [l["id"] for l in sc["light_causes"]["unexplained"]]
    check("light without a source is flagged", lid in unexplained, sc["light_causes"])
    candle = s.list_assets("Objects", "candle", 1)["assets"]
    if candle:
        cid = s.place_object(candle[0], ox + 320, oy + 300, layer=100)["id"]
        sc2 = s.validate_scene(samples=16)
        check("candle explains the light", lid not in [l["id"] for l in sc2["light_causes"]["unexplained"]], sc2["light_causes"])
        s.delete_element(cid)
    check("bare_ground reports a share", 0 <= sc["bare_ground"]["bare_share"] <= 1, sc["bare_ground"])

    # 4. arrange: chairs around a table (dry run then real), a shelf row along a wall
    table = s.list_assets("Objects", "round_table", 1)["assets"] or s.list_assets("Objects", "table", 1)["assets"]
    chair = s.list_assets("Objects", "chair", 1)["assets"]
    tid = s.place_object(table[0], ox + 512, oy + 512)["id"]
    dry = s.arrange_objects("around_target", [chair[0]], count=4, target_id=tid, dry_run=True, seed=1)
    check("around_target dry run plans 4 seats", len(dry["placements"]) == 4 and dry["placed"] == 0, dry)
    real = s.arrange_objects("around_target", [chair[0]], count=4, target_id=tid, seed=1)
    check("around_target placed 4", real["placed"] == 4, real.get("refused"))
    u = s.undo()
    check("arrangement is one undo step", u.get("kind") == "group" and u.get("steps") == 4, u)
    row = s.arrange_objects("along_wall", [chair[0]], along=[[ox, oy + 1024], [ox + 1024, oy + 1024]], toward=[ox + 512, oy + 512], count=3)
    check("along_wall placed a row of 3", row["placed"] == 3, (row.get("refused"), row.get("notes")))
    s.undo()
    s.delete_element(tid)

    # 5. asset packs (live) + offline readers
    lp = s.list_asset_packs()
    check("list_asset_packs answers", "installed" in lp and "included_in_this_map" in lp, lp)
    insp = s.inspect_map_file(MAP_A,
                              compare_to=MAP_B)
    check("inspect_map_file summarises and compares", isinstance(insp.get("size_tiles"), list) and "comparison" in insp, list(insp.keys()))

    # 6. UVTT import: dry run on a real export, then a confirmed import on a throwaway map region check
    export = os.environ.get("DD_MCP_TEST_UVTT", "")   # a .dd2vtt export of a map the open map's size
    if export and os.path.exists(export):
        plan = s.import_dd2vtt(export)
        check("import_dd2vtt dry run plans walls", plan["confirm"] is False and plan["summary"]["walls"] > 0, plan.get("notes"))
        print("  plan summary:", json.dumps(plan["summary"]))
        if not plan.get("needs_resize"):
            done = s.import_dd2vtt(export, confirm=True, trace=False)
            check("import_dd2vtt built walls and portals", len(done["made"]["walls"]) > 0 and len(done["made"]["portals"]) > 0, done["made"].get("skipped"))
            uu = s.undo()
            check("import is one undo step", uu.get("kind") == "group" and uu.get("label") == "import_dd2vtt", uu)
        else:
            print("  (export size differs from the map; confirmed import skipped)", plan["map_size_tiles"])
    else:
        print("  (set DD_MCP_TEST_UVTT to a .dd2vtt file to exercise import_dd2vtt)")

    # cleanup
    for pid in (p1["id"], p2["id"]):
        try: s.delete_element(pid)
        except Exception: pass
    s.delete_element(lid)
    extra_walls = [i for i in (e["id"] for e in s.list_elements("walls", 1000)["elements"]) if i not in start_walls]
    if extra_walls: s.delete_elements(extra_walls)
    extra = [e["id"] for e in s.list_elements("objects", 1000)["elements"] if e["id"] not in start_ids]
    if extra: s.delete_elements(extra)
    check("objects back to start", len(s.list_elements("objects", 1000)["elements"]) == len(start_ids))
    check("walls back to start", {e["id"] for e in s.list_elements("walls", 1000)["elements"]} == start_walls)
    # ---- map size and snapping ----
    size0 = s.get_status()["map_size_woxels"]
    tiles0 = [int(size0[0] // 256), int(size0[1] // 256)]
    if tiles0[0] < 128 and tiles0[1] < 128:
        grown = s.set_map_size(tiles0[0] + 2, tiles0[1] + 2)
        check("set_map_size grew the map", grown.get("changed") and s.get_status()["map_size_woxels"] == [size0[0] + 512, size0[1] + 512], grown)
        back = s.set_map_size(tiles0[0], tiles0[1])
        check("set_map_size shrank it back", back.get("changed") and s.get_status()["map_size_woxels"] == size0, back)
        t2 = s.get_terrain(samples=4)
        check("terrain still reads after resizing", len(t2["samples"]) == 16, t2.get("slots"))
    snap = s.get_snap_settings(points=[[cx + 37, cy + 91]])
    if not snap.get("mod_loaded"):
        wall_step = s.bridge.request("get_snap_settings", points=[[cx + 37, cy + 91]], **{"for": "draw_wall"})["snapped"][0]
        full = 256.0 if not snap.get("half_grid_snap") else 128.0
        check("walls snap to Dungeondraft's whole-tile grid unless half-grid snap is on",
              wall_step[0] % full == 0 and wall_step[1] % full == 0 and snap["snapped"][0][0] % 128 == 0, (wall_step, snap["snapped"]))
    checked = s.place_object(asset, cx + 37, cy + 91, snap=True, check=True, allow_overlap=True)
    cpos = s.get_element(checked["id"])["position"]
    check("place_object(snap=True, check=True) lands on the grid", [round(v, 2) for v in cpos] == [round(v, 2) for v in snap["snapped"][0]], cpos)
    s.delete_element(checked["id"])
    check("get_snap_settings answers", "vanilla_snapping" in snap and len(snap.get("snapped", [])) == 1, snap)
    print("  snap:", {k: v for k, v in snap.items() if k != "snapped"})
    placed = s.place_object(asset, cx + 37, cy + 91, snap=True)
    pos = s.get_element(placed["id"])["position"]
    check("place_object(snap=True) lands where get_snap_settings says", [round(v, 2) for v in pos] == [round(v, 2) for v in snap["snapped"][0]], (pos, snap["snapped"]))
    s.delete_element(placed["id"])

    # ---- crop (opt-in: it SAVES the map and leaves a backup beside it) ----
    if os.environ.get("DD_MCP_TEST_CROP") == "1":
        size_c = s.get_status()["map_size_woxels"]
        tw, th = int(size_c[0] // 256), int(size_c[1] // 256)
        if tw >= 12 and th >= 12:
            far = s.place_object(asset, 300, 300)["id"]
            prev = s.crop_map([2, 2, tw - 4, th - 4])
            check("crop preview lists an object outside the window", far in prev["outside"].get("objects", []) and prev["changed"] is False, prev)
            done = s.crop_map([2, 2, tw - 4, th - 4], confirm=True)
            check("crop resized, deleted the outside object and left a backup",
                  done["changed"] and os.path.exists(done["backup"]) and s.get_status()["map_size_woxels"] == [(tw - 4) * 256, (th - 4) * 256], done)
            print("  backup:", done["backup"])
    else:
        print("  (set DD_MCP_TEST_CROP=1 to exercise crop_map; it saves the map and writes a backup)")

    # ---- preview sheet ----
    import json as _json
    sheet = s.preview_assets([asset, asset], columns=2, cell_px=96)
    cap = _json.loads(sheet[1])
    check("preview_assets returns an image and a caption with sizes", hasattr(sheet[0], "data") and len(cap["cells"]) == 2 and cap["cells"][0]["size_px"], cap)

    # ---- more ----



    # lights: range round trip in Dungeondraft's units
    l = s.add_light(x=cx + 2500, y=cy, range=3.0)
    check("add_light reports range 3", abs(l.get("range", 0) - 3.0) < 0.01, l)
    el = s.get_element(l["id"])
    check("get_element reads range 3", abs(el["range"] - 3.0) < 0.01, el)
    m = s.modify_light(l["id"], range=5.0)
    check("modify_light range 5", abs(m["range"] - 5.0) < 0.01, m)
    s.undo()
    check("undo restores range 3", abs(s.get_element(l["id"])["range"] - 3.0) < 0.01)
    s.delete_element(l["id"])

    # map style
    ms = s.get_map_style()
    check("get_map_style lists options", "grid_style_options" in ms and len(ms["building_wear_options"]) >= 2, ms)
    other = [o for o in ms["building_wear_options"] if o["name"] != ms["building_wear"]][0]
    set1 = s.set_map_style(building_wear=other["name"], grid_style="dotted")
    check("set_map_style applied", set1["building_wear"] == other["name"] and set1["grid_style"] == "dotted", set1)
    u = s.undo()
    after = s.get_map_style()
    check("map style undone", u.get("kind") == "map_style" and after["building_wear"] == ms["building_wear"] and after["grid_style"] == ms["grid_style"], (u, after))

    # prefabs
    lp = s.list_prefabs()
    check("list_prefabs answers", "sets" in lp and "prefabs" in lp, lp)
    print("  sets:", lp.get("sets"), "prefabs in", lp.get("set"), ":", lp.get("prefabs")[:5])
    if lp.get("prefabs"):
        pp = s.place_prefab(lp["prefabs"][0], x=cx + 2800, y=cy + 2800)
        check("place_prefab placed something", pp.get("count", 0) > 0, pp)
        u2 = s.undo()
        check("prefab is one undo step", u2.get("undone") and (u2.get("kind") in ("group", "create_saved", "create")), u2)
    else:
        print("  (no prefabs saved on this machine; place_prefab not exercised)")

    # generator on a scratch level
    go = s.generator_options()
    check("generator_options reports dials", "dials" in go and "boundary" in go["dials"], go)
    lv = s.add_level("gen test")
    idx = [l["index"] for l in s.list_levels()["levels"] if l["id"] == lv["id"]][0]
    s.set_level(idx)
    g = s.generate_dungeon("Dungeon", density=go["dials"]["density"]["min"])
    check("generate_dungeon added walls", g["counts_after"].get("walls", 0) > g["counts_before"].get("walls", 0), g)
    print("  counts:", g["counts_before"], "->", g["counts_after"])
    idx = [l["index"] for l in s.list_levels()["levels"] if l["id"] == lv["id"]][0]
    s.delete_level(idx)
    check("scratch level removed", all(l["id"] != lv["id"] for l in s.list_levels()["levels"]))
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
