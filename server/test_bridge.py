#!/usr/bin/env python3
"""Standalone smoke test for the Dungeondraft bridge — no MCP involved.

Run this FIRST, with Dungeondraft open (mod enabled, a map loaded). It proves
whether DD's modding sandbox allows TCP at all, which is the one unverified
assumption in this whole project.

    python test_bridge.py
"""

import sys

from dungeondraft_mcp.bridge_client import BridgeClient, BridgeError


def main() -> int:
    bridge = BridgeClient()
    try:
        print("ping        ->", bridge.request("ping"))
        print("get_status  ->", bridge.request("get_status"))
        assets = bridge.request("list_assets", category="Objects", limit=5)
        print("list_assets ->", assets)
        sample = assets.get("assets") or []
        if sample:
            print("place_object->", bridge.request("place_object", asset=sample[0]))
        else:
            print("place_object-> skipped (no Object assets available)")
    except BridgeError as exc:
        print("FAILED:", exc, file=sys.stderr)
        return 1
    print("\nAll bridge commands succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
