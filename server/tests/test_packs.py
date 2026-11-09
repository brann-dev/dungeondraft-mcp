"""Offline tests for reading asset packs: no Dungeondraft and no real pack needed.

    .venv/bin/python -m unittest discover -s server/tests

Packs are built here as Godot 3 PCK files laid out the way Dungeondraft's
packer writes them, in a temporary folder.
"""

import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dungeondraft_mcp import packs

ART = b"\x89PNG not really"


def write_pack(path, pack_id, files, info=None, *, tags=None, trailing_comma=False, omit_id=False):
    """A Godot 3 PCK holding pack.json, optional tags, and `files` (pack-relative)."""
    prefix = f"res://packs/{pack_id}/"
    meta = {"name": f"Pack {pack_id}", "id": pack_id, "version": "1.0", "author": "Tester"}
    meta.update(info or {})
    if omit_id:
        del meta["id"]
    contents = {prefix + "pack.json": json.dumps(meta).encode()}
    if tags is not None:
        text = json.dumps({"tags": tags, "sets": {}})
        if trailing_comma:
            text = text[:-1] + ",}"  # {...,} the way pack tools write it
        contents[prefix + "data/default.dungeondraft_tags"] = text.encode()
    for relative, data in files.items():
        contents[prefix + relative] = data
    # Godot pads each path with NULs to a multiple of four bytes.
    padded = [n.encode() + b"\0" * ((4 - len(n.encode()) % 4) % 4) for n in contents]
    header_size = 20 + 64 + 4 + sum(4 + len(p) + 16 + 16 for p in padded)
    index = b""
    body = b""
    for name, data in zip(padded, contents.values()):
        index += struct.pack("<I", len(name)) + name
        index += struct.pack("<QQ", header_size + len(body), len(data)) + b"\0" * 16
        body += data
    header = struct.pack("<5I", packs.PCK_MAGIC, packs.PCK_VERSION, 3, 4, 2) + b"\0" * 64
    Path(path).write_bytes(header + struct.pack("<I", len(contents)) + index + body)
    return Path(path)


def furniture(folder, **kwargs):
    return write_pack(
        Path(folder) / "Furniture.dungeondraft_pack", "FURN0001",
        {
            "textures/objects/Bed - Oak.webp": ART,
            "textures/objects/Table - Round.webp": ART,
            "textures/objects/Garden/Arbour_A.webp": ART,
            "textures/objects/Garden/Rug_Red.webp": ART,
            "textures/walls/Stone.png": ART,
            "textures/walls/Stone_end.png": ART,
            "textures/walls/Stone_Pillar.png": ART,
            "textures/materials/Moss_tile.png": ART,
            "textures/materials/Moss_border.png": ART,
            "textures/roofs/Thatch/tiles.png": ART,
            "textures/roofs/Thatch/ridge.png": ART,
            "textures/roofs/Thatch/hip.png": ART,
            "textures/roofs/Thatch/edge.png": ART,
            "textures/caves/Rocky/floor.png": ART,
            "textures/caves/Rocky/wall.png": ART,
            "textures/patterns/normal/Planks.png": ART,
            "textures/patterns/colorable/Weave.png": ART,
            "textures/tilesets/simple/Cobble.png": ART,
            "textures/tilesets/smart_double/Stone.png": ART,
            "textures/objects/notes.txt": b"not an image",
            "thumbnails/0123abcd.png": ART,
            "preview.png": ART,
        },
        tags={"Outdoor Structures": ["textures/objects/Garden/Arbour_A.webp"],
              "Garden": ["res://packs/FURN0001/textures/objects/Garden/Arbour_A.webp",
                         "textures/objects/Garden/Rug_Red.webp"]},
        **kwargs,
    )


class WithFolder(unittest.TestCase):
    def setUp(self):
        packs.clear_cache()
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)

    def tearDown(self):
        self._folder.cleanup()
        packs.clear_cache()


