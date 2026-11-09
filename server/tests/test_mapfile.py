"""Offline tests for reading and preparing map files: no Dungeondraft needed.

    .venv/bin/python -m unittest discover -s server/tests

The maps are built here in the shape Dungeondraft 1.2 saves (tab-indented
JSON holding Godot literals), so no real map has to be checked in.
"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from dungeondraft_mcp import mapfile

PACK = "res://packs/AbC123/textures"


def splat(width, height, rgba):
    """A splat literal: RGBA bytes at 4 pixels per tile, every pixel alike."""
    return "PoolByteArray( " + ", ".join(map(str, list(rgba) * (width * 4 * height * 4))) + " )"


def a_map(**level_overrides):
    level = {
        "label": "Ground",
        "environment": {"baked_lighting": True, "ambient_light": "ff808080"},
        "walls": [{
            "points": "PoolVector2Array( 0, 0, 512, 0 )",
            "texture": "res://textures/walls/stone.png", "loop": False, "node_id": "3",
            "portals": [
                {"texture": "res://textures/portals/door_01.png", "closed": False, "node_id": "4"},
                {"texture": "res://textures/portals/window_02.png", "closed": False, "node_id": "5"},
            ],
        }],
        "portals": [{"texture": "res://textures/portals/window_01.png", "closed": True, "node_id": "6"}],
        "objects": [
            {"texture": f"{PACK}/objects/tree.png", "scale": "Vector2( 2, 2 )", "layer": 300},
            {"texture": f"{PACK}/objects/tree.png", "scale": "Vector2( 2.2, 2.2 )", "layer": 300},
            {"texture": "res://textures/objects/rock.png", "scale": "Vector2( 1, 1 )", "layer": 100},
            {"texture": "res://packs/Gone99/textures/objects/log.png", "scale": "Vector2( 1, 1 )"},
        ],
        "paths": [
            {"texture": f"{PACK}/paths/cliff.png", "width": 256, "layer": 200},
            {"texture": "res://textures/paths/lip.png", "width": 308, "layer": 100},
        ],
        "lights": [{"range": 9, "texture": "res://textures/lights/soft.png"}],
        "terrain": {
            "enabled": True, "expand_slots": False,
            "texture_1": "res://textures/terrain/terrain_snow.png",
            "texture_2": "res://textures/terrain/terrain_gravel.png",
            "splat": splat(2, 1, (192, 64, 0, 0)),
        },
        "materials": {"-400": [{"texture": "res://textures/materials/ice_tile.png",
                                "bitmap": "PoolByteArray( 1, 0, 0, 1 )"}]},
        # A Clipper tree: the root, one body, a hole in it, and an island's pond in the hole.
        "water": {"disable_border": False, "tree": {
            "polygon": "PoolVector2Array(  )", "deep_color": "ff000000", "children": [
                {"polygon": "PoolVector2Array( 0, 0, 10, 0, 10, 10 )", "deep_color": "ff000000", "children": [
                    {"polygon": "PoolVector2Array( 1, 1, 2, 1, 2, 2 )", "deep_color": "ff000000", "children": [
                        {"polygon": "PoolVector2Array( 1, 1, 2, 1, 1, 2 )", "deep_color": "ff000000", "children": []},
                    ]},
                ]},
            ]}},
        "roofs": {"shade": True, "roofs": [{"texture": "res://textures/roofs/thatched/tiles.png"}]},
        "patterns": [{"texture": "res://textures/tilesets/simple/tileset_wood.png", "layer": -100}],
        "texts": [{"text": "The Inn"}],
    }
    level.update(level_overrides)
    return {
        "header": {
            "creation_build": "1.2.0.1 opulent kirin",
            "creation_date": {"year": 2026, "month": 6, "day": 20, "hour": 22, "minute": 53},
            "uses_default_assets": True,
            "asset_manifest": [
                {"name": "Forest Pack", "id": "AbC123", "version": "1", "author": "x"},
                {"name": "Missing Pack", "id": "Miss42", "version": "2", "author": "y"},
            ],
            "editor_state": {"trace_image": None},
        },
        "world": {"width": 2, "height": 1, "levels": {"0": level}},
    }


def godot_json(data):
    """Tab-indented, the way Dungeondraft writes a map."""
    return json.dumps(data, indent="\t", ensure_ascii=False)


class Literals(unittest.TestCase):
    def test_numbers_inside_godot_literals(self):
        self.assertEqual(mapfile.numbers("Vector2( 6784, 6144 )"), [6784.0, 6144.0])
        self.assertEqual(mapfile.numbers("Vector2( 1.4, -2.5e-3 )"), [1.4, -0.0025])
        self.assertEqual(mapfile.numbers(3), [3.0])
        self.assertEqual(mapfile.numbers("null"), [])
        self.assertEqual(mapfile.numbers(True), [])

    def test_pool_bytes(self):
        self.assertEqual(mapfile.pool_bytes("PoolByteArray( 0, 255, 256 )"), b"\x00\xff\x00")
        self.assertEqual(mapfile.pool_bytes(None), b"")

    def test_pack_of_a_texture_path(self):
        self.assertEqual(mapfile.pack_of("res://packs/FA30DDXY/textures/objects/a.webp"), "FA30DDXY")
        self.assertIsNone(mapfile.pack_of("res://textures/objects/rock.png"))
        self.assertIsNone(mapfile.pack_of(None))

    def test_portal_roles(self):
        self.assertEqual(mapfile.portal_role("res://textures/portals/door_01.png"), "door")
        self.assertEqual(mapfile.portal_role("res://textures/portals/window_02.png"), "window")
        self.assertEqual(mapfile.portal_role("res://textures/portals/door_frame_01.png"), "opening")
        self.assertEqual(mapfile.portal_role("res://packs/X/textures/portals/window_frames/a.png"), "opening")
        self.assertEqual(mapfile.portal_role("res://textures/portals/arch.png"), "opening")
        self.assertTrue(mapfile.vtt_closed("res://textures/portals/door_01.png"))
        self.assertFalse(mapfile.vtt_closed("res://textures/portals/window_02.png"))


class Loading(unittest.TestCase):
    def test_a_map_loads(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "a.dungeondraft_map"
            target.write_text(godot_json(a_map()), encoding="utf-8")
            self.assertEqual(mapfile.load(target)["world"]["width"], 2)

    def test_not_a_map_is_a_map_file_error(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "notes.dungeondraft_map"
            target.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(mapfile.MapFileError, "not valid JSON"):
                mapfile.load(target)
            target.write_text(json.dumps({"header": {}}), encoding="utf-8")
            with self.assertRaisesRegex(mapfile.MapFileError, "no 'world'"):
                mapfile.load(target)
            target.write_text(json.dumps({"world": {}}), encoding="utf-8")
            with self.assertRaisesRegex(mapfile.MapFileError, "no 'header'"):
                mapfile.load(target)
            with self.assertRaisesRegex(mapfile.MapFileError, "cannot read"):
                mapfile.load(Path(folder) / "absent.dungeondraft_map")


class Summary(unittest.TestCase):
    def test_header_size_and_packs(self):
        s = mapfile.summarise(a_map(), installed=["AbC123"])
        self.assertEqual(s["built_with"], "1.2.0.1 opulent kirin")
        self.assertEqual(s["created"], "2026-06-20 22:53")
        self.assertEqual(s["size_tiles"], [2, 1])
        self.assertTrue(s["uses_default_assets"])
        by_name = {p["name"]: p for p in s["packs"]}
        self.assertTrue(by_name["Forest Pack"]["installed"])
        self.assertEqual(by_name["Forest Pack"]["textures_using_it"], 3)
        self.assertFalse(by_name["Missing Pack"]["installed"])
        self.assertEqual(s["packs_missing"], [{"id": "Miss42", "name": "Missing Pack", "version": "2"}])
        # A texture from a pack the header does not include is dropped on load.
        self.assertEqual(s["packs_used_but_not_included"], {"Gone99": 1})
        text = " ".join(s["notes"])
        self.assertIn("Missing Pack", text)
        self.assertIn("Gone99", text)

    def test_unknown_installed_packs_report_nothing_missing(self):
        s = mapfile.summarise(a_map())
        self.assertEqual([p["installed"] for p in s["packs"]], [None, None])
        self.assertEqual(s["packs_missing"], [])
        self.assertTrue(any("not checked" in n for n in s["notes"]))

    def test_counts_per_kind(self):
        (level,) = mapfile.summarise(a_map())["levels"]
        self.assertEqual(level["index"], "0")
        self.assertEqual(level["label"], "Ground")
        self.assertEqual(level["ambient_light"], "ff808080")
        self.assertEqual(level["counts"], {
            "walls": 1, "portals": 3, "lights": 1, "objects": 4, "paths": 2, "texts": 1,
            "roofs": 1, "patterns": 1, "materials": 1, "water": 2,
        })
        self.assertEqual(level["texts"], ["The Inn"])

    def test_the_water_root_and_holes_are_not_bodies(self):
        self.assertEqual(mapfile._water_bodies({"disable_border": False}), 0)
        self.assertEqual(mapfile._water_bodies(a_map()["world"]["levels"]["0"]["water"]), 2)

    def test_textures_with_their_pack_and_count(self):
        (level,) = mapfile.summarise(a_map())["levels"]
        top = level["textures"][0]
        self.assertEqual((top["texture"], top["kind"], top["count"], top["pack"]),
                         (f"{PACK}/objects/tree.png", "objects", 2, "AbC123"))
        kinds = {(t["texture"], t["kind"]) for t in level["textures"]}
        self.assertIn(("res://textures/walls/stone.png", "walls"), kinds)
        self.assertIn(("res://textures/portals/door_01.png", "portals"), kinds)
        self.assertIn(("res://textures/roofs/thatched/tiles.png", "roofs"), kinds)
        self.assertIn(("res://textures/materials/ice_tile.png", "materials"), kinds)
        self.assertIn(("res://textures/terrain/terrain_snow.png", "terrain"), kinds)
        tree = level["objects"][0]
        self.assertEqual(tree["count"], 2)
        self.assertEqual(tree["scale"], {"min": 2.0, "median": 2.1, "max": 2.2})
        self.assertEqual(tree["layers"], [300])
        self.assertEqual(level["object_kinds"], 3)
        cliff, lip = level["paths"]
        self.assertEqual((cliff["widths"], cliff["layers"], lip["widths"]), ([256.0], [200], [308.0]))

    def test_terrain_slots_and_shares(self):
        (level,) = mapfile.summarise(a_map())["levels"]
        shares = {s["texture"].split("/")[-1]: s["share"] for s in level["terrain"]["slots"]}
        self.assertEqual(shares, {"terrain_snow.png": 0.75, "terrain_gravel.png": 0.25})
        self.assertEqual([s["slot"] for s in level["terrain"]["slots"]], [1, 2])
        self.assertFalse(level["terrain"]["expand_slots"])

    def test_a_splat_of_the_wrong_size_is_not_misread(self):
        raw = a_map()
        raw["world"]["levels"]["0"]["terrain"]["splat"] = "PoolByteArray( 1, 2, 3 )"
        slots = mapfile.summarise(raw)["levels"][0]["terrain"]["slots"]
        self.assertTrue(all(slot["share"] == 0.0 for slot in slots))

    def test_disabled_terrain_is_none(self):
        raw = a_map()
        raw["world"]["levels"]["0"]["terrain"]["enabled"] = False
        self.assertIsNone(mapfile.summarise(raw)["levels"][0]["terrain"])

    def test_materials_lights_and_portals(self):
        (level,) = mapfile.summarise(a_map())["levels"]
        self.assertEqual(level["materials"], [{
            "layer": -400, "texture": "res://textures/materials/ice_tile.png", "pack": None, "coverage": 0.5}])
        self.assertEqual(level["lights"]["count"], 1)
        self.assertEqual(level["lights"]["range"], {"min": 9.0, "max": 9.0})
        portals = level["portals"]
        self.assertEqual((portals["count"], portals["doors"], portals["windows"]), (3, 1, 2))

    def test_reversed_doors_and_windows_are_listed(self):
        """A door saved open and a window saved closed export as each other."""
        s = mapfile.summarise(a_map())
        reversed_ = s["levels"][0]["portals"]["vtt_reversed"]
        self.assertEqual([(r["id"], r["named"], r["exports_as"]) for r in reversed_],
                         [("4", "door", "window"), ("6", "window", "door")])
        self.assertTrue(any("Universal VTT" in n for n in s["notes"]))

    def test_levels_come_in_index_order(self):
        raw = a_map()
        raw["world"]["levels"] = {"2": {"label": "Attic"}, "0": {"label": "Ground"}, "1": {"label": "Upper"}}
        s = mapfile.summarise(raw)
        self.assertEqual([lv["label"] for lv in s["levels"]], ["Ground", "Upper", "Attic"])
        self.assertEqual(s["level_count"], 3)
        self.assertEqual(s["levels"][2]["counts"]["objects"], 0)

    def test_the_summary_is_json(self):
        json.dumps(mapfile.summarise(a_map(), installed=["AbC123"]))


class TraceImage(unittest.TestCase):
    def test_no_trace_image(self):
        self.assertIsNone(mapfile.summarise(a_map())["trace_image"])

    def test_a_local_trace_path_is_checked(self):
        with tempfile.TemporaryDirectory() as folder:
            present = Path(folder) / "trace.jpg"
            present.write_bytes(b"x")
            for image, exists in ((str(present), True), (str(Path(folder) / "gone.jpg"), False)):
                raw = a_map()
                raw["header"]["editor_state"] = {"trace_image": {"image": image}}
                s = mapfile.summarise(raw)
                self.assertEqual(s["trace_image"], {"path": image, "exists": exists})
                self.assertEqual(any("trace image is missing" in n for n in s["notes"]), not exists)

    def test_a_non_local_trace_path_is_reported_never_opened(self):
        """On Windows, checking a UNC path connects to its host and offers the
        user's credentials. Only a plain local absolute path is checked."""
        touched = []
        original = mapfile.Path.exists
        mapfile.Path.exists = lambda self: touched.append(self) or False
        try:
            for image in (r"\\attacker\share\t.jpg", "//attacker/share/t.jpg", "relative/t.jpg", "C:t.jpg"):
                raw = a_map()
                raw["header"]["editor_state"] = {"trace_image": {"image": image}}
                s = mapfile.summarise(raw)
                self.assertEqual(s["trace_image"], {"path": image, "exists": None})
                self.assertTrue(any("not checked" in n for n in s["notes"]), s["notes"])
        finally:
            mapfile.Path.exists = original
        self.assertEqual(touched, [])


