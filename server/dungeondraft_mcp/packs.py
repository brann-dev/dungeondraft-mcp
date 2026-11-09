"""Read the installed Dungeondraft asset packs: which exist and what is in them.

Dungeondraft scopes asset packs per map. The bridge's `list_assets` sees only
the packs the open map includes, so choosing a pack to include comes down to
its name, and "FA_Objects_A_v3.51" says nothing about whether it holds a
pergola. This reads the pack files themselves, with Dungeondraft closed or
open, and never through the bridge.

A `.dungeondraft_pack` is a Godot 3 PCK archive: a header (magic "GDPC", the
format version, three engine version ints, sixteen reserved ints), the file
count, then an index of every file (path length, NUL-padded path, absolute
offset, size, md5), then the files themselves, uncompressed. The index comes
first, so listing a multi-gigabyte pack reads a few megabytes. Only three
things are ever read past the index: `pack.json` (name, id, version, author),
`data/default.dungeondraft_tags` (tag -> asset paths) and nothing else. Never
any art.

Pack authors can opt out: `pack.json` may set
`allow_3rd_party_mapping_software_to_read: false`, and then nothing but
`pack.json` itself is read; the pack is listed by name as not searched.

Where the packs are comes from Dungeondraft's own config.ini, `[Assets]
custom_assets_directory`, the folder its Assets window points at and that it
scans recursively (Master.RefreshAssetPacks). The neighbouring
`last_base_assets_directory` is the asset *packer's* source folder, not a
place packs live, so it is not a fallback. config.ini is in Dungeondraft's
user data folder: ~/.local/share/Dungeondraft on Linux, %APPDATA%\\Dungeondraft
on Windows, ~/Library/Application Support/Dungeondraft on macOS. Both places
can be overridden with environment variables (DATA_DIR_ENV, ASSETS_DIR_ENV).

Which texture folder feeds which asset category, and which file of a roof or
cave set is the asset, follows Dungeondraft 1.2's Library and the core
Dungeondraft.pck. The categories are the ones the bridge's
`list_asset_categories` reports.
"""

from __future__ import annotations

import json
import os
import re
import struct
import sys
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping, Optional, Sequence, Union

PACK_SUFFIX = ".dungeondraft_pack"
PCK_MAGIC = 0x43504447  # "GDPC"
PCK_VERSION = 1         # Godot 3
# A real index path is under a few hundred bytes, and the largest pack seen
# (FA_Objects_A, 258,246 entries) is far below the entry cap. Past either the
# file is corrupt or foreign, not a pack worth reading further.
MAX_PATH_BYTES = 4096
MAX_INDEX_ENTRIES = 4_000_000
# The largest tags file read; FA_Objects_A's is 14.7 MB. pack.json is tiny.
MAX_TAGS_BYTES = 64 * 1024 * 1024
MAX_INFO_BYTES = 1024 * 1024
OPT_OUT = "allow_3rd_party_mapping_software_to_read"
DATA_DIR_ENV = "DUNGEONDRAFT_DATA_DIR"
ASSETS_DIR_ENV = "DUNGEONDRAFT_ASSETS_DIR"

# Folder under textures/ -> the asset category it fills. Two-level folders
# are matched before one-level ones.
CATEGORY_FOLDERS = {
    "objects": "Objects",
    "walls": "Walls",
    "paths": "Paths",
    "terrain": "Terrain",
    "lights": "Lights",
    "portals": "Portals",
    "roofs": "Roofs",
    "patterns/normal": "Patterns",
    "patterns/colorable": "Patterns Colorable",
    "caves": "Caves",
    "materials": "Materials",
    "tilesets/simple": "Simple Tiles",
    "tilesets/smart": "Smart Tiles",
    "tilesets/smart_double": "Smart Tiles Double",
}
CATEGORIES = tuple(dict.fromkeys(CATEGORY_FOLDERS.values()))
IMAGE_SUFFIXES = (".png", ".webp", ".jpg", ".jpeg")
# A roof set is a folder of edge/hip/ridge/tiles textures and a cave set a
# folder of floor/wall: Dungeondraft lists the set under one file (Master.cs
# MatchRoof / MatchFloor) and keeps the others as "parts". Wall end caps and
# pillars, and material borders, are parts too (its Library.Load filters).
SET_FILE = {"Roofs": "tiles", "Caves": "floor"}
PART_ENDINGS = {"Walls": ("end", "pillar")}
PART_CONTAINS = {"Materials": ("border",)}

