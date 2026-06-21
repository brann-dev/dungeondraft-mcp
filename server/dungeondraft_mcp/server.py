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
    mount: str = "wall",
    snap_max: float = 256.0,
    flip: bool = False,
    fallback_free: bool = True,
    rotation: float = 0.0,
) -> dict:
    """Add a door/window portal at a woxel position. Returns the new element id.

    By default the portal MOUNTS on the nearest wall and the wall cuts a gap
    around it (like placing a door by hand).

    asset: a Portals asset path. closed: blocks light.
    radius: door half-width (~128 ≈ 1-tile door, ~256 ≈ 2-tile door).
    mount: 'wall' (snap to nearest wall, cut gap) or 'free' (freestanding).
    snap_max: max woxel distance a wall may be and still capture the portal.
    flip: reverse the door's facing. fallback_free: if mount='wall' but no wall
    is within snap_max, place a freestanding portal instead of erroring.
    rotation: degrees, freestanding only.
    """
    params = {
        "asset": asset,
        "closed": closed,
        "radius": radius,
        "mount": mount,
        "snap_max": snap_max,
        "flip": flip,
        "fallback_free": fallback_free,
        "rotation": rotation,
    }
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
def place_pattern(
    asset: str,
    rect: Optional[list[float]] = None,
    points: Optional[list[list[float]]] = None,
    category: str = "Patterns",
    color: str = "",
    rotation: Optional[float] = None,
    z: int = -100,
) -> dict:
    """Place a tiled floor/pattern shape (the Building Tool's "Floor" / Pattern Shape).

    Draws an actual tiled floor (wood planks, tile, brick, ...) that renders
    BELOW objects, distinct from terrain. Provide ONE of:
      rect: [x, y, w, h] axis-aligned rectangle, or
      points: [[x,y], ...] a polygon (>= 3 points), all in woxels.
    asset: a pattern asset path from list_assets(category=...).
    category: which asset bank — 'Patterns', 'Patterns Colorable', 'Materials',
      'Simple Tiles', or 'Smart Tiles'.
    color: '#rrggbb' tint. Omit to use the tileset's own default tint (wood is
      brown, etc.) instead of white — match the UI by leaving it unset.
    rotation: pattern rotation in degrees.
    z: absolute z_index. Default -100 sits below objects (z 0) and above terrain
      shapes (z -200). Raise toward 0+ to overlay on top.
    """
    if (rect is None) == (points is None):
        raise ValueError("provide exactly one of 'rect' or 'points'")
    params: dict = {"asset": asset, "category": category, "z": z}
    if rect is not None:
        params["rect"] = rect
    if points is not None:
        params["points"] = points
    if color:
        params["color"] = color
    if rotation is not None:
        params["rotation"] = rotation
    return bridge.request("place_pattern", **params)


@mcp.tool()
def build_room(
    rect: Optional[list[float]] = None,
    points: Optional[list[list[float]]] = None,
    wall_asset: str = "",
    floor: str = "pattern",
    floor_asset: str = "",
    floor_category: str = "Simple Tiles",
    floor_color: str = "",
    floor_slot: int = 1,
    wall_type: int = 0,
    wall_joint: int = 1,
) -> dict:
    """Build a room in one call: a looped wall AND a matching floor on the SAME path.

    Because the wall and floor share the boundary, the floor meets the wall
    exactly (no gap) — like the UI's combined wall+floor trace. Provide ONE of:
      rect: [x, y, w, h] axis-aligned rectangle, or
      points: [[x,y], ...] a polygon (>= 3 points), all in woxels (the wall
      centerline; the wall covers the floor's outer edge).

    wall_asset: a Walls asset path (omit for the default).
    floor: 'pattern' (a tiled floor; floor_asset + floor_category), 'terrain'
      (paints terrain into floor_slot; floor_asset assigns the slot texture),
      or 'none' for walls only.
    floor_asset: the floor texture. floor_category: pattern bank for floor=
      'pattern' (e.g. 'Simple Tiles', 'Smart Tiles', 'Materials').
    floor_color: optional '#rrggbb' tint (pattern floors).
    wall_type: 0=auto, 1=manual, 2=cave. wall_joint: 0=sharp, 1=bevel, 2=round.

    Returns { wall_id, floor_id?, points }.
    """
    if (rect is None) == (points is None):
        raise ValueError("provide exactly one of 'rect' or 'points'")
    params: dict = {
        "floor": floor,
        "floor_category": floor_category,
        "floor_slot": floor_slot,
        "wall_type": wall_type,
        "wall_joint": wall_joint,
    }
    if rect is not None:
        params["rect"] = rect
    if points is not None:
        params["points"] = points
    if wall_asset:
        params["wall_asset"] = wall_asset
    if floor_asset:
        params["floor_asset"] = floor_asset
    if floor_color:
        params["floor_color"] = floor_color
    return bridge.request("build_room", **params)


