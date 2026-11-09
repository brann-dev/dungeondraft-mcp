"""Reading a Universal VTT file (.dd2vtt) into a plan the bridge's tools can carry out.

Dungeondraft, Foundry and other VTTs exchange a map as Universal VTT: one JSON
document holding the rendered map (a base64 image), the walls as sight lines
(`line_of_sight`), every door and window (`portals`), the lights, the ambient
light and the outline of each object that blocks light
(`objects_line_of_sight`), all positioned in grid units. Dungeondraft writes
these (see `export_uvtt`) but cannot read them back. This module parses one
and works out what `draw_wall`, `add_portal`, `add_light`, `configure_level`
and `set_trace_image` would have to be called with to rebuild it. Everything
here is offline and pure, so it runs, and is tested, without Dungeondraft; the
server turns the plan into bridge calls.

Format facts (Dungeondraft 1.1/1.2 exports, format 0.3, checked against the
decompiled exporter and real files):
- Positions are grid units: woxels / 256. `resolution.map_origin` is where a
  region export's top-left corner sat in the source map, and the items keep
  the source map's coordinates, so the origin is subtracted; the export also
  lists the whole level's walls and lights, so anything outside the region
  is dropped.
- A wall's sight line is broken at every portal: the run stops at one edge
  of the doorway (`bounds[0]`) and resumes at the other (`bounds[1]`). A door
  can only mount on a wall, so the runs are rejoined through each wall-mounted
  portal's bounds before drawing. A loop with no door is exported as an open
  polyline (its occluder is not closed), so it comes back open as well.
- `portals[].rotation` is radians: the angle of the door's direction, plus pi
  when the door was flipped. `closed` is all a VTT knows: a closed portal is
  an openable door, an open one a window.
- `lights[].range` is Dungeondraft's own Range (`texture_scale *
  texture_width / 512`), the same unit `add_light` takes, so it passes
  through unchanged. `intensity` is the Light2D energy.
- Colours are Godot 3 ARGB hex: 'ffd9c3a0' is alpha ff, rgb d9c3a0.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import struct
from dataclasses import dataclass
from typing import Any, Optional, Sequence

Point = tuple[float, float]

WOXELS_PER_GRID = 256.0
# Light2D.Export writes range = texture_scale * texture_width / 512, which is
# Dungeondraft's Range and exactly what add_light takes (the bridge converts
# to the texture scale itself), so the export's range is used as it is.
RANGE_PER_TEXTURE_SCALE = 1.0
# Dungeondraft's largest map (ChangeMapSizeWindow.MAX_SIZE); a bigger export
# cannot be rebuilt in it.
MAX_TILES = 128
MAX_PIXELS_PER_GRID = 4096
# Godot 3 cannot load a texture with a longer side than this; a bigger trace
# image would only fail inside Dungeondraft.
MAX_IMAGE_EDGE = 16384
MAX_IMAGE_BYTES = 256 * 1024 * 1024      # decoded; bigger than any loadable image
MAX_FILE_BYTES = 400 * 1024 * 1024
MAX_RUNS = 20_000                        # wall runs, or object outlines
MAX_POINTS = 1_000_000                   # across all runs of one kind
MAX_RECORDS = 10_000                     # portals, or lights
# Two run ends this close (grid units) are the same point. Exports round to
# 1e-6 and a doorway's bounds are exactly where the wall's runs stop.
JOIN_TOLERANCE = 0.02
# A vertex this far (grid units, about 2.5 woxels) off the line between its
# neighbours is still on it: a joined doorway's edges go, real corners stay.
STRAIGHT_TOLERANCE = 0.01
# A wall or light this far outside a region export (grid units) is kept, so a
# wall drawn along the region's edge survives.
REGION_MARGIN = 1.0
# A portal this close (woxels) to a planned wall segment sits on it.
ON_WALL = 4.0


class UvttError(ValueError):
    """The file is not a Universal VTT export this importer can use."""


@dataclass(frozen=True)
class UvttFile:
    """A parsed export, positions still in the source map's grid units.

    `portals` are {position, bounds, rotation (radians), closed, freestanding}
    and `lights` {position, range, intensity, color ('#rrggbb' or None),
    shadows}, each checked and defaulted; `dropped` counts the records left
    out for having no usable position. `image` is the base64 text as written,
    '' when the export has none.
    """

    format: Optional[float]
    origin: Point
    size: Point
    pixels_per_grid: float
    walls: list[list[Point]]
    object_walls: list[list[Point]]
    portals: list[dict[str, Any]]
    lights: list[dict[str, Any]]
    ambient_light: Optional[str]
    baked_lighting: Optional[bool]
    image: str
    dropped: dict[str, int]

    @property
    def tiles(self) -> tuple[int, int]:
        """The export's size in whole tiles: the map that fits it."""
        return (math.ceil(self.size[0] - 1e-6), math.ceil(self.size[1] - 1e-6))


