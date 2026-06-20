#!/usr/bin/env python3
"""Build a small scene on the open map and LEAVE it in place.

Unlike test_bridge.py (which cleans up after itself), this constructs a visible
little room so you can eyeball the result, and it exercises the commands the
validator skips: fill_terrain, add_portal, add_roof, add_text. Run on a scratch
map — it does not delete anything.

    python demo_build.py
"""

import sys

from dungeondraft_mcp.bridge_client import BridgeClient, BridgeError

bridge = BridgeClient()


def find_asset(category, *searches):
    """First asset matching any search term, else the first asset in the category."""
    for term in searches:
        res = bridge.request("list_assets", category=category, search=term, limit=1)
        if res.get("assets"):
            return res["assets"][0]
    res = bridge.request("list_assets", category=category, limit=1)
    return res["assets"][0] if res.get("assets") else None


def step(label, fn):
    try:
        print(f"  ok    {label:16} -> {fn()}")
    except BridgeError as exc:
        print(f"  FAIL  {label:16} -> {exc}")


def main() -> int:
    try:
        status = bridge.request("get_status")
    except BridgeError as exc:
        print("Cannot reach the bridge:", exc, file=sys.stderr)
        return 1
    if not status.get("map_open"):
        print("Open a map in Dungeondraft first.", file=sys.stderr)
        return 1

    cx, cy = status["map_center"]
    h = 512  # half-size of the room in woxels

    grass = find_asset("Terrain", "grass", "dirt", "floor")
    wall = find_asset("Walls", "stone", "wood", "brick")
    door = find_asset("Portals", "door", "wood")
    roof = find_asset("Roofs", "tile", "thatch", "shingle")
    table = find_asset("Objects", "table")
    chair = find_asset("Objects", "chair", "stool")
    print("Assets:", {"grass": grass, "wall": wall, "door": door, "roof": roof,
                       "table": table, "chair": chair}, "\n")

    if grass:
        step("fill_terrain", lambda: bridge.request("fill_terrain", slot=0, asset=grass))

    room = [[cx - h, cy - h], [cx + h, cy - h], [cx + h, cy + h], [cx - h, cy + h]]
    step("draw_wall", lambda: bridge.request("draw_wall", points=room, asset=wall or "", loop=True))

    if door:
        # A door centered on the south wall.
        step("add_portal", lambda: bridge.request(
            "add_portal", asset=door, x=cx, y=cy + h, radius=64, rotation=0))

    if table:
        step("place table", lambda: bridge.request("place_object", asset=table, x=cx, y=cy))
    if chair:
        step("place chair", lambda: bridge.request("place_object", asset=chair, x=cx + 120, y=cy))

    step("add_light", lambda: bridge.request("add_light", x=cx, y=cy - 150, energy=1.5, range=1.5))

    if roof:
        # Off to the side so it doesn't hide the room.
        rx = cx + h * 3
        rpoly = [[rx - h, cy - h], [rx + h, cy - h], [rx + h, cy + h], [rx - h, cy + h]]
        step("add_roof", lambda: bridge.request("add_roof", points=rpoly, asset=roof, width=256, type=0))

    step("add_text", lambda: bridge.request(
        "add_text", text="Built by Claude", x=cx, y=cy - h - 120, size=48))

    print("\nDone. Look at the map around its center — pan to it if needed.")
    print("Counts now:", bridge.request("get_status")["counts"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