# Words that name the SAME thing, so a blank under one is worth retrying
# under another. The bar is equivalence, not relatedness: a desk is not a
# table and a sconce is not a lantern, and swapping those turns an honest
# "nothing found" into a confident wrong asset. Spelling variants count.
EQUIVALENT_WORDS: tuple[tuple[str, ...], ...] = (
    ("rug", "carpet"),
    ("bookshelf", "bookcase"),
    ("bookshelves", "bookcases"),
    ("column", "pillar"),
    ("couch", "sofa"),
    ("wardrobe", "armoire"),
    ("pail", "bucket"),
    ("barrel", "cask"),
    ("coffin", "casket"),
    ("curtain", "drape"),
    ("candelabra", "candelabrum"),
    ("armor", "armour"),
    ("gray", "grey"),
    ("color", "colour"),
    ("jewelry", "jewellery"),
    ("center", "centre"),
    ("theater", "theatre"),
    ("harbor", "harbour"),
)
MAX_EQUIVALENT_QUERIES = 3
_EQUIVALENTS: dict[str, tuple[str, ...]] = {
    word: tuple(other for other in group if other != word)
    for group in EQUIVALENT_WORDS for word in group
}


class PackError(ValueError):
    """A pack folder, pack file or search request that cannot be used."""


class PackReadError(PackError):
    """A file that is not a readable pack."""


# --------------------------------------------------------------------------
# Dungeondraft's user data folder and config.ini
# --------------------------------------------------------------------------

def data_dir(*, platform: Optional[str] = None, home: Optional[Path] = None,
             environ: Optional[Mapping[str, str]] = None) -> Path:
    """Where Dungeondraft keeps config.ini (its Godot `user://` folder), or
    the folder DATA_DIR_ENV names. The keyword arguments exist for tests."""
    env = os.environ if environ is None else environ
    override = (env.get(DATA_DIR_ENV) or "").strip()
    if override:
        return Path(override)
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else Path(home)
    if platform.startswith("win"):
        return Path(env.get("APPDATA") or home / "AppData" / "Roaming") / "Dungeondraft"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Dungeondraft"
    return Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "Dungeondraft"


def config_path(**where) -> Path:
    """Dungeondraft's config.ini; `where` goes to data_dir."""
    return data_dir(**where) / "config.ini"


def config_value(config: Path, section: str, key: str) -> Optional[str]:
    """One raw value from Dungeondraft's config.ini, or None.

    The file is Godot's ConfigFile format: values are Godot literals, and some
    sections hold multi-line dictionaries that configparser rejects outright,
    so the one key is read by hand.
    """
    try:
        text = Path(config).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]") and "=" not in line:
            current = line[1:-1].strip()
            continue
        if current != section:
            continue
        match = re.match(rf"{re.escape(key)}\s*=\s*(.*)$", line)
        if match:
            return match.group(1).strip()
    return None


def godot_strings(literal: str) -> list[str]:
    """Every quoted string in a Godot literal, unescaped, in order."""
    return [re.sub(r"\\(.)", r"\1", value) for value in re.findall(r'"((?:[^"\\]|\\.)*)"', literal)]


def config_path_value(config: Path, section: str, key: str) -> Optional[Path]:
    """A folder Dungeondraft records as a quoted string, or None when unset."""
    literal = config_value(config, section, key)
    if literal is None or not re.fullmatch(r'"(?:[^"\\]|\\.)*"', literal):
        return None
    (value,) = godot_strings(literal)
    value = value.strip()
    return Path(value) if value else None


def config_string_list(config: Path, section: str, key: str) -> Optional[list[str]]:
    """A list Dungeondraft records as `[ "a", "b" ]`, or None when unset."""
    literal = config_value(config, section, key)
    if literal is None or not literal.startswith("["):
        return None
    return godot_strings(literal)


