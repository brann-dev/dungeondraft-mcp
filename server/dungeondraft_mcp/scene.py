"""Two things a render shows but cannot explain: bare ground, and light with no cause.

Both are invisible to element counts. Terrain has no elements at all, so a
building standing on the unpainted base of a new map reports as a complete,
correct map everywhere else; and a light is an element like any other whether
or not there is a candle, hearth or lantern under it. A viewer reads both
instantly as wrong without being able to say why, and the usual fix was
several rounds of screenshot and opinion.

Ground: the mod samples the level on a grid and reports, per point, the
terrain splat weights and whether a floor covers the point (a pattern or floor
shape, or dug cave floor). A point is bare when nothing covers it and no
weight sits in a painted slot. Slot 0 is the base: on a new map it is the
default texture with weight 1 everywhere, which is exactly the unpainted void
this check exists for, so it only counts as painted when `slots` shows a
chosen texture in it. Points under a wall are skipped when the snapshot's
walls are given, since nothing can be seen there.

Light: an emitter is an object whose asset name carries a word from a small
vocabulary (candle, torch, hearth, forge, ...) or that lives in a light-sources
folder. Every light wants an emitter within `emitter_reach`; every emitter wants
a light. Names are a heuristic, so each finding names the asset it credited or
missed, to be judged rather than obeyed.

Coordinates are woxels (256 per tile). Pure functions over plain data; nothing
here talks to Dungeondraft.
"""

from __future__ import annotations

import math
import re
from typing import Optional, Sequence

from .spatial import bounds as polygon_bounds, edges, point_segment_distance

WOXELS_PER_TILE = 256

# How close an emitter must be to account for a light. One tile: a light is
# meant to sit on its source.
DEFAULT_EMITTER_REACH = 256.0

# A light this big (in Dungeondraft's `range`, where 1.0 fades out about two
# tiles from the centre) is a mood or a moon, not a candle's glow, and is not
# expected to have an emitter under it.
AMBIENT_RANGE = 4.0

# A sample is painted when at least this much of its weight is in a painted
# slot; a blended edge between a chosen material and the base is ground.
PAINTED_EPSILON = 0.02

# The texture a new map's base slot holds, by file stem. Weight in a slot that
# holds one of these is unpainted ground.
NEW_MAP_BASE = frozenset({"terrain_limestone"})

# Asset-name words that mean the object gives off light; matched as a prefix
# of each word in the file name (candle -> candles, candlestick; fire ->
# fireplace, firepit).
EMITTER_WORDS = (
    "candle", "candelabr", "torch", "lantern", "lamp", "brazier", "sconce", "chandelier",
    "fire", "campfire", "bonfire", "flame", "hearth", "ember", "coals", "pyre", "beacon",
    "forge", "furnace", "oven", "stove", "kiln", "lava", "glow", "crystal", "orb", "portal",
)
# Words that make a name a part or an accessory rather than the source itself:
# firewood shares `fire` with a fire, forge_lever and the forge's moulds share `forge`.
NOT_EMITTERS = frozenset({"firewood", "mold", "mould", "lever", "tool", "tools", "unlit"})
# Folder names (lower case, separators dropped) that mark everything in them as a source.
EMITTER_FOLDERS = frozenset({"lightsources", "lightsource", "lights", "lighting"})


# --------------------------------------------------------------------------
# Shared
# --------------------------------------------------------------------------

def _xy(element: dict) -> tuple[float, float]:
    """An element's position: `position: [x, y]` as list_elements reports it, or `x` / `y` keys."""
    position = element.get("position")
    if position is not None:
        return float(position[0]), float(position[1])
    if element.get("x") is not None and element.get("y") is not None:
        return float(element["x"]), float(element["y"])
    raise ValueError(f"element {element.get('id')} has no position")