@mcp.tool()
def add_text(
    text: str,
    x: Optional[float] = None,
    y: Optional[float] = None,
    size: Optional[int] = None,
    color: str = "",
    font: str = "",
) -> dict:
    """Add a text label at a woxel position. Returns the new element id and size.

    size: font size in points (default ~32). color: '#rrggbb' (default black).
    font: a DD font name; omit to keep the default font.
    """
    params = {"text": text}
    if x is not None:
        params["x"] = x
    if y is not None:
        params["y"] = y
    if size is not None:
        params["size"] = size
    if color:
        params["color"] = color
    if font:
        params["font"] = font
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
def fill_region(
    rect: Optional[list[float]] = None,
    points: Optional[list[list[float]]] = None,
    slot: int = 0,
    asset: str = "",
    rate: float = 1.0,
) -> dict:
    """Fill only a region with a terrain slot (e.g. floor a single room), in woxel coords.

    Unlike fill_terrain (whole level), this paints inside a shape. Provide ONE of:
      rect: [x, y, w, h] axis-aligned rectangle, or
      points: [[x,y], ...] a polygon (>= 3 points).
    asset: optional Terrain asset to assign to the slot first.
    rate: paint strength 0..1 (1 = fully replace). Undoable via undo().
    """
    if (rect is None) == (points is None):
        raise ValueError("provide exactly one of 'rect' or 'points'")
    params: dict = {"slot": slot, "rate": rate}
    if rect is not None:
        params["rect"] = rect
    if points is not None:
        params["points"] = points
    if asset:
        params["asset"] = asset
    return bridge.request("fill_region", **params)


@mcp.tool()
def paint_terrain(
    slot: int = 0,
    x: Optional[float] = None,
    y: Optional[float] = None,
    radius: float = 64.0,
    rate: float = 1.0,
    asset: str = "",
) -> dict:
    """Paint a soft circular terrain brush of a slot at a woxel position.

    radius: brush radius in woxels. rate: peak strength 0..1 at the center, with
    a smooth falloff to the rim so strokes blend. asset: optional Terrain asset
    to assign to the slot first (else set it with set_terrain_slot). Undoable.
    For a hard-edged region instead of a brush, use fill_region.
    """
    params = {"slot": slot, "radius": radius, "rate": rate}
    if x is not None:
        params["x"] = x
    if y is not None:
        params["y"] = y
    if asset:
        params["asset"] = asset
    return bridge.request("paint_terrain", **params)


@mcp.tool()
def paint_path(
    points: list[list[float]],
    slot: int = 0,
    radius: float = 96.0,
    rate: float = 1.0,
    asset: str = "",
) -> dict:
    """Paint a continuous terrain stroke (a road/trail) along a polyline in one call.

    points: a list of [x, y] woxel corners the path runs through (>= 2). The
    bridge rasterizes a uniform ribbon of constant width by measuring each
    pixel's distance to the nearest segment, so the route comes out smooth with
    clean edges — no gaps or double-painted overlaps. Prefer this over stamping
    many paint_terrain dabs for any line/road.

    radius: half-width of the stroke in woxels. rate: peak strength 0..1 with a
    soft falloff to the edges so it blends. asset: optional Terrain asset to
    assign to the slot first (else set it with set_terrain_slot). Undoable.
    """
    params = {"points": points, "slot": slot, "radius": radius, "rate": rate}
    if asset:
        params["asset"] = asset
    return bridge.request("paint_path", **params)