@dataclass(frozen=True)
class JoinedWall:
    """A wall to draw, in grid units: its sight-line runs rejoined through the doorways."""

    points: list[Point]
    loop: bool
    doorways: int      # wall-mounted portals this wall carries


@dataclass(frozen=True)
class TraceImage:
    """The export's image, decoded from base64 but not from its pixel format."""

    data: bytes
    format: str                    # 'png', 'webp' or 'jpeg'
    width: Optional[int]           # from the file header; None if it could not be read
    height: Optional[int]

    @property
    def suffix(self) -> str:
        return {"png": ".png", "webp": ".webp", "jpeg": ".jpg"}[self.format]


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def parse(data: bytes | bytearray | str) -> UvttFile:
    """Read a .dd2vtt file's contents. Raises UvttError for anything that is
    not a usable Universal VTT export: not JSON, no `resolution`, a map no
    Dungeondraft can hold, or counts and an image beyond any real map."""
    if len(data) > MAX_FILE_BYTES:
        raise UvttError(f"the file is {_mb(len(data))} MB; a Universal VTT file this large cannot "
                        "be a map Dungeondraft could load (export again at a lower pixels-per-grid)")
    try:
        raw = json.loads(data)
    except (UnicodeDecodeError, ValueError) as exc:
        raise UvttError("not a Universal VTT file: the contents are not valid JSON") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("resolution"), dict):
        raise UvttError("not a Universal VTT file: no 'resolution' section (expected a .dd2vtt export)")
    resolution = raw["resolution"]
    size = _point(resolution.get("map_size"), "resolution.map_size")
    origin = _point(resolution.get("map_origin", {"x": 0, "y": 0}), "resolution.map_origin")
    if size[0] <= 0 or size[1] <= 0:
        raise UvttError(f"resolution.map_size must be positive, got {size[0]:g} x {size[1]:g}")
    if max(size) > MAX_TILES:
        raise UvttError(f"the export is {size[0]:g} x {size[1]:g} grid squares; Dungeondraft's largest map "
                        f"is {MAX_TILES} x {MAX_TILES} tiles")
    ppg = _number(resolution.get("pixels_per_grid"))
    if ppg is None or not 0 < ppg <= MAX_PIXELS_PER_GRID:
        raise UvttError(f"resolution.pixels_per_grid must be a number between 1 and {MAX_PIXELS_PER_GRID}, "
                        f"got {resolution.get('pixels_per_grid')!r}")

    dropped: dict[str, int] = {}
    environment = raw.get("environment")
    if not isinstance(environment, dict):
        environment = {}
    baked = environment.get("baked_lighting")
    image = raw.get("image")
    if not isinstance(image, str):
        image = ""
    if len(image) > MAX_IMAGE_BYTES // 3 * 4 + 4:
        raise UvttError(f"the image is about {_mb(len(image) * 3 // 4)} MB, more than any trace Dungeondraft "
                        "could load; export again at a lower pixels-per-grid")
    fmt = _number(raw.get("format"))
    return UvttFile(
        format=fmt,
        origin=origin,
        size=size,
        pixels_per_grid=ppg,
        walls=_polylines(raw.get("line_of_sight"), "line_of_sight"),
        object_walls=_polylines(raw.get("objects_line_of_sight"), "objects_line_of_sight"),
        portals=_portals(raw.get("portals"), dropped),
        lights=_lights(raw.get("lights"), dropped),
        ambient_light=argb_to_hex(environment.get("ambient_light")),
        baked_lighting=baked if isinstance(baked, bool) else None,
        image=image,
        dropped=dropped,
    )


def _mb(count: int) -> int:
    return round(count / (1024 * 1024))


