"""MCP server exposing Dungeondraft map operations for AI-assisted map building.

Runs over stdio (the standard transport for Claude Desktop / Claude Code) and
forwards each tool call to the in-app bridge mod over localhost TCP.

Coordinates are in world "woxel" (pixel) space. Call get_status() for the map
size and center. Elements are referenced by integer `id` (returned by create
and list calls). Discover asset paths with list_asset_categories() +
list_assets(category, search=...).
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import json
import os
import random
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import pydantic_core
from mcp.server.fastmcp import FastMCP, Image

from . import floorplan, layout, mapfile, packs, spatial, uvtt_import
from . import scene as scene_checks
from .bridge_client import BridgeClient, BridgeError

HOST = os.environ.get("DD_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("DD_BRIDGE_PORT", "8787"))

INSTRUCTIONS = """These tools drive a running Dungeondraft through its MCP Bridge mod. Call ping,
then get_status, before anything else. If a call fails with "paused", the user
paused AI edits in Dungeondraft's MCP Bridge panel: tell them, and make no edits
until they resume (read-only tools still work). If a call fails with a token
error, this server and the mod do not share the bridge token: say so and stop;
restarting Dungeondraft and reconnecting the server fixes it."""

mcp = FastMCP("dungeondraft", instructions=INSTRUCTIONS)

# The open undo group, if any: every bridge request made inside _undo_group()
# carries its token, so the mod records those edits as one undo() step and can
# tell a stale group (its owner went away) from a live one.
_undo_group_token: contextvars.ContextVar[str] = contextvars.ContextVar("undo_group", default="")


class _Bridge(BridgeClient):
    def request(self, cmd: str, **params) -> dict:
        token = _undo_group_token.get()
        if token and "undo_group" not in params:
            params["undo_group"] = token
        return super().request(cmd, **params)


bridge = _Bridge(HOST, PORT)


@contextlib.contextmanager
def _undo_group(label: str):
    """Record every edit made inside the block as ONE undo() step."""
    token = uuid.uuid4().hex
    bridge.request("begin_undo_group", label=label, undo_group=token)
    reset = _undo_group_token.set(token)
    try:
        yield
    finally:
        _undo_group_token.reset(reset)
        try:
            bridge.request("end_undo_group", undo_group=token)
        except BridgeError:
            pass   # the mod closes a group whose owner stopped talking to it


def _model_text(result: Any) -> Any:
    """What a tool result looks like to the model: JSON sent compact.

    The SDK serialises a returned dict with indent=2, which puts every number
    of a nested array on its own line; a spatial snapshot or an asset listing
    costs several times the tokens it needs to. Same serialiser, same
    fallbacks, no indentation. Images and strings pass through untouched.
    """
    if isinstance(result, list) and any(isinstance(item, Image) for item in result):
        return result   # an image with its caption: content blocks, not a JSON string
    if isinstance(result, (dict, list)):
        return pydantic_core.to_json(result, fallback=str).decode()
    return result


def _tool() -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a tool and return the function itself unchanged.

    Python callers (batch(), the tests) keep getting dicts; only the MCP
    boundary sees the compact text. structured_output=False pins the wire
    contract to one text block, so a later SDK cannot start sending every
    result twice. An exception this package did not anticipate is reported
    with its type instead of an empty error.
    """
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def for_the_model(*args: Any, **kwargs: Any) -> Any:
            try:
                return _model_text(fn(*args, **kwargs))
            except (BridgeError, ValueError, TypeError):
                raise
            except Exception as exc:   # noqa: BLE001 - reported, not swallowed
                detail = (str(exc).strip() or "no message")[:300]
                raise RuntimeError(
                    f"{fn.__name__} failed unexpectedly: {type(exc).__name__}: {detail}. "
                    "This is a bug in the server, not in your request; retrying the "
                    "same call is unlikely to help.") from exc
        mcp.tool(structured_output=False)(for_the_model)
        return fn
    return register

_SCREENSHOT_PATH = os.path.join(tempfile.gettempdir(), "dd_mcp_screenshot.png")
_EXPORT_PATH = os.path.join(tempfile.gettempdir(), "dd_mcp_export.png")
_UVTT_PATH = os.path.join(tempfile.gettempdir(), "dd_mcp_export.dd2vtt")
_TRACE_PATH = os.path.join(tempfile.gettempdir(), "dd_mcp_trace")   # + the image's suffix


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


def _run_export(fmt: str, path: str, ppi: int, grid: Optional[bool],
                timeout: float = 60.0) -> dict:
    """Ask the mod to export `path` as `fmt`, wait for the file, restore the editor grid.

    The mod toggles the grid before rendering and reports `grid_was`; the render
    spans later frames, so restoring it is our job once the file is complete.
    """
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    params: dict = {"path": path, "ppi": ppi, "format": fmt}
    if grid is not None:
        params["grid"] = grid
    res = bridge.request("export_map", **params)
    try:
        _wait_for_file(path, timeout=timeout)
    finally:
        # Un-stage the level and put the camera and grid back. The mod does this
        # itself once DD reports the export finished; this is the fallback for a
        # run that died inside DD (which also leaves its IsExporting flag stuck).
        bridge.request("export_finish")
    return res


def _shape_polygon(rect: Optional[list[float]], points: Optional[list[list[float]]]) -> list:
    return spatial.rect_polygon(rect) if rect is not None else [tuple(p[:2]) for p in points or []]


def _refuse_if_blocked(cmd: str, params: dict, allow_overlap: bool, region: str) -> None:
    """The opt-in placement check behind `check=True` / `region=`: raise instead of
    letting an object edit cross a wall, block a door, leave the map, enter a
    clear zone, leave its room or (unless allow_overlap) overlap another object.
    """
    query = {}
    if cmd == "place_object":
        query["asset"] = params["asset"]
    elif cmd == "import_image":
        query["path"] = params["path"]
    scene = bridge.request("spatial_snapshot", **query)
    proposal = spatial.proposed_footprint(scene, cmd, params)
    if proposal is None:
        return   # a light, a text, a wall: nothing with a footprint to check
    conflicts = spatial.check_footprint(
        scene, proposal.polygon, layer=proposal.layer, ignore_id=proposal.ignore_id,
        allow_overlap=allow_overlap, region=region)
    if conflicts:
        raise BridgeError("refused, nothing changed: it " + spatial.describe(conflicts))


def _refuse_if_protected(what: str, **shape) -> None:
    """Raise if an area edit (see spatial.protected_hits for `shape`) would reach
    one of the level's 'protected' regions. Costs one extra bridge call."""
    try:
        regions = bridge.request("list_spatial_regions")["regions"]
    except BridgeError as exc:
        if "unknown cmd" in str(exc):
            return   # an older mod without regions: nothing can be protected
        raise
    hits = spatial.protected_hits(regions, **shape)
    if hits:
        raise BridgeError(
            f"refused, nothing changed: {what} would reach protected region(s) {', '.join(hits)}. "
            "Use remove_spatial_region first if that is intended.")


# --------------------------------------------------------------------------
# Read / query
# --------------------------------------------------------------------------

@_tool()
def ping() -> dict:
    """Check connectivity to the running Dungeondraft bridge: protocol version, engine, whether this server is authenticated, and whether the user has paused AI edits.

    ping answers without a token, so it can diagnose a token problem:
    authenticated=False with auth="token" means the mod and this server do
    not share the bridge token. paused=True means every editing tool will be
    refused until the user resumes in Dungeondraft's MCP Bridge panel.
    """
    return bridge.request("ping")


@_tool()
def get_status() -> dict:
    """Report the current map: whether one is open, its size and center in woxels, per-type element counts, level info, and whether AI edits are paused.

    Call this first — map_center is the default placement point, and counts tell you what's already on the map.
    """
    return bridge.request("get_status")


@_tool()
def list_asset_categories() -> dict:
    """List the valid asset category names (Objects, Walls, Paths, Terrain, Lights, Portals, Roofs, ...)."""
    return bridge.request("list_asset_categories")


@_tool()
def list_assets(category: str = "Objects", search: str = "", limit: int = 100) -> dict:
    """List asset paths in a category, optionally filtered by a case-insensitive substring.

    category: e.g. 'Objects', 'Walls', 'Paths', 'Terrain', 'Lights', 'Portals', 'Roofs'.
    search: substring to match against the asset path (e.g. 'chair', 'door', 'grass').
    Pass a returned path as the 'asset' argument to the create tools.
    """
    return bridge.request("list_assets", category=category, search=search, limit=limit)


@_tool()
def list_elements(kind: str = "objects", limit: int = 200) -> dict:
    """List elements currently on the map with their ids, positions, rotation, scale and asset.

    kind: one of 'objects', 'walls', 'lights', 'paths', 'portals', 'roofs', 'texts'.
    Use the returned ids with move_element / modify_object / delete_element / select_elements.
    """
    return bridge.request("list_elements", kind=kind, limit=limit)


@_tool()
def get_element(id: int) -> dict:
    """Get details (kind, position, rotation, scale, asset) for a single element by id.

    A wall also reports its geometry: points ([[x, y], ...]), loop, type, joint
    and portal_count. Those points are what modify_wall replaces.
    """
    return bridge.request("get_element", id=id)


@_tool()
def list_levels() -> dict:
    """List the map's levels (floors) with their index, id and label, plus the current index and id.

    Levels are listed top floor first. A level's index changes when levels are
    added, cloned or reordered; its id stays the same while the map is open
    (ids are handed out afresh when a map is loaded).
    """
    return bridge.request("list_levels")


@_tool()
def list_layers() -> dict:
    """List the current level's draw layers, bottom to top: {layer, label, locked}.

    A layer is a z-order band. Locked layers are Dungeondraft's own (terrain,
    caves, floor, water, portals, walls, roofs). The others are where objects,
    paths, pattern floors and materials can go: by default -400 'Below Ground',
    -100 'Below Water', 100-400 'User Layer 1-4', 700 'Above Walls', 900 'Above
    Roofs'. New objects and paths go on 100 unless told otherwise, as in
    Dungeondraft itself; on one layer, later objects draw over earlier ones.
    """
    return bridge.request("list_layers")


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------

@_tool()
def place_object(
    asset: str,
    x: Optional[float] = None,
    y: Optional[float] = None,
    scale: float = 1.0,
    rotation: float = 0.0,
    sorting: int = 0,
    color: str = "",
    layer: int = spatial.DEFAULT_LAYER,
    check: bool = False,
    allow_overlap: bool = False,
    region: str = "",
    snap: bool = False,
) -> dict:
    """Place an object (prop) on the current map. Returns the new element id.

    asset: an Objects asset path from list_assets(category='Objects').
    x, y: woxel coordinates; defaults to map center. rotation: degrees.
    sorting: 0=over, 1=under the other objects on the same layer.
    layer: draw layer (see list_layers); a higher layer always draws on top.
    The default, 100, is the one Dungeondraft's own Object tool uses.
    color: '#rrggbb' custom color. Only 'Colorable' assets (red-mask areas) take
    one; with no color they get DD's default '#6b3834'. On other assets color is
    ignored and the result carries a 'warning'. The result reports the applied color.

    check=True refuses the placement (changing nothing) if the object's
    footprint would cross a wall, block a doorway, leave the map, sit in a
    'clearance' region or overlap another object on the same layer; the error
    says which. allow_overlap=True drops only the object-overlap part (chairs
    tucked under a table, a tree canopy). region: name of a 'room' region the
    whole footprint must stay inside (implies check). get_asset_footprint tells
    you an asset's size first; check_object_placement tries a spot without
    placing anything.
    snap: True puts it on the user's grid: the Custom Snap Mod's when that mod is
      loaded, else Dungeondraft's half-tile grid (see get_snap_settings).
    """
    params = {"asset": asset, "scale": scale, "rotation": rotation, "sorting": sorting}
    if x is not None:
        params["x"] = x
    if y is not None:
        params["y"] = y
    if color:
        params["color"] = color
    params["layer"] = layer
    if check or region:
        _refuse_if_blocked("place_object", params, allow_overlap, region)
    return bridge.request("place_object", **({"snap": True} if snap else {}), **params)


