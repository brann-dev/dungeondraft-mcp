"""Furniture arrangement and shared walls, worked out before anything is placed.

Two jobs, both plain geometry on data the bridge already reports, so they run,
and are tested, without Dungeondraft.

Arrangement: three placements a model gets wrong by hand, in ways the reply
never shows: a shelf left a hand's width off its wall (or through it), chairs
stamped on a perfect grid around a table, books stacked past the edge of a
desk. `arrange_along_wall`, `arrange_around_target` and `arrange_on_surface`
return place_object arguments fitted to each asset's footprint, the box around
its visible pixels (get_asset_footprint), never its canvas.

Shared walls: when a new room's edge lies on an existing wall, drawing the
room's own wall there doubles it, and a door later mounts on only one of the
two. `shared_walls` finds the covered parts of a room's boundary and returns
the open runs that still need a wall.

Coordinates are woxels (256 per tile). Rotations are degrees, clockwise on
screen (y points down), as place_object takes them and spatial.transform
applies them. An asset's `facing` is the way its front points at rotation 0:
"down" is how Dungeondraft's own furniture is drawn (backrest at the top), so
it is the default; pack art varies, and every result says which facing it
assumed. Footprints are rectangles, so look at a screenshot afterwards.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Optional, Sequence

from . import spatial
from .spatial import Point, Polygon

FACINGS: dict[str, Point] = {
    "down": (0.0, 1.0),
    "up": (0.0, -1.0),
    "right": (1.0, 0.0),
    "left": (-1.0, 0.0),
}

DEFAULT_WALL_HALF_WIDTH = 32.0   # what the mod assumes for a wall without a texture
DEFAULT_DOOR_RADIUS = 128.0      # half a door's width, when a door reports none

# A wall segment is "the one meant" when it is this close to the line the
# caller gave and this close to parallel with it.
WALL_DISTANCE_TOLERANCE = 96.0   # woxels
WALL_ANGLE_TOLERANCE = 10.0      # degrees

DEFAULT_MARGIN = 32.0            # kept clear at a wall's corners and beside its doors
MAX_ARRANGED = 40

# Seats: small, deliberate irregularity. A set table is tidy; a perfectly even
# one reads as a grid.
SEAT_SHIFT_JITTER = 6.0          # woxels
SEAT_TURN_JITTER = 3.0           # degrees
END_SEAT_SHARE = 0.75            # a table end this share of a seat's width still takes one seat

SURFACE_MARGIN = 12.0            # how far inside a surface's edge its dressing stays
SURFACE_TURN = 25.0              # how far a dressing item turns from the surface's rotation
SURFACE_TRIES = 80

SHARED_WALL_TOLERANCE = 1.0      # woxels an existing wall may lie off a room's edge and still be it


# --------------------------------------------------------------------------
# Footprints and placements
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Footprint:
    """An asset's visible box: its size at scale 1, where its center sits
    relative to the sprite's center (the point place_object positions), and
    the scale it will be placed at."""
    asset: str
    width: float
    height: float
    offset: Point = (0.0, 0.0)
    scale: float = 1.0

    @property
    def w(self) -> float:
        return self.width * self.scale

    @property
    def h(self) -> float:
        return self.height * self.scale

    def across(self, facing: str) -> float:
        """Extent across the facing: along a wall, or a table edge."""
        return self.w if facing in ("down", "up") else self.h

    def depth(self, facing: str) -> float:
        """Extent in the facing direction: out from the wall."""
        return self.h if facing in ("down", "up") else self.w

    def local(self) -> list[Point]:
        """The visible box's corners in object space at scale 1 (the mod's `local`)."""
        ox, oy = self.offset
        return spatial.rect_polygon((ox - self.width / 2, oy - self.height / 2, self.width, self.height))

    @property
    def name(self) -> str:
        return self.asset.replace("\\", "/").rsplit("/", 1)[-1]


def footprint(spec: dict, scale: float = 1.0) -> Footprint:
    """A Footprint from any shape the bridge reports an asset's size in.

    Accepts get_asset_footprint's {asset, footprint[w,h], center_offset?}, the
    raw asset_footprint {asset, size[W,H], visible[x,y,w,h]}, a snapshot
    object's {asset, local[4]}, or a plain {asset, width, height, offset?}.
    Refuses an asset with no visible pixels rather than falling back to its
    canvas: a canvas-fitted plan is the defect this module exists to avoid.
    """
    asset = str(spec.get("asset", ""))
    if "width" in spec and "height" in spec:
        w, h = float(spec["width"]), float(spec["height"])
        ox, oy = spec.get("offset") or (0.0, 0.0)
    elif "footprint" in spec:
        w, h = (float(v) for v in spec["footprint"][:2])
        ox, oy = spec.get("center_offset") or (0.0, 0.0)
    elif "visible" in spec and "size" in spec:
        x, y, w, h = (float(v) for v in spec["visible"][:4])
        ox, oy = x + w / 2 - float(spec["size"][0]) / 2, y + h / 2 - float(spec["size"][1]) / 2
    elif "local" in spec:
        x0, y0, x1, y1 = spatial.bounds(spec["local"])
        w, h, ox, oy = x1 - x0, y1 - y0, (x0 + x1) / 2, (y0 + y1) / 2
    else:
        raise ValueError(f"no footprint for {asset or 'the asset'}: give width and height (see get_asset_footprint)")
    if w <= 0 or h <= 0:
        raise ValueError(f"{asset or 'the asset'} has no visible pixels (footprint {w:g} x {h:g})")
    if scale <= 0:
        raise ValueError("scale must be > 0")
    return Footprint(asset, w, h, (float(ox), float(oy)), float(scale))


def _rotate(x: float, y: float, degrees: float) -> Point:
    cos, sin = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return (x * cos - y * sin, x * sin + y * cos)


def _normalize(degrees: float) -> float:
    """Into (-180, 180]."""
    degrees = (degrees + 180.0) % 360.0 - 180.0
    return 180.0 if degrees == -180.0 else degrees


def rotation_to_face(facing: str, direction: Sequence[float]) -> float:
    """Degrees that turn an asset drawn facing `facing` to face `direction`."""
    fx, fy = FACINGS[facing]
    return _normalize(math.degrees(math.atan2(direction[1], direction[0]) - math.atan2(fy, fx)))


def placement(fp: Footprint, cx: float, cy: float, rotation: float) -> dict:
    """place_object arguments that put the footprint's center at (cx, cy).

    Dungeondraft positions an object by its sprite's center, which the visible
    box is offset from; that offset scales and turns with the object.
    """
    ox, oy = _rotate(fp.offset[0] * fp.scale, fp.offset[1] * fp.scale, rotation)
    return {
        "asset": fp.asset,
        "x": round(cx - ox, 2),
        "y": round(cy - oy, 2),
        "rotation": round(_normalize(rotation), 2),
        "scale": fp.scale,
    }


def placement_footprint(fp: Footprint, placed: dict) -> list[Point]:
    """Where a placement's footprint lands: a quad spatial.check_footprint takes."""
    return spatial.transform(fp.local(), (placed["x"], placed["y"]), (fp.scale, fp.scale), placed["rotation"])


