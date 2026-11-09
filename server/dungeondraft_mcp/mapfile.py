"""Read, compare and prepare `.dungeondraft_map` files without Dungeondraft.

A map file is JSON whose values hold Godot literals as strings: positions are
"Vector2( 6784, 6144 )", outlines "PoolVector2Array( x, y, x, y, ... )",
bitmaps "PoolByteArray( 0, 255, ... )", and colours are eight hex digits with
the alpha first ("ff3a2a14"). Textures are resource paths: a core asset is
"res://textures/objects/...", an asset-pack one "res://packs/<pack id>/textures/...".

Two jobs, both offline:

- `summarise` reports what a render cannot show: the packs a map includes and
  which of them are missing from an installed list, what each level holds, the
  most used textures and the pack each comes from, the terrain slots, and
  doors or windows whose `closed` flag contradicts their name. A Universal VTT
  export goes by that flag, so a door saved open exports as something that
  blocks nothing. `compare` puts two summaries side by side.
- `prepare_copy` gives a map more asset packs. Dungeondraft fixes a map's pack
  set when the map is created, and its documentation calls the header safe to
  read but dangerous to modify on a live map, so the manifest is changed in a
  NEW file on disk, which is then opened like any other map. Only the header's
  `asset_manifest` (and `uses_default_assets`, if asked) change: the rest of
  the file is carried over byte for byte, by splicing the text rather than
  re-serialising the JSON.

Nothing here reads a file other than the map named, and no existing file is
ever replaced.
"""

from __future__ import annotations

import json
import os
import re
import stat
import statistics
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

# Lists in a summary stop here; counts stay complete.
TOP = 25
# A map is hundreds of KB to tens of MB; a terrain splat alone is 64 bytes per
# tile. Past this the file is not a map worth reading into memory.
MAX_MAP_BYTES = 1024 * 1024 * 1024

PACK_PATH = re.compile(r"res://packs/([^/]+)/")
NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_WINDOW = re.compile(r"(?i)window")
_EMPTY_FRAME = re.compile(r"(?i)/window_frames/|[/_]door_frame")
_DOOR = re.compile(r"(?i)door")

# The element kinds a level is counted by, in the order they are reported.
KINDS = ("walls", "portals", "lights", "objects", "paths", "texts", "roofs",
         "patterns", "materials", "water")

# What a manifest entry carries, read off maps Dungeondraft 1.2 wrote: its
# AssetPack.Save writes all seven, but on load it reads back only name, id and
# version. The last three are copied when the pack.json has them.
ENTRY_FIELDS = ("name", "id", "version", "author")
OPTIONAL_FIELDS = ("keywords", "allow_3rd_party_mapping_software_to_read",
                   "custom_color_overrides")


class MapFileError(ValueError):
    """The file is not a Dungeondraft map this module can read or change."""


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def read_text(path: Path) -> str:
    """The map file's text, refusing anything past MAX_MAP_BYTES."""
    path = Path(path)
    try:
        size = path.stat().st_size
        if size > MAX_MAP_BYTES:
            raise MapFileError(f"{path.name} is {size} bytes, too large for a map file")
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MapFileError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except UnicodeDecodeError as exc:
        raise MapFileError(f"{path.name} is not a Dungeondraft map (not UTF-8 text)") from exc