class DataDir(unittest.TestCase):
    def test_per_platform(self):
        home = Path("/home/x")
        self.assertEqual(packs.data_dir(platform="linux", home=home, environ={}),
                         home / ".local" / "share" / "Dungeondraft")
        self.assertEqual(packs.data_dir(platform="linux", home=home, environ={"XDG_DATA_HOME": "/data"}),
                         Path("/data/Dungeondraft"))
        self.assertEqual(packs.data_dir(platform="darwin", home=home, environ={}),
                         home / "Library" / "Application Support" / "Dungeondraft")
        self.assertEqual(packs.data_dir(platform="win32", home=home, environ={}),
                         home / "AppData" / "Roaming" / "Dungeondraft")
        self.assertEqual(packs.data_dir(platform="win32", home=home, environ={"APPDATA": r"C:\Roaming"}),
                         Path(r"C:\Roaming") / "Dungeondraft")

    def test_the_environment_overrides_the_platform(self):
        self.assertEqual(packs.data_dir(platform="win32", environ={packs.DATA_DIR_ENV: "/elsewhere"}),
                         Path("/elsewhere"))
        self.assertEqual(packs.config_path(platform="linux", home=Path("/h"), environ={}),
                         Path("/h/.local/share/Dungeondraft/config.ini"))


# The shape config.ini has on a Linux install, including a multi-line
# dictionary (size_presets) that configparser would reject.
CONFIG = """[Files]

last_map_directory="/home/x/maps"
recently_opened_maps=[ "/home/x/maps/a.dungeondraft_map", "/home/x/maps/b.dungeondraft_map" ]

[Assets]

last_base_assets_directory="/home/x/Documents"
active_asset_packs=[ "8XXbciV2", "FA30DDXY" ]
custom_assets_directory="{assets}"
disable_default_assets=false

[Mods]

active_mods=[ "BrandonFlorian.MCPBridge" ]

[New]

size_presets={{
"a": 1
}}
"""


class Config(WithFolder):
    def write_config(self, assets="/opt/Dungeondraft Assets", text=None):
        config = self.folder / "config.ini"
        config.write_text(text if text is not None else CONFIG.format(assets=assets), encoding="utf-8")
        return config

    def test_reading_values(self):
        config = self.write_config()
        self.assertEqual(packs.config_value(config, "Assets", "disable_default_assets"), "false")
        self.assertEqual(packs.config_path_value(config, "Assets", "custom_assets_directory"),
                         Path("/opt/Dungeondraft Assets"))
        self.assertEqual(packs.config_path_value(config, "Assets", "last_base_assets_directory"),
                         Path("/home/x/Documents"))
        self.assertIsNone(packs.config_path_value(config, "Assets", "missing"))
        self.assertIsNone(packs.config_path_value(config, "Assets", "active_asset_packs"), "a list is not a path")
        self.assertEqual(packs.config_string_list(config, "Assets", "active_asset_packs"), ["8XXbciV2", "FA30DDXY"])
        self.assertEqual(packs.config_string_list(config, "Files", "recently_opened_maps"),
                         ["/home/x/maps/a.dungeondraft_map", "/home/x/maps/b.dungeondraft_map"])
        self.assertIsNone(packs.config_string_list(config, "Assets", "missing"))
        self.assertEqual(packs.active_pack_ids(config), ["8XXbciV2", "FA30DDXY"])
        self.assertIsNone(packs.config_value(self.folder / "absent.ini", "Assets", "x"))

    def test_windows_paths_are_unescaped(self):
        config = self.write_config(text='[Assets]\n\ncustom_assets_directory="C:\\\\Games\\\\DD Assets"\n')
        self.assertEqual(packs.godot_strings('"C:\\\\Games\\\\DD Assets"'), ["C:\\Games\\DD Assets"])
        self.assertEqual(packs.config_path_value(config, "Assets", "custom_assets_directory"),
                         Path("C:\\Games\\DD Assets"))

    def test_an_empty_list_and_a_fresh_install(self):
        config = self.write_config(text="[Assets]\n\nlast_base_assets_directory=\"/home/x/Documents\"\n"
                                        "active_asset_packs=[  ]\n")
        self.assertEqual(packs.active_pack_ids(config), [])
        # last_base_assets_directory is the packer's source folder, never a fallback.
        with self.assertRaisesRegex(packs.PackError, "no asset pack folder"):
            packs.assets_directory(config=config, environ={})

    def test_the_assets_folder_from_config_or_environment(self):
        config = self.write_config(assets=str(self.folder))
        self.assertEqual(packs.assets_directory(config=config, environ={}), self.folder)
        elsewhere = self.folder / "elsewhere"
        elsewhere.mkdir()
        self.assertEqual(packs.assets_directory(config=config, environ={packs.ASSETS_DIR_ENV: str(elsewhere)}),
                         elsewhere)
        gone = self.write_config(assets=str(self.folder / "gone"))
        with self.assertRaisesRegex(packs.PackError, "does not exist"):
            packs.assets_directory(config=gone, environ={})


