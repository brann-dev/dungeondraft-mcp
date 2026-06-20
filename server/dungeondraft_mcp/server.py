"""MCP server exposing Dungeondraft map operations.

Runs over stdio (the standard transport for Claude Desktop / Claude Code) and
forwards each tool call to the in-app bridge mod over localhost TCP.
"""

from __future__ import annotations

import os
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .bridge_client import BridgeClient

HOST = os.environ.get("DD_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("DD_BRIDGE_PORT", "8787"))

mcp = FastMCP("dungeondraft")
bridge = BridgeClient(HOST, PORT)


@mcp.tool()
def ping() -> dict:
    """Check connectivity to the running Dungeondraft bridge and report the Godot engine version."""
    return bridge.request("ping")


@mcp.tool()
def get_status() -> dict:
    """Report the current map state: whether a map is open, its size in woxels, level info, and object count."""
    return bridge.request("get_status")


@mcp.tool()
def list_assets(category: str = "Objects", limit: int = 100) -> dict:
    """List available asset paths in a category.

    category: e.g. 'Objects' or 'Walls'. Pass a returned path as the 'asset'
    argument to place_object / draw_wall.
    """
    return bridge.request("list_assets", category=category, limit=limit)


@mcp.tool()
def place_object(
    asset: str,
    x: Optional[float] = None,
    y: Optional[float] = None,
    scale: float = 1.0,
    rotation: float = 0.0,
    sorting: int = 0,
) -> dict:
    """Place an object on the current map.

    asset: an asset path from list_assets(category='Objects').
    x, y: position in world 'woxel' (pixel) coordinates; defaults to map center.
    scale: uniform scale multiplier. rotation: degrees. sorting: 0=over, 1=under.
    """
    params = {"asset": asset, "scale": scale, "rotation": rotation, "sorting": sorting}
    if x is not None:
        params["x"] = x
    if y is not None:
        params["y"] = y
    return bridge.request("place_object", **params)


@mcp.tool()
def draw_wall(
    points: list[list[float]],
    asset: str = "",
    loop: bool = False,
    shadow: bool = True,
    type: int = 0,
    joint: int = 1,
) -> dict:
    """(Experimental) Draw a wall through a list of [x, y] points in woxel coordinates.

    asset: optional wall texture path from list_assets(category='Walls').
    loop: close the wall into a loop. type: 0=auto, 1=manual, 2=cave.
    joint: 0=sharp, 1=bevel, 2=round.
    """
    return bridge.request(
        "draw_wall", points=points, asset=asset, loop=loop, shadow=shadow, type=type, joint=joint
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
