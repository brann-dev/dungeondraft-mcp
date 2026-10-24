#!/usr/bin/env python3
"""Live check of the map-lifecycle bridge commands — no MCP involved.

Run with Dungeondraft open (mod enabled) and a saved map loaded. It exercises
get_current_map, save_map and open_map, switches to a second saved map and back,
and prints PASS/FAIL per check.

    python test_lifecycle.py <currently open map> <second saved map>

Both maps must be throwaway files: the first gets test objects placed on it and
is saved over (they stay), and the second gets an object placed on it that is
then discarded by switching away. At the end it asks you whether Dungeondraft
showed a dialog during the save or the switch (when run without a terminal it
prints a reminder instead).
"""

import os
import sys
import time

from dungeondraft_mcp.bridge_client import BridgeClient, BridgeError

bridge = BridgeClient(timeout=15.0)
passed = 0
failed = 0

POLL_INTERVAL = 0.5
SWITCH_TIMEOUT = 30.0
SAVE_TIMEOUT = 10.0


def check(label, fn):
    global passed, failed
    try:
        result = fn()
        print(f"  PASS  {label:24} -> {result}")
        passed += 1
        return result
    except BridgeError as exc:
        print(f"  FAIL  {label:24} -> {exc}")
        failed += 1
        return None


def first_asset(category, search=""):
    res = bridge.request("list_assets", category=category, search=search, limit=1)
    assets = res.get("assets") or []
    return assets[0] if assets else None


def place_at_center(dx=0):
    """Place the first Objects asset near the map center (an edit to save or discard)."""
    st = bridge.request("get_status")
    cx, cy = st.get("map_center") or (6400, 6400)
    asset = first_asset("Objects")
    if not asset:
        raise BridgeError("no Objects assets")
    return bridge.request("place_object", asset=asset, x=cx + dx, y=cy)


def same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def wait_for_map(target):
    """Poll get_current_map until its path names `target`, retrying connection errors."""
    deadline = time.monotonic() + SWITCH_TIMEOUT
    last = None
    start = time.monotonic()
    while time.monotonic() < deadline:
        try:
            last = bridge.request("get_current_map")
            if last.get("map_open") and same_path(last.get("path", ""), target):
                return f"loaded after {time.monotonic() - start:.1f}s: {last}"
        except BridgeError as exc:
            last = f"error: {exc}"
        time.sleep(POLL_INTERVAL)
    raise BridgeError(f"{target} not loaded after {SWITCH_TIMEOUT:.0f}s; last: {last}")