class Index(WithFolder):
    def test_entries_offsets_and_nul_padding(self):
        file = write_pack(self.folder / "a.dungeondraft_pack", "ABCD1234", {"textures/objects/x.png": b"12345"})
        with file.open("rb") as handle:
            entries = packs.read_index(handle)
            self.assertEqual([e.path for e in entries],
                             ["res://packs/ABCD1234/pack.json", "res://packs/ABCD1234/textures/objects/x.png"])
            art = entries[1]
            handle.seek(art.offset)
            self.assertEqual(handle.read(art.size), b"12345")

    def test_what_is_not_a_pack(self):
        cases = {
            "not a pck": b"not a pack at all, honestly, not even close to one",
            "too short": b"GDPC",
            "wrong version": struct.pack("<5I", packs.PCK_MAGIC, 2, 4, 0, 0) + b"\0" * 68,
            "absurd count": struct.pack("<5I", packs.PCK_MAGIC, 1, 3, 4, 2) + b"\0" * 64 + struct.pack("<I", 2 ** 31),
            "long path": struct.pack("<5I", packs.PCK_MAGIC, 1, 3, 4, 2) + b"\0" * 64 + struct.pack("<II", 1, 2 ** 20),
            "truncated": struct.pack("<5I", packs.PCK_MAGIC, 1, 3, 4, 2) + b"\0" * 64 + struct.pack("<II", 1, 40) + b"x" * 10,
        }
        for name, data in cases.items():
            file = self.folder / "bad.dungeondraft_pack"
            file.write_bytes(data)
            with self.assertRaises(packs.PackReadError, msg=name):
                with file.open("rb") as handle:
                    packs.read_index(handle)


