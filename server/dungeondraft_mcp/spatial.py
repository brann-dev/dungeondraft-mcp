"""Layout checks on a snapshot of the current level.

The mod's `spatial_snapshot` command reports geometry only: every object's
footprint (the box around its sprite's opaque pixels, carried through the
object's rotation and scale), the walls with their thickness, the wall-mounted
doors and windows, the map size and the level's regions. Everything in this
module is plain geometry on that snapshot, so it runs, and is tested, without
Dungeondraft.

Coordinates are woxels (256 per tile). A footprint is always a convex quad;
regions and edit shapes are simple polygons and may be concave. This is a 2D
guard against obvious mistakes (a table through a wall, a crate in a doorway),
not a physics or line-of-sight model: an L-shaped sofa is still a rectangle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

Point = tuple[float, float]
Polygon = Sequence[Sequence[float]]

# Furniture pushed flush against a wall or another object touches it; that is
# what a furnished room looks like, not a conflict. Distances within TOUCH of
# contact, and overlaps smaller than MIN_OVERLAP, count as touching.
TOUCH = 0.5          # woxels
MIN_OVERLAP = 16.0   # square woxels (a 4 x 4 woxel sliver)

DEFAULT_DOOR_CLEARANCE = 128.0   # half a tile on each side of a door


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------

def _cross(a, b, c) -> float:
    """z of (b - a) x (c - a): which side of the line a->b the point c is on."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def signed_area(poly: Polygon) -> float:
    n = len(poly)
    return 0.5 * sum(poly[i][0] * poly[(i + 1) % n][1] - poly[(i + 1) % n][0] * poly[i][1]
                     for i in range(n))


def area(poly: Polygon) -> float:
    return abs(signed_area(poly)) if len(poly) >= 3 else 0.0


def edges(poly: Polygon, closed: bool = True):
    n = len(poly)
    for i in range(n if closed else n - 1):
        yield poly[i], poly[(i + 1) % n]


def bounds(poly: Polygon) -> tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def _far_apart(a: Polygon, b: Polygon, margin: float = 0.0) -> bool:
    """Cheap rejection: the bounding boxes are more than `margin` apart."""
    ax0, ay0, ax1, ay1 = bounds(a)
    bx0, by0, bx1, by1 = bounds(b)
    return ax0 > bx1 + margin or bx0 > ax1 + margin or ay0 > by1 + margin or by0 > ay1 + margin


def clip(subject: Polygon, convex: Polygon) -> list[Point]:
    """The part of `subject` inside the convex polygon `convex` (Sutherland-Hodgman).

    `subject` may be concave; the result can then carry zero-width bridges
    between its pieces, which is fine for measuring area.
    """
    clipper = list(convex) if signed_area(convex) >= 0 else list(reversed(convex))
    out = [(float(p[0]), float(p[1])) for p in subject]
    for a, b in edges(clipper):
        if not out:
            break
        points, out = out, []
        for i, cur in enumerate(points):
            prev = points[i - 1]
            side_cur, side_prev = _cross(a, b, cur), _cross(a, b, prev)
            if (side_cur >= 0) != (side_prev >= 0):
                t = side_prev / (side_prev - side_cur)
                out.append((prev[0] + (cur[0] - prev[0]) * t, prev[1] + (cur[1] - prev[1]) * t))
            if side_cur >= 0:
                out.append(cur)
    return out


def overlap_area(poly: Polygon, convex: Polygon) -> float:
    """Area shared by a simple polygon and a convex one."""
    if _far_apart(poly, convex):
        return 0.0
    return area(clip(poly, convex))


def point_in_polygon(p, poly: Polygon) -> bool:
    """Even-odd test; a point on the outline counts as inside."""
    inside = False
    for a, b in edges(poly):
        if point_segment_distance(p, a, b) <= 1e-9:
            return True
        if (a[1] > p[1]) != (b[1] > p[1]):
            if p[0] < (b[0] - a[0]) * (p[1] - a[1]) / (b[1] - a[1]) + a[0]:
                inside = not inside
    return inside