@_tool()
def draw_wall(
    points: list[list[float]],
    asset: str = "",
    loop: bool = False,
    shadow: bool = True,
    type: int = 0,
    joint: int = 1,
    color: str = "",
    snap: bool = False,
) -> dict:
    """Draw a wall through a list of [x, y] woxel points. Returns the new element id.

    asset: optional Walls asset path. loop: close into a loop (e.g. a room).
    type: 0=auto, 1=manual, 2=cave. joint: 0=sharp, 1=bevel, 2=round.
    snap: True puts it on the user's grid: the Custom Snap Mod's when that mod is
      loaded, else Dungeondraft's half-tile grid (see get_snap_settings).
    """
    return bridge.request(
        "draw_wall", **({"snap": True} if snap else {}), points=points, asset=asset, loop=loop, shadow=shadow, type=type, joint=joint, color=color
    )


@_tool()
def draw_path(
    points: list[list[float]],
    asset: str,
    layer: int = spatial.DEFAULT_LAYER,
    sorting: int = 0,
    smoothness: Optional[float] = None,
    width: Optional[float] = None,
    snap: bool = False,
) -> dict:
    """Draw a path/road/river through a list of [x, y] woxel points. Returns the new element id.

    asset: a Paths asset path from list_assets(category='Paths').
    layer: draw layer (see list_layers); on the default, 100, a path lies under
    the objects placed there. sorting: 0=over, 1=under other paths on its layer.
    smoothness: optional curve smoothing. width: optional width scale multiplier.
    snap: True puts it on the user's grid: the Custom Snap Mod's when that mod is
      loaded, else Dungeondraft's half-tile grid (see get_snap_settings).
    """
    params = {"points": points, "asset": asset, "layer": layer, "sorting": sorting}
    if smoothness is not None:
        params["smoothness"] = smoothness
    if width is not None:
        params["width"] = width
    return bridge.request("draw_path", **({"snap": True} if snap else {}), **params)


@_tool()
def add_light(
    x: Optional[float] = None,
    y: Optional[float] = None,
    color: str = "",
    energy: float = 1.0,
    range: float = 2.0,
    shadows: bool = True,
    asset: str = "",
    snap: bool = False,
) -> dict:
    """Add a light at a woxel position. Returns the new element id.

    color: '#rrggbb' (default warm). energy: brightness.
    range: Dungeondraft's own Range, the number its Light tool shows and its
      saved map and Universal VTT export carry (2.0 lights about two tiles
      from the centre with the default texture). The default is 2.0.
    asset: optional Lights gradient/cookie texture path.
    snap: True puts it on the user's grid: the Custom Snap Mod's when that mod is
      loaded, else Dungeondraft's half-tile grid (see get_snap_settings).
    """
    params = {"color": color, "energy": energy, "range": range, "shadows": shadows, "asset": asset}
    if x is not None:
        params["x"] = x
    if y is not None:
        params["y"] = y
    return bridge.request("add_light", **({"snap": True} if snap else {}), **params)


@_tool()
def add_portal(
    asset: str,
    x: Optional[float] = None,
    y: Optional[float] = None,
    closed: Optional[bool] = None,
    radius: float = 64.0,
    mount: str = "wall",
    snap_max: float = 256.0,
    flip: bool = False,
    fallback_free: bool = True,
    rotation: float = 0.0,
    snap: bool = False,
) -> dict:
    """Add a door/window portal at a woxel position. Returns the new element id.

    By default the portal MOUNTS on the nearest wall and the wall cuts a gap
    around it (like placing a door by hand).

    asset: a Portals asset path.
    closed: whether the portal blocks sight and light. Omitted, it follows
      the asset: a door is closed (an openable door in a VTT export); a
      window or an empty frame is open. Foundry and Roll20 go by this flag,
      not the picture.
    radius: door half-width (~128 ≈ 1-tile door, ~256 ≈ 2-tile door).
    mount: 'wall' (snap to nearest wall, cut gap) or 'free' (freestanding).
    snap_max: max woxel distance a wall may be and still capture the portal.
    flip: reverse the door's facing. fallback_free: if mount='wall' but no wall
    is within snap_max, place a freestanding portal instead of erroring.
    rotation: degrees, freestanding only.
    snap: True puts it on the user's grid: the Custom Snap Mod's when that mod is
      loaded, else Dungeondraft's half-tile grid (see get_snap_settings).
    """
    if closed is None:
        closed = mapfile.vtt_closed(asset)   # a door blocks; a window or an empty frame is open
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
    return bridge.request("add_portal", **({"snap": True} if snap else {}), **params)


@_tool()
def add_roof(
    points: list[list[float]],
    asset: str,
    width: float = 256.0,
    type: int = 0,
    sorting: int = 0,
) -> dict:
    """Add a roof from its RIDGE LINE. Returns the new element id.

    IMPORTANT: `points` is the roof's ridge (the peak line), NOT a footprint to
    trace. DD builds a complete, self-closing roof that slopes down `width`
    woxels perpendicular to each side of the ridge, with hips/gables off the
    ridge ends. The footprint covered = the ridge's bounding box expanded by
    `width` on every side.

    To roof a building W x H: put the ridge along the LONG axis, centered, and
    set width = half the SHORT dimension so the eaves reach the side walls. The
    ridge should be shorter than the long wall by ~width at each end (so the end
    hips land on the short walls). Two points that are close together give a
    near-pyramid (a square hip roof).

    asset: a Roofs asset path (list_assets('Roofs')). type: 0=gable, 1=hip,
    2=dormer. Roofs render above everything (z 800). Undoable as a create.
    """
    return bridge.request(
        "add_roof", points=points, asset=asset, width=width,
        type=type, sorting=sorting,
    )


@_tool()
def place_pattern(
    asset: str,
    rect: Optional[list[float]] = None,
    points: Optional[list[list[float]]] = None,
    category: str = "Patterns",
    color: str = "",
    rotation: Optional[float] = None,
    z: int = -100,
    snap: bool = False,
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
    z: DD pattern layer. Default -100 is DD's "below objects" layer (above
      terrain/floor shapes at -200); 100+ overlays on top of objects. The layer
      is saved with the map, so it survives reload and export.
    snap: True puts it on the user's grid: the Custom Snap Mod's when that mod is
      loaded, else Dungeondraft's half-tile grid (see get_snap_settings).
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
    return bridge.request("place_pattern", **({"snap": True} if snap else {}), **params)


@_tool()
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
    snap: bool = False,
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
    snap: True puts the boundary's corners on the user's grid (see get_snap_settings).
    """
    if (rect is None) == (points is None):
        raise ValueError("provide exactly one of 'rect' or 'points'")
    if floor not in ("pattern", "terrain", "none"):
        raise ValueError("floor must be 'pattern', 'terrain' or 'none'")
    if floor == "pattern" and not floor_asset:
        raise ValueError("floor='pattern' needs floor_asset: a texture from list_assets('Simple Tiles') "
                         "or another floor_category; or pass floor='none' for walls only")
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
    boundary = _shape_polygon(rect, points)
    if floor == "terrain":
        _refuse_if_protected("the room's terrain floor", polygon=boundary, radius=64)
    shared = _shared_walls(boundary, wall_type, wall_asset)
    if not shared["shared_wall_ids"]:
        return bridge.request("build_room", **({"snap": True} if snap else {}), **params)
    # Part of the boundary is already walled. Draw only the free sides, so a
    # door on the shared wall opens into both rooms (a second wall there would
    # still block the doorway in a VTT), and the floor on the whole boundary.
    if shared["enclosed"] and floor == "none":
        raise ValueError("every side of the boundary is already walled and floor='none': nothing to build")
    result: dict = {"wall_ids": [], "shared_wall_ids": shared["shared_wall_ids"],
                    "points": boundary, "notes": list(shared.get("notes", []))}
    with _undo_group("build_room"):
        for run in shared["runs"]:
            result["wall_ids"].append(
                draw_wall(run, asset=wall_asset, loop=False, type=wall_type, joint=wall_joint, snap=snap)["id"])
        if floor == "pattern":
            result["floor_id"] = place_pattern(
                floor_asset, points=boundary, category=floor_category, color=floor_color, snap=snap)["id"]
        elif floor == "terrain":
            result["floor_pixels"] = fill_region(points=boundary, slot=floor_slot, asset=floor_asset).get("pixels")
    result["note"] = (f"{len(shared['shared_wall_ids'])} existing wall(s) stand on the boundary and were "
                      f"reused; {len(result['wall_ids'])} new wall run(s) drawn")
    return result


def _shared_walls(boundary: list, wall_type: int, wall_asset: str) -> dict:
    """Existing walls that already stand on the room boundary, same kind only.

    The snapshot has every wall's geometry but not its type, so a first pass
    assumes the type matches to find the candidates, and only those few are
    read back with get_element before the real decision.
    """
    snapshot = bridge.request("spatial_snapshot")
    guess = [dict(w, type=wall_type) for w in snapshot.get("walls", [])]
    first = layout.shared_walls(boundary, guess, wall_type=wall_type)
    if not first["shared_wall_ids"]:
        return first
    # Keep the snapshot's geometry (world space); get_element reports a wall's
    # raw Points, which are local to the node and differ once a wall was moved.
    by_id = {w["id"]: w for w in snapshot.get("walls", [])}
    typed = []
    for wid in first["shared_wall_ids"]:
        el = bridge.request("get_element", id=wid)
        typed.append(dict(by_id[wid], type=el.get("type", wall_type), asset=el.get("asset", "")))
    return layout.shared_walls(boundary, typed, wall_type=wall_type, wall_asset=wall_asset)


@_tool()
def add_text(
    text: str,
    x: Optional[float] = None,
    y: Optional[float] = None,
    size: Optional[int] = None,
    color: str = "",
    font: str = "",
    snap: bool = False,
) -> dict:
    """Add a text label at a woxel position. Returns the new element id and size.

    size: font size in points (default ~32). color: '#rrggbb' (default black).
    font: a DD font name; omit to keep the default font.
    snap: True puts it on the user's grid: the Custom Snap Mod's when that mod is
      loaded, else Dungeondraft's half-tile grid (see get_snap_settings).
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
    return bridge.request("add_text", **({"snap": True} if snap else {}), **params)


@_tool()
def import_image(
    path: str,
    x: Optional[float] = None,
    y: Optional[float] = None,
    scale: float = 1.0,
    rotation: float = 0.0,
    sorting: int = 0,
    shadow: bool = True,
    layer: int = spatial.DEFAULT_LAYER,
    check: bool = False,
    allow_overlap: bool = False,
    region: str = "",
    snap: bool = False,
) -> dict:
    """Embed an image file from disk into the map as an object. Returns the new element id.

    Use it for art that is not in an asset pack: a custom prop or token, a
    handout, or a whole battle map as a backdrop. The pixels are stored inside
    the map file (as when a file is dropped onto Dungeondraft), so the saved
    map does not need the original image.

    path: absolute path to a .png, .jpg or .webp on the machine running
    Dungeondraft. PNG keeps transparency. x, y: woxel position of the image's
    center (defaults to map center). At scale=1, 256 image pixels cover one
    tile. rotation: degrees. sorting: 0=over, 1=under other objects.
    shadow: drop shadow; pass False for a backdrop. layer: draw layer, as in
    place_object (-400 puts a backdrop under floors and everything else).

    The result is an ordinary object: move_element, modify_object,
    duplicate_object and delete_element work on it, and undo removes it.
    Importing the same path again reuses the copy already embedded (even if
    the file changed since); save an edited image under a new name.

    check / allow_overlap / region: the placement check, as in place_object.
    snap: True puts it on the user's grid: the Custom Snap Mod's when that mod is
      loaded, else Dungeondraft's half-tile grid (see get_snap_settings).
    """
    params: dict = {
        "path": os.path.abspath(os.path.expanduser(path)),
        "scale": scale, "rotation": rotation, "sorting": sorting, "shadow": shadow,
        "layer": layer,
    }
    if x is not None:
        params["x"] = x
    if y is not None:
        params["y"] = y
    if check or region:
        _refuse_if_blocked("import_image", params, allow_overlap, region)
    return bridge.request("import_image", **({"snap": True} if snap else {}), **params)