def sequence(items: Sequence[Footprint], count: int) -> list[Footprint]:
    """The assets in the order given, repeated to `count`."""
    return [items[i % len(items)] for i in range(count)]


def _footprints(assets: Sequence[dict], scale: float) -> list[Footprint]:
    if not assets:
        raise ValueError("give at least one asset footprint")
    return [footprint(spec, scale) for spec in assets]


def _check_facing(facing: str) -> None:
    if facing not in FACINGS:
        raise ValueError(f"facing must be one of {sorted(FACINGS)}")


def _check_count(count: Optional[int]) -> None:
    if count is not None and not 1 <= int(count) <= MAX_ARRANGED:
        raise ValueError(f"count must be 1 to {MAX_ARRANGED}")


def _seed(seed: Optional[int]) -> int:
    return random.randrange(2**31) if seed is None else int(seed)


def _orientation_notes(fps: Sequence[Footprint], facing: str) -> list[str]:
    """Advice when an asset's shape says the facing assumption is wrong: a shelf
    or bench is wider than deep, so one drawn deeper than wide is likely side-on."""
    notes = []
    for fp in {fp.asset: fp for fp in fps}.values():
        if fp.depth(facing) > 1.3 * fp.across(facing):
            notes.append(f"{fp.name} is drawn deeper than wide when facing {facing}, so it may be "
                         "drawn side-on; look, and pass the facing it has")
    return notes


# --------------------------------------------------------------------------
# Along a wall
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class WallSegment:
    """One straight piece of a wall, with the wall it belongs to and that
    wall's whole outline (to tell which side is the room)."""
    wall_id: int
    index: int
    a: Point
    b: Point
    half_width: float
    loop: bool
    outline: tuple[Point, ...]

    @property
    def length(self) -> float:
        return math.dist(self.a, self.b)

    @property
    def unit(self) -> Point:
        length = self.length
        return ((self.b[0] - self.a[0]) / length, (self.b[1] - self.a[1]) / length)

    def project(self, p: Sequence[float]) -> tuple[float, float]:
        """(distance along the segment from `a`, signed distance off its line)."""
        ux, uy = self.unit
        dx, dy = float(p[0]) - self.a[0], float(p[1]) - self.a[1]
        return dx * ux + dy * uy, dx * -uy + dy * ux

    def at(self, s: float, normal: Point = (0.0, 0.0), out: float = 0.0) -> Point:
        ux, uy = self.unit
        return (self.a[0] + ux * s + normal[0] * out, self.a[1] + uy * s + normal[1] * out)