def parse(text: str, name: str = "map") -> dict:
    """The parsed map, checked to have the `header` and `world` objects."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise MapFileError(f"{name} is not a Dungeondraft map (not valid JSON)") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("world"), dict):
        raise MapFileError(f"{name} has no 'world' object; is it a .dungeondraft_map?")
    if not isinstance(raw.get("header"), dict):
        raise MapFileError(f"{name} has no 'header' object; is it a .dungeondraft_map?")
    return raw


def load(path: Path) -> dict:
    """Read and parse a map file."""
    path = Path(path)
    return parse(read_text(path), path.name)


# --------------------------------------------------------------------------
# Godot literals
# --------------------------------------------------------------------------

def numbers(literal: Any) -> list[float]:
    """The numbers inside a Godot literal such as "Vector2( 1, 2 )"."""
    if isinstance(literal, (int, float)) and not isinstance(literal, bool):
        return [float(literal)]
    if not isinstance(literal, str) or "(" not in literal:
        return []
    return [float(n) for n in NUMBER.findall(literal.split("(", 1)[1])]


def pool_bytes(literal: Any) -> bytes:
    """The bytes of a "PoolByteArray( ... )" literal; empty for anything else."""
    if not isinstance(literal, str) or "(" not in literal:
        return b""
    return bytes(int(n) & 0xFF for n in re.findall(r"\d+", literal.split("(", 1)[1]))


def pack_of(texture: Any) -> Optional[str]:
    """The pack id a texture path points into, or None for a core asset."""
    found = PACK_PATH.search(texture) if isinstance(texture, str) else None
    return found.group(1) if found else None


def portal_role(texture: Any) -> str:
    """'door', 'window' or 'opening', read from a Portals asset path."""
    name = texture if isinstance(texture, str) else ""
    if not name or _EMPTY_FRAME.search(name):
        return "opening"
    if _WINDOW.search(name):
        return "window"
    if _DOOR.search(name):
        return "door"
    return "opening"


def vtt_closed(texture: Any) -> bool:
    """The `closed` flag a portal needs to behave as itself in a VTT export."""
    return portal_role(texture) == "door"


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------

def _dicts(value: Any) -> list[dict]:
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def _texture(element: dict) -> str:
    return str(element.get("texture") or "")


def _levels(world: dict) -> list[tuple[str, dict]]:
    """(index, level) pairs in index order; the file keys them "0", "1", ..."""
    levels = world.get("levels")
    if isinstance(levels, dict):
        pairs = [(str(k), v) for k, v in levels.items() if isinstance(v, dict)]
        return sorted(pairs, key=lambda kv: (not kv[0].lstrip("-").isdigit(),
                                             int(kv[0]) if kv[0].lstrip("-").isdigit() else 0, kv[0]))
    if isinstance(levels, list):
        return [(str(i), v) for i, v in enumerate(levels) if isinstance(v, dict)]
    return []


def _portals(level: dict) -> list[dict]:
    """Wall-mounted portals (saved inside each wall) and freestanding ones."""
    found: list[dict] = []
    for wall in _dicts(level.get("walls")):
        found.extend(_dicts(wall.get("portals")))
    found.extend(_dicts(level.get("portals")))
    return found


def _roofs(level: dict) -> list[dict]:
    roofs = level.get("roofs")
    return _dicts(roofs.get("roofs")) if isinstance(roofs, dict) else []


def _materials(level: dict) -> list[tuple[int, dict]]:
    """(layer, material mesh) pairs; the file keys them by layer."""
    materials = level.get("materials")
    rows: list[tuple[int, dict]] = []
    if isinstance(materials, dict):
        for layer, entries in materials.items():
            digits = str(layer).lstrip("-").isdigit()
            rows.extend((int(layer) if digits else 0, e) for e in _dicts(entries))
    return rows


def _water_bodies(water: Any) -> int:
    """Water is a Clipper polygon tree: the root's children are bodies, their
    children holes, and a hole's children bodies again (islands' lakes)."""
    tree = water.get("tree") if isinstance(water, dict) else None
    if not isinstance(tree, dict):
        return 0

    def count(node: dict, depth: int) -> int:
        own = 1 if depth % 2 == 1 and len(numbers(node.get("polygon"))) >= 6 and not node.get("is_open") else 0
        return own + sum(count(c, depth + 1) for c in _dicts(node.get("children")))

    return count(tree, 0)


def _layers(items: Sequence[dict]) -> list[int]:
    return sorted({int(i["layer"]) for i in items
                   if isinstance(i.get("layer"), (int, float)) and not isinstance(i.get("layer"), bool)})


def _scale(element: dict) -> float:
    found = numbers(element.get("scale"))
    return round(max(found), 3) if found else 1.0


def _texture_row(texture: str, count: int, **extra) -> dict:
    return {"texture": texture, "pack": pack_of(texture), "count": count, **extra}


def _objects(level: dict) -> list[dict]:
    """Objects grouped by texture, most used first, with their scale range."""
    grouped: dict[str, list[dict]] = {}
    for obj in _dicts(level.get("objects")):
        grouped.setdefault(_texture(obj), []).append(obj)
    rows = []
    for texture, items in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        scales = [_scale(o) for o in items]
        rows.append(_texture_row(
            texture, len(items),
            scale={"min": min(scales), "median": round(statistics.median(scales), 3), "max": max(scales)},
            layers=_layers(items)))
    return rows


def _paths(level: dict) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for path in _dicts(level.get("paths")):
        grouped.setdefault(_texture(path), []).append(path)
    return [
        _texture_row(texture, len(items),
                     widths=sorted({round(float(p.get("width") or 1), 3) for p in items}),
                     layers=_layers(items))
        for texture, items in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]


def _terrain(level: dict, width: int, height: int) -> Optional[dict]:
    """The terrain slots with each one's share of the painted weight.

    Weights are `splat` (slots 1-4) and `splat2` (slots 5-8): RGBA bytes at 4
    pixels per tile, one channel per slot. A splat of the wrong size is not
    read, so a truncated map gives shares of 0 rather than nonsense.
    """
    terrain = level.get("terrain")
    if not isinstance(terrain, dict) or str(terrain.get("enabled")).lower() == "false":
        return None
    sums = [0] * 8
    expected = width * 4 * height * 4 * 4
    for offset, key in ((0, "splat"), (4, "splat2")):
        data = pool_bytes(terrain.get(key))
        if data and len(data) == expected:
            for channel in range(4):
                sums[offset + channel] = sum(data[channel::4])
    total = sum(sums)
    slots = []
    for index in range(8):
        texture = terrain.get(f"texture_{index + 1}")
        if not texture and not sums[index]:
            continue
        slots.append({"slot": index + 1, "texture": texture, "pack": pack_of(texture),
                      "share": round(sums[index] / total, 3) if total else 0.0})
    return {"expand_slots": str(terrain.get("expand_slots")).lower() == "true", "slots": slots}


def _material_rows(level: dict) -> list[dict]:
    rows = []
    for layer, entry in _materials(level):
        bitmap = pool_bytes(entry.get("bitmap"))
        rows.append({"layer": layer, "texture": _texture(entry), "pack": pack_of(_texture(entry)),
                     "coverage": round(sum(1 for b in bitmap if b) / len(bitmap), 3) if bitmap else None})
    return rows


def _portal_report(level: dict) -> dict:
    """Portal counts by role, and the ones whose `closed` flag contradicts it."""
    portals = _portals(level)
    roles = Counter(portal_role(_texture(p)) for p in portals)
    reversed_ = []
    for p in portals:
        role = portal_role(_texture(p))
        if role != "opening" and isinstance(p.get("closed"), bool) and p["closed"] != vtt_closed(_texture(p)):
            reversed_.append({"id": p.get("node_id"), "texture": _texture(p), "closed": p["closed"],
                              "named": role, "exports_as": "door" if p["closed"] else "window"})
    return {"count": len(portals), "doors": roles["door"], "windows": roles["window"],
            "openings": roles["opening"], "vtt_reversed": reversed_}


def _lights(level: dict) -> dict:
    lights = _dicts(level.get("lights"))
    ranges = [float(x["range"]) for x in lights
              if isinstance(x.get("range"), (int, float)) and not isinstance(x.get("range"), bool)]
    return {"count": len(lights),
            "range": {"min": min(ranges), "max": max(ranges)} if ranges else None,
            "textures": dict(Counter(_texture(x) for x in lights).most_common(TOP))}


def _texture_use(level: dict, width: int, height: int) -> list[dict]:
    """Every texture the level uses, by kind, most used first."""
    counts: Counter = Counter()
    for kind in ("objects", "paths", "walls", "patterns", "lights"):
        for element in _dicts(level.get(kind)):
            counts[(_texture(element), kind)] += 1
    for portal in _portals(level):
        counts[(_texture(portal), "portals")] += 1
    for roof in _roofs(level):
        counts[(_texture(roof), "roofs")] += 1
    for _layer, material in _materials(level):
        counts[(_texture(material), "materials")] += 1
    terrain = _terrain(level, width, height)
    for slot in (terrain or {}).get("slots", []):
        if slot["texture"] and slot["share"]:
            counts[(str(slot["texture"]), "terrain")] += 1
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [_texture_row(texture, count, kind=kind) for (texture, kind), count in rows if texture]


def _pack_use(world: dict) -> Counter:
    """How many texture references in the world point into each pack."""
    found: Counter = Counter()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and node.startswith("res://packs/"):
            pack = pack_of(node)
            if pack:
                found[pack] += 1

    walk(world)
    return found


# A plain local absolute path: on Windows with its drive letter and no second
# separator, elsewhere one leading slash. A map can come from anyone, and on
# Windows merely checking whether a UNC path exists connects to its host and
# offers the user's credentials, so any other spelling is reported unchecked.
_LOCAL_PATH = re.compile(r"[A-Za-z]:[\\/](?![\\/])[^\x00]*" if os.name == "nt" else r"/(?!/)[^\\\x00]*")


def _local_path(path: str) -> bool:
    return _LOCAL_PATH.fullmatch(path) is not None


def _trace_image(header: dict) -> Optional[dict]:
    editor = header.get("editor_state")
    trace = editor.get("trace_image") if isinstance(editor, dict) else None
    if not isinstance(trace, dict):
        return None
    image = trace.get("image")
    if not isinstance(image, str) or not image:
        return None
    return {"path": image, "exists": Path(image).exists() if _local_path(image) else None}


def _created(header: dict) -> Optional[str]:
    date = header.get("creation_date")
    if not isinstance(date, dict):
        return None
    try:
        return "%04d-%02d-%02d %02d:%02d" % tuple(int(date[k]) for k in ("year", "month", "day", "hour", "minute"))
    except (KeyError, TypeError, ValueError):
        return None


def manifest(raw: dict) -> list[dict]:
    """The header's asset_manifest entries (the packs the map includes)."""
    header = raw.get("header")
    return _dicts(header.get("asset_manifest")) if isinstance(header, dict) else []


