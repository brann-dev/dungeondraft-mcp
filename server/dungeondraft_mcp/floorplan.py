"""Structure of a floorplan before it is furnished: rooms, doors and a way in.

The spatial snapshot says where the walls and doors are, not whether they make
a building. A room with no door, a door whose far side is another wall, a
house that cannot be entered from outside: their element counts look like a
finished plan, and the mistake surfaces in a screenshot once the furniture is
in and has to move twice. This module answers those questions from the
snapshot alone; nothing is placed or changed.

Walls are polylines that overlap, double back or stop a few woxels short of
meeting, so this is a raster, not exact polygon work. The walls are painted
with their thickness onto a grid of square cells and the open cells are
flood-filled: each pool is an area, and an area touching the map edge is the
outside. A wall that misses its neighbour by less than a cell reads as joined,
which is also how it renders. Each door and window is then probed on both
sides of its wall to see which areas it joins, and the doors form a graph over
the areas that says what can be reached from outside.

Coordinates are woxels (256 per tile). A cell is `cell_woxels` wide, 64 (a
quarter tile) by default: a one-tile doorway is then four cells wide and no
rounding can close it. The grid is capped at MAX_CELLS cells; a bigger map
gets a coarser cell and the report says so. A 200-tile map at the default
cell is 800 x 800 = 640,000 cells, under the cap, and takes about a second in
pure Python.

Everything here runs, and is tested, without Dungeondraft.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence, Union

from .spatial import edges, point_segment_distance

WOXELS_PER_TILE = 256
DEFAULT_CELL = 64                 # a quarter tile
DEFAULT_MIN_ROOM_TILES = 1.0      # below this an enclosed area is a sliver, not a room
DEFAULT_HALF_WIDTH = 32.0         # for a wall the snapshot gives no half_width
MAX_CELLS = 1_000_000

# What a portal can have on one side besides a room (a room is its integer id).
OUTSIDE = "outside"     # open ground that reaches the map edge
WALL = "wall"           # the probe landed in a wall: the door opens onto masonry
SLIVER = "sliver"       # an enclosed area under min_room_tiles
OFF_MAP = "off_map"

Side = Union[int, str]


# --------------------------------------------------------------------------
# Raster
# --------------------------------------------------------------------------

@dataclass
class Grid:
    """Walls painted onto cells. `blocked[i]` is 1 for a wall cell, i = y * cols + x."""
    cell: int
    cols: int
    rows: int
    blocked: bytearray

    def index(self, x: float, y: float) -> Optional[int]:
        """The cell holding a woxel point, or None off the grid."""
        cx, cy = int(x // self.cell), int(y // self.cell)
        if 0 <= cx < self.cols and 0 <= cy < self.rows:
            return cy * self.cols + cx
        return None


@dataclass
class Area:
    """One pool of open cells, in cell coordinates."""
    label: int
    cells: list[int] = field(default_factory=list)
    x0: int = 1 << 30
    y0: int = 1 << 30
    x1: int = -1
    y1: int = -1
    border: bool = False      # touches the map edge: this is the outside


def grid_dims(width: float, height: float, cell: int) -> tuple[int, int]:
    return max(1, math.ceil(width / cell)), max(1, math.ceil(height / cell))


def choose_cell(width: float, height: float, cell_woxels: int, max_cells: int = MAX_CELLS) -> int:
    """The smallest cell >= cell_woxels whose grid over the map has at most max_cells cells."""
    if cell_woxels <= 0:
        raise ValueError("cell_woxels must be > 0")
    if max_cells < 4:
        raise ValueError("max_cells must be >= 4")
    cell = max(int(cell_woxels), math.ceil(math.sqrt(width * height / max_cells)))
    while math.prod(grid_dims(width, height, cell)) > max_cells:
        cell += 1
    return cell


def rasterise(walls: Sequence[dict], width: float, height: float, cell: int) -> Grid:
    """Paint snapshot walls ({points, loop, half_width}) onto a fresh grid."""
    cols, rows = grid_dims(width, height, cell)
    grid = Grid(cell, cols, rows, bytearray(cols * rows))
    for wall in walls:
        points = [(float(p[0]), float(p[1])) for p in wall.get("points") or []]
        if len(points) < 2:
            continue
        half = float(wall.get("half_width", DEFAULT_HALF_WIDTH))
        for a, b in edges(points, bool(wall.get("loop"))):
            _paint(grid, a, b, half)
    return grid


def _paint(grid: Grid, a, b, half_width: float) -> None:
    """Block every cell whose inscribed disc the wall band (half_width to each
    side of the segment a-b) reaches.

    That band always covers an 8-connected chain of cells, which a 4-connected
    flood cannot leak through. Sampled along the segment at half-cell steps,
    testing the exact distance for the cells around each sample, so the cost
    follows the wall's length rather than its bounding box.
    """
    cell, cols, rows, blocked = grid.cell, grid.cols, grid.rows, grid.blocked
    reach = half_width + cell / 2
    step = cell / 2
    steps = max(1, math.ceil(math.hypot(b[0] - a[0], b[1] - a[1]) / step))
    window = int((reach + step / 2) // cell) + 1
    for k in range(steps + 1):
        t = k / steps
        px, py = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        cx, cy = int(px // cell), int(py // cell)
        for yy in range(max(0, cy - window), min(rows, cy + window + 1)):
            row = yy * cols
            for xx in range(max(0, cx - window), min(cols, cx + window + 1)):
                i = row + xx
                if not blocked[i] and point_segment_distance(((xx + 0.5) * cell, (yy + 0.5) * cell), a, b) <= reach:
                    blocked[i] = 1


def label_areas(grid: Grid) -> tuple[list[int], dict[int, Area]]:
    """Flood-fill the open cells (4-connected). Returns a label per cell (0 for a
    wall cell) and the areas by label."""
    cols, rows, blocked = grid.cols, grid.rows, grid.blocked
    n = cols * rows
    labels = [0] * n
    areas: dict[int, Area] = {}
    last_col, last_row = cols - 1, rows - 1
    for start in range(n):
        if blocked[start] or labels[start]:
            continue
        area = Area(len(areas) + 1)
        areas[area.label] = area
        labels[start] = area.label
        stack = [start]
        while stack:
            i = stack.pop()
            y, x = divmod(i, cols)
            area.cells.append(i)
            if x < area.x0:
                area.x0 = x
            if x > area.x1:
                area.x1 = x
            if y < area.y0:
                area.y0 = y
            if y > area.y1:
                area.y1 = y
            if x == 0 or y == 0 or x == last_col or y == last_row:
                area.border = True
            if x > 0 and not blocked[i - 1] and not labels[i - 1]:
                labels[i - 1] = area.label
                stack.append(i - 1)
            if x < last_col and not blocked[i + 1] and not labels[i + 1]:
                labels[i + 1] = area.label
                stack.append(i + 1)
            if y > 0 and not blocked[i - cols] and not labels[i - cols]:
                labels[i - cols] = area.label
                stack.append(i - cols)
            if y < last_row and not blocked[i + cols] and not labels[i + cols]:
                labels[i + cols] = area.label
                stack.append(i + cols)
    return labels, areas


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def _finding(code: str, message: str, **data) -> dict:
    return {"code": code, "message": message, **data}


def _probe_sides(grid: Grid, labels: list[int], portal: dict, distance: float) -> list[Side]:
    """The area label on each side of a portal, probed `distance` woxels from
    the wall's centreline at the portal's middle and at half its radius to
    either side (so a door placed at a corner is not mistaken for one that
    opens onto the corner's other wall). WALL if every probe hit a wall cell,
    OFF_MAP if one left the map."""
    x, y = (float(v) for v in portal["position"][:2])
    tx, ty = (float(v) for v in (portal.get("direction") or (1.0, 0.0))[:2])
    length = math.hypot(tx, ty)
    tx, ty = (tx / length, ty / length) if length > 1e-6 else (1.0, 0.0)
    nx, ny = -ty, tx
    r = float(portal.get("radius", 64.0))
    sides: list[Side] = []
    for sign in (-1.0, 1.0):
        found: Side = WALL
        for along in (0.0, -r / 2, r / 2):
            i = grid.index(x + tx * along + nx * sign * distance, y + ty * along + ny * sign * distance)
            if i is None:
                found = OFF_MAP
            elif not grid.blocked[i]:
                found = labels[i]
                break
        sides.append(found)
    return sides


def _room_center(grid: Grid, area: Area) -> list[float]:
    """A point inside the room near its middle: the cell closest to the centroid
    (the centroid itself can fall outside an L-shaped room)."""
    cols = grid.cols
    n = len(area.cells)
    cx = sum(i % cols for i in area.cells) / n
    cy = sum(i // cols for i in area.cells) / n
    best = min(area.cells, key=lambda i: (i % cols - cx) ** 2 + (i // cols - cy) ** 2)
    by, bx = divmod(best, cols)
    return [(bx + 0.5) * grid.cell, (by + 0.5) * grid.cell]


def analyse(
    scene: dict,
    *,
    cell_woxels: int = DEFAULT_CELL,
    min_room_tiles: float = DEFAULT_MIN_ROOM_TILES,
    max_cells: int = MAX_CELLS,
) -> dict:
    """Rooms, portals and a way in, from a `spatial_snapshot` (its map_size,
    walls and portals; objects and regions are ignored).

    Rooms are the enclosed areas of at least `min_room_tiles`, numbered in
    reading order (top-left first) for this report only. Each portal lists the
    two `sides` it joins: a room id, or OUTSIDE, WALL, SLIVER, OFF_MAP. Doors
    are analysed as openings whatever their closed flag; windows attach to
    their sides too but are not a way through. Findings carry `code`,
    `message` and the `room_id` or `portal_id` concerned:

      sealed_room        a room with no door (windows do not count)
      unreachable_room   a room with doors, but no chain of doors leads outside
      portal_to_nothing  a door or window with a wall, a sliver or the map edge
                         on one side, or the same area on both sides
      no_entrance        there are rooms, but no door opens to the outside

    `ok` is true when there are no findings. `entrances` lists the doors
    between a room and the outside. Enclosed areas under `min_room_tiles` (the
    gap between two walls that nearly meet) are counted in `notes` instead of
    being reported as rooms. When no open ground reaches the map edge (a
    dungeon walled to the border) reachability is not judged and every room's
    `reachable_from_outside` is null.
    """
    width, height = (float(v) for v in scene["map_size"][:2])
    if width <= 0 or height <= 0:
        raise ValueError("map_size must be positive")
    if min_room_tiles < 0:
        raise ValueError("min_room_tiles must be >= 0")
    walls = scene.get("walls", [])
    notes: list[str] = []

    cell = choose_cell(width, height, cell_woxels, max_cells)
    if cell != cell_woxels:
        notes.append(f"cell raised from {cell_woxels} to {cell} woxels to keep the grid "
                     f"under {max_cells:,} cells; doorways narrower than two cells may read as closed")
    grid = rasterise(walls, width, height, cell)
    labels, areas = label_areas(grid)
    tile_area = (cell / WOXELS_PER_TILE) ** 2

    outside_labels = {label for label, area in areas.items() if area.border}
    sliver_labels = {label for label, area in areas.items()
                     if not area.border and len(area.cells) * tile_area < min_room_tiles}
    room_labels = sorted((label for label in areas if label not in outside_labels and label not in sliver_labels),
                         key=lambda label: (areas[label].y0, areas[label].x0))
    room_id = {label: n + 1 for n, label in enumerate(room_labels)}

    def side_name(side: Side) -> Side:
        if isinstance(side, str):
            return side
        if side in outside_labels:
            return OUTSIDE
        if side in sliver_labels:
            return SLIVER
        return room_id[side]

    def describe(side: Side) -> str:
        name = side_name(side)
        return f"room {name}" if isinstance(name, int) else ("the outside" if name == OUTSIDE else name)

    # Portals: what is on each side, and the door graph between areas.
    half_widths = {w["id"]: float(w.get("half_width", DEFAULT_HALF_WIDTH)) for w in walls if "id" in w}
    portals: list[dict] = []
    room_portals: dict[int, list[int]] = {label: [] for label in room_labels}
    links: dict[int, set[int]] = {}
    entrances: list[int] = []
    findings: list[dict] = []
    for portal in scene.get("portals", []):
        pid = portal["id"]
        kind = "window" if portal.get("window") else "door"
        reach = half_widths.get(portal.get("wall_id"), DEFAULT_HALF_WIDTH) + cell / 2
        sides = _probe_sides(grid, labels, portal, reach + cell)
        problem: Optional[str] = None
        if WALL in sides:
            problem = f"{kind} {pid} opens onto a wall"
        elif OFF_MAP in sides:
            problem = f"{kind} {pid} leads off the map"
        elif any(side in sliver_labels for side in sides):
            problem = f"{kind} {pid} opens onto a sliver of enclosed space, not a room"
        elif sides[0] == sides[1]:
            problem = f"{kind} {pid} has {describe(sides[0])} on both sides: the wall it is in encloses nothing"
        for side in set(sides):
            if side in room_portals:
                room_portals[side].append(pid)
        if problem is None and kind == "door":
            a, b = sides
            links.setdefault(a, set()).add(b)
            links.setdefault(b, set()).add(a)
            if (a in outside_labels) != (b in outside_labels):
                entrances.append(pid)
        if problem is not None:
            findings.append(_finding("portal_to_nothing", problem, portal_id=pid))
        portals.append({
            "id": pid, "kind": kind, "wall_id": portal.get("wall_id"),
            "position": [float(v) for v in portal["position"][:2]],
            "sides": [side_name(side) for side in sides], "problem": problem,
        })

    # Walk the door graph from the outside.
    reached: set[int] = set(outside_labels)
    queue = list(outside_labels)
    while queue:
        for nxt in links.get(queue.pop(), ()):
            if nxt not in reached:
                reached.add(nxt)
                queue.append(nxt)

    rooms: list[dict] = []
    for label in room_labels:
        area, rid = areas[label], room_id[label]
        pids = room_portals[label]
        doors = [p for p in portals if p["id"] in pids and p["kind"] == "door"]
        windows = len(pids) - len(doors)
        reachable: Optional[bool] = (label in reached) if outside_labels else None
        rooms.append({
            "id": rid,
            "area_tiles": round(len(area.cells) * tile_area, 2),
            "bounds": [area.x0 * cell, area.y0 * cell,
                       min(width, (area.x1 + 1) * cell), min(height, (area.y1 + 1) * cell)],
            "center": _room_center(grid, area),
            "portal_ids": pids,
            "reachable_from_outside": reachable,
        })
        if not doors:
            extra = f" ({windows} window{'s' if windows != 1 else ''} only)" if windows else ""
            findings.append(_finding("sealed_room", f"room {rid} has no door{extra}", room_id=rid))
        elif reachable is False:
            findings.append(_finding("unreachable_room",
                                     f"room {rid} has a door but no chain of doors leads outside", room_id=rid))
    if rooms and outside_labels and not entrances:
        findings.append(_finding("no_entrance", "no door opens to the outside: the building cannot be entered"))

    if not rooms:
        notes.append("no enclosed rooms: the walls do not close around anything")
    if sliver_labels:
        notes.append(f"{len(sliver_labels)} enclosed area{'s' if len(sliver_labels) != 1 else ''} under "
                     f"{min_room_tiles:g} tile{'s' if min_room_tiles != 1 else ''} ignored (walls that nearly meet)")
    if not outside_labels:
        notes.append("no open ground reaches the map edge, so reachability from outside is not judged")

    return {
        "ok": not findings,
        "cell_woxels": cell,
        "grid": [grid.cols, grid.rows],
        "outside": bool(outside_labels),
        "rooms": rooms,
        "portals": portals,
        "entrances": entrances,
        "findings": findings,
        "notes": notes,
    }