@_tool()
def set_trace_image(
    path: str = "",
    x: Optional[float] = None,
    y: Optional[float] = None,
    scale: float = 1.0,
    opacity: float = 0.5,
) -> dict:
    """Show a reference image over the map to trace from, or remove it with an empty path.

    For rebuilding a map from a picture: put a floor plan or sketch on screen,
    draw walls and floors over it, and compare with screenshot. Unlike
    import_image it is not part of the map: Dungeondraft leaves it out of
    exports, and there is only one at a time.

    path: absolute path to a .png, .jpg or .webp. x, y: woxel position of its
    center (defaults to the map center). scale: 256 image pixels cover one tile
    at 1.0. opacity: 0-1.
    """
    if not path:
        return bridge.request("set_trace_image", path="")
    params: dict = {"path": os.path.abspath(os.path.expanduser(path)), "scale": scale, "opacity": opacity}
    if x is not None:
        params["x"] = x
    if y is not None:
        params["y"] = y
    return bridge.request("set_trace_image", **params)


@_tool()
def scatter_objects(
    assets: list[str],
    rect: Optional[list[float]] = None,
    points: Optional[list[list[float]]] = None,
    count: int = 20,
    seed: int = 0,
    min_scale: float = 0.8,
    max_scale: float = 1.2,
    random_rotation: bool = True,
    layer: int = spatial.DEFAULT_LAYER,
    allow_overlap: bool = False,
) -> dict:
    """Scatter objects at random over an area in one call: a stand of trees, rocks, rubble, crates.

    assets: one or more Objects asset paths; each object picks one at random.
    Provide ONE of rect: [x, y, w, h] or points: [[x,y], ...] (>= 3), in
    woxels; object centers fall inside it. count: how many to place (1-200).
    seed: the same seed gives the same scatter. min_scale / max_scale: each
    object's scale is drawn from this range. random_rotation: turn each one
    freely (pass False for things that should stay upright).

    Every spot is checked like place_object(check=True): nothing lands across
    a wall, in a doorway, off the map or in a 'clearance' region, and objects
    don't overlap each other or what is already there. allow_overlap=True lets
    them overlap, which is what a forest canopy needs. Spots that fail are
    re-drawn, so fewer than count may fit; the result says how many were placed
    and returns their ids. One undo() removes the whole scatter.
    """
    if (rect is None) == (points is None):
        raise ValueError("provide exactly one of 'rect' or 'points'")
    if not assets or not 1 <= count <= 200:
        raise ValueError("give at least one asset and a count from 1 to 200")
    if not 0 < min_scale <= max_scale:
        raise ValueError("need 0 < min_scale <= max_scale")
    area = _shape_polygon(rect, points)
    if len(area) < 3 or spatial.area(area) <= 0:
        raise ValueError("the scatter area needs at least 3 points and some area")
    locals_ = {a: bridge.request("asset_footprint", asset=a)["local"] for a in dict.fromkeys(assets)}
    scene = bridge.request("spatial_snapshot")
    x0, y0, x1, y1 = spatial.bounds(area)
    rng = random.Random(seed)
    ids: list[int] = []
    attempts = 0
    # The undo group opens with the first placement (an empty scatter records
    # nothing) and always closes, so a bridge error mid-way cannot leave later
    # edits joining a stale group.
    group = _undo_group("scatter_objects")
    opened = False
    try:
        while len(ids) < count and attempts < count * 30:
            attempts += 1
            x, y = round(rng.uniform(x0, x1), 1), round(rng.uniform(y0, y1), 1)
            asset = rng.choice(assets)
            s = round(rng.uniform(min_scale, max_scale), 3)
            turn = round(rng.uniform(0, 360), 1) if random_rotation else 0.0
            if not spatial.point_in_polygon((x, y), area):
                continue
            footprint = spatial.transform(locals_[asset], (x, y), (s, s), turn)
            if spatial.check_footprint(scene, footprint, layer=layer, allow_overlap=allow_overlap):
                continue
            if not opened:
                group.__enter__()
                opened = True
            made = bridge.request("place_object", asset=asset, x=x, y=y, scale=s, rotation=turn, layer=layer)
            ids.append(made["id"])
            # Later objects must keep clear of this one too.
            scene["objects"].append({"id": made["id"], "asset": asset, "layer": layer, "polygon": footprint})
    finally:
        if opened:
            group.__exit__(None, None, None)
    result: dict = {"placed": len(ids), "requested": count, "ids": ids, "seed": seed}
    if len(ids) < count:
        result["note"] = (f"only {len(ids)} of {count} fit after {attempts} tries; "
                          "use a larger area, smaller scales or allow_overlap=True")
    return result


@_tool()
def draw_water(
    rect: Optional[list[float]] = None,
    points: Optional[list[list[float]]] = None,
    erase: bool = False,
    deep_color: str = "",
    shallow_color: str = "",
    blend_distance: Optional[float] = None,
) -> dict:
    """Draw (or erase) water: a lake, pond, river or moat, in woxel coords.

    Provide ONE of:
      rect: [x, y, w, h] axis-aligned rectangle, or
      points: [[x,y], ...] a polygon (>= 3 points); trace a river as a long
      thin polygon along both banks.
    The shape is merged into the water already on the level; Dungeondraft adds
    the shoreline and the shallow-to-deep gradient. erase=True cuts the shape
    out of existing water instead (an island, or removing a pond).

    deep_color / shallow_color: '#rrggbb' for the middle and the shore.
    blend_distance: tiles from the shore to full depth (0-4). Each defaults to
    the Water Brush's current setting (initially '#3aa19a', '#8bceb0', 1.5).
    They apply to the body of water this shape creates or joins; the result
    reports the values used. Undoable via undo().
    """
    if (rect is None) == (points is None):
        raise ValueError("provide exactly one of 'rect' or 'points'")
    params: dict = {"erase": erase}
    if rect is not None:
        params["rect"] = rect
    if points is not None:
        params["points"] = points
    if deep_color:
        params["deep_color"] = deep_color
    if shallow_color:
        params["shallow_color"] = shallow_color
    if blend_distance is not None:
        params["blend_distance"] = blend_distance
    _refuse_if_protected("the water", polygon=_shape_polygon(rect, points), radius=32)
    return bridge.request("draw_water", **params)


@_tool()
def draw_material(
    asset: str,
    rect: Optional[list[float]] = None,
    points: Optional[list[list[float]]] = None,
    layer: int = -400,
    smooth: Optional[bool] = None,
    erase: bool = False,
) -> dict:
    """Paint (or erase) a ground material over a region: lava, ice, cobblestone, acid, etc.

    A material is a textured patch with its own blended border (the Material
    Brush). Unlike terrain it is independent of the 4/8 terrain slots, so any
    number of materials can be used on one level.

    asset: a Materials asset path from list_assets(category='Materials').
    Provide ONE of rect: [x, y, w, h] or points: [[x,y], ...] (>= 3 points), in
    woxels. Materials are drawn on a half-tile grid with an organic edge, so
    the patch covers the shape and may run up to a quarter tile (64 woxels)
    past it; do not rely on it for exact edges.

    layer: one of the level's draw layers. -400 'Below Ground' (default) lies
    on the terrain, beneath floors, objects and walls. The others are, unless
    the map renamed them: -100 'Below Water', 100/200/300/400 'User Layer 1-4',
    700 'Above Walls', 900 'Above Roofs'.
    smooth: round the outline (default on for a new material patch).
    erase=True removes this material from the region. Undoable via undo().
    """
    if (rect is None) == (points is None):
        raise ValueError("provide exactly one of 'rect' or 'points'")
    params: dict = {"asset": asset, "layer": layer, "erase": erase}
    if rect is not None:
        params["rect"] = rect
    if points is not None:
        params["points"] = points
    if smooth is not None:
        params["smooth"] = smooth
    # A material patch can run a quarter tile past its shape, plus its border.
    _refuse_if_protected("the material", polygon=_shape_polygon(rect, points), radius=96)
    return bridge.request("draw_material", **params)


# --------------------------------------------------------------------------
# Terrain
# --------------------------------------------------------------------------

@_tool()
def set_terrain_slot(asset: str, slot: int = 0, expand: bool = False) -> dict:
    """Assign a Terrain asset to a terrain slot index so it can be filled/painted with that slot.

    A level has slots 0-3. Slots 4-7 exist only once the level's expanded
    slots are on (get_status reports terrain_slots: 4 or 8): pass expand=True
    with a slot of 4-7 to turn them on, which stays on for the level.
    """
    params: dict = {"asset": asset, "slot": slot}
    if expand:
        params["expand"] = True
    return bridge.request("set_terrain_slot", **params)


@_tool()
def fill_terrain(slot: int = 0, asset: str = "") -> dict:
    """Flood-fill the whole current level with a terrain slot. If asset is given, it is assigned to the slot first."""
    params = {"slot": slot}
    if asset:
        params["asset"] = asset
    _refuse_if_protected("a whole-level terrain fill", whole_level=True)
    return bridge.request("fill_terrain", **params)


@_tool()
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
    # Terrain is a 64-woxel grid of blended weights: a fill bleeds about one cell.
    _refuse_if_protected("the terrain fill", polygon=_shape_polygon(rect, points), radius=64)
    return bridge.request("fill_region", **params)


@_tool()
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
    if x is not None and y is not None:
        _refuse_if_protected("the terrain brush", path=[(x, y)], radius=radius + 64)
    else:
        _refuse_if_protected("the terrain brush (at the map center)", whole_level=True)
    return bridge.request("paint_terrain", **params)


@_tool()
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
    _refuse_if_protected("the terrain stroke", path=points, radius=radius + 64)
    return bridge.request("paint_path", **params)


@_tool()
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
    # Caves are cut on a 64-woxel grid and grow a rocky border around the opening.
    _refuse_if_protected("the cave", path=points, radius=radius + 128)
    return bridge.request("dig_cave", **params)


@_tool()
def clear_caves() -> dict:
    """Wipe the entire cave layer back to solid rock.

    Removes all carved caves at once (the whole cave system), rebuilding the
    mesh. Undoable like dig_cave. Use this instead of filling regions back with
    dig_cave(dig=False) when you want to reset all caves.
    """
    _refuse_if_protected("clearing the caves", whole_level=True)
    return bridge.request("clear_caves")


# --------------------------------------------------------------------------
# Modify / delete
# --------------------------------------------------------------------------

@_tool()
def move_element(
    id: int,
    x: float,
    y: float,
    check: bool = False,
    allow_overlap: bool = False,
    region: str = "",
    snap: bool = False,
) -> dict:
    """Move any element to a new woxel position by id.

    check / allow_overlap / region: for an object, the placement check as in
    place_object, applied to where it would land (other kinds are not checked).
    snap: True puts it on the user's grid: the Custom Snap Mod's when that mod is
      loaded, else Dungeondraft's half-tile grid (see get_snap_settings).
    """
    params = {"id": id, "x": x, "y": y}
    if check or region:
        _refuse_if_blocked("move_element", params, allow_overlap, region)
    return bridge.request("move_element", **({"snap": True} if snap else {}), **params)