def _number(value: Any) -> Optional[float]:
    """A finite number, or None for anything else (bool included)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _try_point(value: Any) -> Optional[Point]:
    if not isinstance(value, dict):
        return None
    x, y = _number(value.get("x")), _number(value.get("y"))
    if x is None or y is None:
        return None
    return (x, y)


def _point(value: Any, name: str) -> Point:
    point = _try_point(value)
    if point is None:
        raise UvttError(f"{name} must be an object with finite numeric x and y, got {value!r}")
    return point


def _polylines(value: Any, name: str) -> list[list[Point]]:
    """Runs of two or more points; shorter runs and bad points are left out."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise UvttError(f"{name} must be a list of point lists")
    if len(value) > MAX_RUNS:
        raise UvttError(f"{name} holds {len(value)} runs; the most this importer handles is {MAX_RUNS}")
    lines: list[list[Point]] = []
    total = 0
    for line in value:
        if not isinstance(line, list):
            raise UvttError(f"{name} must be a list of point lists")
        total += len(line)
        if total > MAX_POINTS:
            raise UvttError(f"{name} holds more than {MAX_POINTS} points; that is not a map")
        points = [p for p in (_try_point(v) for v in line) if p is not None]
        if len(points) >= 2:
            lines.append(points)
    return lines


def _records(value: Any, name: str, dropped: dict[str, int]) -> list[dict]:
    """The list's entries that are objects with a usable position; the rest are counted."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise UvttError(f"{name} must be a list of objects")
    if len(value) > MAX_RECORDS:
        raise UvttError(f"{name} holds {len(value)} entries; the most this importer handles is {MAX_RECORDS}")
    usable = [r for r in value if isinstance(r, dict) and _try_point(r.get("position")) is not None]
    if len(usable) < len(value):
        dropped[name] = len(value) - len(usable)
    return usable


def _portals(value: Any, dropped: dict[str, int]) -> list[dict[str, Any]]:
    portals = []
    for record in _records(value, "portals", dropped):
        position = _point(record["position"], "portal position")
        bounds = record.get("bounds")
        ends = None
        if isinstance(bounds, list) and len(bounds) == 2:
            a, b = _try_point(bounds[0]), _try_point(bounds[1])
            if a is not None and b is not None and not _close(a, b, 1e-6):
                ends = (a, b)
        portals.append({
            "position": position,
            "bounds": ends,
            "rotation": _number(record.get("rotation")) or 0.0,
            "closed": bool(record.get("closed", False)),
            "freestanding": bool(record.get("freestanding", False)),
        })
    return portals


def _lights(value: Any, dropped: dict[str, int]) -> list[dict[str, Any]]:
    lights = []
    for record in _records(value, "lights", dropped):
        position = _point(record["position"], "light position")
        reach = _number(record.get("range"))
        intensity = _number(record.get("intensity"))
        lights.append({
            "position": position,
            "range": reach if reach is not None and reach > 0 else None,
            "intensity": intensity if intensity is not None and intensity >= 0 else 1.0,
            "color": argb_to_hex(record.get("color")),
            "shadows": bool(record.get("shadows", True)),
        })
    return lights


def argb_to_hex(value: Any) -> Optional[str]:
    """Godot's 'aarrggbb' (or a bare 'rrggbb') -> '#rrggbb'; None if absent or malformed."""
    if not isinstance(value, str):
        return None
    text = value.strip().lstrip("#").lower()
    if len(text) == 8:
        text = text[2:]
    if len(text) != 6 or any(c not in "0123456789abcdef" for c in text):
        return None
    return "#" + text


# --------------------------------------------------------------------------
# Walls: rejoining the sight lines through their doorways
# --------------------------------------------------------------------------

def _close(a: Point, b: Point, tolerance: float) -> bool:
    return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance


class _Junctions:
    """Numbers the points where run ends meet, treating ends within `tolerance` as one point."""

    def __init__(self, tolerance: float):
        self.tolerance = tolerance
        self._points: list[Point] = []
        self._cells: dict[tuple[int, int], list[int]] = {}

    def at(self, point: Point) -> int:
        cx, cy = math.floor(point[0] / self.tolerance), math.floor(point[1] / self.tolerance)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for jid in self._cells.get((cx + dx, cy + dy), ()):
                    if _close(self._points[jid], point, self.tolerance):
                        return jid
        jid = len(self._points)
        self._points.append(point)
        self._cells.setdefault((cx, cy), []).append(jid)
        return jid