def _asset_name(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _stem(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


# --------------------------------------------------------------------------
# Ground
# --------------------------------------------------------------------------

def is_new_map_base(texture: Optional[str]) -> bool:
    """True for an empty slot or one holding the texture a new map starts with."""
    if not texture:
        return True
    return _stem(_asset_name(str(texture))).lower() in NEW_MAP_BASE


def _pitch(values: Sequence[float]) -> float:
    """The spacing of a sample grid along one axis (the smallest gap between
    distinct coordinates); one tile when there is only one coordinate."""
    distinct = sorted(set(values))
    gaps = [b - a for a, b in zip(distinct, distinct[1:]) if b - a > 1e-6]
    return min(gaps) if gaps else float(WOXELS_PER_TILE)


def _under_wall(x: float, y: float, walls: Sequence[dict]) -> bool:
    for wall in walls:
        points = wall.get("points") or []
        if len(points) < 2:
            continue
        half = float(wall.get("half_width", 32.0))
        x0, y0, x1, y1 = polygon_bounds(points)
        if x < x0 - half or x > x1 + half or y < y0 - half or y > y1 + half:
            continue
        if any(point_segment_distance((x, y), a, b) <= half for a, b in edges(points, bool(wall.get("loop")))):
            return True
    return False


def _clusters(points: list[tuple[float, float]], pitch_x: float, pitch_y: float) -> list[dict]:
    """Group bare samples that are grid neighbours (8-connected) into patches."""
    cells = {(round(x / pitch_x), round(y / pitch_y)): (x, y) for x, y in points}
    seen: set[tuple[int, int]] = set()
    out: list[dict] = []
    for start in cells:
        if start in seen:
            continue
        seen.add(start)
        stack, members = [start], []
        while stack:
            cx, cy = stack.pop()
            members.append(cells[(cx, cy)])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nxt = (cx + dx, cy + dy)
                    if nxt in cells and nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
        xs = [p[0] for p in members]
        ys = [p[1] for p in members]
        out.append({
            "samples": len(members),
            "area_tiles": round(len(members) * pitch_x * pitch_y / WOXELS_PER_TILE ** 2, 2),
            "bounds": [round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys))],
            "center": [round(sum(xs) / len(xs)), round(sum(ys) / len(ys))],
        })
    out.sort(key=lambda c: -c["samples"])
    return out


def bare_ground(
    samples: Sequence[dict],
    *,
    slots: Optional[Sequence[Optional[str]]] = None,
    walls: Optional[Sequence[dict]] = None,
    limit: int = 12,
) -> dict:
    """Open ground with no terrain painted and no floor over it.

    samples: the mod's terrain samples, each {x, y, weights: [slot0, slot1, ...]
      (splat weights 0..1), covered: bool (a pattern or floor shape or cave
      floor covers the point)}, on a regular grid.
    slots: the texture path (or None) each terrain slot holds. Slot 0 counts as
      painted only when it holds a texture other than the new-map default;
      without `slots` it is taken as the unpainted base and a note says so.
    walls: snapshot walls ({points, loop, half_width}); samples under one are
      skipped and counted as `under_walls`.

    Returns {ok, sampled, covered, under_walls, bare, bare_share (of all
    sampled points), clusters[{samples, area_tiles, bounds, center}] (largest
    first, at most `limit`), notes}. A cluster is a patch of neighbouring bare
    samples; its bounds span the samples themselves, each of which stands for
    a pitch-sized square around it. `ok` is true when nothing is bare.
    """
    notes: list[str] = []
    if slots is None:
        base_painted = False
        notes.append("slot 0 taken as the unpainted base: no `slots` given")
    else:
        base_painted = len(slots) > 0 and not is_new_map_base(slots[0])

    sampled = covered = under_walls = 0
    bare: list[tuple[float, float]] = []
    xs: list[float] = []
    ys: list[float] = []
    for sample in samples:
        x, y = _xy(sample)
        xs.append(x)
        ys.append(y)
        if walls and _under_wall(x, y, walls):
            under_walls += 1
            continue
        sampled += 1
        if sample.get("covered"):
            covered += 1
            continue
        weights = [float(w) for w in sample.get("weights") or []]
        painted = sum(weights[1:]) + (weights[0] if base_painted and weights else 0.0)
        if painted <= PAINTED_EPSILON:
            bare.append((x, y))

    # The grid pitch comes from every sample, not just the bare ones: sparse
    # bare points would otherwise read as a dense grid and merge into one patch.
    clusters = _clusters(bare, _pitch(xs), _pitch(ys)) if bare else []
    share = len(bare) / sampled if sampled else 0.0
    open_ground = sampled - covered
    if not samples:
        notes.append("no samples")
    elif bare:
        biggest = clusters[0]
        notes.append(f"{share:.0%} of the sampled points ({len(bare) / open_ground:.0%} of the uncovered ground) "
                     f"is bare, in {len(clusters)} patch{'es' if len(clusters) != 1 else ''}; the largest is about "
                     f"{biggest['area_tiles']:g} tiles around ({biggest['center'][0]}, {biggest['center'][1]})")
        if not base_painted:
            notes.append("bare means the base slot's texture with no paint over it; if that texture is "
                         "the ground you want, paint nothing and ignore this")
    return {
        "ok": not bare,
        "sampled": sampled,
        "covered": covered,
        "under_walls": under_walls,
        "bare": len(bare),
        "bare_share": round(share, 3),
        "clusters": clusters[:limit],
        "notes": notes,
    }