@_tool()
def modify_object(
    id: int,
    scale: Optional[float] = None,
    rotation: Optional[float] = None,
    color: str = "",
    shadow: Optional[bool] = None,
    mirror: Optional[bool] = None,
    block_light: Optional[bool] = None,
    check: bool = False,
    allow_overlap: bool = False,
    region: str = "",
) -> dict:
    """Modify an existing object by id: scale, rotation (degrees), color ('#rrggbb'), shadow, mirror, block_light.

    color applies only to 'Colorable' objects; otherwise it is ignored and the
    result has a 'warning'. The returned descriptor includes 'colorable' and 'color'.
    mirror: flip the object left-to-right. block_light: make it cast shadows
    from lights, following its silhouette (a pillar, a statue, a big crate).

    check / allow_overlap / region: the placement check as in place_object,
    applied to the footprint after the new scale and rotation.
    """
    params: dict = {"id": id}
    if scale is not None:
        params["scale"] = scale
    if rotation is not None:
        params["rotation"] = rotation
    if color:
        params["color"] = color
    if shadow is not None:
        params["shadow"] = shadow
    if mirror is not None:
        params["mirror"] = mirror
    if block_light is not None:
        params["block_light"] = block_light
    if check or region:
        _refuse_if_blocked("modify_object", params, allow_overlap, region)
    return bridge.request("modify_object", **params)


@_tool()
def modify_wall(
    id: int,
    points: Optional[list[list[float]]] = None,
    loop: Optional[bool] = None,
    type: Optional[int] = None,
    joint: Optional[int] = None,
    asset: str = "",
    color: str = "",
    shadow: Optional[bool] = None,
) -> dict:
    """Edit an existing wall by id: reshape it and/or change its look. Returns the wall with its points.

    points: the wall's new [[x, y], ...] woxel points, replacing all of them
    (read the current ones with get_element). loop: closed loop or open run.
    type: 0=auto, 1=manual, 2=cave. joint: 0=sharp, 1=bevel, 2=round.
    asset: a Walls asset path; the wall takes that texture's default tint unless
    color is given. color: '#rrggbb' tint. shadow: wall shadow on/off.
    Anything omitted is left as it is.

    Doors and windows mounted on the wall are kept: when points move they stay
    at the same place along their segment and turn with it. That only works
    while the wall keeps its shape, so a wall with doors/windows must get the
    same number of points and unchanged loop/type/joint, and a segment can't
    shrink below its door's width; otherwise the call fails and nothing
    changes. To reshape such a wall, delete its doors first and add them back.
    Undoable via undo().
    """
    params: dict = {"id": id}
    if points is not None:
        params["points"] = points
    if loop is not None:
        params["loop"] = loop
    if type is not None:
        params["type"] = type
    if joint is not None:
        params["joint"] = joint
    if asset:
        params["asset"] = asset
    if color:
        params["color"] = color
    if shadow is not None:
        params["shadow"] = shadow
    return bridge.request("modify_wall", **params)


@_tool()
def modify_light(
    id: int,
    color: str = "",
    energy: Optional[float] = None,
    range: Optional[float] = None,
    shadows: Optional[bool] = None,
    rotation: Optional[float] = None,
    asset: str = "",
) -> dict:
    """Change an existing light by id: color, brightness, size, shadows, rotation or texture. Returns the light.

    color: '#rrggbb'. energy: brightness (>= 0). range: Dungeondraft's own Range, as the Light tool shows it (see
      add_light); a texture change alone keeps the Range.
    rotation: degrees, for a directional texture. asset: a Lights asset path.
    Anything omitted is kept; move a light with move_element. Undoable.
    """
    params: dict = {"id": id}
    if color:
        params["color"] = color
    if energy is not None:
        params["energy"] = energy
    if range is not None:
        params["range"] = range
    if shadows is not None:
        params["shadows"] = shadows
    if rotation is not None:
        params["rotation"] = rotation
    if asset:
        params["asset"] = asset
    return bridge.request("modify_light", **params)


@_tool()
def modify_text(
    id: int,
    text: Optional[str] = None,
    size: Optional[int] = None,
    color: str = "",
    font: str = "",
) -> dict:
    """Change an existing text label by id: its wording, font size, color or font. Returns the label.

    Anything omitted is kept; move a label with move_element. Undoable.
    """
    params: dict = {"id": id}
    if text is not None:
        params["text"] = text
    if size is not None:
        params["size"] = size
    if color:
        params["color"] = color
    if font:
        params["font"] = font
    return bridge.request("modify_text", **params)


@_tool()
def set_element_layer(id: int, layer: int) -> dict:
    """Move an object, path or pattern floor to another draw layer (see list_layers).

    Layers decide what covers what regardless of creation order: a rug on 100
    stays under a table on 200. Walls, portals, roofs, lights and texts have
    fixed layers and are refused. Undoable via undo().
    """
    return bridge.request("set_element_layer", id=id, layer=layer)


@_tool()
def duplicate_object(
    id: int,
    dx: float = 64.0,
    dy: float = 0.0,
    check: bool = False,
    allow_overlap: bool = False,
    region: str = "",
) -> dict:
    """Duplicate an object by id, offset by (dx, dy) woxels. Returns the new element id.

    The copy keeps the source's scale, rotation, color and layer.
    check / allow_overlap / region: the placement check as in place_object,
    applied to the copy (which can then not overlap its source either).
    """
    params = {"id": id, "dx": dx, "dy": dy}
    if check or region:
        _refuse_if_blocked("duplicate_object", params, allow_overlap, region)
    return bridge.request("duplicate_object", **params)


@_tool()
def delete_element(id: int) -> dict:
    """Delete any element from the map by id. Reversible with undo().

    An object is kept serialised and anything else is held off the map, so
    undo() puts it back with the same id. To take back what build_room,
    scatter_objects or a batch just made, call undo() once instead of
    deleting the pieces: each of those is one undo step.
    """
    return bridge.request("delete_element", id=id)


@_tool()
def delete_elements(ids: list[int]) -> dict:
    """Delete several elements in ONE call and ONE undo() step.

    ids: up to 100 element ids. Every id is checked first, so an unknown id
    deletes nothing. Reversible with one undo().
    """
    return bridge.request("delete_elements", ids=ids)


# --------------------------------------------------------------------------
# Map lifecycle
# --------------------------------------------------------------------------

@_tool()
def get_current_map() -> dict:
    """Report the open map: {map_open, path, dirty, saving}.

    path is the map file's full path, or "" if the map has never been saved.
    dirty is always null: Dungeondraft exposes no unsaved-changes flag.
    saving is true while a save is still being written.
    """
    return bridge.request("get_current_map")


@_tool()
def save_map() -> dict:
    """Save the open map to its current file, like DD's Save button. Returns {saved, path, saving}.

    saved=True means a save was REQUESTED, not that it finished: the write can
    still be running (saving=True), and a save that fails inside Dungeondraft
    also replies saved=True. To confirm, poll get_current_map until saving is
    False AND the map file's modification time has advanced.
    Fails with "map has never been saved" (no file yet; save it once in DD),
    "save already in progress", or "no map open".
    """
    return bridge.request("save_map")


@_tool()
def open_map(path: str) -> dict:
    """Open another saved map file in Dungeondraft. Returns {opening: path}.

    Returns BEFORE the new map has loaded: the switch happens on the next frame
    and the bridge is briefly unavailable. Poll get_current_map until its path
    names the new map before sending further commands. Unsaved changes to the
    current map are discarded silently (no prompt); call save_map first to keep them.
    Fails with "file not found: <path>", or "save in progress" while a save is
    still being written (retry shortly). If a save starts just after this
    replies, the switch waits for it, and gives up (staying on the current
    map) if it hasn't finished within 30 s.
    """
    return bridge.request("open_map", path=path)


# --------------------------------------------------------------------------
# Levels
# --------------------------------------------------------------------------

@_tool()
def add_level(label: str = "Level", terrain: bool = False) -> dict:
    """Add a new, empty level (floor) to the map. Returns its id, label and index.

    The new level becomes the top floor (index 0), so every other level's index
    goes up by one. The current level stays current: call set_level(0) to work
    on the new one.

    terrain: whether the new level shows a terrain layer. Off by default, as in
    Dungeondraft: an upper floor has no ground, only the floors you draw, and
    compare_levels can show the level below through it. Pass True for a level
    that needs ground of its own (a cellar, a cave). The terrain tools paint a
    hidden terrain without showing anything; get_status reports terrain_visible.
    """
    return bridge.request("add_level", label=label, terrain=terrain)


@_tool()
def set_level(index: int) -> dict:
    """Switch the active level (floor) by its index (see list_levels). All edits go to the active level."""
    return bridge.request("set_level", index=index)


@_tool()
def rename_level(index: int, label: str) -> dict:
    """Rename a level (floor) by its index. Returns the level list."""
    return bridge.request("rename_level", index=index, label=label)


@_tool()
def clone_level(index: int, label: str = "") -> dict:
    """Copy a whole level (floor) with everything on it. Returns the copy's id, label and index.

    Use it to start an upper floor from the footprint of the one below. Like
    add_level, the copy becomes the top floor (index 0) and is not switched to.
    Its elements get new ids, and it finishes loading a moment after the reply:
    wait for get_status counts to settle after set_level before editing it.
    label defaults to '<source label> copy'.
    """
    params: dict = {"index": index}
    if label:
        params["label"] = label
    return bridge.request("clone_level", **params)


@_tool()
def reorder_levels(ids: list[int]) -> dict:
    """Set the order of the levels (floors): every level id exactly once, top floor first. Returns the level list."""
    return bridge.request("reorder_levels", ids=ids)


@_tool()
def compare_levels(
    index: int = -1,
    reference_opacity: float = 0.35,
    current_opacity: float = 1.0,
) -> dict:
    """Show another level faintly under the current one, to line floors up. index=-1 turns it off.

    index: the level to show as the reference (not the current level).
    reference_opacity / current_opacity: 0-1. The overlay is an editing aid
    tied to the current level; it also appears in screenshot, so turn it off
    before a final look.
    """
    return bridge.request(
        "compare_levels", index=index,
        reference_opacity=reference_opacity, current_opacity=current_opacity)


@_tool()
def add_layer(layer: int, label: str = "") -> dict:
    """Add a draw layer to the current level, e.g. 150 to sit between layers 100 and 200. Returns the layer list.

    layer: a z value from -4096 to 4096 that is not already a layer; a higher
    value draws on top. label: the name shown in Dungeondraft's layer menus.
    The default layers (see list_layers) are enough for most maps.
    """
    params: dict = {"layer": layer}
    if label:
        params["label"] = label
    return bridge.request("add_layer", **params)


@_tool()
def configure_level(
    ambient_light: str = "",
    lighting: Optional[bool] = None,
    terrain_visible: Optional[bool] = None,
    smooth_terrain: Optional[bool] = None,
    water_shoreline: Optional[bool] = None,
) -> dict:
    """Read or change the current level's overall look. With no arguments it just reports the settings.

    ambient_light: '#rrggbb', the color of everything no light reaches. White
    (the default) is full daylight; a dark blue such as '#2a2f4a' makes a night
    scene where the placed lights stand out; a dim warm gray suits a cellar.
    lighting: turn lighting on or off. Off shows the map flat, as if fully lit.
    Unlike the rest, this applies to every level.
    terrain_visible: show or hide this level's terrain (see add_level).
    smooth_terrain: blend terrain textures softly instead of with a rough edge.
    water_shoreline: show or hide the outline drawn around this level's water.
    Not undoable; call it again with the old values to go back.
    """
    params: dict = {}
    if ambient_light:
        params["ambient_light"] = ambient_light
    for name, value in (("lighting", lighting), ("terrain_visible", terrain_visible),
                        ("smooth_terrain", smooth_terrain), ("water_shoreline", water_shoreline)):
        if value is not None:
            params[name] = value
    return bridge.request("configure_level", **params)