def _doorway(portal: dict[str, Any]) -> Optional[tuple[Point, Point]]:
    """The gap a wall-mounted portal leaves in its wall, as the two points to bridge."""
    if portal.get("freestanding"):
        return None
    return portal.get("bounds")


def join_at_portals(
    walls: Sequence[Sequence[Point]],
    portals: Sequence[dict[str, Any]],
    tolerance: float = JOIN_TOLERANCE,
) -> list[JoinedWall]:
    """Rejoin the wall runs an export broke at its doorways.

    Each wall-mounted portal's bounds becomes a short run of its own; runs are
    then chained wherever exactly two run ends meet. Two doors side by side
    have no wall between them, and chaining their doorway runs joins the wall
    across both. Three or more ends at one point (a T-junction) stay apart. A
    chain whose ends meet is a loop. A doorway that joined no wall has
    nothing to mount on and is dropped; the vertices a doorway's edges leave
    on a straight wall are removed.
    """
    runs: list[tuple[list[Point], int, bool]] = [
        (list(wall), 0, True) for wall in walls if len(wall) >= 2]
    for portal in portals:
        gap = _doorway(portal)
        if gap is not None:
            runs.append(([gap[0], gap[1]], 1, False))

    junctions = _Junctions(tolerance)
    ends = [(junctions.at(points[0]), junctions.at(points[-1])) for points, _, _ in runs]
    meeting: dict[int, list[tuple[int, bool]]] = {}
    for index, (head, tail) in enumerate(ends):
        meeting.setdefault(head, []).append((index, False))
        meeting.setdefault(tail, []).append((index, True))

    def partner(junction: int, run: int) -> Optional[tuple[int, bool]]:
        """The one other run end at this junction, if exactly two meet there."""
        here = meeting.get(junction, ())
        if len(here) != 2:
            return None
        others = [(j, at_end) for j, at_end in here if j != run]
        return others[0] if len(others) == 1 else None

    used = [False] * len(runs)
    joined: list[JoinedWall] = []
    for start in range(len(runs)):
        if used[start]:
            continue
        used[start] = True
        points, doorways, has_wall = list(runs[start][0]), runs[start][1], runs[start][2]
        head, tail = ends[start]
        # Walk forward from the tail, then backward from the head, taking each
        # neighbouring run in the direction that continues the chain.
        current = start
        while True:
            found = partner(tail, current)
            if found is None or used[found[0]]:
                break
            j, at_end = found
            used[j] = True
            more = runs[j][0][::-1] if at_end else list(runs[j][0])
            points.extend(more[1:])
            doorways, has_wall = doorways + runs[j][1], has_wall or runs[j][2]
            tail = ends[j][0] if at_end else ends[j][1]
            current = j
        current = start
        while True:
            found = partner(head, current)
            if found is None or used[found[0]]:
                break
            j, at_end = found
            used[j] = True
            more = list(runs[j][0]) if at_end else runs[j][0][::-1]
            points = more[:-1] + points
            doorways, has_wall = doorways + runs[j][1], has_wall or runs[j][2]
            head = ends[j][0] if at_end else ends[j][1]
            current = j
        if not has_wall:
            continue
        loop = head == tail and len(points) > 3
        if loop:
            points = points[:-1]
        joined.append(JoinedWall(_drop_straight(points, loop), loop, doorways))
    return joined


def _straight(a: Point, b: Point, c: Point, tolerance: float) -> bool:
    """True if b lies on the segment a-c (within `tolerance` of it)."""
    dx, dy = c[0] - a[0], c[1] - a[1]
    length = math.hypot(dx, dy)
    if length < 1e-12:
        return _close(a, b, tolerance)
    off = abs((b[0] - a[0]) * dy - (b[1] - a[1]) * dx) / length
    along = ((b[0] - a[0]) * dx + (b[1] - a[1]) * dy) / length
    return off <= tolerance and -tolerance <= along <= length + tolerance