def wall_segments(walls: Sequence[dict]) -> list[WallSegment]:
    """Every wall's segments in order; a loop adds its closing edge.

    A wall is {id, points[[x,y]...], loop, half_width?}, as spatial_snapshot
    reports it; a missing half_width is the mod's default.
    """
    out: list[WallSegment] = []
    for wall in walls:
        pts = tuple((float(p[0]), float(p[1])) for p in wall.get("points") or [])
        if len(pts) < 2:
            continue
        loop = bool(wall.get("loop")) and len(pts) > 2
        half = float(wall.get("half_width", DEFAULT_WALL_HALF_WIDTH))
        wall_id = int(wall.get("id", -1))
        for index, (a, b) in enumerate(spatial.edges(pts, loop)):
            if a != b:
                out.append(WallSegment(wall_id, index, a, b, half, loop, pts))
    return out


def choose_wall_segment(
    walls: Sequence[dict],
    along: Polygon,
    wall_id: Optional[int] = None,
    *,
    distance_tolerance: float = WALL_DISTANCE_TOLERANCE,
    angle_tolerance: float = WALL_ANGLE_TOLERANCE,
) -> tuple[WallSegment, list[int]]:
    """The wall segment the line `along` ([[x0,y0],[x1,y1]]) runs beside, and
    the ids of the other walls that run there too.

    A segment qualifies when it is within `angle_tolerance` of parallel and
    the line's midpoint lies within `distance_tolerance` of it, across and
    past its ends. Two build_room rooms sharing an edge each own a wall along
    it, and a door cuts only one of them: the others are reported so the
    caller can pick with `wall_id`.
    """
    if len(along) != 2:
        raise ValueError("along takes two points: [[x0, y0], [x1, y1]]")
    (ax, ay), (bx, by) = ((float(p[0]), float(p[1])) for p in along)
    if math.hypot(bx - ax, by - ay) == 0:
        raise ValueError("along needs two different points")
    line_angle = math.degrees(math.atan2(by - ay, bx - ax))
    mid = ((ax + bx) / 2, (ay + by) / 2)
    found: list[tuple[float, WallSegment]] = []
    for seg in wall_segments(walls):
        seg_angle = math.degrees(math.atan2(seg.b[1] - seg.a[1], seg.b[0] - seg.a[0]))
        if abs((line_angle - seg_angle + 90.0) % 180.0 - 90.0) > angle_tolerance:
            continue
        s, across = seg.project(mid)
        past_end = max(0.0, -s, s - seg.length)
        if abs(across) > distance_tolerance or past_end > distance_tolerance:
            continue
        found.append((math.hypot(across, past_end), seg))
    candidates = found if wall_id is None else [f for f in found if f[1].wall_id == wall_id]
    if not candidates:
        if wall_id is not None:
            raise ValueError(f"wall {wall_id} has no segment within {distance_tolerance:g} woxels of "
                             "`along` and parallel to it")
        raise ValueError(f"no wall runs within {distance_tolerance:g} woxels of `along` and within "
                         f"{angle_tolerance:g} degrees of parallel to it; read the walls first")
    chosen = min(candidates, key=lambda f: (f[0], f[1].wall_id, f[1].index))[1]
    others = sorted({seg.wall_id for _, seg in found} - {chosen.wall_id})
    return chosen, others


def inward_normal(segment: WallSegment, toward: Optional[Sequence[float]] = None) -> Point:
    """The unit normal pointing from the wall into the room.

    Toward `toward` when given; otherwise, for a looped wall, whichever side
    of the segment's middle lies inside the loop. An open run of wall has no
    inside, so it needs `toward`.
    """
    ux, uy = segment.unit
    side = (-uy, ux)
    if toward is not None:
        _, across = segment.project(toward)
        if abs(across) < 1e-9:
            raise ValueError("`toward` lies on the wall's line; give a point inside the room")
        return side if across > 0 else (-side[0], -side[1])
    if segment.loop:
        mx, my = segment.at(segment.length / 2)
        if spatial.point_in_polygon((mx + side[0] * 2.0, my + side[1] * 2.0), segment.outline):
            return side
        return (-side[0], -side[1])
    raise ValueError(f"wall {segment.wall_id} is an open run with no inside; pass `toward`, "
                     "a point on the side the objects go")


def _door_radius(door: dict) -> float:
    if door.get("radius"):
        return float(door["radius"])
    if door.get("width"):
        return float(door["width"]) / 2
    return DEFAULT_DOOR_RADIUS