def main() -> int:
    global passed, failed
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    first_map, second_map = sys.argv[1], sys.argv[2]
    for p in (first_map, second_map):
        if not os.path.isfile(p):
            print("Map file not found:", p, file=sys.stderr)
            return 2

    try:
        status = bridge.request("ping")
    except BridgeError as exc:
        print("Cannot reach the bridge:", exc, file=sys.stderr)
        return 1
    print("Bridge reachable:", status, "\n")

    # --- get_current_map ---
    def current_map():
        res = bridge.request("get_current_map")
        missing = [k for k in ("map_open", "path", "dirty", "saving") if k not in res]
        if missing:
            raise BridgeError(f"missing keys {missing}: {res}")
        if res["map_open"] is not True:
            raise BridgeError(f"map_open is not True: {res}")
        if not same_path(res["path"], first_map):
            raise BridgeError(f"path {res['path']!r} is not {first_map!r}")
        return res

    check("get_current_map", current_map)

    # --- save_map: edit the map first so there is something to save ---
    def save():
        before = os.path.getmtime(first_map)
        res = bridge.request("save_map")
        if res.get("saved") is not True:
            raise BridgeError(f"saved is not True: {res}")
        deadline = time.monotonic() + SAVE_TIMEOUT
        while time.monotonic() < deadline:
            if os.path.getmtime(first_map) > before:
                return f"{res}, mtime advanced"
            time.sleep(POLL_INTERVAL)
        raise BridgeError(f"mtime did not advance within {SAVE_TIMEOUT:.0f}s: {res}")

    check("place_object (first)", place_at_center)
    check("save_map", save)

    # --- open_map on a missing file ---
    missing_path = os.path.join(os.path.dirname(os.path.abspath(first_map)),
                                "does-not-exist-mcp-test.dungeondraft_map")

    def open_missing():
        try:
            res = bridge.request("open_map", path=missing_path)
        except BridgeError as exc:
            if "file not found" in str(exc):
                return f"error: {exc}"
            raise
        raise BridgeError(f"expected 'file not found' error, got {res}")

    check("open_map missing file", open_missing)
    check("ping after bad open", lambda: bridge.request("ping"))

    # --- edit + save, then switch away at once: the switch must not cut the save off ---
    state = {}

    def count_objects():
        return bridge.request("get_status")["counts"]["objects"]

    def place_and_save():
        place_at_center(dx=-300)
        state["objects"] = count_objects()
        res = bridge.request("save_map")
        if res.get("saved") is not True:
            raise BridgeError(f"saved is not True: {res}")
        cur = bridge.request("get_current_map")
        return f"objects={state['objects']}, reply={res}, saving right after={cur.get('saving')!r}"

    check("place + save (no wait)", place_and_save)

    def open_second():
        deadline = time.monotonic() + SWITCH_TIMEOUT
        retries = 0
        while True:
            try:
                res = bridge.request("open_map", path=second_map)
            except BridgeError as exc:
                if "save in progress" in str(exc) and time.monotonic() < deadline:
                    retries += 1
                    time.sleep(0.2)
                    continue
                raise
            if "opening" not in res:
                raise BridgeError(f"no 'opening' in reply: {res}")
            return f"{res} after {retries} 'save in progress' retries"

    on_second = None
    if check("open_map second", open_second) is not None:
        on_second = check("second map loaded", lambda: wait_for_map(second_map))
    if on_second is None:
        # The discard test below must only ever edit the second map.
        print(f"  FAIL  {'switch-away checks':24} -> skipped: second map never loaded")
        failed += 1
        print(f"\n{passed} passed, {failed} failed.")
        return 1

    # --- modify the second map, then switch away without saving (risk 2) ---
    check("place_object", place_at_center)
    after_edit = check("get_current_map dirty", lambda: bridge.request("get_current_map"))
    if after_edit is not None:
        print(f"        recorded dirty after edit: {after_edit.get('dirty')!r}")

    def open_first():
        res = bridge.request("open_map", path=first_map)
        if "opening" not in res:
            raise BridgeError(f"no 'opening' in reply: {res}")
        return res

    if check("open_map back to first", open_first) is not None:
        if check("first map loaded", lambda: wait_for_map(first_map)) is not None:
            def save_survived():
                n = count_objects()
                if n != state.get("objects"):
                    raise BridgeError(f"first map has {n} objects, expected {state.get('objects')} "
                                      "(the object saved just before the switch was lost)")
                return f"{n} objects, as saved"

            check("quick save survived", save_survived)

    # --- human check: did a dialog appear? ---
    print("\n  >>> QUESTION FOR THE HUMAN <<<")
    print("  During the save, or while switching from the second map (which had an")
    print("  unsaved object) back to the first, did Dungeondraft show ANY dialog?")
    print("  If a dialog is still showing, do NOT save; dismiss it.")
    answer = ""
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            answer = input("  Did a dialog appear? [y/n]: ").strip().lower()
        except EOFError:
            pass
    if answer.startswith("n"):
        print(f"  PASS  {'no dialog':24} -> human reports no dialog")
        passed += 1
    elif answer.startswith("y"):
        print(f"  FAIL  {'no dialog':24} -> human reports a dialog appeared")
        failed += 1
    else:
        print(f"  NOTE  {'no dialog':24} -> no answer (not interactive?): confirm with "
              "the human that no dialog appeared (not counted)")

    print(f"\n{passed} passed, {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