def missing_packs(entries: Sequence[dict], installed: Iterable[str]) -> list[dict]:
    """The manifest entries whose pack id is not among the installed ids."""
    have = {str(i) for i in installed}
    return [{"id": e.get("id"), "name": e.get("name"), "version": e.get("version")}
            for e in entries if str(e.get("id")) not in have]


def summarise(raw: dict, *, installed: Optional[Iterable[str]] = None, path: Optional[Path] = None,
              top: Optional[int] = TOP) -> dict:
    """The summary of a parsed map.

    installed: the ids of the asset packs installed here (see packs.discover),
      or None when they are unknown; each pack's `installed` is then None and
      none is reported missing.
    top: lists stop at this many entries (None for all); counts are complete.
    """
    def cut(items: list) -> list:
        return items if top is None else items[:top]

    header: dict = raw["header"] if isinstance(raw.get("header"), dict) else {}
    world: dict = raw["world"]
    width, height = int(world.get("width") or 0), int(world.get("height") or 0)
    entries = manifest(raw)
    have = None if installed is None else {str(i) for i in installed}
    use = _pack_use(world)
    included = {str(e.get("id")) for e in entries}
    packs = [{"id": e.get("id"), "name": e.get("name"), "version": e.get("version"),
              "installed": None if have is None else str(e.get("id")) in have,
              "textures_using_it": use.get(str(e.get("id")), 0)}
             for e in entries]
    missing = [] if have is None else missing_packs(entries, have)
    not_included = {pid: n for pid, n in sorted(use.items()) if pid not in included}
    trace = _trace_image(header)

    levels = []
    for index, level in _levels(world):
        objects = _objects(level)
        portals = _portal_report(level)
        environment = level.get("environment")
        levels.append({
            "index": index,
            "label": level.get("label"),
            "ambient_light": environment.get("ambient_light") if isinstance(environment, dict) else None,
            "counts": {
                "walls": len(_dicts(level.get("walls"))),
                "portals": portals["count"],
                "lights": len(_dicts(level.get("lights"))),
                "objects": len(_dicts(level.get("objects"))),
                "paths": len(_dicts(level.get("paths"))),
                "texts": len(_dicts(level.get("texts"))),
                "roofs": len(_roofs(level)),
                "patterns": len(_dicts(level.get("patterns"))),
                "materials": len(_materials(level)),
                "water": _water_bodies(level.get("water")),
            },
            "textures": cut(_texture_use(level, width, height)),
            "objects": cut(objects),
            "object_kinds": len(objects),
            "paths": cut(_paths(level)),
            "terrain": _terrain(level, width, height),
            "materials": cut(_material_rows(level)),
            "portals": portals,
            "lights": _lights(level),
            "texts": cut([str(t.get("text", "")) for t in _dicts(level.get("texts"))]),
        })

    notes = []
    if missing:
        names = ", ".join(f"{p['name']} v{p['version']} ({p['id']})" for p in missing)
        lost = sum(use.get(str(p["id"]), 0) for p in missing)
        notes.append(f"{len(missing)} pack(s) this map includes are not installed: {names}; "
                     f"{lost} texture(s) from them will be missing when it opens")
    elif have is None and entries:
        notes.append("the installed packs were not checked, so whether the map's packs are present is unknown")
    if not_included:
        notes.append(f"{sum(not_included.values())} texture(s) point into pack(s) the map does not include "
                     f"({', '.join(not_included)}); Dungeondraft drops those elements on load")
    reversed_total = sum(len(level["portals"]["vtt_reversed"]) for level in levels)
    if reversed_total:
        notes.append(f"{reversed_total} door(s) or window(s) have a `closed` flag contrary to their name; "
                     "a Universal VTT export treats each as the other (an open door blocks nothing, "
                     "a closed window blocks like a door)")
    if trace and trace["exists"] is None:
        notes.append(f"the trace image is not a plain local path, so it was not checked: {trace['path']}")
    elif trace and not trace["exists"]:
        notes.append(f"the trace image is missing: {trace['path']}")

    return {
        "file": str(path) if path else None,
        "built_with": header.get("creation_build"),
        "created": _created(header),
        "size_tiles": [width, height],
        "uses_default_assets": header.get("uses_default_assets"),
        "packs": packs,
        "packs_missing": missing,
        "packs_used_but_not_included": not_included,
        "trace_image": trace,
        "level_count": len(levels),
        "levels": levels,
        "notes": notes,
    }