def assets_directory(*, config: Optional[Path] = None, environ: Optional[Mapping[str, str]] = None) -> Path:
    """The folder Dungeondraft loads asset packs from.

    ASSETS_DIR_ENV wins; otherwise `[Assets] custom_assets_directory` from
    `config` (Dungeondraft's own config.ini by default). Raises PackError when
    neither names an existing folder.
    """
    env = os.environ if environ is None else environ
    override = (env.get(ASSETS_DIR_ENV) or "").strip()
    folder = Path(override) if override else config_path_value(
        config if config is not None else config_path(environ=env), "Assets", "custom_assets_directory")
    if folder is None:
        raise PackError(
            "Dungeondraft has no asset pack folder set (Assets window > Browse), so there are no "
            f"packs to read. Set {ASSETS_DIR_ENV} if the packs live somewhere config.ini does not name.")
    if not folder.is_dir():
        raise PackError(f"the asset pack folder {folder} does not exist")
    return folder


def active_pack_ids(config: Optional[Path] = None) -> list[str]:
    """The pack ids ticked in Dungeondraft's Assets window (`active_asset_packs`):
    the packs a NEW map includes. An untracked or empty setting gives []."""
    return config_string_list(config if config is not None else config_path(), "Assets", "active_asset_packs") or []


# --------------------------------------------------------------------------
# Reading a pack
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class IndexEntry:
    path: str
    offset: int
    size: int


@dataclass(frozen=True)
class Asset:
    path: str                       # res://packs/<id>/textures/...
    tags: tuple[str, ...] = ()


@dataclass
class Pack:
    """One pack file: its pack.json and, unless it opted out, its assets by category."""

    file: Path
    info: dict
    restricted: bool
    assets: dict[str, list[Asset]] = field(default_factory=dict)
    _rows: dict[str, list[tuple[Asset, str, str]]] = field(default_factory=dict, repr=False, compare=False)

    @property
    def id(self) -> str:
        return str(self.info.get("id") or "")

    @property
    def name(self) -> str:
        return str(self.info.get("name") or "") or self.file.stem

    @property
    def version(self) -> str:
        return str(self.info.get("version") or "")

    @property
    def author(self) -> str:
        return str(self.info.get("author") or "")

    def rows(self, category: str) -> list[tuple[Asset, str, str]]:
        """(asset, stem words, searchable text) per asset, built on first use."""
        if category not in self._rows:
            self._rows[category] = [(a, " ".join(words(_stem(a.path))), searchable_text(a.path, a.tags))
                                    for a in self.assets.get(category, ())]
        return self._rows[category]


def _exact(handle: BinaryIO, count: int) -> bytes:
    data = handle.read(count)
    if len(data) != count:
        raise PackReadError("the file ends inside its index; not a complete pack")
    return data


def read_index(handle: BinaryIO) -> list[IndexEntry]:
    """Every file in a Godot 3 PCK, from its header and index."""
    magic, version, _major, _minor, _patch = struct.unpack("<5I", _exact(handle, 20))
    if magic != PCK_MAGIC:
        raise PackReadError("not a Godot PCK archive")
    if version != PCK_VERSION:
        raise PackReadError(f"PCK format version {version}; only version {PCK_VERSION} (Godot 3) is read")
    _exact(handle, 64)  # 16 reserved u32
    (count,) = struct.unpack("<I", _exact(handle, 4))
    if count > MAX_INDEX_ENTRIES:
        raise PackReadError(f"index claims {count} files; not a plausible pack")
    entries = []
    for _ in range(count):
        (length,) = struct.unpack("<I", _exact(handle, 4))
        if length > MAX_PATH_BYTES:
            raise PackReadError("an index entry's path is implausibly long")
        try:
            path = _exact(handle, length).rstrip(b"\0").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PackReadError("an index entry's path is not UTF-8") from exc
        offset, size = struct.unpack("<QQ", _exact(handle, 16))
        _exact(handle, 16)  # md5
        entries.append(IndexEntry(path, offset, size))
    return entries


def _read_text(handle: BinaryIO, entry: IndexEntry, limit: int) -> str:
    if entry.size > limit:
        raise PackReadError(f"{entry.path} is {entry.size} bytes, past the {limit} read here")
    handle.seek(entry.offset)
    try:
        return _exact(handle, entry.size).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PackReadError(f"{entry.path} is not UTF-8 text") from exc