def _drop_straight(points: list[Point], loop: bool, tolerance: float = STRAIGHT_TOLERANCE) -> list[Point]:
    """Remove the vertices that lie on the line between their neighbours."""
    count = len(points)
    if count < 3:
        return list(points)
    if loop:
        # Start at a corner so the wrap-around needs no special case.
        corner = next((i for i in range(count)
                       if not _straight(points[i - 1], points[i], points[(i + 1) % count], tolerance)), None)
        if corner is None:
            return list(points)   # everything on one line: nothing to choose
        points = points[corner:] + points[:corner]
    kept = [points[0]]
    for i in range(1, count):
        if loop:
            following: Optional[Point] = points[(i + 1) % count]
        else:
            following = points[i + 1] if i + 1 < count else None
        if following is not None and _straight(kept[-1], points[i], following, tolerance):
            continue
        kept.append(points[i])
    return kept


def _wall_tangent_at(point: Sequence[float], walls: Sequence[dict], tolerance: float = ON_WALL) -> Optional[Point]:
    """Unit direction of the planned wall segment under `point` (woxels), or None."""
    best: Optional[tuple[float, Point]] = None
    px, py = point[0], point[1]
    for wall in walls:
        pts = wall["points"]
        count = len(pts)
        for i in range(count if wall["loop"] else count - 1):
            (ax, ay), (bx, by) = pts[i], pts[(i + 1) % count]
            dx, dy = bx - ax, by - ay
            length = math.hypot(dx, dy)
            if length == 0:
                continue
            along = ((px - ax) * dx + (py - ay) * dy) / length
            off = abs((px - ax) * dy - (py - ay) * dx) / length
            if -tolerance <= along <= length + tolerance and off <= tolerance:
                if best is None or off < best[0]:
                    best = (off, (dx / length, dy / length))
    return best[1] if best else None


# --------------------------------------------------------------------------
# Light-blocking objects
# --------------------------------------------------------------------------

def fit_blocker(outline: Sequence[Point]) -> dict[str, Any]:
    """Centre, long-axis angle and size of a sight-blocking outline, in woxels.

    The outline (grid units, origin already subtracted) is Dungeondraft's
    silhouette of an object that blocks light. A box along its principal axis
    is enough to drop something matching on it: place at (x, y), turn to
    rotation_degrees, and scale to about length x width.
    """
    points = [(x * WOXELS_PER_GRID, y * WOXELS_PER_GRID) for x, y in outline]
    if len(points) > 1 and _close(points[0], points[-1], 1e-6):
        points = points[:-1]
    count = len(points)
    cx = sum(p[0] for p in points) / count
    cy = sum(p[1] for p in points) / count
    sxx = sum((p[0] - cx) ** 2 for p in points) / count
    syy = sum((p[1] - cy) ** 2 for p in points) / count
    sxy = sum((p[0] - cx) * (p[1] - cy) for p in points) / count
    angle = 0.5 * math.atan2(2 * sxy, sxx - syy)
    ux, uy = math.cos(angle), math.sin(angle)
    along = [(p[0] - cx) * ux + (p[1] - cy) * uy for p in points]
    across = [-(p[0] - cx) * uy + (p[1] - cy) * ux for p in points]
    mid_along = (max(along) + min(along)) / 2
    mid_across = (max(across) + min(across)) / 2
    return {
        "x": round(cx + mid_along * ux - mid_across * uy, 1),
        "y": round(cy + mid_along * uy + mid_across * ux, 1),
        "rotation_degrees": round(math.degrees(angle) % 180, 1),
        "length_woxels": round(max(along) - min(along), 1),
        "width_woxels": round(max(across) - min(across), 1),
        "points": count,
    }


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------