def compare(first: dict, second: dict) -> dict:
    """What changed from `first` to `second`, from two summaries.

    Levels are matched by index. Per level: the count of each kind that
    changed (second minus first), textures only in one, objects whose median
    scale changed, terrain shares that moved by a percent or more. Texture
    differences are exact only for uncapped summaries (summarise(top=None)),
    which is what `inspect` compares; the lists here still stop at TOP.
    """
    def names(summary: dict) -> set:
        return {str(p.get("name") or p.get("id")) for p in summary["packs"]}

    def shares(level: dict) -> dict:
        out: dict = {}
        for slot in (level["terrain"] or {}).get("slots", []):
            if slot["texture"]:
                out[slot["texture"]] = out.get(slot["texture"], 0.0) + slot["share"]
        return out

    by_index = {level["index"]: level for level in second["levels"]}
    levels = []
    notes = []
    for level in first["levels"]:
        other = by_index.pop(level["index"], None)
        where = f"level {level['index']} ({level['label']})"
        if other is None:
            levels.append({"index": level["index"], "label": level["label"], "only_in": "first"})
            notes.append(f"{where} is only in the first map")
            continue
        counts = {kind: other["counts"].get(kind, 0) - count
                  for kind, count in level["counts"].items() if other["counts"].get(kind, 0) != count}
        mine = {o["texture"]: o for o in level["objects"]}
        theirs = {o["texture"]: o for o in other["objects"]}
        textures_a = {(t["texture"], t["kind"]) for t in level["textures"]}
        textures_b = {(t["texture"], t["kind"]) for t in other["textures"]}
        shares_a, shares_b = shares(level), shares(other)
        levels.append({
            "index": level["index"],
            "label": level["label"],
            "label_second": other["label"],
            "count_changes": counts,
            "textures_only_in_first": sorted(t for t, _k in textures_a - textures_b)[:TOP],
            "textures_only_in_second": sorted(t for t, _k in textures_b - textures_a)[:TOP],
            "scale_changes": [
                {"texture": t, "first": mine[t]["scale"]["median"], "second": theirs[t]["scale"]["median"]}
                for t in sorted(set(mine) & set(theirs))
                if mine[t]["scale"]["median"] != theirs[t]["scale"]["median"]][:TOP],
            "terrain_share_changes": {
                t: round(shares_b.get(t, 0.0) - shares_a.get(t, 0.0), 3)
                for t in sorted(set(shares_a) | set(shares_b))
                if abs(shares_b.get(t, 0.0) - shares_a.get(t, 0.0)) >= 0.01},
        })
        for kind, delta in counts.items():
            notes.append(f"{where}: {kind} {level['counts'][kind]} -> {other['counts'].get(kind, 0)}")
    for level in by_index.values():
        levels.append({"index": level["index"], "label": level["label"], "only_in": "second"})
        notes.append(f"level {level['index']} ({level['label']}) is only in the second map")
    if first["size_tiles"] != second["size_tiles"]:
        notes.append(f"size {first['size_tiles']} -> {second['size_tiles']} tiles")
    only_first, only_second = sorted(names(first) - names(second)), sorted(names(second) - names(first))
    if only_first or only_second:
        notes.append(f"packs only in the first: {only_first}; only in the second: {only_second}")
    return {
        "size_tiles": {"first": first["size_tiles"], "second": second["size_tiles"]},
        "packs_only_in_first": only_first,
        "packs_only_in_second": only_second,
        "levels": levels,
        "notes": notes,
    }