def lenient_json(text: str) -> Any:
    """JSON as pack tools write it: trailing commas are common in tags files."""
    try:
        return json.loads(text)
    except ValueError:
        return json.loads(re.sub(r",(\s*[}\]])", r"\1", text))


def category_of(relative: str) -> Optional[str]:
    """The asset category of a pack-relative texture path, or None when the
    file is not an asset (a thumbnail, the preview, a roof's ridge, a wall's
    end cap, a material's border)."""
    if not relative.startswith("textures/") or not relative.lower().endswith(IMAGE_SUFFIXES):
        return None
    parts = relative.split("/")
    folders, filename = parts[1:-1], parts[-1]
    category = None
    for depth in (2, 1):
        if len(folders) >= depth:
            category = CATEGORY_FOLDERS.get("/".join(folders[:depth]))
            if category is not None:
                break
    if category is None:
        return None
    stem = filename.rsplit(".", 1)[0].lower()
    if category in SET_FILE and (stem != SET_FILE[category] or len(folders) < 2):
        return None
    if stem.endswith(PART_ENDINGS.get(category, ())) or any(p in stem for p in PART_CONTAINS.get(category, ())):
        return None
    return category


def _opted_out(value: Any) -> bool:
    return value is False or (isinstance(value, str) and value.strip().lower() == "false")


def _tag_index(document: Any, prefix: str) -> dict[str, list[str]]:
    """Pack-relative path -> tags, from a default.dungeondraft_tags document.
    Pack tools write paths relative to the pack root; Dungeondraft's own core
    file writes them as res:// paths, so both spellings are accepted."""
    tags: dict[str, list[str]] = {}
    raw = document.get("tags") if isinstance(document, dict) else None
    for tag, paths in (raw.items() if isinstance(raw, dict) else ()):
        for path in (paths if isinstance(paths, list) else ()):
            relative = str(path)
            if relative.startswith(prefix):
                relative = relative[len(prefix):]
            elif relative.startswith("res://"):
                relative = relative[len("res://"):]
            tags.setdefault(relative, []).append(str(tag))
    return tags


def read_pack(file: Path) -> Pack:
    """Read one pack's index, pack.json and, unless it opted out, its tags."""
    file = Path(file)
    with file.open("rb") as handle:
        entries = {e.path: e for e in read_index(handle)}
        manifests = [p for p in entries if re.fullmatch(r"res://packs/[^/]+/pack\.json", p)]
        if len(manifests) != 1:
            raise PackReadError("the pack has no single pack.json")
        pack_id = manifests[0].split("/")[3]
        prefix = f"res://packs/{pack_id}/"
        info = lenient_json(_read_text(handle, entries[manifests[0]], MAX_INFO_BYTES))
        if not isinstance(info, dict):
            raise PackReadError("pack.json is not an object")
        info.setdefault("id", pack_id)
        if _opted_out(info.get(OPT_OUT)):
            return Pack(file, info, restricted=True)
        tags: dict[str, list[str]] = {}
        tags_entry = entries.get(prefix + "data/default.dungeondraft_tags")
        if tags_entry is not None:
            try:
                tags = _tag_index(lenient_json(_read_text(handle, tags_entry, MAX_TAGS_BYTES)), prefix)
            except (ValueError, PackReadError):
                tags = {}  # unreadable tags cost the search its tag words, not the pack
    assets: dict[str, list[Asset]] = {}
    for path in entries:
        if not path.startswith(prefix):
            continue
        relative = path[len(prefix):]
        category = category_of(relative)
        if category is not None:
            assets.setdefault(category, []).append(Asset(path, tuple(tags.get(relative, ()))))
    return Pack(file, info, restricted=False, assets=assets)


_cache: dict[Path, tuple[tuple[int, int], Union[Pack, PackReadError]]] = {}


def load_pack(file: Path) -> Pack:
    """read_pack, remembered until the file's size or mtime changes."""
    resolved = Path(file).resolve()
    status = resolved.stat()
    key = (status.st_size, status.st_mtime_ns)
    cached = _cache.get(resolved)
    if cached is None or cached[0] != key:
        try:
            result: Union[Pack, PackReadError] = read_pack(resolved)
        except PackReadError as error:
            result = error
        except (OSError, ValueError, struct.error) as error:
            result = PackReadError(str(error))
        cached = (key, result)
        _cache[resolved] = cached
    if isinstance(cached[1], PackReadError):
        raise cached[1]
    return cached[1]