@mcp.tool()
def dig_cave(
    points: list[list[float]],
    radius: float = 256.0,
    dig: bool = True,
    ground_color: str = "",
    wall_color: str = "",
    texture: str = "",
) -> dict:
    """Carve a cave along a path with the Cave Brush (the dig/blast tool).

    Dungeondraft caves are a separate layer: you dig open floor out of solid
    rock, and DD auto-generates the rocky wall border + debris around the opening.

    points: a list of [x, y] woxel points the cave runs through (>= 1). A single
    point digs one circular chamber; multiple points dig a connected tunnel
    (rasterized as a constant-width ribbon, like paint_path). radius: half-width
    in woxels (default 256 = ~1 tile). dig: True carves open cave; False fills it
    back to rock. ground_color / wall_color: optional cave floor/wall tints
    ("#rrggbb" or [r,g,b]). texture: optional Caves floor asset (see
    list_assets(category="Caves")). The mesh rebuilds automatically.
    """
    params = {"points": points, "radius": radius, "dig": dig}
    if ground_color:
        params["ground_color"] = ground_color
    if wall_color:
        params["wall_color"] = wall_color
    if texture:
        params["texture"] = texture
    return bridge.request("dig_cave", **params)


@mcp.tool()
def clear_caves() -> dict:
    """Wipe the entire cave layer back to solid rock.

    Removes all carved caves at once (the whole cave system), rebuilding the
    mesh. Undoable like dig_cave. Use this instead of filling regions back with
    dig_cave(dig=False) when you want to reset all caves.
    """
    return bridge.request("clear_caves")


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
# Camera
# --------------------------------------------------------------------------

@mcp.tool()
def get_camera() -> dict:
    """Report the editor camera: world-center position [x,y], zoom, and viewport size.

    zoom is a Camera2D factor where LARGER = zoomed OUT (zoom 0.5 magnifies 2x,
    zoom 2.0 shows twice as much). Use this to read the view before adjusting it.
    """
    return bridge.request("get_camera")


@mcp.tool()
def set_camera(
    x: Optional[float] = None,
    y: Optional[float] = None,
    zoom: Optional[float] = None,
) -> dict:
    """Pan and/or zoom the editor camera. Returns the resulting camera state.

    x, y: world (woxel) center to move the view to (omit to keep current).
    zoom: Camera2D factor (LARGER = zoomed OUT; ~0.5 = a close look, ~2 = wide).
    Follow with screenshot() to see the framed view.
    """
    params: dict = {}
    if x is not None:
        params["x"] = x
    if y is not None:
        params["y"] = y
    if zoom is not None:
        params["zoom"] = zoom
    return bridge.request("set_camera", **params)


@mcp.tool()
def focus_element(id: int, zoom: Optional[float] = None) -> dict:
    """Center the camera on an element by id (works for any kind, including text).

    zoom: optional Camera2D factor to apply (LARGER = zoomed OUT). Follow with
    screenshot() to verify a specific element (e.g. a door cut into a wall).
    """
    params: dict = {"id": id}
    if zoom is not None:
        params["zoom"] = zoom
    return bridge.request("focus_element", **params)


@mcp.tool()
def fit_elements(ids: list[int], pad: float = 0.15) -> dict:
    """Frame a group of elements: center and zoom so their bounding box fits the view.

    ids: element ids to frame. pad: extra margin as a fraction (0.15 = 15%).
    Ids without a position are skipped and listed in the 'missing' field.
    Follow with screenshot() to see the framed group.
    """
    return bridge.request("fit_elements", ids=ids, pad=pad)


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
    """Reverse the last reversible edit you made (create / move / modify / terrain).

    The bridge keeps its own undo/redo stack, so this reliably steps back through
    your own edits. Note: it is independent of Dungeondraft's Ctrl+Z, and
    delete_element is not reversible.
    """
    return bridge.request("undo")


@mcp.tool()
def redo() -> dict:
    """Re-apply the last edit reversed by undo()."""
    return bridge.request("redo")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