def inspect(path: Path, *, installed: Optional[Iterable[str]] = None,
            compare_to: Optional[Path] = None) -> dict:
    """Summarise a map file and, with `compare_to`, add a `comparison` to it.
    The comparison is made on uncapped summaries, so it misses nothing that
    the TOP cut on the returned lists leaves out."""
    path = Path(path)
    installed = None if installed is None else list(installed)
    raw = load(path)
    summary = summarise(raw, installed=installed, path=path)
    if compare_to:
        other = Path(compare_to)
        summary["comparison"] = compare(summarise(raw, installed=installed, path=path, top=None),
                                        summarise(load(other), installed=installed, path=other, top=None))
    return summary


# --------------------------------------------------------------------------
# Giving a map more asset packs
# --------------------------------------------------------------------------

def manifest_entry(pack: dict) -> dict:
    """One asset_manifest entry from a pack's own pack.json fields."""
    entry = {field: pack.get(field, "") for field in ENTRY_FIELDS}
    for field in OPTIONAL_FIELDS:
        if field in pack:
            entry[field] = pack[field]
    return entry


def build_manifest(packs: Sequence[dict], wanted: Optional[Sequence[str]] = None) -> list[dict]:
    """Manifest entries for the `wanted` pack ids (every pack when None), in
    the order given, each id once: a duplicate is how a map ends up listing
    one pack under two versions."""
    chosen: list[dict] = []
    seen: set[str] = set()
    for pack in packs:
        pack_id = str(pack.get("id") or "")
        if not pack_id or pack_id in seen or (wanted is not None and pack_id not in wanted):
            continue
        seen.add(pack_id)
        chosen.append(manifest_entry(pack))
    return chosen