def clear_cache() -> None:
    _cache.clear()


def _version_key(pack: Pack) -> tuple:
    numbers = [int(part) for part in re.findall(r"\d+", pack.version)]
    return (numbers, pack.file.stat().st_mtime_ns)


def discover(folder: Path, mounted_versions: Optional[Mapping[str, str]] = None) -> dict:
    """Every pack file under `folder` (recursively, as Dungeondraft scans), one
    Pack per id.

    Two files can carry the same id (an old and a new version side by side).
    Dungeondraft mounts one of them; when `mounted_versions` (id -> version,
    from the running editor) names it, that one is used, else the newest.
    Returns {"folder", "packs": {id: Pack}, "duplicates", "unreadable"}.
    """
    folder = Path(folder)
    by_id: dict[str, list[Pack]] = {}
    unreadable = []
    for file in sorted(folder.rglob(f"*{PACK_SUFFIX}")):
        if not file.is_file():
            continue
        try:
            pack = load_pack(file)
        except (OSError, PackReadError) as error:
            unreadable.append({"file": str(file.relative_to(folder)), "reason": str(error)})
            continue
        by_id.setdefault(pack.id, []).append(pack)
    chosen: dict[str, Pack] = {}
    duplicates = {}
    for pack_id, packs in by_id.items():
        wanted = (mounted_versions or {}).get(pack_id)
        matching = [p for p in packs if wanted is not None and p.version == str(wanted)]
        chosen[pack_id] = (matching or sorted(packs, key=_version_key))[-1]
        if len(packs) > 1:
            duplicates[pack_id] = {"used": chosen[pack_id].file.name,
                                   "also_found": [p.file.name for p in packs if p is not chosen[pack_id]]}
    return {"folder": str(folder), "packs": chosen, "duplicates": duplicates, "unreadable": unreadable}


def installed(mounted_versions: Optional[Mapping[str, str]] = None, **where) -> dict:
    """discover() on Dungeondraft's own asset folder (see assets_directory)."""
    return discover(assets_directory(**where), mounted_versions)


def describe(pack: Pack, top: int = 30) -> dict:
    """A JSON-friendly account of one pack: who made it, whether it can be
    searched, how many assets of each category it has and its most used tags."""
    counts = {category: len(items) for category, items in pack.assets.items() if items}
    tags: dict[str, int] = {}
    for items in pack.assets.values():
        for asset in items:
            for tag in asset.tags:
                tags[tag] = tags.get(tag, 0) + 1
    out = {
        "id": pack.id, "name": pack.name, "version": pack.version, "author": pack.author,
        "file": pack.file.name, "searchable": not pack.restricted,
        "assets": sum(counts.values()), "counts": counts,
        "tags": dict(sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))[:top]),
    }
    if pack.info.get("keywords"):
        out["keywords"] = pack.info["keywords"]
    if pack.restricted:
        out["note"] = "its author opted out of third-party tools reading the pack; listed, not searched"
    return out


# --------------------------------------------------------------------------
# Searching
# --------------------------------------------------------------------------

def words(text: str) -> list[str]:
    """Lower-case alphanumeric words, split on everything else (asset names
    use "_", "-", spaces and CamelCase-free digits as separators)."""
    cleaned = "".join(c if c.isalnum() else " " for c in text.lower())
    return cleaned.split()


def _stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _relative(path: str) -> str:
    """The path below the pack's (or the core's) textures folder, so a pack's
    id and "textures" are not words every search matches."""
    found = re.match(r"res://(?:packs/[^/]+/)?textures/", path)
    return path[found.end():] if found else path


def searchable_text(path: str, tags: Sequence[str] = ()) -> str:
    """The words a search looks in: the path's below textures/, then the tags'."""
    text = " ".join(words(_relative(path)))
    return f"{text} {' '.join(words(' '.join(tags)))}" if tags else text