# --------------------------------------------------------------------------
# Capture — let the model see its own work
# --------------------------------------------------------------------------

@_tool()
def screenshot() -> Image:
    """Capture the current Dungeondraft window (the on-screen view) and return it as an image.

    Fast; shows exactly what's visible including the current camera framing. Use this
    to check your work as you build. For a clean full-map render without UI, use export_map.
    """
    res = bridge.request("screenshot", path=_SCREENSHOT_PATH)
    return Image(path=res["path"])


@_tool()
def export_map(ppi: int = 40, grid: Optional[bool] = None) -> Image:
    """Render the entire current map to a clean PNG (no UI) and return it as an image.

    ppi controls resolution (pixels per grid cell): higher = sharper but larger/slower.
    grid: False exports without the grid, True with it; omitted = as currently shown
    in the editor. The editor's grid setting is restored afterwards.
    The render runs asynchronously in Dungeondraft; this waits for it to finish.
    """
    _run_export("png", _EXPORT_PATH, ppi, grid)
    return Image(path=_EXPORT_PATH)


@_tool()
def export_uvtt(dest_path: str = "", ppi: int = 100, grid: bool = False) -> dict:
    """Export the map as Universal VTT (.dd2vtt) for Foundry / Roll20: the image plus walls, doors and lights.

    dest_path: where to write the .dd2vtt (the extension is added if missing);
    default is a temp file. Point it somewhere your VTT can reach, then import it
    there (Foundry: the Universal Battlemap / DD Import module).
    ppi: pixels per grid cell in the embedded image; 100 matches Foundry's default grid.
    grid: False (default) leaves the grid out, since VTTs draw their own.
    Returns the path plus a summary parsed from the file: map size in cells and
    counts of wall segments, object (furniture) walls, portals/doors and lights.
    Large maps at high ppi take a while; this waits up to 3 minutes.
    """
    path = os.path.abspath(os.path.expanduser(dest_path)) if dest_path else _UVTT_PATH
    if not path.lower().endswith(".dd2vtt"):
        path += ".dd2vtt"
    _run_export("uvtt", path, ppi, grid, timeout=180.0)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    res = data.get("resolution") or {}
    size = res.get("map_size") or {}
    return {
        "path": path,
        "format_version": data.get("format"),
        "map_size_cells": [size.get("x"), size.get("y")],
        "pixels_per_grid": res.get("pixels_per_grid"),
        "walls": len(data.get("line_of_sight") or []),
        "object_walls": len(data.get("objects_line_of_sight") or []),
        "portals": len(data.get("portals") or []),
        "lights": len(data.get("lights") or []),
        "bytes": os.path.getsize(path),
    }


@_tool()
def set_grid(visible: Optional[bool] = None) -> dict:
    """Show (True) or hide (False) the editor grid, like DD's Grid toggle; omit to just read it.

    Affects screenshot (and export_map when its grid param is omitted). Returns {grid}.
    """
    params: dict = {}
    if visible is not None:
        params["visible"] = visible
    return bridge.request("set_grid", **params)


# --------------------------------------------------------------------------
# Camera
# --------------------------------------------------------------------------

@_tool()
def reset_camera() -> dict:
    """Reset the editor view: default anchor, centered on the map, 100% zoom.

    Use when the view is lost or stuck (for example zoomed far out and off the
    map after an export that failed inside Dungeondraft). Returns the camera state.
    """
    return bridge.request("reset_camera")


@_tool()
def get_camera() -> dict:
    """Report the editor camera: world-center position [x,y], zoom, and viewport size.

    zoom is a Camera2D factor where LARGER = zoomed OUT (zoom 0.5 magnifies 2x,
    zoom 2.0 shows twice as much). Use this to read the view before adjusting it.
    """
    return bridge.request("get_camera")


@_tool()
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


@_tool()
def focus_element(id: int, zoom: Optional[float] = None) -> dict:
    """Center the camera on an element by id (works for any kind, including text).

    zoom: optional Camera2D factor to apply (LARGER = zoomed OUT). Follow with
    screenshot() to verify a specific element (e.g. a door cut into a wall).
    """
    params: dict = {"id": id}
    if zoom is not None:
        params["zoom"] = zoom
    return bridge.request("focus_element", **params)


@_tool()
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

@_tool()
def select_elements(ids: list[int]) -> dict:
    """Select the given element ids in Dungeondraft's UI (replaces the current selection)."""
    return bridge.request("select_elements", ids=ids)


@_tool()
def clear_selection() -> dict:
    """Clear the current selection in Dungeondraft."""
    return bridge.request("clear_selection")


# --------------------------------------------------------------------------
# Layout checks — sizes, collisions and regions (geometry in spatial.py)
# --------------------------------------------------------------------------

def _round_points(points) -> list:
    return [[round(x, 1), round(y, 1)] for x, y in points]


@_tool()
def get_asset_footprint(asset: str) -> dict:
    """How big an Objects asset really is: call this before choosing a scale or a spot for furniture.

    Sprites differ wildly in size and most carry transparent padding, so the
    footprint is the box around the asset's visible pixels. Returns, at scale 1:
    footprint [w, h] in woxels and in tiles, the full image size, and the
    footprint's center offset from the placement point (usually ~0). To make an
    object N tiles wide use scale = N / footprint_tiles[0].
    """
    fp = bridge.request("asset_footprint", asset=asset)
    x, y, w, h = fp["visible"]
    return {
        "asset": fp["asset"],
        "image_size": fp["size"],
        "footprint": [w, h],
        "footprint_tiles": [round(w / 256, 2), round(h / 256, 2)],
        "center_offset": [round(x + w / 2 - fp["size"][0] / 2, 1), round(y + h / 2 - fp["size"][1] / 2, 1)],
    }


@_tool()
def check_object_placement(
    asset: str = "",
    id: Optional[int] = None,
    x: Optional[float] = None,
    y: Optional[float] = None,
    scale: Optional[float] = None,
    rotation: Optional[float] = None,
    layer: Optional[int] = None,
    allow_overlap: bool = False,
    wall_clearance: float = 0.0,
    door_clearance: float = 128.0,
    object_clearance: float = 0.0,
    region: str = "",
) -> dict:
    """Try a spot for an object without changing the map. Returns {ok, conflicts, footprint}.

    Give asset (a new object: x, y, scale, rotation, layer as for place_object)
    OR id (an existing object: x, y to move it, scale / rotation to change it;
    anything omitted stays as it is).

    The footprint is the box around the sprite's visible pixels, turned and
    scaled with the object. Conflicts, each with a code and the element
    involved:
      outside_map    sticks out of the map
      wall           crosses a wall (its thickness included); flush is fine.
                     wall_clearance asks for extra room, in woxels
      door           within door_clearance woxels in front of or behind a door
                     (windows don't count; 0 turns this off)
      object         overlaps another object on the SAME layer, or comes
                     within object_clearance of one. Put rugs on a lower layer
                     and tabletop items on a higher one; allow_overlap=True
                     skips this check
      clearance      inside a 'clearance' region (see set_spatial_region)
      outside_room   not fully inside the 'room' region named by region
    A sofa is still a rectangle here and a door is told from a window by its
    asset name: treat this as a guard against obvious mistakes, then look at a
    screenshot.
    """
    if bool(asset) == (id is not None):
        raise ValueError("give exactly one of 'asset' (a new object) or 'id' (an existing one)")
    if asset:
        scene = bridge.request("spatial_snapshot", asset=asset)
        params = {"asset": asset, "x": x, "y": y, "scale": 1.0 if scale is None else scale,
                  "rotation": rotation or 0.0,
                  "layer": spatial.DEFAULT_LAYER if layer is None else layer}
        proposal = spatial.proposed_footprint(scene, "place_object", params)
    else:
        scene = bridge.request("spatial_snapshot")
        obj = spatial.find_object(scene, id)
        if obj is None:
            raise ValueError(f"no object with id {id} on the current level")
        proposal = spatial.moved(obj, x=x, y=y, scale=scale, rotation=rotation)
        if layer is not None:
            proposal.layer = layer
    conflicts = spatial.check_footprint(
        scene, proposal.polygon, layer=proposal.layer, ignore_id=proposal.ignore_id,
        allow_overlap=allow_overlap, wall_clearance=wall_clearance,
        door_clearance=door_clearance, object_clearance=object_clearance, region=region)
    return {"ok": not conflicts, "conflicts": conflicts,
            "footprint": _round_points(proposal.polygon), "layer": proposal.layer}


@_tool()
def validate_layout(region: str = "", door_clearance: float = 128.0, limit: int = 100) -> dict:
    """Audit every object on the current level for layout mistakes. Run it after furnishing.

    Returns:
      problems: objects that stick out of the map, cross a wall, stand in a
      doorway (within door_clearance woxels) or sit in a 'clearance' region,
      each with the object's id and the wall / door / region involved;
      overlaps: pairs of objects on the same layer that overlap. Often
      intended (tree canopies, chairs under a table), so review them rather
      than treating each as an error;
      counts, objects_checked, and ok (true when there are no problems).
    region: only audit objects whose center is inside this region.
    limit caps each list; counts always cover everything.
    """
    scene = bridge.request("spatial_snapshot")
    return spatial.audit(scene, door_clearance=door_clearance, region=region, limit=limit)


@_tool()
def set_spatial_region(
    name: str,
    kind: str = "clearance",
    rect: Optional[list[float]] = None,
    points: Optional[list[list[float]]] = None,
) -> dict:
    """Mark an area of the current level for the layout checks. Returns the level's regions.

    Provide ONE of rect: [x, y, w, h] or points: [[x,y], ...] (>= 3), in woxels.
    kind:
      'clearance'  keep it free of objects: a corridor, the path to a door.
                   Checked placements (check=True) refuse it and
                   validate_layout reports objects in it.
      'room'       a room's interior, for the region= argument of the object
                   tools: the object must fit entirely inside. Draw it inside
                   the walls, not on their centerline.
      'protected'  finished ground that must not be painted over: every
                   terrain, cave, water and material tool refuses an edit that
                   would reach it (whole-level fills included) until it is
                   removed.
    Reusing a name replaces that region. Regions are bookkeeping for these
    checks only: nothing is drawn, and they last until the map is closed or
    reloaded, not in the saved file.
    """
    if (rect is None) == (points is None):
        raise ValueError("provide exactly one of 'rect' or 'points'")
    params: dict = {"name": name, "kind": kind}
    if rect is not None:
        params["rect"] = rect
    if points is not None:
        params["points"] = points
    return bridge.request("set_spatial_region", **params)


@_tool()
def remove_spatial_region(name: str) -> dict:
    """Remove a region set with set_spatial_region from the current level. Returns the remaining regions."""
    return bridge.request("remove_spatial_region", name=name)


@_tool()
def list_spatial_regions() -> dict:
    """List the current level's regions (name, kind, points) set with set_spatial_region."""
    return bridge.request("list_spatial_regions")