def unknown_ids(packs: Sequence[dict], wanted: Sequence[str]) -> list[str]:
    """The wanted ids no pack carries."""
    have = {str(pack.get("id") or "") for pack in packs}
    return [pack_id for pack_id in wanted if pack_id not in have]


def merge_manifest(existing: Sequence[dict], adding: Sequence[dict]) -> list[dict]:
    """What the map already includes, then the new packs; an id already present
    keeps the map's own entry (its version is the one the map was built with)."""
    merged = [dict(e) for e in existing if isinstance(e, dict)]
    have = {str(e.get("id")) for e in merged}
    for entry in adding:
        if str(entry.get("id")) not in have:
            have.add(str(entry.get("id")))
            merged.append(dict(entry))
    return merged


_decoder = json.JSONDecoder()
_WHITESPACE = " \t\n\r"


def _skip_space(text: str, i: int) -> int:
    while i < len(text) and text[i] in _WHITESPACE:
        i += 1
    return i


def _member_span(text: str, start: int, key: str) -> Optional[tuple[int, int]]:
    """[start, end) of the value of `key` in the JSON object at text[start],
    or None when the object has no such member. Keys and values are read
    with the stdlib decoder, so the scan is exactly as strict as json.loads."""
    i = _skip_space(text, start)
    if i >= len(text) or text[i] != "{":
        raise MapFileError("expected a JSON object")
    i = _skip_space(text, i + 1)
    if text[i] == "}":
        return None
    while True:
        name, i = _decoder.raw_decode(text, i)
        i = _skip_space(text, i)
        if text[i] != ":":
            raise MapFileError("malformed JSON object")
        i = _skip_space(text, i + 1)
        _value, end = _decoder.raw_decode(text, i)
        if name == key:
            return i, end
        i = _skip_space(text, end)
        if text[i] == ",":
            i = _skip_space(text, i + 1)
            continue
        if text[i] == "}":
            return None
        raise MapFileError("malformed JSON object")