def point_segment_distance(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def segments_intersect(a, b, c, d) -> bool:
    """True if the segments cross or touch."""
    d1, d2 = _cross(c, d, a), _cross(c, d, b)
    d3, d4 = _cross(a, b, c), _cross(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 != 0 and d2 != 0 and d3 != 0 and d4 != 0:
        return True
    return min(point_segment_distance(a, c, d), point_segment_distance(b, c, d),
               point_segment_distance(c, a, b), point_segment_distance(d, a, b)) <= 1e-9


def segment_distance(a, b, c, d) -> float:
    if segments_intersect(a, b, c, d):
        return 0.0
    return min(point_segment_distance(a, c, d), point_segment_distance(b, c, d),
               point_segment_distance(c, a, b), point_segment_distance(d, a, b))


def polygon_segment_distance(poly: Polygon, a, b) -> float:
    """Distance from a polygon (its inside included) to a segment; 0 if they meet."""
    if point_in_polygon(a, poly) or point_in_polygon(b, poly):
        return 0.0
    return min(segment_distance(p, q, a, b) for p, q in edges(poly))


def polygon_distance(a: Polygon, b: Polygon) -> float:
    """Distance between two polygons (insides included); 0 if they meet."""
    if point_in_polygon(a[0], b) or point_in_polygon(b[0], a):
        return 0.0
    return min(segment_distance(p, q, r, s) for p, q in edges(a) for r, s in edges(b))


def polyline_polygon_distance(path: Polygon, poly: Polygon) -> float:
    """Distance from an open path (or a single point) to a polygon; 0 if they meet."""
    if len(path) == 1:
        if point_in_polygon(path[0], poly):
            return 0.0
        return min(point_segment_distance(path[0], p, q) for p, q in edges(poly))
    return min(polygon_segment_distance(poly, a, b) for a, b in edges(path, closed=False))


def rect_polygon(rect: Sequence[float]) -> list[Point]:
    x, y, w, h = (float(v) for v in rect[:4])
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


def transform(local: Polygon, position, scale, rotation_deg: float) -> list[Point]:
    """Place an object-space footprint in the world: scale, then rotate, then move."""
    cos, sin = math.cos(math.radians(rotation_deg)), math.sin(math.radians(rotation_deg))
    out = []
    for x, y in local:
        x, y = x * scale[0], y * scale[1]
        out.append((position[0] + cos * x - sin * y, position[1] + sin * x + cos * y))
    return out


def centroid(poly: Polygon) -> Point:
    return (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))


# --------------------------------------------------------------------------
# Where would this edit put the object?
# --------------------------------------------------------------------------

@dataclass
class Proposal:
    polygon: list[Point]       # the footprint after the edit
    layer: int                 # the layer it will be on
    ignore_id: Optional[int]   # the object being moved, so it can't block itself


def proposed_footprint(scene: dict, cmd: str, params: dict) -> Optional[Proposal]:
    """The footprint an object command would produce, or None if the command's
    target is not an object in the snapshot (a light, a text, a wall).

    Understands the bridge parameters of place_object, import_image,
    move_element, modify_object and duplicate_object.
    """
    if cmd in ("place_object", "import_image"):
        fp = scene.get("footprint")
        if fp is None:
            raise ValueError("snapshot has no 'footprint' for the new object")
        size = scene["map_size"]
        position = (size[0] / 2, size[1] / 2)    # the bridge's default: map center
        if params.get("x") is not None and params.get("y") is not None:
            position = (params["x"], params["y"])
        s = float(params.get("scale", 1.0))
        polygon = transform(fp["local"], position, (s, s), float(params.get("rotation", 0.0)))
        return Proposal(polygon, int(params.get("layer", 0)), None)

    obj = find_object(scene, params.get("id"))
    if obj is None:
        return None
    if cmd == "move_element":
        return moved(obj, x=params["x"], y=params["y"])
    if cmd == "modify_object":
        return moved(obj, scale=params.get("scale"), rotation=params.get("rotation"))
    if cmd == "duplicate_object":
        x, y = obj["position"]
        copy = moved(obj, x=x + params.get("dx", 64.0), y=y + params.get("dy", 0.0))
        copy.ignore_id = None    # the copy can overlap its source
        return copy
    raise ValueError(f"not an object command: {cmd}")


def find_object(scene: dict, id) -> Optional[dict]:
    return next((o for o in scene["objects"] if o["id"] == id), None)


def moved(obj: dict, *, x=None, y=None, scale=None, rotation=None) -> Proposal:
    """A snapshot object's footprint after a move, a uniform rescale and/or a turn."""
    position = obj["position"]
    if x is not None and y is not None:
        position = (x, y)
    size = obj["scale"] if scale is None else (float(scale), float(scale))
    turn = obj["rotation"] if rotation is None else float(rotation)
    return Proposal(transform(obj["local"], position, size, turn), int(obj["layer"]), obj["id"])


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def _conflict(code: str, message: str, **data) -> dict:
    return {"code": code, "message": message, **data}


def _asset_name(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def door_zone(portal: dict, depth: float) -> list[Point]:
    """The strip to keep clear at a door: as wide as the door, `depth` to each side of the wall."""
    (x, y), (tx, ty), r = portal["position"], portal["direction"], portal["radius"]
    nx, ny = -ty, tx
    return [(x + tx * a + nx * b, y + ty * a + ny * b)
            for a, b in ((-r, -depth), (r, -depth), (r, depth), (-r, depth))]


def find_region(scene: dict, name: str, kind: Optional[str] = None) -> dict:
    for region in scene.get("regions", []):
        if region["name"] == name and (kind is None or region["kind"] == kind):
            return region
    wanted = f"{kind} region" if kind else "region"
    raise ValueError(f"no {wanted} named '{name}' on this level (see list_spatial_regions)")


def check_footprint(
    scene: dict,
    polygon: Polygon,
    *,
    layer: int = 0,
    ignore_id: Optional[int] = None,
    allow_overlap: bool = False,
    wall_clearance: float = 0.0,
    door_clearance: float = DEFAULT_DOOR_CLEARANCE,
    object_clearance: float = 0.0,
    region: str = "",
) -> list[dict]:
    """Everything wrong with putting a footprint at `polygon`. Empty list = fine.

    Conflict codes: outside_map, wall, door, object, clearance, outside_room.
    Objects only conflict with objects on the same layer (a rug on a lower
    layer, a candle on a higher one are meant to overlap), and not at all with
    allow_overlap. Touching is never a conflict.
    """
    if min(wall_clearance, door_clearance, object_clearance) < 0:
        raise ValueError("clearances must be >= 0")
    out: list[dict] = []
    width, height = scene["map_size"]
    if any(x < -TOUCH or y < -TOUCH or x > width + TOUCH or y > height + TOUCH for x, y in polygon):
        out.append(_conflict("outside_map", "sticks out of the map"))

    for wall in scene["walls"]:
        reach = wall["half_width"] + wall_clearance
        if _far_apart(polygon, wall["points"], reach):
            continue
        if any(polygon_segment_distance(polygon, a, b) < reach - TOUCH
               for a, b in edges(wall["points"], wall["loop"])):
            out.append(_conflict("wall", f"crosses wall {wall['id']}", wall_id=wall["id"]))

    if door_clearance > 0:
        for portal in scene["portals"]:
            if portal.get("window"):
                continue
            if overlap_area(door_zone(portal, door_clearance), polygon) > MIN_OVERLAP:
                out.append(_conflict("door", f"blocks door {portal['id']}", portal_id=portal["id"]))

    if not allow_overlap:
        for other in scene["objects"]:
            if other["id"] == ignore_id or int(other["layer"]) != int(layer):
                continue
            if _far_apart(polygon, other["polygon"], object_clearance):
                continue
            too_close = object_clearance > 0 and polygon_distance(polygon, other["polygon"]) < object_clearance - TOUCH
            if too_close or overlap_area(other["polygon"], polygon) > MIN_OVERLAP:
                out.append(_conflict(
                    "object", f"overlaps object {other['id']} ({_asset_name(other['asset'])})",
                    object_id=other["id"]))

    for reg in scene.get("regions", []):
        if reg["kind"] == "clearance" and overlap_area(reg["points"], polygon) > MIN_OVERLAP:
            out.append(_conflict("clearance", f"is in the clear zone '{reg['name']}'", region=reg["name"]))
    if region:
        room = find_region(scene, region, "room")
        if overlap_area(room["points"], polygon) < area(polygon) - MIN_OVERLAP:
            out.append(_conflict("outside_room", f"is not fully inside room '{region}'", region=region))
    return out


def audit(
    scene: dict,
    *,
    door_clearance: float = DEFAULT_DOOR_CLEARANCE,
    region: str = "",
    limit: int = 100,
) -> dict:
    """Check every object on the level: `problems` (outside the map, through a
    wall, in a doorway, in a clear zone) and, separately, same-layer `overlaps`,
    which are often intended (tree canopies, chairs tucked under a table).

    region: only audit objects whose center is inside this region (any kind).
    Lists are capped at `limit`; `counts` cover everything found.
    """
    objects = scene["objects"]
    if region:
        inside = find_region(scene, region)["points"]
        objects = [o for o in objects if point_in_polygon(centroid(o["polygon"]), inside)]
    problems: list[dict] = []
    counts: dict[str, int] = {}
    for obj in objects:
        for c in check_footprint(scene, obj["polygon"], layer=obj["layer"], ignore_id=obj["id"],
                                 allow_overlap=True, door_clearance=door_clearance):
            counts[c["code"]] = counts.get(c["code"], 0) + 1
            if len(problems) < limit:
                problems.append({"id": obj["id"], "asset": _asset_name(obj["asset"]), **c})
    overlaps: list[dict] = []
    overlap_count = 0
    for i, a in enumerate(objects):
        for b in objects[i + 1:]:
            if int(a["layer"]) != int(b["layer"]) or _far_apart(a["polygon"], b["polygon"]):
                continue
            shared = overlap_area(a["polygon"], b["polygon"])
            if shared > MIN_OVERLAP:
                overlap_count += 1
                if len(overlaps) < limit:
                    overlaps.append({"ids": [a["id"], b["id"]], "layer": a["layer"], "area": round(shared)})
    return {
        "ok": not counts,
        "objects_checked": len(objects),
        "counts": {**counts, "overlap": overlap_count},
        "problems": problems,
        "overlaps": overlaps,
        "unreadable": scene.get("unreadable", []),
    }


def protected_hits(
    regions: Sequence[dict],
    *,
    polygon: Optional[Polygon] = None,
    path: Optional[Polygon] = None,
    radius: float = 0.0,
    whole_level: bool = False,
) -> list[str]:
    """Names of the 'protected' regions an area edit would reach.

    Give the edit as a filled `polygon`, as a brush stroke (`path` of one or
    more points with a `radius`), or as `whole_level`. `radius` also pads a
    polygon, for edits that bleed past their outline. Touching counts: for a
    protected region the check errs on the side of refusing.
    """
    hits = []
    for reg in regions:
        if reg["kind"] != "protected":
            continue
        if whole_level:
            hit = True
        elif polygon is not None:
            hit = not _far_apart(polygon, reg["points"], radius) and polygon_distance(polygon, reg["points"]) <= radius
        elif path:
            hit = polyline_polygon_distance(path, reg["points"]) <= radius
        else:
            raise ValueError("give a polygon, a path or whole_level")
        if hit:
            hits.append(reg["name"])
    return hits


def describe(conflicts: Sequence[dict]) -> str:
    """One line for an error message: 'crosses wall 40; blocks door 41'."""
    return "; ".join(c["message"] for c in conflicts)