# --------------------------------------------------------------------------
# Light
# --------------------------------------------------------------------------

def is_emitter(asset: str) -> bool:
    """Whether an object's asset path names something that could give off light."""
    path = (asset or "").replace("\\", "/")
    words = re.findall(r"[a-z]+", _stem(path.rsplit("/", 1)[-1]).lower())
    if NOT_EMITTERS.intersection(words):
        return False
    folders = {re.sub(r"[^a-z]", "", part.lower()) for part in path.split("/")[:-1]}
    if folders & EMITTER_FOLDERS:
        return True
    return any(word.startswith(stem) for word in words for stem in EMITTER_WORDS)


def light_causes(
    lights: Sequence[dict],
    objects: Sequence[dict],
    *,
    emitter_reach: float = DEFAULT_EMITTER_REACH,
    ambient_range: float = AMBIENT_RANGE,
) -> dict:
    """Match each light to the emitter object nearest it, and each emitter to a light.

    lights / objects: the `elements` of list_elements('lights') and
      list_elements('objects'): {id, position: [x, y], range, ...} and
      {id, position: [x, y], asset, ...}.
    emitter_reach: how far (woxels) an emitter may be from a light it explains.
    ambient_range: a light with `range` at or above this is a mood light, listed
      under `ambient` and never flagged.

    Returns {ok, lights, emitters, explained[{id, position, by, asset,
    distance}], unexplained[{id, position, range}], ambient[...],
    unlit_emitters[{id, asset, position}], notes}. `ok` is true when no light
    is unexplained; unlit emitters are advice (an unlit lantern as decor is
    fine), like the overlaps in validate_layout.
    """
    if emitter_reach <= 0:
        raise ValueError("emitter_reach must be > 0")
    emitters = [(obj, _xy(obj)) for obj in objects if is_emitter(str(obj.get("asset", "")))]

    explained: list[dict] = []
    unexplained: list[dict] = []
    ambient: list[dict] = []
    lit: set = set()
    for light in lights:
        lx, ly = _xy(light)
        entry = {"id": light.get("id"), "position": [lx, ly]}
        best = None
        for obj, (ox, oy) in emitters:
            distance = math.hypot(ox - lx, oy - ly)
            if distance <= emitter_reach:
                lit.add(id(obj))
                if best is None or distance < best[0]:
                    best = (distance, obj)
        rng = float(light.get("range", 1.0))
        if best is not None:
            explained.append({**entry, "by": best[1].get("id"),
                              "asset": _asset_name(str(best[1].get("asset", ""))), "distance": round(best[0])})
        elif rng >= ambient_range:
            ambient.append({**entry, "range": rng})
        else:
            unexplained.append({**entry, "range": rng})

    unlit = [{"id": obj.get("id"), "asset": _asset_name(str(obj.get("asset", ""))), "position": [ox, oy]}
             for obj, (ox, oy) in emitters if id(obj) not in lit]

    notes = ["emitters are matched on asset name, so judge each finding: an oddly named source reads "
             "as unexplained, and a lantern placed as decor reads as an explanation"]
    if unexplained:
        notes.append(f"{len(unexplained)} light{'s' if len(unexplained) != 1 else ''} with nothing within "
                     f"{emitter_reach:g} woxels that could be emitting")
    if unlit:
        notes.append(f"{len(unlit)} emitter{'s' if len(unlit) != 1 else ''} with no light nearby "
                     "(add_light on it, or leave it if it is meant to be out)")
    return {
        "ok": not unexplained,
        "lights": len(lights),
        "emitters": len(emitters),
        "explained": explained,
        "unexplained": unexplained,
        "ambient": ambient,
        "unlit_emitters": unlit,
        "notes": notes,
    }