class Comparison(unittest.TestCase):
    def test_comparing_a_rebuild_with_its_original(self):
        original = mapfile.summarise(a_map())
        rebuilt_raw = a_map(
            objects=[{"texture": f"{PACK}/objects/tree.png", "scale": "Vector2( 1, 1 )"},
                     {"texture": "res://textures/objects/bush.png", "scale": "Vector2( 1, 1 )"}],
            materials={}, lights=[])
        rebuilt_raw["world"]["levels"]["0"]["terrain"]["splat"] = splat(2, 1, (128, 128, 0, 0))
        rebuilt_raw["header"]["asset_manifest"] = rebuilt_raw["header"]["asset_manifest"][:1]
        diff = mapfile.compare(original, mapfile.summarise(rebuilt_raw))
        (level,) = diff["levels"]
        self.assertEqual(level["count_changes"], {"objects": -2, "materials": -1, "lights": -1})
        self.assertEqual(level["textures_only_in_second"], ["res://textures/objects/bush.png"])
        self.assertIn("res://textures/objects/rock.png", level["textures_only_in_first"])
        self.assertEqual([c["texture"] for c in level["scale_changes"]], [f"{PACK}/objects/tree.png"])
        self.assertEqual(level["terrain_share_changes"], {
            "res://textures/terrain/terrain_gravel.png": 0.25,
            "res://textures/terrain/terrain_snow.png": -0.25})
        self.assertEqual(diff["packs_only_in_first"], ["Missing Pack"])
        self.assertTrue(any("objects 4 -> 2" in n for n in diff["notes"]))

    def test_levels_only_in_one_map(self):
        two = a_map()
        two["world"]["levels"]["1"] = {"label": "Upper"}
        two["world"]["width"] = 4
        diff = mapfile.compare(mapfile.summarise(a_map()), mapfile.summarise(two))
        self.assertEqual(diff["levels"][1], {"index": "1", "label": "Upper", "only_in": "second"})
        self.assertEqual(diff["size_tiles"], {"first": [2, 1], "second": [4, 1]})
        self.assertTrue(any("size" in n for n in diff["notes"]))

    def test_inspect_reads_both_files(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder) / "a.dungeondraft_map", Path(folder) / "b.dungeondraft_map"
            first.write_text(godot_json(a_map()), encoding="utf-8")
            second.write_text(godot_json(a_map(lights=[])), encoding="utf-8")
            result = mapfile.inspect(first, installed=["AbC123"], compare_to=second)
            self.assertEqual(result["file"], str(first))
            self.assertEqual([p["id"] for p in result["packs_missing"]], ["Miss42"])
            self.assertEqual(result["comparison"]["levels"][0]["count_changes"], {"lights": -1})