def _render(value: Any, text: str, at: int) -> str:
    """`value` as JSON in the file's own layout: Dungeondraft writes one member
    per line, tab-indented, so nested lines take the key's indent plus one unit."""
    line_start = text.rfind("\n", 0, at) + 1
    key_indent = text[line_start:at]
    key_indent = key_indent[:len(key_indent) - len(key_indent.lstrip(" \t"))]
    unit_match = re.match(r"\{[ \t]*\r?\n([ \t]+)", text)
    unit = unit_match.group(1) if unit_match else ""
    if not unit:
        return json.dumps(value, ensure_ascii=False)
    rendered = json.dumps(value, indent=unit, ensure_ascii=False)
    return rendered.replace("\n", "\n" + key_indent)


def prepared_text(text: str, manifest_entries: Sequence[dict], *,
                  uses_default_assets: Optional[bool] = None, name: str = "map") -> tuple[str, dict]:
    """The map text with `manifest_entries` merged into its header's
    asset_manifest, and the report of what changed. Every other byte is kept."""
    data = parse(text, name)
    header_span = _member_span(text, 0, "header")
    if header_span is None:
        raise MapFileError(f"{name} has no header object")
    manifest_span = _member_span(text, header_span[0], "asset_manifest")
    if manifest_span is None:
        raise MapFileError(f"{name}'s header has no asset_manifest; is it a Dungeondraft map?")
    existing = manifest(data)
    final = merge_manifest(existing, manifest_entries)
    edits = [(manifest_span, _render(final, text, manifest_span[0]))]
    if uses_default_assets is not None:
        flag_span = _member_span(text, header_span[0], "uses_default_assets")
        if flag_span is None:
            raise MapFileError(f"{name}'s header has no uses_default_assets flag")
        edits.append((flag_span, "true" if uses_default_assets else "false"))
    out = text
    for (start, end), replacement in sorted(edits, reverse=True):
        out = out[:start] + replacement + out[end:]

    check = parse(out, name)
    expected_header = dict(data["header"], asset_manifest=final)
    if uses_default_assets is not None:
        expected_header["uses_default_assets"] = uses_default_assets
    if check["header"] != expected_header or check["world"] != data["world"]:
        raise MapFileError("the prepared map did not read back as intended; nothing was written")
    kept = [str(e.get("id")) for e in existing]
    return out, {
        "packs_before": len(existing),
        "packs_after": len(final),
        "pack_ids": [str(e.get("id")) for e in final],
        "kept_from_source": kept,
        "added": [str(e.get("id")) for e in final if str(e.get("id")) not in set(kept)],
        "uses_default_assets": check["header"].get("uses_default_assets"),
    }