def plan(uvtt: UvttFile, current_map_size_tiles: Optional[Sequence[float]] = None) -> dict[str, Any]:
    """What rebuilding the export takes, in woxels of the map being built.

    `walls` are draw_wall's points and loop; `portals` add_portal's x, y,
    radius, closed, mount, rotation and flip (plus kind: 'door' or 'window',
    which picks the asset); `lights` add_light's x, y, range, energy, color
    and shadows; `ambient_light` is for configure_level; `trace` holds
    set_trace_image's scale and centre; `blocking_objects` are boxes to put a
    light-blocking object on. `summary` has the counts for a dry-run report
    and `notes` what the user should hear. With the open map's size in tiles,
    `needs_resize` says whether it matches the export.
    """
    ox, oy = uvtt.origin
    width, height = uvtt.size
    tiles = list(uvtt.tiles)

    def inside(point: Point) -> bool:
        x, y = point[0] - ox, point[1] - oy
        return -REGION_MARGIN <= x <= width + REGION_MARGIN and -REGION_MARGIN <= y <= height + REGION_MARGIN

    def woxels(point: Point) -> list[float]:
        return [round((point[0] - ox) * WOXELS_PER_GRID, 2), round((point[1] - oy) * WOXELS_PER_GRID, 2)]

    skipped = {"walls": 0, "portals": 0, "lights": 0, "blocking_objects": 0}
    notes: list[str] = []

    walls: list[dict[str, Any]] = []
    doorways_joined = 0
    past_region = 0
    for wall in join_at_portals(uvtt.walls, uvtt.portals):
        where = [inside(p) for p in wall.points]
        if not any(where):
            skipped["walls"] += 1
            continue
        if not all(where):
            past_region += 1
        doorways_joined += wall.doorways
        walls.append({"points": [woxels(p) for p in wall.points], "loop": wall.loop})

    portals: list[dict[str, Any]] = []
    off_wall = 0
    for record in uvtt.portals:
        if not inside(record["position"]):
            skipped["portals"] += 1
            continue
        x, y = woxels(record["position"])
        rotation = record["rotation"]
        bounds = record["bounds"]
        if bounds is not None:
            (ax, ay), (bx, by) = bounds
            span = math.hypot(bx - ax, by - ay)
            direction = ((bx - ax) / span, (by - ay) / span)
            width_woxels = span * WOXELS_PER_GRID
        else:
            direction = (math.cos(rotation), math.sin(rotation))
            width_woxels = WOXELS_PER_GRID     # a one-tile door
        mount = "free" if record["freestanding"] else "wall"
        flip = False
        if mount == "wall":
            tangent = _wall_tangent_at((x, y), walls)
            if tangent is None:
                off_wall += 1
            else:
                # The bridge faces a mounted door along its wall segment and
                # `flip` turns it round: match the facing the export recorded.
                facing = (math.cos(rotation), math.sin(rotation))
                flip = facing[0] * tangent[0] + facing[1] * tangent[1] < 0
        portals.append({
            "x": x, "y": y,
            "width_woxels": round(width_woxels, 2),
            "radius": round(width_woxels / 2, 2),
            "closed": record["closed"],
            "kind": "door" if record["closed"] else "window",
            "mount": mount,
            "rotation_degrees": round(math.degrees(rotation) % 360, 2),
            "flip": flip,
            "direction": [round(direction[0], 4), round(direction[1], 4)],
        })

    lights: list[dict[str, Any]] = []
    for record in uvtt.lights:
        if not inside(record["position"]):
            skipped["lights"] += 1
            continue
        x, y = woxels(record["position"])
        reach = record["range"]
        lights.append({
            "x": x, "y": y,
            "range": round(reach / RANGE_PER_TEXTURE_SCALE, 4) if reach is not None else 2.0,
            "energy": record["intensity"],
            "color": record["color"] or "",
            "shadows": record["shadows"],
        })

    blocking: list[dict[str, Any]] = []
    for outline in uvtt.object_walls:
        if not any(inside(p) for p in outline):
            skipped["blocking_objects"] += 1
            continue
        blocking.append(fit_blocker([(p[0] - ox, p[1] - oy) for p in outline]))

    trace = None
    if uvtt.image:
        # set_trace_image: 256 image pixels cover one tile at scale 1.0. The
        # image covers the exported region, whose centre is given so it lines
        # up with the walls even on a map of another size.
        trace = {
            "scale": round(WOXELS_PER_GRID / uvtt.pixels_per_grid, 6),
            "x": round(width * WOXELS_PER_GRID / 2, 2),
            "y": round(height * WOXELS_PER_GRID / 2, 2),
            "opacity": 0.5,
        }

    current = None
    needs_resize = None
    if current_map_size_tiles is not None:
        current = [round(float(v)) for v in list(current_map_size_tiles)[:2]]
        needs_resize = current != tiles
        if needs_resize:
            notes.append(
                f"the open map is {current[0]} x {current[1]} tiles and the export {tiles[0]} x {tiles[1]}: "
                + ("everything lands in its top-left corner, with the trace placed to match"
                   if current[0] >= tiles[0] and current[1] >= tiles[1]
                   else "part of the import would fall outside the map; open or create a map of the export's size"))
    if (ox, oy) != (0.0, 0.0):
        notes.append(f"a region export: positions were moved by ({-ox:g}, {-oy:g}) grid squares so the "
                     "region's top-left corner is the map's origin")
    outside = {kind: count for kind, count in skipped.items() if count}
    if outside:
        notes.append("left out as outside the exported region: "
                     + ", ".join(f"{count} {kind.replace('_', ' ')}" for kind, count in outside.items()))
    if past_region:
        notes.append(f"{past_region} wall(s) cross the region's edge and keep their points beyond it")
    if off_wall:
        notes.append(f"{off_wall} wall-mounted portal(s) sit on no planned wall: add_portal mounts them on the "
                     "nearest wall within snap_max, or places them freestanding")
    if uvtt.dropped:
        notes.append("malformed entries left out: "
                     + ", ".join(f"{count} {kind}" for kind, count in uvtt.dropped.items()))
    if uvtt.baked_lighting is False:
        notes.append("the source map had lighting turned off (baked_lighting false)")

    summary = {
        "map_size_tiles": tiles,
        "walls": len(walls),
        "doorways_joined": doorways_joined,
        "portals": len(portals),
        "doors": sum(1 for p in portals if p["kind"] == "door"),
        "windows": sum(1 for p in portals if p["kind"] == "window"),
        "freestanding_portals": sum(1 for p in portals if p["mount"] == "free"),
        "lights": len(lights),
        "blocking_objects": len(blocking),
        "ambient_light": uvtt.ambient_light,
        "trace_image": trace is not None,
        "outside_region": outside,
        "malformed": dict(uvtt.dropped),
    }
    return {
        "map_size_tiles": tiles,
        "map_origin": [ox, oy],
        "pixels_per_grid": uvtt.pixels_per_grid,
        "current_map_size_tiles": current,
        "needs_resize": needs_resize,
        "walls": walls,
        "portals": portals,
        "lights": lights,
        "ambient_light": uvtt.ambient_light,
        "blocking_objects": blocking,
        "trace": trace,
        "summary": summary,
        "notes": notes,
    }


