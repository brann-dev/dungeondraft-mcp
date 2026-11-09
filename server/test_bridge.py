#!/usr/bin/env python3
"""Per-command validator for the Dungeondraft bridge — no MCP involved.

Run with Dungeondraft open (mod enabled, a map loaded). It exercises every
bridge command and prints PASS/FAIL per command, so you can see exactly what
works on your Dungeondraft version. It builds a small throwaway scene near the
map center (a room of walls with a door, an object, a light, a path, a patch of
water and of material) and then deletes or undoes what it created.

    python test_bridge.py
"""

import sys

from dungeondraft_mcp.bridge_client import BridgeClient, BridgeError

bridge = BridgeClient()
created_ids: list[int] = []
passed = 0
failed = 0


def check(label, fn):
    global passed, failed
    try:
        result = fn()
        print(f"  PASS  {label:18} -> {result}")
        passed += 1
        return result
    except BridgeError as exc:
        print(f"  FAIL  {label:18} -> {exc}")
        failed += 1
        return None


def first_asset(category, search=""):
    res = bridge.request("list_assets", category=category, search=search, limit=1)
    assets = res.get("assets") or []
    return assets[0] if assets else None


def main() -> int:
    try:
        status = bridge.request("ping")
    except BridgeError as exc:
        print("Cannot reach the bridge:", exc, file=sys.stderr)
        return 1
    print("Bridge reachable:", status, "\n")

    cx, cy = 6400, 6400
    st = check("get_status", lambda: bridge.request("get_status"))
    if st and st.get("map_center"):
        cx, cy = st["map_center"]

    check("list_categories", lambda: bridge.request("list_asset_categories"))

    obj_asset = first_asset("Objects")
    wall_asset = first_asset("Walls")
    path_asset = first_asset("Paths")

    # --- create ---
    obj = check("place_object", lambda: bridge.request("place_object", asset=obj_asset, x=cx, y=cy))
    if obj:
        created_ids.append(obj["id"])

    room = [[cx - 200, cy - 200], [cx + 200, cy - 200], [cx + 200, cy + 200], [cx - 200, cy + 200]]
    wall = check("draw_wall", lambda: bridge.request("draw_wall", points=room, asset=wall_asset or "", loop=True))
    if wall:
        created_ids.append(wall["id"])

    if path_asset:
        path = check("draw_path", lambda: bridge.request(
            "draw_path", points=[[cx - 300, cy], [cx, cy - 50], [cx + 300, cy]], asset=path_asset))
        if path:
            created_ids.append(path["id"])
    else:
        print("  SKIP  draw_path          -> no Paths assets")

    light = check("add_light", lambda: bridge.request("add_light", x=cx, y=cy, energy=1.5))
    if light:
        created_ids.append(light["id"])

    # --- query / modify ---
    check("list_elements", lambda: bridge.request("list_elements", kind="objects", limit=5))
    if obj:
        check("get_element", lambda: bridge.request("get_element", id=obj["id"]))
        check("move_element", lambda: bridge.request("move_element", id=obj["id"], x=cx + 80, y=cy + 80))
        check("modify_object", lambda: bridge.request("modify_object", id=obj["id"], scale=1.5, rotation=45))
        dup = check("duplicate_object", lambda: bridge.request("duplicate_object", id=obj["id"], dx=120))
        if dup:
            created_ids.append(dup["id"])

    # --- wall edit: a door mounted on the room must survive a reshape ---
    door_asset = first_asset("Portals", "door")
    if wall and door_asset:
        def wall_edit():
            door = bridge.request("add_portal", asset=door_asset, x=cx, y=cy - 200, radius=64)
            grown = [[cx - 300, cy - 300], [cx + 300, cy - 300], [cx + 300, cy + 200], [cx - 200, cy + 200]]
            res = bridge.request("modify_wall", id=wall["id"], points=grown)
            doors = [e for e in bridge.request("list_elements", kind="portals")["elements"]
                     if e["id"] == door["id"]]
            if res["portal_count"] != 1 or not doors:
                raise BridgeError("the mounted door did not survive the wall edit")
            x, y = doors[0]["position"]
            if abs(x - cx) > 1 or abs(y - (cy - 300)) > 1:
                raise BridgeError(f"door should have followed its segment to {[cx, cy - 300]}, is at {[x, y]}")
            return f"{len(res['points'])} points moved, door {door['id']} followed to {[x, y]}"

        check("modify_wall", wall_edit)
    else:
        print("  SKIP  modify_wall        -> no wall or no Portals assets")

    # --- water / material: draw a patch, then undo it (restores the layer exactly) ---
    def draw_and_undo(cmd, **params):
        res = bridge.request(cmd, **params)
        undone = bridge.request("undo")
        if undone.get("kind") != cmd.replace("draw_", ""):
            raise BridgeError(f"undo after {cmd} reverted {undone} instead")
        return res

    check("draw_water", lambda: draw_and_undo(
        "draw_water", rect=[cx - 768, cy + 384, 512, 384], deep_color="#204080"))
    material_asset = first_asset("Materials")
    if material_asset:
        check("draw_material", lambda: draw_and_undo(
            "draw_material", asset=material_asset, rect=[cx + 384, cy + 384, 384, 384]))
    else:
        print("  SKIP  draw_material      -> no Materials assets")

    check("list_levels", lambda: bridge.request("list_levels"))

    # --- layers ---
    layers = check("list_layers", lambda: bridge.request("list_layers"))
    if obj and layers:
        def relayer():
            res = bridge.request("set_element_layer", id=obj["id"], layer=100)
            now = bridge.request("get_element", id=obj["id"])["layer"]
            if now != 100:
                raise BridgeError(f"object is on layer {now}, expected 100")
            return res

        check("set_element_layer", relayer)

    # --- layout facts: footprints, one snapshot, a region set and removed again ---
    check("asset_footprint", lambda: bridge.request("asset_footprint", asset=obj_asset))

    def snapshot():
        snap = bridge.request("spatial_snapshot", asset=obj_asset)
        if obj and not any(o["id"] == obj["id"] for o in snap["objects"]):
            raise BridgeError("the placed object is missing from the snapshot")
        return {k: (len(v) if isinstance(v, list) else v) for k, v in snap.items() if k != "footprint"}

    check("spatial_snapshot", snapshot)

    def region_round_trip():
        name = "test_bridge region"
        bridge.request("set_spatial_region", name=name, kind="clearance", rect=[cx - 100, cy - 100, 200, 200])
        listed = [r["name"] for r in bridge.request("list_spatial_regions")["regions"]]
        left = [r["name"] for r in bridge.request("remove_spatial_region", name=name)["regions"]]
        if name not in listed or name in left:
            raise BridgeError(f"region not set and removed cleanly: listed={listed} left={left}")
        return f"set, listed and removed ({len(left)} other region(s) untouched)"

    check("spatial regions", region_round_trip)

    # --- native access: read-only probes of DD's own objects and UI ---
    check("native_targets", lambda: f"{len(bridge.request('native_targets')['targets'])} targets")

    def describe_terrain():
        res = bridge.request("native_describe", target="Level.Terrain", search="splat")
        names = [m["name"] for m in res["methods"]]
        if "RestoreSplat" not in names:
            raise BridgeError(f"Terrain.RestoreSplat not found in {names}")
        return f"{res['class']}: {names}"

    check("native_describe", describe_terrain)
    check("native_get", lambda: bridge.request(
        "native_get", target="Level.Terrain", properties=["ExpandedSlots", "SmoothBlending", "visible"]))
    check("ui_tree", lambda: f"{bridge.request('ui_tree', target='Tool:TerrainBrush')['matched']} controls in the Terrain Brush panel")
    if created_ids:
        check("select_elements", lambda: bridge.request("select_elements", ids=created_ids))
        check("clear_selection", lambda: bridge.request("clear_selection"))

    # --- undo / redo round trip: place an object, undo it, redo it ---
    def undo_round_trip():
        before = bridge.request("get_status")["counts"]["objects"]
        made = bridge.request("place_object", asset=obj_asset, x=cx - 200, y=cy)
        after_create = bridge.request("get_status")["counts"]["objects"]
        bridge.request("undo")
        after_undo = bridge.request("get_status")["counts"]["objects"]
        bridge.request("redo")
        after_redo = bridge.request("get_status")["counts"]["objects"]
        created_ids.append(made["id"])   # redo rebuilds the object under the same id
        if not (after_create == before + 1 and after_undo == before and after_redo == before + 1):
            raise BridgeError(f"counts before={before} create={after_create} undo={after_undo} redo={after_redo}")
        return f"object count {before}->{after_create} undo->{after_undo} redo->{after_redo}"

    check("undo/redo", undo_round_trip)

    # --- cleanup: delete everything we made ---
    deleted = 0
    for eid in created_ids:
        try:
            if bridge.request("delete_element", id=eid).get("deleted"):
                deleted += 1
        except BridgeError:
            pass
    print(f"\nCleaned up {deleted}/{len(created_ids)} created elements.")
    print(f"\n{passed} passed, {failed} failed.")
    print("(Terrain / image import / roof / text / level commands and the native write calls "
          "aren't auto-tested here to avoid messing up your map — try them by hand once the basics pass.)")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