# Shaped like the pack.json Dungeondraft mounts: one pack ships colour
# overrides and one does not.
INSTALLED = [
    {"id": "8XXbciV2", "name": "2MT Medieval Building", "version": "2.2", "author": "2-Minute Tabletop"},
    {"id": "FA30DDXY", "name": "FA_Starter_Pack_v3.0", "version": "3.0", "author": "Forgotten Adventures",
     "allow_3rd_party_mapping_software_to_read": True,
     "custom_color_overrides": {"enabled": True, "min_redness": 0.001}},
]


class Manifest(unittest.TestCase):
    def test_an_entry_carries_what_dungeondraft_writes(self):
        entry = mapfile.manifest_entry(INSTALLED[1])
        self.assertEqual(entry["id"], "FA30DDXY")
        self.assertEqual(entry["name"], "FA_Starter_Pack_v3.0")
        self.assertEqual(entry["version"], "3.0")
        self.assertEqual(entry["author"], "Forgotten Adventures")
        self.assertTrue(entry["custom_color_overrides"]["enabled"])
        self.assertTrue(entry["allow_3rd_party_mapping_software_to_read"])

    def test_a_pack_without_the_optional_fields_omits_them(self):
        entry = mapfile.manifest_entry(INSTALLED[0])
        self.assertEqual(set(entry), {"name", "id", "version", "author"})

    def test_build_manifest_every_pack_or_a_subset_once_each(self):
        self.assertEqual([e["id"] for e in mapfile.build_manifest(INSTALLED)], ["8XXbciV2", "FA30DDXY"])
        self.assertEqual([e["id"] for e in mapfile.build_manifest(INSTALLED, ["FA30DDXY"])], ["FA30DDXY"])
        twice = INSTALLED + [dict(INSTALLED[1], version="3.51")]
        self.assertEqual([e["id"] for e in mapfile.build_manifest(twice)], ["8XXbciV2", "FA30DDXY"])

    def test_unknown_ids(self):
        self.assertEqual(mapfile.unknown_ids(INSTALLED, ["FA30DDXY", "nope"]), ["nope"])

    def test_merge_keeps_the_maps_own_entries_and_adds_new_ones_once(self):
        existing = [{"id": "packA", "name": "Pack A", "version": "1.0", "author": "x"}]
        merged = mapfile.merge_manifest(existing, [{"id": "packB", "name": "Pack B", "version": "2.0"}])
        self.assertEqual([e["id"] for e in merged], ["packA", "packB"])
        merged = mapfile.merge_manifest(existing, [{"id": "packA", "name": "Pack A", "version": "9.9"}])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["version"], "1.0", "the map's own entry wins, not the installed copy")