class ReadPack(WithFolder):
    def test_assets_by_category(self):
        pack = packs.read_pack(furniture(self.folder))
        self.assertEqual((pack.id, pack.name, pack.version, pack.author), ("FURN0001", "Pack FURN0001", "1.0", "Tester"))
        self.assertFalse(pack.restricted)
        objects = [a.path for a in pack.assets["Objects"]]
        self.assertEqual(objects, [
            "res://packs/FURN0001/textures/objects/Bed - Oak.webp",
            "res://packs/FURN0001/textures/objects/Table - Round.webp",
            "res://packs/FURN0001/textures/objects/Garden/Arbour_A.webp",
            "res://packs/FURN0001/textures/objects/Garden/Rug_Red.webp",
        ])
        # Dungeondraft's own filters: a wall's end cap and pillar, a material's
        # border, and all but one texture of a roof or cave set are parts.
        self.assertEqual([a.path for a in pack.assets["Walls"]], ["res://packs/FURN0001/textures/walls/Stone.png"])
        self.assertEqual([a.path for a in pack.assets["Materials"]],
                         ["res://packs/FURN0001/textures/materials/Moss_tile.png"])
        self.assertEqual([a.path for a in pack.assets["Roofs"]], ["res://packs/FURN0001/textures/roofs/Thatch/tiles.png"])
        self.assertEqual([a.path for a in pack.assets["Caves"]], ["res://packs/FURN0001/textures/caves/Rocky/floor.png"])
        self.assertEqual([a.path for a in pack.assets["Patterns"]], ["res://packs/FURN0001/textures/patterns/normal/Planks.png"])
        self.assertEqual([a.path for a in pack.assets["Patterns Colorable"]],
                         ["res://packs/FURN0001/textures/patterns/colorable/Weave.png"])
        self.assertEqual([a.path for a in pack.assets["Simple Tiles"]], ["res://packs/FURN0001/textures/tilesets/simple/Cobble.png"])
        self.assertEqual([a.path for a in pack.assets["Smart Tiles Double"]],
                         ["res://packs/FURN0001/textures/tilesets/smart_double/Stone.png"])
        # Thumbnails, the preview and a stray text file are not assets at all.
        self.assertEqual(set(pack.assets), {"Objects", "Walls", "Materials", "Roofs", "Caves", "Patterns",
                                            "Patterns Colorable", "Simple Tiles", "Smart Tiles Double"})

    def test_tags_attach_by_relative_or_absolute_path(self):
        pack = packs.read_pack(furniture(self.folder))
        by_path = {a.path.rsplit("/", 1)[-1]: a.tags for a in pack.assets["Objects"]}
        self.assertEqual(by_path["Arbour_A.webp"], ("Outdoor Structures", "Garden"))
        self.assertEqual(by_path["Rug_Red.webp"], ("Garden",))
        self.assertEqual(by_path["Bed - Oak.webp"], ())

    def test_tags_with_trailing_commas_still_load(self):
        pack = packs.read_pack(furniture(self.folder, trailing_comma=True))
        self.assertTrue(any(a.tags for a in pack.assets["Objects"]))

    def test_no_art_is_ever_read(self):
        read = []
        original = packs._read_text

        def recording(handle, entry, limit):
            text = original(handle, entry, limit)
            read.append((entry.path.rsplit("/", 1)[-1], text[:1]))
            return text

        with mock.patch.object(packs, "_read_text", recording):
            packs.read_pack(furniture(self.folder))
        self.assertEqual(read, [("pack.json", "{"), ("default.dungeondraft_tags", "{")])

    def test_a_pack_whose_author_opted_out_is_not_indexed(self):
        read = []
        original = packs._read_text
        with mock.patch.object(packs, "_read_text", lambda *args: read.append(args[1].path) or original(*args)):
            pack = packs.read_pack(furniture(self.folder, info={packs.OPT_OUT: False}))
        self.assertTrue(pack.restricted)
        self.assertEqual(pack.assets, {})
        self.assertEqual(len(read), 1, "pack.json, to learn the author's choice; not the tags")
        self.assertTrue(packs.read_pack(furniture(self.folder, info={packs.OPT_OUT: "false"})).restricted)

    def test_opting_in_or_saying_nothing_is_searched(self):
        self.assertFalse(packs.read_pack(furniture(self.folder, info={packs.OPT_OUT: True})).restricted)
        self.assertFalse(packs.read_pack(furniture(self.folder)).restricted)

    def test_the_id_defaults_to_the_folder_name(self):
        pack = packs.read_pack(write_pack(self.folder / "x.dungeondraft_pack", "FOLDER01", {}, omit_id=True))
        self.assertEqual(pack.id, "FOLDER01")

    def test_an_oversized_pack_json_is_refused(self):
        file = write_pack(self.folder / "x.dungeondraft_pack", "BIG00001", {})
        with mock.patch.object(packs, "MAX_INFO_BYTES", 10):
            with self.assertRaisesRegex(packs.PackReadError, "past the 10"):
                packs.read_pack(file)

    def test_describe_is_json_friendly(self):
        pack = packs.read_pack(furniture(self.folder, info={"keywords": "garden, furniture"}))
        described = packs.describe(pack)
        json.dumps(described)
        self.assertEqual(described["counts"]["Objects"], 4)
        self.assertEqual(described["assets"], 12)
        self.assertEqual(described["tags"], {"Garden": 2, "Outdoor Structures": 1})
        self.assertEqual(described["keywords"], "garden, furniture")
        self.assertTrue(described["searchable"])
        closed = packs.describe(packs.read_pack(furniture(self.folder, info={packs.OPT_OUT: False})))
        self.assertFalse(closed["searchable"])
        self.assertIn("opted out", closed["note"])