# --------------------------------------------------------------------------
# The trace image
# --------------------------------------------------------------------------

def decode_image(uvtt: UvttFile) -> TraceImage:
    """The export's image as file bytes, with its format and header size.

    The caller writes `data` to a file with `suffix` and hands it to
    set_trace_image; nothing here decodes pixels. Refused when the export has
    no image, the base64 is broken, the format is not PNG, WEBP or JPEG, or
    the image is too large for Dungeondraft to load.
    """
    if not uvtt.image:
        raise UvttError("the export has no image to trace")
    try:
        data = base64.b64decode("".join(uvtt.image.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UvttError("the export's image is not valid base64") from exc
    fmt = _image_format(data)
    if fmt is None:
        raise UvttError("the export's image is not a PNG, WEBP or JPEG")
    size = _image_size(data, fmt)
    if size is not None and max(size) > MAX_IMAGE_EDGE:
        raise UvttError(f"the image is {size[0]} x {size[1]} pixels; Dungeondraft cannot load a texture "
                        f"longer than {MAX_IMAGE_EDGE} on a side: export again at a lower pixels-per-grid")
    width, height = size if size is not None else (None, None)
    return TraceImage(data, fmt, width, height)


def _image_format(data: bytes) -> Optional[str]:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    return None


def _image_size(data: bytes, fmt: str) -> Optional[tuple[int, int]]:
    """Pixel size from the file header, or None if the header is not as expected."""
    try:
        if fmt == "png":
            if data[12:16] != b"IHDR":
                return None
            width, height = struct.unpack(">II", data[16:24])
            return (width, height)
        if fmt == "webp":
            chunk = data[12:16]
            if chunk == b"VP8X":
                return (int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1)
            if chunk == b"VP8L" and data[20] == 0x2F:
                bits = int.from_bytes(data[21:25], "little")
                return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
            if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
                width, height = struct.unpack("<HH", data[26:30])
                return (width & 0x3FFF, height & 0x3FFF)
            return None
        # JPEG: the first start-of-frame marker carries the size.
        offset = 2
        while offset + 9 <= len(data) and data[offset] == 0xFF:
            marker = data[offset + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                offset += 2
                continue
            length = struct.unpack(">H", data[offset + 2:offset + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[offset + 5:offset + 9])
                return (width, height)
            offset += 2 + length
        return None
    except (struct.error, IndexError):
        return None