class PreparedText(unittest.TestCase):
    def test_only_the_manifest_changes_and_the_layout_is_dungeondrafts(self):
        text = godot_json(a_map())
        out, report = mapfile.prepared_text(text, mapfile.build_manifest(INSTALLED))
        # Everything before and after the manifest is byte-identical.
        before = text.index('"asset_manifest"')
        self.assertEqual(out[:before], text[:before])
        after_in, after_out = text.index('"editor_state"'), out.index('"editor_state"')
        self.assertEqual(out[after_out:], text[after_in:])
        written = json.loads(out)
        self.assertEqual([e["id"] for e in written["header"]["asset_manifest"]], ["AbC123", "Miss42", "8XXbciV2", "FA30DDXY"])
        self.assertEqual(written["world"], a_map()["world"])
        self.assertTrue(written["header"]["uses_default_assets"])
        # New entries are laid out one member per line at the header's depth, tab-indented.
        self.assertIn('\n\t\t\t{\n\t\t\t\t"name": "2MT Medieval Building",', out)
        self.assertEqual(report["packs_before"], 2)
        self.assertEqual(report["packs_after"], 4)
        self.assertEqual(report["kept_from_source"], ["AbC123", "Miss42"])
        self.assertEqual(report["added"], ["8XXbciV2", "FA30DDXY"])

    def test_a_compact_file_stays_valid(self):
        text = json.dumps(a_map(), separators=(",", ":"))
        out, _report = mapfile.prepared_text(text, [{"id": "packB", "name": "B", "version": "1", "author": ""}])
        self.assertEqual([e["id"] for e in json.loads(out)["header"]["asset_manifest"]], ["AbC123", "Miss42", "packB"])

    def test_uses_default_assets_changes_only_when_asked(self):
        text = godot_json(a_map())
        out, report = mapfile.prepared_text(text, [], uses_default_assets=False)
        self.assertFalse(json.loads(out)["header"]["uses_default_assets"])
        self.assertFalse(report["uses_default_assets"])
        self.assertEqual(json.loads(out)["world"], a_map()["world"])

    def test_non_ascii_pack_names_are_written_as_text(self):
        out, _ = mapfile.prepared_text(godot_json(a_map()), [{"id": "p", "name": "Höhle", "version": "1", "author": ""}])
        self.assertIn('"name": "Höhle"', out)

    def test_a_file_without_a_manifest_is_refused(self):
        raw = a_map()
        del raw["header"]["asset_manifest"]
        with self.assertRaisesRegex(mapfile.MapFileError, "no asset_manifest"):
            mapfile.prepared_text(godot_json(raw), [{"id": "p"}])
        with self.assertRaisesRegex(mapfile.MapFileError, "no 'header'"):
            mapfile.prepared_text(json.dumps({"world": {}}), [{"id": "p"}])

    def test_member_span_walks_an_object(self):
        text = '{"a": [1, 2], "b" : {"c": "x"}, "d": null}'
        start, end = mapfile._member_span(text, 0, "b")
        self.assertEqual(text[start:end], '{"c": "x"}')
        self.assertIsNone(mapfile._member_span(text, 0, "zz"))
        self.assertIsNone(mapfile._member_span("{}", 0, "a"))