def equivalent_queries(query: str) -> list[str]:
    """The query reworded with true equivalents, one word swapped at a time:
    "red rug" gives ["red carpet"]. A plural is matched by its singular and
    replaced by it. A query with no equivalent gives []."""
    lowered = query.lower()
    out: list[str] = []
    for found in re.finditer(r"[a-z0-9]+", lowered):
        word = found.group()
        key = word if word in _EQUIVALENTS else word[:-1] if word.endswith("s") else ""
        for other in _EQUIVALENTS.get(key, ()):
            reworded = lowered[:found.start()] + other + lowered[found.end():]
            if reworded not in out:
                out.append(reworded)
    return out[:MAX_EQUIVALENT_QUERIES]


def score(query_words: Sequence[str], stem_words: str, haystack: str) -> Optional[float]:
    """How well an asset answers a query, or None when it does not.

    Every query word must appear somewhere in the haystack (path words and
    tags, any order). The score is how closely the filename resembles the
    query, so `round_table_01` outranks a table that only has "round" in a
    directory or a tag; a filename holding every word gets a bonus.
    """
    if not all(word in haystack for word in query_words):
        return None
    query = " ".join(query_words)
    value = SequenceMatcher(None, query, stem_words).ratio()
    if all(word in stem_words for word in query_words):
        value += 0.25
    return round(value, 4)


def _distinct_terms(terms: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for term in terms:
        cleaned = " ".join(str(term).split()).lower()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


def search(packs: Union[Mapping[str, Pack], Iterable[Pack]], terms: Sequence[str], category: str = "Objects", *,
           pack_ids: Optional[Sequence[str]] = None, limit: int = 20) -> dict:
    """Match each term against the searchable packs' asset paths and tags.

    A term is words in any order ("table round" finds round_table_01); a term
    nothing matches is retried under its equivalents (see EQUIVALENT_WORDS),
    and the result then says which `wording` matched. Per term: how many
    assets matched in each pack (all of them) and the best `limit` across
    packs, best first. Packs that opted out are listed under `not_searched`.

    pack_ids: search only these packs; an unknown id is an error.
    """
    pool = dict(packs) if isinstance(packs, Mapping) else {p.id: p for p in packs}
    if category not in CATEGORIES:
        raise PackError(f"unknown category {category!r}; one of {list(CATEGORIES)}")
    cleaned = _distinct_terms(terms)
    if not cleaned:
        raise PackError("give at least one search term")
    if limit < 1:
        raise PackError("limit must be at least 1")
    if pack_ids is not None:
        unknown = sorted(set(pack_ids) - set(pool))
        if unknown:
            raise PackError(f"no installed pack has id {unknown}; installed: {sorted(pool)}")
        pool = {pack_id: pool[pack_id] for pack_id in pack_ids}
    searched = {pack_id: pack for pack_id, pack in pool.items() if not pack.restricted}
    not_searched = [{"id": pack.id, "name": pack.name,
                     "reason": "its author opted out of third-party tools reading the pack"}
                    for pack in pool.values() if pack.restricted]

    results = {}
    notes = []
    for term in cleaned:
        found = None
        for wording in [term] + equivalent_queries(term):
            query_words = words(wording)
            scored = []
            counts: dict[str, int] = {}
            for pack_id, pack in searched.items():
                for asset, stem_words, haystack in pack.rows(category):
                    value = score(query_words, stem_words, haystack)
                    if value is None:
                        continue
                    counts[pack_id] = counts.get(pack_id, 0) + 1
                    scored.append((value, asset, pack_id))
            if scored:
                found = (wording, scored, counts)
                break
        if found is None:
            results[term] = {"matched": 0, "matched_by_pack": {}, "best": []}
            notes.append(f"nothing matches '{term}' in {category}; try a broader or different word")
            continue
        wording, scored, counts = found
        scored.sort(key=lambda item: (-item[0], len(item[1].path), item[1].path))
        entry: dict = {
            "matched": len(scored),
            "matched_by_pack": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
            "best": [{"path": asset.path, "pack": pack_id, "tags": list(asset.tags)}
                     for _value, asset, pack_id in scored[:limit]],
        }
        if wording != term:
            entry["wording"] = wording
            notes.append(f"'{term}' matched nothing; '{wording}' did")
        results[term] = entry
    return {"category": category, "searched": sorted(searched), "not_searched": not_searched,
            "results": results, "notes": notes}
