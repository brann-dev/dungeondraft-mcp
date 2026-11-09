"""What a crop keeps, drops and cuts, decided on a snapshot of the level.

A crop keeps the tiles in a window and Dungeondraft moves everything by the
trimmed amount. What lies wholly outside the window would sit off the map
afterwards, so crop_map deletes it; what crosses the window's edge is kept
and reported, because cutting a wall or path at the edge is not done here.
Everything in this module is plain geometry on data the bridge already
reports, so it runs, and is tested, without Dungeondraft.

Coordinates are woxels (256 per tile). The window is [x0, y0, x1, y1].
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

WOXELS_PER_TILE = 256

Window = tuple[float, float, float, float]


def window_woxels(rect: Sequence[int]) -> Window:
    """A tile window [x, y, w, h] as woxel edges."""
    x, y, w, h = rect
    return (x * WOXELS_PER_TILE, y * WOXELS_PER_TILE,
            (x + w) * WOXELS_PER_TILE, (y + h) * WOXELS_PER_TILE)


def _inside(point: Sequence[float], window: Window) -> bool:
    x0, y0, x1, y1 = window
    return x0 <= point[0] <= x1 and y0 <= point[1] <= y1


def _segment_meets(a: Sequence[float], b: Sequence[float], window: Window) -> bool:
    """Whether the segment a-b touches the window (Liang-Barsky clipping)."""
    x0, y0, x1, y1 = window
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def _contains(polygon: Sequence[Sequence[float]], point: Sequence[float]) -> bool:
    inside = False
    px, py = point
    j = len(polygon) - 1
    for i, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[j][0], polygon[j][1]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def place_points(points: Sequence[Sequence[float]], loop: bool, window: Window) -> str:
    """'inside', 'outside' or 'crossing' for a polyline or polygon."""
    if not points:
        return "inside"
    flags = [_inside(p, window) for p in points]
    if all(flags):
        return "inside"
    if any(flags):
        return "crossing"
    pairs = list(zip(points, points[1:]))
    if loop and len(points) > 2:
        pairs.append((points[-1], points[0]))
    if any(_segment_meets(a, b, window) for a, b in pairs):
        return "crossing"
    if loop and len(points) > 2:
        # A closed shape can surround the window without touching it.
        x0, y0, x1, y1 = window
        if _contains(points, ((x0 + x1) / 2, (y0 + y1) / 2)):
            return "crossing"
    return "outside"


def place_polygon(polygon: Sequence[Sequence[float]], window: Window) -> str:
    """'inside', 'outside' or 'crossing' for an object's footprint quad."""
    return place_points(polygon, True, window)


def is_cave_wall(asset: Any) -> bool:
    """Cave borders are rebuilt from the cave floor; a crop never edits them."""
    return "/caves/" in str(asset or "")


def classify(snapshot: dict, positioned: dict[str, Iterable[dict]], window: Window) -> dict:
    """Ids of what lies wholly outside the window and what crosses its edge, per kind.

    snapshot: the spatial_snapshot result (objects with polygon, walls with
    points and loop, portals with position and wall_id). positioned: other
    list_elements results by kind ({id, position}): lights, texts, roofs,
    paths. A wall's doors go with the wall: a door is listed on its own only
    when its wall stays.
    """
    outside: dict[str, list[int]] = {}
    crossing: dict[str, list[int]] = {}
    dropped_walls: set[int] = set()
    for obj in snapshot.get("objects", []):
        where = place_polygon(obj.get("polygon") or [], window) if obj.get("polygon") else "inside"
        if where == "outside":
            outside.setdefault("objects", []).append(int(obj["id"]))
        elif where == "crossing":
            crossing.setdefault("objects", []).append(int(obj["id"]))
    for wall in snapshot.get("walls", []):
        if is_cave_wall(wall.get("asset")):
            continue
        where = place_points(wall.get("points") or [], bool(wall.get("loop")), window)
        if where == "outside":
            outside.setdefault("walls", []).append(int(wall["id"]))
            dropped_walls.add(int(wall["id"]))
        elif where == "crossing":
            crossing.setdefault("walls", []).append(int(wall["id"]))
    for portal in snapshot.get("portals", []):
        if int(portal.get("wall_id", -1)) in dropped_walls:
            continue
        if not _inside(portal.get("position") or [0, 0], window):
            outside.setdefault("portals", []).append(int(portal["id"]))
    for kind, entries in positioned.items():
        for element in entries:
            if not _inside(element.get("position") or [0, 0], window):
                outside.setdefault(kind, []).append(int(element["id"]))
    return {"outside": outside, "crossing": crossing}