class PrepareCopy(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.source = self.folder / "scratch.dungeondraft_map"
        self.source.write_text(godot_json(a_map()), encoding="utf-8")
        self.destination = self.folder / "with-packs.dungeondraft_map"

    def tearDown(self):
        self._folder.cleanup()

    def test_preparing_a_map_keeps_everything_but_the_manifest(self):
        report = mapfile.prepare_copy(self.source, self.destination, mapfile.build_manifest(INSTALLED))
        self.assertEqual((report["packs_before"], report["packs_after"]), (2, 4))
        self.assertEqual(report["path"], str(self.destination))
        written = json.loads(self.destination.read_text(encoding="utf-8"))
        self.assertEqual([e["id"] for e in written["header"]["asset_manifest"]],
                         ["AbC123", "Miss42", "8XXbciV2", "FA30DDXY"])
        self.assertEqual(written["world"], a_map()["world"])
        # The source is left alone and no temporary remains.
        self.assertEqual(self.source.read_text(encoding="utf-8"), godot_json(a_map()))
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()),
                         ["scratch.dungeondraft_map", "with-packs.dungeondraft_map"])

    def test_refuses_to_write_onto_its_source(self):
        with self.assertRaisesRegex(mapfile.MapFileError, "COPY"):
            mapfile.prepare_copy(self.source, self.source, [{"id": "packB"}])
        self.assertEqual(json.loads(self.source.read_text())["world"], a_map()["world"])

    def test_never_replaces_an_existing_destination(self):
        self.destination.write_text("appeared after the check")
        with self.assertRaisesRegex(mapfile.MapFileError, "already exists"):
            mapfile.prepare_copy(self.source, self.destination, [{"id": "packB"}])
        self.assertEqual(self.destination.read_text(), "appeared after the check")
        self.assertEqual([p.name for p in self.folder.iterdir() if "preparing" in p.name], [])

    def test_the_publish_step_itself_refuses_an_existing_destination(self):
        """The exists() check is not atomic; the link/rename must refuse too."""
        self.destination.write_text("raced in")
        with self.assertRaisesRegex(mapfile.MapFileError, "already exists"):
            mapfile._publish_new_file(self.destination, b"{}", like=self.source)
        self.assertEqual(self.destination.read_text(), "raced in")
        self.assertEqual([p.name for p in self.folder.iterdir() if "preparing" in p.name], [])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits and symlinks")
    def test_a_planted_temporary_symlink_is_not_followed(self):
        canary = self.folder / "unrelated.txt"
        canary.write_text("precious")
        os.symlink(canary, self.folder / ".with-packs.dungeondraft_map.preparing")
        mapfile.prepare_copy(self.source, self.destination, [{"id": "packB", "name": "B", "version": "1", "author": ""}])
        self.assertEqual(canary.read_text(), "precious")
        self.assertFalse(self.destination.is_symlink())
        self.assertEqual([e["id"] for e in json.loads(self.destination.read_text())["header"]["asset_manifest"]],
                         ["AbC123", "Miss42", "packB"])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_a_symlink_at_the_destination_is_refused(self):
        os.symlink(self.folder / "elsewhere", self.destination)
        with self.assertRaisesRegex(mapfile.MapFileError, "already exists"):
            mapfile.prepare_copy(self.source, self.destination, [{"id": "packB"}])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_the_copy_takes_the_sources_permissions(self):
        self.source.chmod(0o644)
        mapfile.prepare_copy(self.source, self.destination, [{"id": "packB"}])
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o644)


if __name__ == "__main__":
    unittest.main()