class Discover(WithFolder):
    def test_unreadable_files_are_reported_not_raised(self):
        (self.folder / "broken.dungeondraft_pack").write_bytes(b"not a pack at all, honestly")
        furniture(self.folder)
        found = packs.discover(self.folder)
        self.assertEqual(list(found["packs"]), ["FURN0001"])
        self.assertEqual(found["unreadable"][0]["file"], "broken.dungeondraft_pack")
        self.assertEqual(found["folder"], str(self.folder))

    def test_the_scan_is_recursive_like_dungeondrafts(self):
        nested = self.folder / "Forgotten Adventures"
        nested.mkdir()
        write_pack(nested / "FA.dungeondraft_pack", "FA000001", {})
        self.assertEqual(list(packs.discover(self.folder)["packs"]), ["FA000001"])

    def two_versions(self):
        write_pack(self.folder / "A_v1.dungeondraft_pack", "SAME", {}, {"version": "3.9"})
        write_pack(self.folder / "A_v2.dungeondraft_pack", "SAME", {}, {"version": "3.10"})

    def test_the_newest_of_two_files_with_one_id_is_used(self):
        self.two_versions()
        found = packs.discover(self.folder)
        self.assertEqual(found["packs"]["SAME"].version, "3.10", "numerically, not as text")
        self.assertEqual(found["duplicates"]["SAME"], {"used": "A_v2.dungeondraft_pack",
                                                       "also_found": ["A_v1.dungeondraft_pack"]})

    def test_the_version_dungeondraft_mounted_wins(self):
        self.two_versions()
        self.assertEqual(packs.discover(self.folder, {"SAME": "3.9"})["packs"]["SAME"].version, "3.9")

    def test_a_changed_pack_is_read_again(self):
        file = furniture(self.folder)
        first = packs.load_pack(file)
        self.assertIs(packs.load_pack(file), first)
        write_pack(file, "FURN0001", {"textures/objects/Chair.webp": ART})
        os.utime(file, ns=(1, 1))
        self.assertEqual([a.path for a in packs.load_pack(file).assets["Objects"]],
                         ["res://packs/FURN0001/textures/objects/Chair.webp"])

    def test_installed_reads_dungeondrafts_folder(self):
        furniture(self.folder)
        with mock.patch.dict(os.environ, {packs.ASSETS_DIR_ENV: str(self.folder)}):
            self.assertEqual(list(packs.installed()["packs"]), ["FURN0001"])


class Words(unittest.TestCase):
    def test_words_and_searchable_text(self):
        self.assertEqual(packs.words("Bed - Oak_01.webp"), ["bed", "oak", "01", "webp"])
        self.assertEqual(packs.searchable_text("res://packs/FA30DDXY/textures/objects/Garden/Arbour_A.webp"),
                         "objects garden arbour a webp")
        self.assertEqual(packs.searchable_text("res://textures/objects/rock.png", ("Outdoor", "Rocks")),
                         "objects rock png outdoor rocks")

    def test_equivalent_queries(self):
        self.assertEqual(packs.equivalent_queries("red rug"), ["red carpet"])
        self.assertEqual(packs.equivalent_queries("rug_red"), ["carpet_red"], "separators survive")
        self.assertEqual(packs.equivalent_queries("bookshelves"), ["bookcases"])
        self.assertEqual(packs.equivalent_queries("armour"), ["armor"])
        self.assertEqual(packs.equivalent_queries("round table"), [])
        self.assertEqual(packs.equivalent_queries("rug carpet"), ["carpet carpet", "rug rug"])


LIBRARY = [
    "res://textures/objects/furniture/tables/round_table_01.png",
    "res://textures/objects/furniture/tables/round_table_02.png",
    "res://textures/objects/furniture/tables/bar_table.png",
    "res://textures/objects/furniture/chairs/bar_chair.png",
    "res://textures/objects/furniture/storage/merchant_shelf_01.png",
    "res://textures/objects/supplies/barrels/barrel_01.png",
    "res://textures/objects/vegetation/shrubs/bush_flower_03.png",
    "res://textures/portals/door_01_decorated_variant.png",
    "res://textures/portals/door_01.png",
]


def library_pack(assets=LIBRARY):
    pack = packs.Pack(Path("library.dungeondraft_pack"), {"id": "LIB", "name": "Library"}, restricted=False)
    for path in assets:
        category = "Portals" if "/portals/" in path else "Objects"
        pack.assets.setdefault(category, []).append(packs.Asset(path))
    return pack


def paths(result, term):
    return [hit["path"] for hit in result["results"][term]["best"]]