def _cut(intervals: list[tuple[float, float]], blocked: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    for b0, b1 in sorted(blocked):
        cut: list[tuple[float, float]] = []
        for i0, i1 in intervals:
            if b1 <= i0 or b0 >= i1:
                cut.append((i0, i1))
                continue
            if b0 > i0:
                cut.append((i0, b0))
            if b1 < i1:
                cut.append((b1, i1))
        intervals = cut
    return intervals


def free_stretches(
    segment: WallSegment,
    along: Polygon,
    doors: Sequence[dict],
    walls: Sequence[dict] = (),
    *,
    margin: float = DEFAULT_MARGIN,
) -> tuple[list[tuple[float, float]], list[int]]:
    """Stretches of the segment, as (start, end) woxels from its first point,
    that objects may use.

    The span `along` covers, clipped to the segment, less `margin` at each end,
    less every other wall meeting this segment (a corner, a partition) widened
    by its half width and `margin`, and less every door on this wall widened
    by `margin`. A door is {id, wall_id?, position[x,y], radius? | width?,
    window?}; windows are not doors and are not kept clear. Returns the
    stretches and the ids of the doors kept clear.
    """
    if margin < 0:
        raise ValueError("margin must be >= 0")
    length = segment.length
    s_a, _ = segment.project(along[0])
    s_b, _ = segment.project(along[1])
    start, end = max(min(s_a, s_b), margin), min(max(s_a, s_b), length - margin)
    blocked: list[tuple[float, float]] = []
    for other in wall_segments(walls):
        if other.wall_id == segment.wall_id and other.index == segment.index:
            continue
        for end_point in (other.a, other.b):
            s, across = segment.project(end_point)
            if abs(across) <= segment.half_width + other.half_width and -other.half_width <= s <= length + other.half_width:
                blocked.append((s - other.half_width - margin, s + other.half_width + margin))
    kept_clear: list[int] = []
    for door in doors:
        position = door.get("position")
        if not position or door.get("window"):
            continue
        if door.get("wall_id") not in (None, segment.wall_id):
            continue
        s, across = segment.project(position)
        radius = _door_radius(door)
        if abs(across) > segment.half_width + margin or s + radius < 0 or s - radius > length:
            continue
        blocked.append((s - radius - margin, s + radius + margin))
        kept_clear.append(int(door.get("id", -1)))
    intervals = [(start, end)] if end > start else []
    return _cut(intervals, blocked), kept_clear


def pack_row(
    intervals: Sequence[tuple[float, float]],
    widths: Sequence[float],
    *,
    gap: Optional[float],
    fill: bool,
) -> list[tuple[int, float]]:
    """(item index, center distance along the wall) for each item that fits.

    Items keep their order and go into the stretches in turn. With `gap`, each
    stretch's row is that far apart and centered; without, the spare room is
    shared evenly around the items. `fill` repeats the widths until nothing
    more fits, up to MAX_ARRANGED.
    """
    minimum = gap or 0.0
    placed: list[tuple[int, float]] = []
    item = 0
    total = MAX_ARRANGED if fill else len(widths)
    for start, end in intervals:
        if item >= total:
            break
        length = end - start
        members: list[int] = []
        used = 0.0
        while item < total:
            need = widths[item % len(widths)] + (minimum if members else 0.0)
            if used + need > length + 1e-6:
                break
            used += need
            members.append(item)
            item += 1
        if not members:
            continue
        here = [widths[i % len(widths)] for i in members]
        if gap is not None:
            spacing = gap
            cursor = start + (length - sum(here) - gap * (len(members) - 1)) / 2
        else:
            spacing = (length - sum(here)) / (len(members) + 1)
            cursor = start + spacing
        for index, width in zip(members, here):
            placed.append((index, cursor + width / 2))
            cursor += width + spacing
    return placed


def arrange_along_wall(
    assets: Sequence[dict],
    walls: Sequence[dict],
    along: Polygon,
    *,
    doors: Sequence[dict] = (),
    wall_id: Optional[int] = None,
    toward: Optional[Sequence[float]] = None,
    count: Optional[int] = None,
    gap: Optional[float] = None,
    margin: float = DEFAULT_MARGIN,
    wall_clearance: float = 0.0,
    facing: str = "down",
    scale: float = 1.0,
) -> dict:
    """A row of objects backed flush against a wall, fronts into the room.

    assets: footprint specs (see `footprint`), used in turn, repeated to
    `count`; `count` omitted fills the stretch. walls / doors: as the
    spatial_snapshot reports them. along: [[x0,y0],[x1,y1]], the stretch of
    wall to line; the nearest parallel wall segment is used (`wall_id` picks
    one of a double wall). toward: a point on the room side, needed when the
    wall is not a loop. gap: fixed spacing, centered; omitted, the spare room
    is shared evenly. margin keeps corners and doorways clear; wall_clearance
    stands the row off the wall's face.

    Returns {placements, footprints, skipped, facing, wall, notes}: each
    placement is place_object's {asset, x, y, rotation, scale}, each footprint
    the quad it covers (for spatial.check_footprint), `wall` the segment used
    with its `free` stretches and `doors_kept_clear`.
    """
    _check_facing(facing)
    _check_count(count)
    if (gap is not None and gap < 0) or wall_clearance < 0:
        raise ValueError("gap and wall_clearance must be >= 0")
    fps = _footprints(assets, scale)
    segment, others = choose_wall_segment(walls, along, wall_id)
    normal = inward_normal(segment, toward)
    stretches, kept_clear = free_stretches(segment, along, doors, walls, margin=margin)
    order = sequence(fps, count) if count else list(fps)
    widths = [fp.across(facing) for fp in order]
    rotation = rotation_to_face(facing, normal)
    placements, footprints = [], []
    for index, s in pack_row(stretches, widths, gap=gap, fill=count is None):
        fp = order[index % len(order)]
        cx, cy = segment.at(s, normal, segment.half_width + wall_clearance + fp.depth(facing) / 2)
        placed = placement(fp, cx, cy, rotation)
        placements.append(placed)
        footprints.append(placement_footprint(fp, placed))
    skipped = count - len(placements) if count else 0
    notes = []
    if others:
        notes.append(f"walls {others} also run along this line (a double wall): wall {segment.wall_id} "
                     "was used; pass wall_id to choose")
    if not stretches:
        notes.append(f"no free stretch of wall {segment.wall_id} once its corners and doors are kept "
                     f"{margin:g} woxels clear")
    if skipped:
        notes.append(f"{skipped} of {count} did not fit along the wall")
    notes.extend(_orientation_notes(fps, facing))
    return {
        "placements": placements,
        "footprints": [_round_points(fp) for fp in footprints],
        "skipped": skipped,
        "facing": facing,
        "wall": {
            "id": segment.wall_id,
            "segment": [list(segment.a), list(segment.b)],
            "half_width": segment.half_width,
            "normal": [round(normal[0], 6) + 0.0, round(normal[1], 6) + 0.0],   # + 0.0 turns -0.0 into 0.0
            "free": [[round(s0, 1), round(s1, 1)] for s0, s1 in stretches],
            "doors_kept_clear": kept_clear,
        },
        "notes": notes,
    }


def _round_points(poly: Polygon) -> list[list[float]]:
    return [[round(x, 1), round(y, 1)] for x, y in poly]


# --------------------------------------------------------------------------
# Around a table, on a surface
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Box:
    """An oriented rectangle: a table's or surface's footprint."""
    cx: float
    cy: float
    half_w: float
    half_h: float
    rotation: float

    def corners(self) -> list[Point]:
        local = spatial.rect_polygon((-self.half_w, -self.half_h, 2 * self.half_w, 2 * self.half_h))
        return spatial.transform(local, (self.cx, self.cy), (1.0, 1.0), self.rotation)

    def to_world(self, lx: float, ly: float) -> Point:
        x, y = _rotate(lx, ly, self.rotation)
        return (self.cx + x, self.cy + y)


def target_box(target: dict) -> Box:
    """The oriented rectangle of a table or surface.

    From its footprint quad (`polygon`, as spatial_snapshot objects and
    check_object_placement report it), or from a center (`x`, `y` or
    `position`), a `size` / `footprint` [w, h] (or `width`, `height`) and a
    `rotation` in degrees.
    """
    quad = target.get("polygon")
    if quad:
        if len(quad) != 4:
            raise ValueError("a footprint quad has four corners")
        pts = [(float(p[0]), float(p[1])) for p in quad]
        (x0, y0), (x1, y1), (x2, y2) = pts[:3]
        w, h = math.hypot(x1 - x0, y1 - y0), math.hypot(x2 - x1, y2 - y1)
        if w <= 0 or h <= 0:
            raise ValueError("the target's footprint has no area")
        cx, cy = spatial.centroid(pts)
        return Box(cx, cy, w / 2, h / 2, _normalize(math.degrees(math.atan2(y1 - y0, x1 - x0))))
    if "x" in target and "y" in target:
        cx, cy = float(target["x"]), float(target["y"])
    elif target.get("position"):
        cx, cy = float(target["position"][0]), float(target["position"][1])
    else:
        raise ValueError("the target needs a footprint `polygon`, or a center (`x`, `y`) with a size")
    size = target.get("size") or target.get("footprint")
    if size:
        w, h = float(size[0]), float(size[1])
    elif "width" in target and "height" in target:
        w, h = float(target["width"]), float(target["height"])
    else:
        raise ValueError("the target needs a `size` [w, h] (or `width` and `height`) with its center")
    if w <= 0 or h <= 0:
        raise ValueError("the target's size must be > 0")
    return Box(cx, cy, w / 2, h / 2, float(target.get("rotation", 0.0)))


def table_shape(asset: str) -> str:
    """'round' for an asset named round or circular, else 'rect'. The footprint
    cannot tell a round table from a square one, so the name decides; a square
    table seats the same four either way."""
    name = asset.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return "round" if ("round" in name or "circ" in name) else "rect"


def _seats_per_side(count: int, sides: Sequence[float], capacity: Sequence[int]) -> list[int]:
    """Seats per side in proportion to its length, within what each side holds."""
    total = sum(sides)
    shares = [count * side / total for side in sides]
    seats = [min(int(share), cap) for share, cap in zip(shares, capacity)]
    order = sorted(range(len(sides)), key=lambda i: (-(shares[i] - int(shares[i])), -sides[i], i))
    while sum(seats) < count:
        progressed = False
        for i in order:
            if sum(seats) >= count:
                break
            if seats[i] < capacity[i]:
                seats[i] += 1
                progressed = True
        if not progressed:
            break
    return seats


def _describe_box(box: Box) -> dict:
    return {"center": [round(box.cx, 1), round(box.cy, 1)],
            "size": [round(2 * box.half_w, 1), round(2 * box.half_h, 1)],
            "rotation": round(box.rotation, 2)}


def arrange_around_target(
    assets: Sequence[dict],
    target: dict,
    *,
    count: int = 4,
    shape: str = "auto",
    clearance: float = -16.0,
    jitter: bool = True,
    facing: str = "down",
    scale: float = 1.0,
    seed: Optional[int] = None,
) -> dict:
    """A ring of `count` seats around a target's footprint, each facing it.

    assets: footprint specs, used in turn. target: see `target_box`; with
    shape 'auto' its `asset` name decides round or rect. A round table is
    ringed evenly (an oval too); a rectangular one seats by side length, as
    many per side as fit edge to edge, a short end still taking one.
    clearance: gap from the table's edge to the seat's; negative tucks the
    seat under (the default, -16). jitter adds a few degrees and woxels so
    the table does not read as a grid; the result's `seed` reproduces it.

    Returns {placements, footprints, skipped, shape, facing, seed, target, notes}.
    """
    _check_facing(facing)
    _check_count(count)
    if shape not in ("auto", "round", "rect"):
        raise ValueError("shape must be 'auto', 'round' or 'rect'")
    fps = _footprints(assets, scale)
    box = target_box(target)
    if shape == "auto":
        shape = table_shape(str(target.get("asset", "")))
    seed = _seed(seed)
    rng = random.Random(seed)
    order = sequence(fps, count)
    placements, footprints = [], []

    def seat(fp: Footprint, lx: float, ly: float, inward: Point) -> None:
        if jitter:
            lx += rng.uniform(-SEAT_SHIFT_JITTER, SEAT_SHIFT_JITTER)
            ly += rng.uniform(-SEAT_SHIFT_JITTER, SEAT_SHIFT_JITTER)
        wx, wy = box.to_world(lx, ly)
        rotation = rotation_to_face(facing, _rotate(inward[0], inward[1], box.rotation))
        if jitter:
            rotation += rng.uniform(-SEAT_TURN_JITTER, SEAT_TURN_JITTER)
        placed = placement(fp, wx, wy, rotation)
        placements.append(placed)
        footprints.append(placement_footprint(fp, placed))

    if shape == "round":
        for i, fp in enumerate(order):
            angle = math.radians(-90.0 + 360.0 * i / count)
            cos, sin = math.cos(angle), math.sin(angle)
            # The ellipse's radius in this direction, so an oval seats evenly too.
            radius = (box.half_w * box.half_h) / math.hypot(box.half_h * cos, box.half_w * sin)
            out = radius + clearance + fp.depth(facing) / 2
            seat(fp, cos * out, sin * out, (-cos, -sin))
        seated = count
    else:
        # Clockwise from the top, in the table's own frame: (length, outward normal).
        sides = [
            (2 * box.half_w, (0.0, -1.0)),
            (2 * box.half_h, (1.0, 0.0)),
            (2 * box.half_w, (0.0, 1.0)),
            (2 * box.half_h, (-1.0, 0.0)),
        ]
        widest = max(fp.across(facing) for fp in order)
        capacity = [max(int(length // widest), 1 if length >= END_SEAT_SHARE * widest else 0)
                    for length, _ in sides]
        seats = _seats_per_side(count, [length for length, _ in sides], capacity)
        seated = 0
        for (length, (nx, ny)), n in zip(sides, seats):
            tangent = (-ny, nx)
            half_normal = box.half_h if ny else box.half_w
            for slot in range(n):
                fp = order[seated]
                seated += 1
                s = -length / 2 + length * (slot + 0.5) / n
                out = half_normal + clearance + fp.depth(facing) / 2
                seat(fp, tangent[0] * s + nx * out, tangent[1] * s + ny * out, (-nx, -ny))
    skipped = count - seated
    notes = []
    if skipped:
        notes.append(f"{skipped} of {count} seats did not fit around the table")
    if clearance < 0 and placements:
        notes.append(f"seats are tucked {-clearance:g} woxels under the table's edge: place them with "
                     "allow_overlap=True")
    notes.extend(_orientation_notes(fps, facing))
    return {
        "placements": placements,
        "footprints": [_round_points(fp) for fp in footprints],
        "skipped": skipped,
        "shape": shape,
        "facing": facing,
        "seed": seed,
        "target": _describe_box(box),
        "notes": notes,
    }


def arrange_on_surface(
    assets: Sequence[dict],
    target: dict,
    *,
    count: int = 3,
    margin: float = SURFACE_MARGIN,
    turn: float = SURFACE_TURN,
    scale: float = 1.0,
    seed: Optional[int] = None,
) -> dict:
    """`count` items set on top of a target's footprint, inside its edges and
    not overlapping each other.

    Every corner of each item's footprint stays at least `margin` inside the
    surface's. Items take the surface's rotation plus up to `turn` degrees
    either way, less on each retry, so a tight surface still takes a straight
    item. The result's `seed` reproduces the scatter.

    Returns {placements, footprints, skipped, seed, target, notes}. The items
    go on a layer above the surface's (or on its layer, placed after it) so
    they draw on top; a `layer` on the target gets a note saying so.
    """
    _check_count(count)
    if margin < 0 or turn < 0:
        raise ValueError("margin and turn must be >= 0")
    fps = _footprints(assets, scale)
    box = target_box(target)
    inner_w, inner_h = box.half_w - margin, box.half_h - margin
    if inner_w <= 0 or inner_h <= 0:
        raise ValueError(f"the surface ({2 * box.half_w:g} x {2 * box.half_h:g} woxels) is too small for a "
                         f"{margin:g}-woxel margin")
    seed = _seed(seed)
    rng = random.Random(seed)
    placements, footprints = [], []
    taken: list[list[Point]] = []
    for fp in sequence(fps, count):
        for attempt in range(SURFACE_TRIES):
            reach = turn * (1.0 - attempt / (SURFACE_TRIES - 1))
            t = rng.uniform(-reach, reach)
            cos, sin = abs(math.cos(math.radians(t))), abs(math.sin(math.radians(t)))
            # Centers only where the turned item stays inside.
            room_x = inner_w - (fp.w * cos + fp.h * sin) / 2
            room_y = inner_h - (fp.w * sin + fp.h * cos) / 2
            if room_x < 0 or room_y < 0:
                continue
            lx, ly = rng.uniform(-room_x, room_x), rng.uniform(-room_y, room_y)
            quad = spatial.transform(spatial.rect_polygon((-fp.w / 2, -fp.h / 2, fp.w, fp.h)), (lx, ly), (1.0, 1.0), t)
            if any(spatial.overlap_area(quad, other) > spatial.MIN_OVERLAP for other in taken):
                continue
            taken.append(quad)
            wx, wy = box.to_world(lx, ly)
            placed = placement(fp, wx, wy, box.rotation + t)
            placements.append(placed)
            footprints.append(placement_footprint(fp, placed))
            break
    skipped = count - len(placements)
    notes = []
    if skipped:
        notes.append(f"{skipped} of {count} found no room on the surface")
    if target.get("layer") is not None:
        layer = int(target["layer"])
        notes.append(f"the surface is on layer {layer}: place these on a higher layer (or on {layer} after "
                     "it) so they draw on top, and with allow_overlap=True")
    return {
        "placements": placements,
        "footprints": [_round_points(fp) for fp in footprints],
        "skipped": skipped,
        "seed": seed,
        "target": _describe_box(box),
        "notes": notes,
    }


# --------------------------------------------------------------------------
# Shared walls for build_room
# --------------------------------------------------------------------------

def _wall_type(wall: dict) -> Optional[int]:
    kind = wall.get("type", wall.get("wall_type"))
    return None if kind is None else int(kind)


def _covered(a: Point, b: Point, segments: Sequence[WallSegment], tol: float) -> list[tuple[float, float, set[int]]]:
    """Merged (start, end, wall ids) distances along a->b that collinear wall
    segments cover. A span ending within `tol` of a corner ends at the corner."""
    length = math.dist(a, b)
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    spans: list[tuple[float, float, set[int]]] = []
    for seg in segments:
        # Both ends of the wall segment must lie on the edge's line.
        if any(abs((p[0] - a[0]) * uy - (p[1] - a[1]) * ux) > tol for p in (seg.a, seg.b)):
            continue
        tp = (seg.a[0] - a[0]) * ux + (seg.a[1] - a[1]) * uy
        tq = (seg.b[0] - a[0]) * ux + (seg.b[1] - a[1]) * uy
        lo, hi = max(min(tp, tq), 0.0), min(max(tp, tq), length)
        if hi - lo > tol:
            spans.append((lo, hi, {seg.wall_id}))
    merged: list[list] = []
    for lo, hi, ids in sorted(spans, key=lambda s: (s[0], s[1])):
        if merged and lo <= merged[-1][1] + tol:
            merged[-1][1] = max(merged[-1][1], hi)
            merged[-1][2] |= ids
        else:
            merged.append([lo, hi, set(ids)])
    return [(0.0 if lo <= tol else lo, length if hi >= length - tol else hi, ids) for lo, hi, ids in merged]


def shared_walls(
    boundary: Polygon,
    walls: Sequence[dict],
    *,
    wall_type: int = 0,
    wall_asset: str = "",
    tolerance: float = SHARED_WALL_TOLERANCE,
) -> dict:
    """Which parts of a new room's boundary existing walls already cover.

    boundary: the room's polygon (the wall centerline build_room would draw).
    walls: the level's walls, each {id, points, loop, type, asset?} as
    get_element reports them. Only a wall of the same kind is shared: its
    `type` must equal `wall_type` (so a cave wall never stands in for a stone
    room's), and when `wall_asset` is named, a wall reporting another asset
    is left alone. A wall with no `type` is not shared, and a note says so.
    A wall counts as covering an edge where it is collinear within `tolerance`
    woxels and overlaps it.

    Returns {runs, closed, enclosed, shared_wall_ids, shared_edges, notes}:
    `runs` are the open polylines that still need a wall (draw each with
    loop=False); with nothing shared `runs` is [boundary] and `closed` is
    True (draw it as one loop, as before); `enclosed` is True when every edge
    is already walled and `runs` is empty, so only a floor is needed.
    """
    poly = [(float(p[0]), float(p[1])) for p in boundary]
    if len(poly) < 3:
        raise ValueError("a room boundary needs at least three points")
    if tolerance <= 0:
        raise ValueError("tolerance must be > 0")
    notes: list[str] = []
    alike, untyped = [], []
    for wall in walls:
        kind = _wall_type(wall)
        if kind is None:
            untyped.append(wall.get("id"))
            continue
        if kind != int(wall_type):
            continue
        if wall_asset and wall.get("asset") and wall["asset"] != wall_asset:
            continue
        alike.append(wall)
    if untyped:
        notes.append(f"{len(untyped)} wall(s) report no type and were not considered for sharing "
                     "(get_element reports it)")
    segments = wall_segments(alike)

    def at(a: Point, b: Point, t: float, length: float) -> Point:
        if t <= 0.0:
            return a
        if t >= length:
            return b
        f = t / length
        return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)

    shared_ids: set[int] = set()
    shared_edges: list[list[Point]] = []
    # Uncovered pieces in boundary order: (start, end, starts at a corner, ends at a corner).
    pieces: list[tuple[Point, Point, bool, bool]] = []
    for a, b in spatial.edges(poly):
        length = math.dist(a, b)
        if length <= tolerance:
            continue
        t = 0.0
        for lo, hi, ids in _covered(a, b, segments, tolerance):
            if lo - t > tolerance:
                pieces.append((at(a, b, t, length), at(a, b, lo, length), t == 0.0, False))
            shared_edges.append([at(a, b, lo, length), at(a, b, hi, length)])
            shared_ids |= ids
            t = hi
        if length - t > tolerance:
            pieces.append((at(a, b, t, length), b, t == 0.0, True))
    if not shared_edges:
        return {"runs": [[list(p) for p in poly]], "closed": True, "enclosed": False,
                "shared_wall_ids": [], "shared_edges": [], "notes": notes}

    runs: list[list[Point]] = []
    prev_ends_at_corner = False
    for start, end, starts_at_corner, ends_at_corner in pieces:
        if prev_ends_at_corner and starts_at_corner and runs[-1][-1] == start:
            runs[-1].append(end)        # the run turns a corner of the room
        else:
            runs.append([start, end])
        prev_ends_at_corner = ends_at_corner
    # The last run may continue into the first through the boundary's first corner.
    if len(runs) > 1 and pieces[0][2] and pieces[-1][3] and runs[-1][-1] == runs[0][0]:
        runs[0] = runs.pop() + runs[0][1:]
    ids = sorted(shared_ids)
    if runs:
        notes.append(f"walls {ids} already cover part of the boundary: {len(runs)} open run(s) still need a wall")
    else:
        notes.append(f"walls {ids} already enclose this room: only a floor is needed")
    return {
        "runs": [[list(p) for p in run] for run in runs],
        "closed": False,
        "enclosed": not runs,
        "shared_wall_ids": ids,
        "shared_edges": [[list(a), list(b)] for a, b in shared_edges],
        "notes": notes,
    }