# --------------------------------------------------------------------------
# Native access — Dungeondraft's own API and UI, for what no tool above covers
# --------------------------------------------------------------------------
#
# Shared notes for the native_* and ui_* tools:
#   target: "World", "Editor", "Level" (the current level), "Camera",
#   "Exporter", "Header", "Tool:<name>", "Window:<name>", "Element:<id>",
#   "Level:<id>", or an "Object:<n>" handle returned by one of these tools,
#   optionally followed by members: "Level.Terrain", "Tool:WaterBrush.Mesh".
#   Values come back as plain JSON where possible; other Godot types as
#   {"$type": "Vector2", "value": [x, y]} and objects as
#   {"$target": "Object:<n>", "class": ...}, which you can pass back in.

@_tool()
def native_targets() -> dict:
    """List the named entry points into Dungeondraft's own objects: the roots, every tool and every window.

    Start here when no dedicated tool does what you need, then native_describe
    a target to see what it offers. See the Dungeondraft modding API reference
    for what the members mean.
    """
    return bridge.request("native_targets")


@_tool()
def native_describe(target: str = "Level", search: str = "") -> dict:
    """List a Dungeondraft object's methods (with argument names and types) and properties.

    target: see native_targets; members can be chained ("Level.Terrain").
    search: case-insensitive substring to filter the names; listings are capped
    at 200, so search on big objects. Read-only.

    Plain C# fields (e.g. World.levels) are not listed but native_get can still
    read them by name.
    """
    return bridge.request("native_describe", target=target, search=search)


@_tool()
def native_get(target: str, properties: list[str]) -> dict:
    """Read properties of a Dungeondraft object by name. Read-only.

    A name the object does not have reads as null rather than failing.
    Example: native_get("Level.Terrain", ["SmoothBlending", "visible"]).
    """
    return bridge.request("native_get", target=target, properties=properties)


@_tool()
def native_set(target: str, property: str, value: Any) -> dict:
    """Set a property on a Dungeondraft object. Returns the value it holds afterwards.

    LAST RESORT: prefer a dedicated tool. This bypasses the bridge's undo, and a
    raw property write can skip the bookkeeping the matching Dungeondraft method
    does, so look for a method with native_describe first. save_map before
    experimenting.

    value: plain JSON is converted to the property's type ([x, y] for a
    Vector2, '#rrggbb' for a Color, a whole number for an int). Check the
    returned value: a write Dungeondraft ignored comes back unchanged.
    """
    return bridge.request("native_set", target=target, property=property, value=value)


@_tool()
def native_call(target: str, method: str, args: Optional[list[Any]] = None) -> dict:
    """Call a method of Dungeondraft's own API on a live object. Returns its result.

    LAST RESORT: prefer a dedicated tool. Nothing done this way can be undone
    with undo(), and a wrong call can leave the map in a broken state: save_map
    first, and read the method's signature with native_describe.

    args: one value per declared argument, in order. Arguments that have a
    default in Dungeondraft must still be passed. Plain JSON is converted to
    each declared type: [x, y] for Vector2, [[x, y], ...] for Vector2Array,
    [x, y, w, h] for Rect2, '#rrggbb' for Color. For an object argument pass
    {"$target": "<target>"}; for a texture
    {"$type": "Texture", "category": "Objects", "asset": "<asset path>"}; for an
    image file {"$type": "Image", "path": "<absolute path>"}.

    Only Dungeondraft's own methods are callable, not the engine's. If the
    method fails inside Dungeondraft the result is simply null (the error goes
    to Dungeondraft's log), so verify the effect with native_get or a
    screenshot rather than trusting a null.
    """
    return bridge.request("native_call", target=target, method=method, args=args or [])


@_tool()
def ui_tree(target: str = "Editor", search: str = "", limit: int = 200) -> dict:
    """List the controls in part of Dungeondraft's own UI: buttons, toggles, sliders, text fields, option lists, labels.

    target: "Tool:<name>" for a tool's side panel (e.g. "Tool:TerrainBrush"),
    "Window:<name>" for a dialog, or "Editor" for everything (use search).
    search: case-insensitive substring over a control's text, name, class and
    items. Each control reports its state (text, pressed, value/min/max, items,
    color, visible) and a "target" to give ui_action; controls a tool keeps by
    name also report it as "control". Read-only.
    """
    return bridge.request("ui_tree", target=target, search=search, limit=limit)


@_tool()
def ui_action(target: str, action: str, value: Any = None) -> dict:
    """Operate one of Dungeondraft's own controls, as if the user had: for features with no dedicated tool.

    target: a control's "target" from ui_tree.
    action: "press" clicks a button (or flips a toggle);
    "set" gives the control a value: true/false for a toggle, a number for a
    slider or spin box, a string for a text field, an item index or its exact
    text for an option list, '#rrggbb' for a color picker;
    "show" / "hide" open or close a window (target "Window:<name>").

    LAST RESORT, like native_call: no undo(), so save_map first. A press takes
    effect just after the reply; some buttons open dialogs that then need more
    ui_tree / ui_action steps, or a person at the screen.
    """
    params: dict = {"target": target, "action": action}
    if value is not None:
        params["value"] = value
    return bridge.request("ui_action", **params)


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

@_tool()
def undo() -> dict:
    """Reverse the last reversible edit you made (create / delete / move / modify / terrain / cave / water / material).

    The bridge keeps its own undo/redo stack, so this reliably steps back through
    your own edits; it is independent of Dungeondraft's Ctrl+Z. A batch, a
    scatter, a built room or a multi-element delete is one step. Not
    reversible: delete_level, set_terrain_slot, and native_* / ui_* calls.
    History keeps the last 200 steps.
    """
    return bridge.request("undo")


@_tool()
def redo() -> dict:
    """Re-apply the last edit reversed by undo()."""
    return bridge.request("redo")


# --------------------------------------------------------------------------
# Terrain read-back, floorplan and scene checks, arrangement
# --------------------------------------------------------------------------

@_tool()
def get_terrain(rect: Optional[list[float]] = None, samples: int = 16) -> dict:
    """Read the painted terrain back: sample the splat weights on a grid. Read-only.

    rect: [x, y, w, h] in woxels; omit for the whole map. samples: points per
    side, 2-64 (16 = 256 points). Each sample is {x, y, weights, covered}:
    weights are the splat channel values for slots 0-3 (and 4-7 with expanded
    slots), summing to about 1; covered is true where a pattern floor or a
    dug cave hides the terrain. slots lists each slot's texture. Slot 0 is
    whatever the other slots leave unclaimed, so a blank map reads [1,0,0,0].
    """
    if not 2 <= samples <= 64:
        raise ValueError("samples must be 2 to 64")
    params: dict = {"samples": samples}
    if rect is not None:
        params["rect"] = rect
    return bridge.request("terrain_samples", **params)


@_tool()
def validate_floorplan(cell_woxels: int = 64, min_room_tiles: float = 1.0) -> dict:
    """Check a floorplan's structure BEFORE furnishing it: is every room reachable, does every door open onto something, is there a way in. Read-only.

    Walls are rasterised onto a grid of cell_woxels (64 = a quarter tile) and
    the enclosed areas flood-filled; areas under min_room_tiles are ignored as
    slivers. Returns the rooms (area, bounds, centre, their doors, whether a
    chain of doors reaches the outside), each portal's two sides, the
    entrances, and findings: sealed_room, unreachable_room, portal_to_nothing,
    no_entrance. A sealed room and a connected one have identical element
    counts; this is how to tell them apart.
    """
    if cell_woxels < 8:
        raise ValueError("cell_woxels must be at least 8")
    return floorplan.analyse(bridge.request("spatial_snapshot"),
                             cell_woxels=cell_woxels, min_room_tiles=min_room_tiles)


@_tool()
def validate_scene(samples: int = 32, emitter_reach: float = 256.0) -> dict:
    """Check that the map has ground and that its light has causes. Read-only.

    bare_ground: open ground with no terrain painted and no floor over it,
    sampled on a samples x samples grid (points under walls are skipped);
    reports the share and the largest bare patches with their bounds. Floors
    drawn with Dungeondraft's Floor tool are not detected, so a Floor-tool room
    reads as bare. light_causes: every light should have a visible source
    nearby (a candle, torch, lantern, brazier, fire, lamp...) within
    emitter_reach woxels; lists lights with no cause and sources with no
    light. A wide light (range >= 4) counts as mood light and is never flagged.
    """
    if not 2 <= samples <= 64:
        raise ValueError("samples must be 2 to 64")
    terrain = bridge.request("terrain_samples", samples=samples)
    snapshot = bridge.request("spatial_snapshot")
    ground = scene_checks.bare_ground(terrain["samples"], slots=terrain.get("slots"),
                                      walls=snapshot.get("walls"))
    lights = scene_checks.light_causes(
        bridge.request("list_elements", kind="lights", limit=1000)["elements"],
        bridge.request("list_elements", kind="objects", limit=2000)["elements"],
        emitter_reach=emitter_reach)
    return {"ok": ground["ok"] and lights["ok"], "bare_ground": ground, "light_causes": lights}