class Search(unittest.TestCase):
    def test_words_in_any_order_and_every_word_required(self):
        result = packs.search([library_pack()], ["table round", "round barrel"])
        self.assertEqual(paths(result, "table round")[:2], LIBRARY[:2])
        self.assertNotIn(LIBRARY[3], paths(result, "table round"))
        self.assertEqual(result["results"]["round barrel"]["matched"], 0)
        self.assertTrue(any("nothing matches 'round barrel'" in n for n in result["notes"]))

    def test_directory_words_count(self):
        result = packs.search([library_pack()], ["furniture storage"])
        self.assertEqual(paths(result, "furniture storage"), [LIBRARY[4]])

    def test_the_closest_filename_ranks_first_and_ties_prefer_the_shorter_path(self):
        result = packs.search([library_pack()], ["bar table"])
        self.assertEqual(paths(result, "bar table")[0], LIBRARY[2])
        doors = packs.search([library_pack()], ["door 01"], "Portals")
        self.assertEqual(paths(doors, "door 01"), ["res://textures/portals/door_01.png",
                                                   "res://textures/portals/door_01_decorated_variant.png"])

    def test_matched_counts_everything_and_best_is_limited(self):
        result = packs.search([library_pack()], ["table"], limit=1)
        term = result["results"]["table"]
        self.assertEqual(term["matched"], 3)
        self.assertEqual(term["matched_by_pack"], {"LIB": 3})
        self.assertEqual(len(term["best"]), 1)
        self.assertEqual(term["best"][0]["pack"], "LIB")

    def test_synonyms_are_tried_only_when_a_term_draws_a_blank(self):
        pack = library_pack(["res://packs/P/textures/objects/carpet_red.png", "res://packs/P/textures/objects/rug_blue.png"])
        result = packs.search([pack], ["red rug", "rug"])
        red = result["results"]["red rug"]
        self.assertEqual(red["wording"], "red carpet")
        self.assertEqual([h["path"] for h in red["best"]], ["res://packs/P/textures/objects/carpet_red.png"])
        self.assertNotIn("wording", result["results"]["rug"], "a direct hit is never reworded")
        self.assertTrue(any("'red carpet' did" in n for n in result["notes"]))

    def test_it_does_not_pretend_to_understand_meaning(self):
        """"mug" finding a tankard would need a notion of what words mean."""
        result = packs.search([library_pack()], ["mug", "seating"])
        self.assertEqual([result["results"][t]["matched"] for t in ("mug", "seating")], [0, 0])

    def test_terms_are_deduplicated_and_validated(self):
        result = packs.search([library_pack()], ["Table", " table ", "TABLE"])
        self.assertEqual(list(result["results"]), ["table"])
        with self.assertRaisesRegex(packs.PackError, "at least one"):
            packs.search([library_pack()], ["", "  "])
        with self.assertRaisesRegex(packs.PackError, "unknown category"):
            packs.search([library_pack()], ["table"], "Furniture")
        with self.assertRaisesRegex(packs.PackError, "limit"):
            packs.search([library_pack()], ["table"], limit=0)

    def test_results_are_json(self):
        json.dumps(packs.search([library_pack()], ["table"]))


class SearchAcrossPacks(WithFolder):
    def setUp(self):
        super().setUp()
        furniture(self.folder)
        write_pack(self.folder / "Closed.dungeondraft_pack", "CLOSED01",
                   {"textures/objects/Bed - Secret.webp": ART}, {"name": "Closed Pack", packs.OPT_OUT: False})
        write_pack(self.folder / "Camp.dungeondraft_pack", "CAMP0001",
                   {"textures/objects/Bed - Roll.webp": ART, "textures/objects/Table - Camp.webp": ART})
        self.found = packs.discover(self.folder)

    def test_tags_match_and_each_pack_is_counted(self):
        result = packs.search(self.found["packs"], ["outdoor", "bed"], limit=5)
        # "outdoor" is only in the arbour's TAG, never its path.
        self.assertEqual(paths(result, "outdoor"), ["res://packs/FURN0001/textures/objects/Garden/Arbour_A.webp"])
        self.assertEqual(result["results"]["outdoor"]["best"][0]["tags"], ["Outdoor Structures", "Garden"])
        self.assertEqual(result["results"]["bed"]["matched_by_pack"], {"CAMP0001": 1, "FURN0001": 1})
        self.assertEqual(result["searched"], ["CAMP0001", "FURN0001"])

    def test_an_opted_out_pack_is_named_never_searched(self):
        result = packs.search(self.found["packs"], ["bed"])
        self.assertEqual(result["not_searched"], [{
            "id": "CLOSED01", "name": "Closed Pack",
            "reason": "its author opted out of third-party tools reading the pack"}])
        self.assertNotIn("CLOSED01", result["results"]["bed"]["matched_by_pack"])

    def test_the_search_can_be_limited_to_packs(self):
        result = packs.search(self.found["packs"], ["bed"], pack_ids=["CAMP0001"])
        self.assertEqual(result["results"]["bed"]["matched_by_pack"], {"CAMP0001": 1})
        self.assertEqual(result["not_searched"], [])
        with self.assertRaisesRegex(packs.PackError, "NOPE"):
            packs.search(self.found["packs"], ["bed"], pack_ids=["NOPE"])


if __name__ == "__main__":
    unittest.main()
