"""MCP server exposing Dungeondraft map operations for AI-assisted map building.

Runs over stdio (the standard transport for Claude Desktop / Claude Code) and
forwards each tool call to the in-app bridge mod over localhost TCP.

Coordinates are in world "woxel" (pixel) space. Call get_status() for the map
size and center. Elements are referenced by integer `id` (returned by create
and list calls). Discover asset paths with list_asset_categories() +
list_assets(category, search=...).
"""

from __future__ import annotations

import os
import tempfile
import time
from typing import Optional

from mcp.server.fastmcp import FastMCP, Image

from .bridge_client import BridgeClient, BridgeError

HOST = os.environ.get("DD_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("DD_BRIDGE_PORT", "8787"))

mcp = FastMCP("dungeondraft")
bridge = BridgeClient(HOST, PORT)

_SCREENSHOT_PATH = os.path.join(tempfile.gettempdir(), "dd_mcp_screenshot.png")
_EXPORT_PATH = os.path.join(tempfile.gettempdir(), "dd_mcp_export.png")


def _wait_for_file(path: str, timeout: float = 60.0) -> None:
    """Block until `path` exists with a non-zero size that is stable across polls."""
    deadline = time.time() + timeout
    last = -1
    while time.time() < deadline:
        if os.path.exists(path):
            size = os.path.getsize(path)
            if size > 0 and size == last:
                return
            last = size
        time.sleep(0.3)
    raise BridgeError(f"render did not complete within {timeout:.0f}s ({path})")


# --------------------------------------------------------------------------
# Read / query
# --------------------------------------------------------------------------

@mcp.tool()
def ping() -> dict:
    """Check connectivity to the running Dungeondraft bridge and report the Godot engine version."""
    return bridge.request("ping")


@mcp.tool()
def get_status() -> dict:
    """Report the current map: whether one is open, its size and center in woxels, per-type element counts, and level info.

    Call this first — map_center is the default placement point, and counts tell you what's already on the map.
    """
    return bridge.request("get_status")


@mcp.tool()
def list_asset_categories() -> dict:
    """List the valid asset category names (Objects, Walls, Paths, Terrain, Lights, Portals, Roofs, ...)."""
    return bridge.request("list_asset_categories")


@mcp.tool()
def list_assets(category: str = "Objects", search: str = "", limit: int = 100) -> dict:
    """List asset paths in a category, optionally filtered by a case-insensitive substring.

    category: e.g. 'Objects', 'Walls', 'Paths', 'Terrain', 'Lights', 'Portals', 'Roofs'.
    search: substring to match against the asset path (e.g. 'chair', 'door', 'grass').
    Pass a returned path as the 'asset' argument to the create tools.
    """
    return bridge.request("list_assets", category=category, search=search, limit=limit)


@mcp.tool()
def list_elements(kind: str = "objects", limit: int = 200) -> dict:
    """List elements currently on the map with their ids, positions, rotation, scale and asset.

    kind: one of 'objects', 'walls', 'lights', 'paths', 'portals', 'roofs', 'texts'.
    Use the returned ids with move_element / modify_object / delete_element / select_elements.
    """
    return bridge.request("list_elements", kind=kind, limit=limit)


@mcp.tool()
def get_element(id: int) -> dict:
    """Get details (kind, position, rotation, scale, asset) for a single element by id."""
    return bridge.request("get_element", id=id)


@mcp.tool()
def list_levels() -> dict:
    """List the map's levels (floors) with their index, id and label, plus the current index."""
    return bridge.request("list_levels")


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------

@mcp.tool()
def place_object(
    asset: str,
    x: Optional[float] = None,
    y: Optional[float] = None,
    scale: float = 1.0,
    rotation: float = 0.0,
    sorting: int = 0,
    color: str = "",
) -> dict:
    """Place an object (prop) on the current map. Returns the new element id.

    asset: an Objects asset path from list_assets(category='Objects').
    x, y: woxel coordinates; defaults to map center. rotation: degrees.
    sorting: 0=over, 1=under. color: optional tint as '#rrggbb'.
    """
    params = {"asset": asset, "scale": scale, "rotation": rotation, "sorting": sorting}
    if x is not None:
        params["x"] = x
    if y is not None:
        params["y"] = y
    if color:
        params["color"] = color
    return bridge.request("place_object", **params)


@mcp.tool()
def draw_wall(
    points: list[list[float]],
    asset: str = "",
    loop: bool = False,
    shadow: bool = True,
    type: int = 0,
    joint: int = 1,
    color: str = "",
) -> dict:
    """Draw a wall through a list of [x, y] woxel points. Returns the new element id.

    asset: optional Walls asset path. loop: close into a loop (e.g. a room).
    type: 0=auto, 1=manual, 2=cave. joint: 0=sharp, 1=bevel, 2=round.
    """
    return bridge.request(
        "draw_wall", points=points, asset=asset, loop=loop, shadow=shadow, type=type, joint=joint, color=color
    )


@mcp.tool()
def draw_path(
    points: list[list[float]],
    asset: str,
    layer: int = 0,
    sorting: int = 0,
    smoothness: Optional[float] = None,
    width: Optional[float] = None,
) -> dict:
    """Draw a path/road/river through a list of [x, y] woxel points. Returns the new element id.

    asset: a Paths asset path from list_assets(category='Paths').
    smoothness: optional curve smoothing. width: optional width scale multiplier.
    """
    params = {"points": points, "asset": asset, "layer": layer, "sorting": sorting}
    if smoothness is not None:
        params["smoothness"] = smoothness
    if width is not None:
        params["width"] = width
    return bridge.request("draw_path", **params)


@mcp.tool()
def add_light(
    x: Optional[float] = None,
    y: Optional[float] = None,
    color: str = "",
    energy: float = 1.0,
    range: float = 1.0,
    shadows: bool = True,
    asset: str = "",
) -> dict:
    """Add a light at a woxel position. Returns the new element id.

    color: '#rrggbb' (default warm). energy: brightness. range: radius scale.
    asset: optional Lights gradient/cookie texture path.
    """
    params = {"color": color, "energy": energy, "range": range, "shadows": shadows, "asset": asset}
    if x is not None:
        params["x"] = x
    if y is not None:
        params["y"] = y
    return bridge.request("add_light", **params)


@mcp.tool()
def add_portal(
    asset: str,
    x: Optional[float] = None,
    y: Optional[float] = None,
    closed: bool = False,
    radius: float = 64.0,
    rotation: float = 0.0,
) -> dict:
    """Add a freestanding portal (door/window) at a woxel position. Returns the new element id.

    asset: a Portals asset path. closed: blocks light. radius: half-width. rotation: degrees.
    """
    params = {"asset": asset, "closed": closed, "radius": radius, "rotation": rotation}
    if x is not None:
        params["x"] = x
    if y is not None:
        params["y"] = y
    return bridge.request("add_portal", **params)


@mcp.tool()
def add_roof(
    points: list[list[float]],
    asset: str,
    width: float = 256.0,
    type: int = 0,
    sorting: int = 0,
) -> dict:
    """Add a roof over a polygon of [x, y] woxel points. Returns the new element id.

    asset: a Roofs asset path. type: 0=gable, 1=hip, 2=dormer. width: roof width.
    """
    return bridge.request("add_roof", points=points, asset=asset, width=width, type=type, sorting=sorting)


@mcp.tool()
def add_text(
    text: str,
    x: Optional[float] = None,
    y: Optional[float] = None,
    size: Optional[int] = None,
    color: str = "",
    font: str = "",
) -> dict:
    """(Experimental) Add a text label at a woxel position. Returns the new element id.

    The text-content setter is undocumented in the modding API, so this may not
    populate the string correctly on all Dungeondraft versions — verify visually.
    """
    params = {"text": text, "color": color, "font": font}
    if x is not None:
        params["x"] = x
    if y is not None:
        params["y"] = y
    if size is not None:
        params["size"] = size
    return bridge.request("add_text", **params)


# --------------------------------------------------------------------------
# Terrain
# --------------------------------------------------------------------------

@mcp.tool()
def set_terrain_slot(asset: str, slot: int = 0) -> dict:
    """Assign a Terrain asset to a terrain slot index so it can be filled/painted with that slot."""
    return bridge.request("set_terrain_slot", asset=asset, slot=slot)


@mcp.tool()
def fill_terrain(slot: int = 0, asset: str = "") -> dict:
    """Flood-fill the whole current level with a terrain slot. If asset is given, it is assigned to the slot first."""
    params = {"slot": slot}
    if asset:
        params["asset"] = asset
    return bridge.request("fill_terrain", **params)


@mcp.tool()
def paint_terrain(
    slot: int = 0,
    x: Optional[float] = None,
    y: Optional[float] = None,
    radius: float = 64.0,
    rate: float = 1.0,
) -> dict:
    """(Experimental) Paint a circular terrain brush of a slot at a woxel position.

    Brush footprint / blend semantics are inferred from the API; verify visually.
    Assign the slot's texture first with set_terrain_slot.
    """
    params = {"slot": slot, "radius": radius, "rate": rate}
    if x is not None:
        params["x"] = x
    if y is not None:
        params["y"] = y
    return bridge.request("paint_terrain", **params)


# --------------------------------------------------------------------------
# Modify / delete
# --------------------------------------------------------------------------

@mcp.tool()
def move_element(id: int, x: float, y: float) -> dict:
    """Move any element to a new woxel position by id."""
    return bridge.request("move_element", id=id, x=x, y=y)


@mcp.tool()
def modify_object(
    id: int,
    scale: Optional[float] = None,
    rotation: Optional[float] = None,
    color: str = "",
    shadow: Optional[bool] = None,
) -> dict:
    """Modify an existing object's scale, rotation (degrees), color ('#rrggbb') and/or shadow flag by id."""
    params: dict = {"id": id}
    if scale is not None:
        params["scale"] = scale
    if rotation is not None:
        params["rotation"] = rotation
    if color:
        params["color"] = color
    if shadow is not None:
        params["shadow"] = shadow
    return bridge.request("modify_object", **params)


@mcp.tool()
def duplicate_object(id: int, dx: float = 64.0, dy: float = 0.0) -> dict:
    """Duplicate an object by id, offset by (dx, dy) woxels. Returns the new element id."""
    return bridge.request("duplicate_object", id=id, dx=dx, dy=dy)


@mcp.tool()
def delete_element(id: int) -> dict:
    """Delete any element from the map by id."""
    return bridge.request("delete_element", id=id)


# --------------------------------------------------------------------------
# Levels
# --------------------------------------------------------------------------

@mcp.tool()
def add_level(label: str = "Level") -> dict:
    """Add a new level (floor) to the map. Returns its id and label."""
    return bridge.request("add_level", label=label)


@mcp.tool()
def set_level(index: int) -> dict:
    """Switch the active level (floor) by its index (see list_levels)."""
    return bridge.request("set_level", index=index)


# --------------------------------------------------------------------------
# Capture — let the model see its own work
# --------------------------------------------------------------------------

@mcp.tool()
def screenshot() -> Image:
    """Capture the current Dungeondraft window (the on-screen view) and return it as an image.

    Fast; shows exactly what's visible including the current camera framing. Use this
    to check your work as you build. For a clean full-map render without UI, use export_map.
    """
    res = bridge.request("screenshot", path=_SCREENSHOT_PATH)
    return Image(path=res["path"])


@mcp.tool()
def export_map(ppi: int = 40) -> Image:
    """Render the entire current map to a clean PNG (no UI) and return it as an image.

    ppi controls resolution (pixels per grid cell): higher = sharper but larger/slower.
    The render runs asynchronously in Dungeondraft; this waits for it to finish.
    """
    try:
        os.remove(_EXPORT_PATH)
    except FileNotFoundError:
        pass
    bridge.request("export_map", path=_EXPORT_PATH, ppi=ppi)
    _wait_for_file(_EXPORT_PATH)
    return Image(path=_EXPORT_PATH)


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

@mcp.tool()
def select_elements(ids: list[int]) -> dict:
    """Select the given element ids in Dungeondraft's UI (replaces the current selection)."""
    return bridge.request("select_elements", ids=ids)


@mcp.tool()
def clear_selection() -> dict:
    """Clear the current selection in Dungeondraft."""
    return bridge.request("clear_selection")


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

@mcp.tool()
def undo() -> dict:
    """Undo the last map edit (same as Ctrl+Z in Dungeondraft). Reverses your own create/move/modify/terrain actions."""
    return bridge.request("undo")


@mcp.tool()
def redo() -> dict:
    """Redo the last undone map edit (same as Ctrl+Y in Dungeondraft)."""
    return bridge.request("redo")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