@_tool()
def arrange_objects(
    mode: str,
    assets: list[str],
    count: Optional[int] = None,
    along: Optional[list[list[float]]] = None,
    wall_id: Optional[int] = None,
    toward: Optional[list[float]] = None,
    target_id: Optional[int] = None,
    facing: str = "down",
    shape: str = "auto",
    gap: Optional[float] = None,
    margin: Optional[float] = None,
    clearance: float = -16.0,
    scale: float = 1.0,
    layer: int = spatial.DEFAULT_LAYER,
    color: str = "",
    jitter: bool = True,
    seed: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """Lay furniture out against a wall, around a table, or on a surface, in ONE undo step.

    Fitted to each asset's DRAWN area (what get_asset_footprint reports), never
    its canvas, so a shelf sits flush to its wall. assets are used in turn,
    repeated to count.

    mode='along_wall': a row backed against a wall, fronts into the room.
      along: [[x0,y0],[x1,y1]], the stretch of wall to line (woxels); wall_id
      picks the wall when two overlap there; toward: a point inside the room,
      needed for a wall that is not a closed loop. Doors on the wall and the
      corners are kept clear by margin (default 32). count=None fills the
      stretch; gap spaces the row (None spreads it evenly).
    mode='around_target': seats ringing the object target_id (a table), each
      facing it. count seats (default 4); shape 'auto' | 'round' | 'rect';
      clearance from the table edge, negative tucks them under (default -16,
      placed with allow_overlap).
    mode='on_surface': count items (default 3) on top of target_id, inside
      its edges, not overlapping each other; margin inset (default 12).
    facing: which way the assets' front points at rotation 0: 'down' (DD's
      own furniture), 'up', 'left', 'right'. scale, layer, color apply to all.
    seed makes a jittered result reproducible. dry_run=True returns the
    placements without placing anything.

    Returns the placements (asset, x, y, rotation, scale), what was skipped
    and why, and, when placed, the new ids and any placement refused as in
    place_object(check=True).
    """
    if mode not in ("along_wall", "around_target", "on_surface"):
        raise ValueError("mode must be 'along_wall', 'around_target' or 'on_surface'")
    if not assets:
        raise ValueError("give at least one asset")
    specs = []
    for asset in dict.fromkeys(assets):
        spec = dict(bridge.request("asset_footprint", asset=asset))
        spec["asset"] = asset
        specs.append(spec)
    snapshot = bridge.request("spatial_snapshot")
    if mode == "along_wall":
        if along is None or len(along) != 2:
            raise ValueError("along_wall needs along=[[x0, y0], [x1, y1]]")
        plan = layout.arrange_along_wall(
            specs, snapshot.get("walls", []), along, doors=snapshot.get("portals", []),
            wall_id=wall_id, toward=toward, count=count, gap=gap,
            margin=32.0 if margin is None else margin, facing=facing, scale=scale)
        overlap_ok = False
    else:
        if target_id is None:
            raise ValueError(f"{mode} needs target_id (the table or surface, an object id)")
        target = spatial.find_object(snapshot, target_id)
        if target is None:
            raise ValueError(f"no object with id {target_id} on this level")
        if mode == "around_target":
            plan = layout.arrange_around_target(
                specs, target, count=4 if count is None else count, shape=shape,
                clearance=clearance, jitter=jitter, facing=facing, scale=scale, seed=seed)
            overlap_ok = clearance < 0
        else:
            plan = layout.arrange_on_surface(
                specs, target, count=3 if count is None else count,
                margin=12.0 if margin is None else margin, scale=scale, seed=seed)
            overlap_ok = True
    result = dict(plan)
    result["mode"] = mode
    if dry_run or not plan["placements"]:
        result["placed"] = 0
        return result
    calls = []
    for placed in plan["placements"]:
        args = {"asset": placed["asset"], "x": placed["x"], "y": placed["y"],
                "rotation": placed["rotation"], "scale": placed["scale"], "layer": layer,
                "check": True, "allow_overlap": overlap_ok}
        if color:
            args["color"] = color
        calls.append({"tool": "place_object", "args": args})
    run = _run_calls(calls, stop_on_error=False, single_undo=True, label=f"arrange_{mode}")
    result["ids"] = [r["result"]["id"] for r in run["results"] if r["ok"]]
    result["refused"] = [{"placement": plan["placements"][i], "error": r["error"]}
                         for i, r in enumerate(run["results"]) if not r["ok"]]
    result["placed"] = len(result["ids"])
    return result


@_tool()
def preview_assets(
    assets: list[str],
    category: str = "Objects",
    columns: int = 4,
    cell_px: int = 192,
    true_scale: bool = False,
) -> list:
    """LOOK at candidate assets before committing to one: one contact sheet of up to 32 assets. Read-only.

    Asset names are not reliable descriptions: bench_01 may be a stone bench,
    and that is easy to miss until it stands in a timber tavern. Cells are
    laid out row-major in the order you passed them, so cell 1 is assets[0].
    category: as in list_assets. columns 1-8, cell_px 64-512. true_scale=True
    draws every asset at one scale, so a stool shows small beside a table.
    Returns the sheet and a caption with each asset's pixel size, whether it
    is colorable (then the sheet shows its red mask, not how it will look),
    and any asset that could not be loaded.
    """
    if not 1 <= len(assets) <= 32:
        raise ValueError("give 1 to 32 assets")
    res = bridge.request("preview_assets", assets=assets, category=category, columns=columns,
                         cell_px=cell_px, true_scale=true_scale)
    colorable = res.get("colorable") or [False] * len(assets)
    caption = {"cells": [{"cell": i + 1, "asset": a, "size_px": res["sizes"][i], "colorable": colorable[i]}
                         for i, a in enumerate(assets)],
               "columns": res["columns"], "rows": res["rows"], "true_scale": res["true_scale"],
               "missing": res["missing"]}
    if any(colorable):
        caption["note"] = ("a colorable asset is drawn here as its red mask; placed, it takes the color "
                           "you give it (or Dungeondraft's default #6b3834)")
    return [Image(path=res["path"]), json.dumps(caption)]


@_tool()
def get_snap_settings(points: Optional[list[list[float]]] = None) -> dict:
    """Report the grid the user snaps to, and snap points to it. Read-only.

    vanilla_snapping / half_snap / cell_size describe Dungeondraft's own
    snapping. mod_loaded says whether Lievven's Custom Snap Mod is installed;
    then mod has its geometry (square, hex_vertical, hex_horizontal,
    isometric), interval, offset and preset. points: up to 200 [x, y] to snap;
    snapped returns where each would land, which is what snap=True does on
    place_object, move_element, add_light, add_portal, add_text, draw_wall,
    draw_path, place_pattern, build_room and import_image. The user's own
    snap settings are never changed.
    """
    params: dict = {}
    if points:
        params["points"] = points
    return bridge.request("get_snap_settings", **params)


@_tool()
def get_map_style() -> dict:
    """Read the map-wide finishing effects: building wear and grid line style. Read-only.

    Reports the current building_wear (none, or a wear texture's name) with
    every option the Map Settings tool offers, and the grid_style (dashes,
    dotted, narrow_line, thick_line).
    """
    return bridge.request("get_map_style")


@_tool()
def set_map_style(building_wear: str = "", grid_style: str = "") -> dict:
    """Set map-wide finishing effects, as one undoable edit.

    building_wear: an option name from get_map_style ('none' clears it), or
      its index. It applies to building floors and walls on every level, so
      use it only when that global look fits the map.
    grid_style: 'dashes', 'dotted', 'narrow_line' or 'thick_line'. This is
      the grid's line pattern, not whether the grid shows (set_grid) or is
      exported (export_map).
    """
    params: dict = {}
    if building_wear:
        params["building_wear"] = building_wear
    if grid_style:
        params["grid_style"] = grid_style
    if not params:
        raise ValueError("give building_wear and/or grid_style")
    return bridge.request("set_map_style", **params)


@_tool()
def set_map_size(width: int, height: int) -> dict:
    """Resize the map, in TILES (8 to 128 a side), as Dungeondraft's own Change Map Size does. NOT undoable.

    The map grows or shrinks on its right and bottom edges; the top-left
    corner stays, so nothing placed moves. Shrinking does not delete what now
    lies outside the map. Trimming from the top or left is not offered, since
    Dungeondraft's own trim there loses terrain paint. Every level's terrain,
    cave and floor layers are resized together and read back; the bridge's
    undo history is cleared afterwards, as Dungeondraft clears its own.
    save_map first if in doubt. Returns the new size and the previous one.
    """
    if not (8 <= width <= 128 and 8 <= height <= 128):
        raise ValueError("width and height must each be 8 to 128 tiles")
    return bridge.request("set_map_size", width=width, height=height)


@_tool()
def list_prefabs(set: str = "") -> dict:
    """List Dungeondraft's prefabs: groups saved with the Prefab tool, ready to place.

    Returns the prefab sets, and the prefabs in the selected set (name one
    with set to see its prefabs). Prefabs are the user's own saved groups,
    so a fresh install may have none.
    """
    return bridge.request("list_prefabs", **({"set": set} if set else {}))


@_tool()
def place_prefab(name: str, set: str = "", x: Optional[float] = None, y: Optional[float] = None) -> dict:
    """Place a prefab by name, as the Prefab tool would, in ONE undo step.

    name: a prefab from list_prefabs; set: its set when it is not in the
    selected one. x, y: where the group's centre lands (woxels); omitted, it
    lands where it was saved. Props, lights, free portals and texts are
    moved; walls, paths, roofs and pattern floors keep their saved position
    (their ids come back as unmoved). Returns the new element ids.
    """
    params: dict = {"name": name}
    if set:
        params["set"] = set
    if x is not None:
        params["x"] = x
    if y is not None:
        params["y"] = y
    return bridge.request("place_prefab", **params)


@_tool()
def generator_options() -> dict:
    """Read the Map Wizard's generator settings: the design (Dungeon or Cave) and its dials with their ranges. Read-only."""
    return bridge.request("generator_options")


@_tool()
def generate_dungeon(
    design: str = "",
    boundary: Optional[float] = None,
    complexity: Optional[float] = None,
    density: Optional[float] = None,
) -> dict:
    """Run Dungeondraft's own Map Wizard generator: a whole dungeon or cave layout in one call.

    design: 'Dungeon' (rooms and corridors) or 'Cave' (organic caverns);
    omitted, the wizard's current choice. boundary, complexity, density: the
    wizard's dials, within the ranges generator_options reports. The layout
    is ADDED to the current level, so run it on an empty level (add_level,
    then set_level) and save_map first: it is NOT undoable through the
    bridge. Returns the element counts before and after.
    """
    params: dict = {}
    if design:
        params["design"] = design
    for key, value in (("boundary", boundary), ("complexity", complexity), ("density", density)):
        if value is not None:
            params[key] = value
    return bridge.request("generate_dungeon", **params)


# --------------------------------------------------------------------------
# Map files and asset packs (offline readers, see mapfile.py / packs.py)
# --------------------------------------------------------------------------

def _installed_packs() -> tuple[Optional[dict], list[str]]:
    """Installed packs by id (None when the pack folder is unknown), plus notes."""
    try:
        return packs.installed()["packs"], []
    except packs.PackError as exc:
        return None, [str(exc)]


@_tool()
def inspect_map_file(path: str, compare_to: str = "") -> dict:
    """Summarise a .dungeondraft_map file without opening it. Read-only; works with Dungeondraft closed.

    Reports what a render cannot show: the packs the map includes and whether
    each is installed (packs_missing are dropped when it opens), its size,
    and per level the counts per kind, the most used textures with the pack
    each comes from, object scales, terrain slot shares, materials, doors and
    windows whose closed flag contradicts their name (a Universal VTT export
    treats each as the other), lights and texts. compare_to: a second map
    file to diff against (counts, textures, packs, terrain). Handy for maps
    you download or are sent, and for two versions of one map.
    """
    found, notes = _installed_packs()
    out = mapfile.inspect(Path(path), installed=None if found is None else list(found),
                          compare_to=Path(compare_to) if compare_to else None)
    if notes:
        out.setdefault("notes", []).extend(notes)
    return out


@_tool()
def list_asset_packs() -> dict:
    """Which asset packs are installed, and which this map can actually use.

    Dungeondraft scopes packs per MAP: a map includes a set of packs, chosen
    when it was created, and list_assets only ever offers assets from those.
    A map that includes none sees roughly 1800 core assets however large the
    library is, and nothing fails: it quietly builds everything from the core
    set. installed_but_not_in_this_map are mounted but invisible here, and an
    object placed from one would not survive a reload. prepare_map_with_packs
    adds packs to a copy of the map; search_pack_contents looks inside packs
    the map lacks. active_for_new_maps is what a new map would include.
    """
    out = bridge.request("list_asset_packs")
    try:
        out["active_for_new_maps"] = packs.active_pack_ids()
        out["pack_folder"] = str(packs.assets_directory())
    except packs.PackError as exc:
        out["note"] = str(exc)
    return out


@_tool()
def search_pack_contents(
    terms: list[str],
    category: str = "Objects",
    pack_ids: Optional[list[str]] = None,
    limit: int = 20,
) -> dict:
    """Search INSIDE every installed asset pack, including packs this map lacks. Read-only; Dungeondraft need not be running.

    list_assets sees only the packs this map includes. Use this to choose
    packs by what they contain: terms are words in any order ('round table',
    'hay bale'), matched against each asset's path and the pack's tags and
    ranked by filename similarity; a term with no hits is retried with an
    equivalent wording (rug/carpet, armour/armor...) and says so. category:
    as in list_assets. pack_ids restricts the search. Packs whose authors
    opted out of third-party reading are listed as not_searched. Only each
    pack's file index and tags are read, never the art.
    """
    found = packs.installed()["packs"]
    return packs.search(found, terms, category, pack_ids=pack_ids, limit=limit)


@_tool()
def prepare_map_with_packs(pack_ids: list[str], destination: str = "") -> dict:
    """Copy the open map into one that INCLUDES more asset packs, and open the copy.

    A map's set of packs is fixed when it is created, so a map made without
    a pack can never reach its assets. This writes a copy of the saved map
    file whose header includes the given packs (ids from list_asset_packs or
    search_pack_contents) and opens it; the original file is left as it was.
    destination: the new file's path (default: beside the original, with
    '-packs' added to the name). The map is saved first, so no edit is lost
    when the copy opens. Not undoable.
    """
    if not pack_ids:
        raise ValueError("give at least one pack id")
    src = bridge.request("get_current_map").get("path") or ""
    if not src:
        raise ValueError("the map has never been saved; save it in Dungeondraft first")
    found = packs.installed()["packs"]
    _save_and_wait(src)
    infos = [pack.info for pack in found.values()]
    unknown = mapfile.unknown_ids(infos, pack_ids)
    if unknown:
        raise ValueError(f"not installed: {unknown}; installed ids: {sorted(found)}")
    source = Path(src)
    dest = Path(destination) if destination else source.with_name(f"{source.stem}-packs{source.suffix}")
    if dest.suffix != ".dungeondraft_map":
        raise ValueError("destination must end in .dungeondraft_map")
    report = mapfile.prepare_copy(source, dest, mapfile.build_manifest(infos, pack_ids))
    report["opening"] = bridge.request("open_map", path=str(dest))
    return report


@_tool()
def import_dd2vtt(
    path: str,
    confirm: bool = False,
    walls: bool = True,
    portals: bool = True,
    lights: bool = True,
    ambient: bool = True,
    trace: bool = True,
    wall_asset: str = "",
    door_asset: str = "",
    window_asset: str = "",
) -> dict:
    """Rebuild a Universal VTT export (.dd2vtt) on the open map: walls, doors and windows, lights and ambient light, with the export's image underneath as a trace to follow.

    Without confirm it only reads the file and reports the plan: the map size
    it needs, counts and notes. Show the user, then call again with
    confirm=True. It recreates walls from line_of_sight (rejoined through each
    doorway so every door mounts on a wall), portals at their exported width
    (closed ones as door_asset, open ones as window_asset; by default the
    first door and window in the Portals category), lights with their colour,
    range and shadows, and the ambient light; the image goes under the map as
    a trace at the right scale. It does not pick art: blocking_objects says
    where each sight-blocking object stood and its size, so you can place
    something matching. The map must be at least the export's size (see
    map_size_tiles); on a larger map everything lands in the top-left
    corner. The rebuild is ONE undo step; the ambient light and trace image
    are not undoable.
    """
    source = Path(path)
    if not source.is_file():
        raise ValueError(f"file not found: {path}")
    parsed = uvtt_import.parse(source.read_bytes())
    status = bridge.request("get_status")
    size = status.get("map_size_woxels")
    tiles = [size[0] / 256, size[1] / 256] if size else None
    plan = uvtt_import.plan(parsed, current_map_size_tiles=tiles)
    plan["confirm"] = confirm
    if not confirm:
        plan["note"] = "nothing was changed; call again with confirm=True to rebuild"
        return plan
    need = plan["map_size_tiles"]
    if tiles and (tiles[0] < need[0] or tiles[1] < need[1]):
        raise ValueError(f"the map is {tiles} tiles but the export needs {need}; "
                         "make a map at least that size first")
    if portals and plan["portals"]:
        door_asset = door_asset or _first_asset("Portals", "door")
        window_asset = window_asset or _first_asset("Portals", "window") or door_asset
    made: dict = {"walls": [], "portals": [], "lights": [], "ambient_light": None, "trace_image": None,
                  "skipped": []}
    with _undo_group("import_dd2vtt"):
        if walls:
            for wall in plan["walls"]:
                made["walls"].append(draw_wall(wall["points"], asset=wall_asset, loop=wall["loop"])["id"])
        if portals:
            for portal in plan["portals"]:
                asset = door_asset if portal["kind"] == "door" else window_asset
                try:
                    made["portals"].append(add_portal(
                        asset, x=portal["x"], y=portal["y"], closed=portal["closed"],
                        radius=portal["radius"], mount=portal["mount"], flip=portal["flip"],
                        rotation=portal["rotation_degrees"])["id"])
                except BridgeError as exc:
                    made["skipped"].append({"portal": portal, "error": str(exc)})
        if lights:
            for light in plan["lights"]:
                made["lights"].append(add_light(
                    x=light["x"], y=light["y"], color=light["color"], energy=light["energy"],
                    range=light["range"], shadows=light["shadows"])["id"])
    if ambient and plan.get("ambient_light"):
        made["ambient_light"] = configure_level(ambient_light=plan["ambient_light"]).get("ambient_light")
    if trace and plan.get("trace"):
        image = uvtt_import.decode_image(parsed)
        trace_path = _TRACE_PATH + image.suffix
        with open(trace_path, "wb") as handle:
            handle.write(image.data)
        made["trace_image"] = set_trace_image(trace_path, x=plan["trace"]["x"], y=plan["trace"]["y"],
                                              scale=plan["trace"]["scale"], opacity=plan["trace"]["opacity"])
    return {"confirm": True, "summary": plan["summary"], "made": made,
            "blocking_objects": plan.get("blocking_objects", []), "notes": plan.get("notes", [])}


def _save_and_wait(path: str, timeout: float = 30.0) -> None:
    """save_map, then wait until the write is done and the file is newer."""
    before = os.path.getmtime(path) if os.path.exists(path) else 0.0
    bridge.request("save_map")
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = bridge.request("get_current_map")
        if not state.get("saving") and os.path.exists(path) and os.path.getmtime(path) > before:
            return
        time.sleep(0.2)
    raise ValueError(f"the save did not finish within {timeout:.0f} s; try again")


def _first_asset(category: str, search: str) -> str:
    found = bridge.request("list_assets", category=category, search=search, limit=1).get("assets", [])
    return found[0] if found else ""


# --------------------------------------------------------------------------
# History: checkpoints, grouped edits
# --------------------------------------------------------------------------

@_tool()
def checkpoint(label: str) -> dict:
    """Mark a point in history, so everything after it can be undone in one call.

    Call it before a multi-step change (a room, a set piece, a lighting
    pass); rollback_checkpoint(label) then undoes every edit made since,
    newest first. Reusing a label moves its mark to now. History keeps the
    last 200 steps, so a checkpoint older than that can no longer be reached.
    """
    return bridge.request("checkpoint", label=label)


@_tool()
def rollback_checkpoint(label: str) -> dict:
    """Undo every edit made since checkpoint(label), in one call.

    Reports how many steps it reversed and their kinds. The checkpoint stays
    open, so a second attempt can be rolled back to the same point. Refuses,
    changing nothing, when history no longer reaches it. Edits that are not
    undoable (delete_level, set_terrain_slot, native_* and ui_* calls) are
    not reversed.
    """
    return bridge.request("rollback_checkpoint", label=label)


@_tool()
def list_checkpoints() -> dict:
    """List the open checkpoints: label, steps made since, and whether a rollback still reaches it."""
    return bridge.request("list_checkpoints")


@_tool()
def place_objects(objects: list[dict], stop_on_error: bool = True) -> dict:
    """Place many objects at chosen positions in ONE call and ONE undo() step.

    objects: up to 100 entries, each with exactly place_object's arguments:
    {"asset": ..., "x": ..., "y": ..., "rotation": ..., "scale": ...,
    "layer": ..., "color": ..., "check": ..., "allow_overlap": ...}. Use it
    when you know where several things go: a row of tables, the dressing on a
    bar, eight trees ringing a clearing. scatter_objects is for RANDOM
    placement over an area.

    Returns one entry per object: {ok, result} with the new id, or {ok:
    false, error}. With stop_on_error (the default) it stops at the first
    refusal; what was placed before stays, and one undo() removes all of it.
    """
    if not 1 <= len(objects) <= 100:
        raise ValueError("give 1 to 100 objects")
    calls = [{"tool": "place_object", "args": o} for o in objects]
    return _run_calls(calls, stop_on_error, single_undo=True, label="place_objects")


@_tool()
def move_elements(ids: list[int], dx: float, dy: float) -> dict:
    """Move several elements by the same offset, in one call and ONE undo() step.

    ids: up to 100 element ids; dx, dy: the offset in woxels. Every id is
    read first, so an unknown id moves nothing. The placement check is not
    applied here; move_element(check=True) checks one object at a time.
    """
    if not 1 <= len(ids) <= 100:
        raise ValueError("give 1 to 100 ids")
    before = {int(i): bridge.request("get_element", id=int(i)) for i in dict.fromkeys(ids)}
    for i, el in before.items():
        if not isinstance(el.get("position"), list):
            raise ValueError(f"element {i} has no position to move")
    moved = []
    with _undo_group("move_elements"):
        for i, el in before.items():
            x, y = el["position"][0] + dx, el["position"][1] + dy
            res = bridge.request("move_element", id=i, x=x, y=y)
            moved.append({"id": i, "position": res.get("position", [x, y])})
    return {"moved": len(moved), "dx": dx, "dy": dy, "elements": moved}


@_tool()
def delete_level(index: int) -> dict:
    """Delete a level (floor) by its index (see list_levels). NOT undoable.

    Dungeondraft frees the level and everything on it, so save_map first if
    in doubt. The last remaining level cannot be deleted; deleting the
    current level switches the editor to a neighbour first. Returns the
    level list, with the deleted level's id and label.
    """
    return bridge.request("delete_level", index=index)


# --------------------------------------------------------------------------
# Batch
# --------------------------------------------------------------------------

# Tools batch() won't run: the ones that return an image, the one that swaps
# the map out from under the rest of the batch, and itself.
_NOT_BATCHABLE = {"batch", "screenshot", "export_map", "export_uvtt", "open_map",
                  # history commands close the batch's own undo group
                  "undo", "redo", "checkpoint", "rollback_checkpoint", "list_checkpoints"}


@_tool()
def batch(calls: list[dict], stop_on_error: bool = True, single_undo: bool = True) -> dict:
    """Run several tool calls in order in one request. Use it for runs of independent edits.

    calls: [{"tool": "<tool name>", "args": {...}}, ...] (up to 100), with the
    same names and arguments as the tools themselves, e.g.
      [{"tool": "place_object", "args": {"asset": "...", "x": 1024, "y": 768}},
       {"tool": "add_light", "args": {"x": 1024, "y": 768, "range": 1.5}}]
    A later call cannot use an earlier call's result (such as a new id); when
    you need one, end the batch there. Not available inside a batch:
    screenshot, export_map, export_uvtt, open_map, and the history tools
    (undo, redo, checkpoints).

    Returns one entry per call that ran: {tool, ok, result} or {tool, ok: false,
    error}. With stop_on_error (the default) the batch stops at the first
    failure, leaving the earlier calls done: it is not all-or-nothing. With
    single_undo (the default) every edit in the batch is ONE undo() step;
    pass False to keep each edit its own step.
    """
    if not 1 <= len(calls) <= 100:
        raise ValueError("give 1 to 100 calls")
    return _run_calls(calls, stop_on_error, single_undo, label="batch")


def _run_calls(calls: list[dict], stop_on_error: bool, single_undo: bool, label: str) -> dict:
    if single_undo:
        with _undo_group(label):
            return _run_calls(calls, stop_on_error, False, label)
    results: list[dict] = []
    for call in calls:
        name = call.get("tool", "") if isinstance(call, dict) else ""
        entry: dict = {"tool": name}
        try:
            if name in _NOT_BATCHABLE:
                raise ValueError(f"'{name}' cannot be used inside a batch")
            if name not in _TOOLS:
                raise ValueError(f"unknown tool '{name}'")
            args = call.get("args") or {}
            if not isinstance(args, dict):
                raise ValueError("'args' must be an object of argument names to values")
            entry.update(ok=True, result=_TOOLS[name](**args))
        except (BridgeError, ValueError, TypeError) as exc:
            entry.update(ok=False, error=str(exc))
        results.append(entry)
        if not entry["ok"] and stop_on_error:
            break
    return {"requested": len(calls), "ran": len(results),
            "failed": sum(1 for r in results if not r["ok"]), "results": results}


# Every tool above by name, for batch(). Tools are this module's public
# functions; helpers start with an underscore.
_TOOLS = {name: fn for name, fn in list(globals().items())
          if inspect.isfunction(fn) and fn.__module__ == __name__ and not name.startswith("_")}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