def _publish_new_file(destination: Path, payload: bytes, *, like: Path) -> None:
    """Create `destination` holding `payload`, atomically, never replacing a file.

    Save folders can be shared, so anyone who can write there can plant names
    in advance. A predictable temporary written by path would follow a symlink
    planted at that name and truncate whatever it points at, so the temporary
    comes from `mkstemp` (an unpredictable name opened O_CREAT|O_EXCL, which
    refuses an existing entry, symlinks included) and is written through that
    descriptor only. Publishing must not clobber either: `os.replace` would
    overwrite a destination that appeared after the caller looked, so a hard
    link (which fails if the name exists) is used on POSIX and `os.rename`
    (which fails likewise there) on Windows. The copy takes the source map's
    permission bits, so a map shared with other users stays readable by them.
    """
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.", suffix=".preparing")
    try:
        with os.fdopen(fd, "wb") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), stat.S_IMODE(like.stat().st_mode))
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            if os.name == "nt":
                os.rename(temporary, destination)
            else:
                os.link(temporary, destination)
        except FileExistsError as exc:
            raise MapFileError(f"{destination} already exists; refusing to replace it") from exc
        except OSError:
            # No hard links here (FAT, exFAT, some network and FUSE mounts):
            # create the destination exclusively and write it directly.
            try:
                fd2 = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                              stat.S_IMODE(like.stat().st_mode))
            except FileExistsError as exc:
                raise MapFileError(f"{destination} already exists; refusing to replace it") from exc
            with os.fdopen(fd2, "wb") as out:
                out.write(payload)
                out.flush()
                os.fsync(out.fileno())
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def prepare_copy(source: Path, destination: Path, manifest_entries: Sequence[dict], *,
                 uses_default_assets: Optional[bool] = None) -> dict:
    """Write a copy of `source` at `destination` whose header also includes
    `manifest_entries` (see build_manifest). The destination must not exist;
    the source is never touched. Returns what changed, with `source` and `path`."""
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise MapFileError(f"refusing to write the prepared map onto its own source ({source.name}); "
                           "this prepares a COPY, so choose a different filename")
    if destination.exists() or destination.is_symlink():
        raise MapFileError(f"{destination} already exists; refusing to replace it")
    text = read_text(source)
    out, report = prepared_text(text, manifest_entries, uses_default_assets=uses_default_assets, name=source.name)
    _publish_new_file(destination, out.encode("utf-8"), like=source)
    return {"source": str(source), "path": str(destination), **report}
