"""Offline tests for the scene checks (bare ground, light causes): no Dungeondraft needed.

    .venv/bin/python -m unittest discover -s server/tests
"""

import unittest

from dungeondraft_mcp import scene

MAP = (4096.0, 3072.0)   # 16 x 12 tiles

LIMESTONE = "res://textures/terrain/terrain_limestone.png"   # what a new map's base slot holds
GRASS = "res://textures/terrain/terrain_grass.png"
GRAVEL = "res://textures/terrain/terrain_gravel.png"

BASE = [1, 0, 0, 0]    # all weight in the base slot: nothing painted
SLOT1 = [0, 1, 0, 0]


def samples(weights_at, n=16, covered_at=lambda x, y: False):
    """A sample grid in the shape the mod's terrain_samples command returns."""
    out = []
    for j in range(n):
        for i in range(n):
            x, y = MAP[0] * i / (n - 1), MAP[1] * j / (n - 1)
            out.append({"x": x, "y": y, "weights": weights_at(x, y), "covered": covered_at(x, y)})
    return out


def light(id, x, y, range=1.0):
    return {"id": id, "kind": "light", "position": [x, y], "rotation": 0.0, "scale": 1.0,
            "color": "#ffcc88", "energy": 1.0, "range": range, "shadows": True}


def obj(id, asset, x, y):
    return {"id": id, "kind": "object", "position": [x, y], "rotation": 0.0, "scale": 1.0,
            "asset": f"res://textures/objects/{asset}", "layer": 100}


class BareGround(unittest.TestCase):
    def test_a_pristine_map_is_all_bare(self):
        report = scene.bare_ground(samples(lambda x, y: BASE), slots=[LIMESTONE] * 4)
        self.assertFalse(report["ok"])
        self.assertEqual(report["bare"], report["sampled"])
        self.assertEqual(report["bare_share"], 1.0)
        self.assertEqual(len(report["clusters"]), 1)
        self.assertTrue(any("100%" in n for n in report["notes"]))

    def test_a_chosen_base_texture_is_ground(self):
        """[1, 0, 0, 0] reads the same on grass-everywhere and on an untouched map;
        the slot's texture decides."""
        report = scene.bare_ground(samples(lambda x, y: BASE), slots=[GRASS, LIMESTONE, LIMESTONE, LIMESTONE])
        self.assertTrue(report["ok"])
        self.assertEqual(report["bare"], 0)
        self.assertEqual(report["clusters"], [])

    def test_grass_only_under_the_trees_is_caught(self):
        def patches(x, y):
            near = (abs(x - 400) < 200 and abs(y - 2600) < 200) or (abs(x - 3600) < 200 and abs(y - 500) < 200)
            return SLOT1 if near else BASE
        report = scene.bare_ground(samples(patches, 24), slots=[LIMESTONE, GRASS, LIMESTONE, LIMESTONE])
        self.assertFalse(report["ok"])
        self.assertGreater(report["bare_share"], 0.8)
        self.assertTrue(report["clusters"], "a finding has to say where")

    def test_a_covered_interior_is_not_bare(self):
        """A floored building stands on unpainted terrain; the floor covers it."""
        def inside(x, y):
            return 1024 < x < 3072 and 1024 < y < 2048
        report = scene.bare_ground(samples(lambda x, y: BASE if inside(x, y) else SLOT1, 24, covered_at=inside),
                                   slots=[LIMESTONE, GRASS, LIMESTONE, LIMESTONE])
        self.assertTrue(report["ok"])
        self.assertEqual(report["bare"], 0)
        self.assertGreater(report["covered"], 0)

    def test_a_blended_edge_counts_as_painted(self):
        report = scene.bare_ground(samples(lambda x, y: [0.7, 0.3, 0, 0], 8),
                                   slots=[LIMESTONE, GRASS, LIMESTONE, LIMESTONE])
        self.assertEqual(report["bare"], 0)

    def test_weight_in_an_expanded_slot_counts_as_painted(self):
        report = scene.bare_ground(samples(lambda x, y: [0.4, 0, 0, 0, 0, 0.6, 0, 0], 8),
                                   slots=[LIMESTONE, None, None, None, None, GRAVEL, None, None])
        self.assertEqual(report["bare"], 0)

    def test_without_slots_the_base_is_assumed_unpainted_and_the_note_says_so(self):
        report = scene.bare_ground(samples(lambda x, y: BASE, 8))
        self.assertFalse(report["ok"])
        self.assertTrue(any("slot 0" in n for n in report["notes"]))

    def test_samples_under_walls_are_skipped(self):
        room = {"id": 1, "loop": True, "half_width": 32,
                "points": [[1024, 1024], [3072, 1024], [3072, 2048], [1024, 2048]]}
        on_the_walls = samples(lambda x, y: BASE if (x == 1024 or y == 1024) else SLOT1, 17)
        without = scene.bare_ground(on_the_walls, slots=[LIMESTONE, GRASS, LIMESTONE, LIMESTONE])
        with_walls = scene.bare_ground(on_the_walls, slots=[LIMESTONE, GRASS, LIMESTONE, LIMESTONE], walls=[room])
        self.assertGreater(without["bare"], with_walls["bare"])
        self.assertEqual(with_walls["under_walls"], 10)
        self.assertEqual(with_walls["sampled"] + with_walls["under_walls"], without["sampled"])

    def test_clusters_are_separate_patches_largest_first_with_bounds_inside_them(self):
        def two_corners(x, y):
            return BASE if (x < 600 and y < 600) or (x > 3400 and y > 2400) else SLOT1
        report = scene.bare_ground(samples(two_corners), slots=[LIMESTONE, GRASS, LIMESTONE, LIMESTONE])
        self.assertEqual([c["samples"] for c in report["clusters"]], [12, 9])
        big, small = report["clusters"]
        self.assertTrue(big["bounds"][0] > 3400 and big["bounds"][1] > 2400)
        self.assertTrue(small["bounds"][2] < 600 and small["bounds"][3] < 600)
        self.assertEqual(big["bounds"][2:], [4096, 3072])
        self.assertGreater(big["area_tiles"], small["area_tiles"])
        self.assertEqual(report["bare"], 21)

    def test_the_cluster_list_is_capped_but_the_counts_are_not(self):
        def every_third(x, y):
            i, j = round(x / (MAP[0] / 15)), round(y / (MAP[1] / 15))
            return BASE if i % 3 == 0 and j % 3 == 0 else SLOT1     # 36 isolated bare samples
        report = scene.bare_ground(samples(every_third), slots=[LIMESTONE, GRASS, LIMESTONE, LIMESTONE], limit=3)
        self.assertEqual(len(report["clusters"]), 3)
        self.assertEqual(report["bare"], 36)
        self.assertTrue(any("36 patches" in n for n in report["notes"]), report["notes"])

    def test_no_samples_is_clean(self):
        report = scene.bare_ground([], slots=[LIMESTONE] * 4)
        self.assertTrue(report["ok"])
        self.assertEqual(report["sampled"], 0)
        self.assertIn("no samples", report["notes"])

    def test_the_new_map_base_is_recognised_by_stem(self):
        self.assertTrue(scene.is_new_map_base(LIMESTONE))
        self.assertTrue(scene.is_new_map_base("res://packs/x/terrain/terrain_limestone.webp"))
        self.assertTrue(scene.is_new_map_base(None))
        self.assertFalse(scene.is_new_map_base(GRASS))


# Real file names from Dungeondraft's library and a common pack: the cases that
# shaped the vocabulary and its exclusions.
REAL_SOURCES = [
    "stove_01.png", "stove_02_roman.png", "funeral_pyre_01.png", "mana_orb_01.png", "wall_torch_01.png",
    "candle_holder_02.png", "fireplace_03.png", "campfire_01.png", "dwarven_forge.png", "forge_fire.png",
    "bowl_lamp_01.png", "magical_brazier_01.png", "cave_crystals_04.png", "magic_portal_01.png",
    "arcane_summoning_circle_glow_01.png", "alchemy_furnace_01.png", "chandelier_01.png", "lantern_01.png",
    "candelabrum_02.png",
]
REAL_NON_SOURCES = [
    "sundial_01.png",          # `sun` is not a stem
    "stairs_wood.png",         # nor `star`
    "magic_mushrooms_01.png",  # nor `magic`
    "forge_lever.png",         # a part of the forge, excluded by `lever`
    "forge_hammer_mold.png",
    "coal_bag.png",            # `coals` is the stem, not `coal`
    "runed_podium.png",
    "log_pile_01.png",
    "bar_table.png",
]
FA_LIGHTS = "res://packs/FA35OB01/textures/objects/Lightsources"


class Emitters(unittest.TestCase):
    def test_real_light_sources_are_recognised(self):
        for name in REAL_SOURCES:
            with self.subTest(name=name):
                self.assertTrue(scene.is_emitter(f"res://textures/objects/x/{name}"))

    def test_real_non_sources_are_not(self):
        for name in REAL_NON_SOURCES:
            with self.subTest(name=name):
                self.assertFalse(scene.is_emitter(f"res://textures/objects/x/{name}"))

    def test_a_light_sources_folder_credits_names_no_stem_anticipates(self):
        self.assertTrue(scene.is_emitter(f"{FA_LIGHTS}/Candelabras/Candelabra_Metal_Gray_A_1x1.webp"))
        self.assertTrue(scene.is_emitter(f"{FA_LIGHTS}/Oil_Vessels/Oil_Vessel_Clay_A_1x1.webp"))
        self.assertTrue(scene.is_emitter("res://packs/x/textures/objects/Light_Sources/Thing_A.webp"))

    def test_the_folder_does_not_credit_a_part(self):
        self.assertFalse(scene.is_emitter(f"{FA_LIGHTS}/Fireplaces/Firewood_Pile_A_1x1.webp"))

    def test_an_empty_asset_is_not_an_emitter(self):
        self.assertFalse(scene.is_emitter(""))


class LightCauses(unittest.TestCase):
    def test_a_light_over_a_hearth_is_explained_and_the_hearth_is_named(self):
        report = scene.light_causes([light(1, 1200, 1200)], [obj(2, "environment/fire_02.png", 1250, 1220)])
        self.assertTrue(report["ok"])
        self.assertEqual(report["unexplained"], [])
        self.assertEqual(report["explained"][0]["by"], 2)
        self.assertEqual(report["explained"][0]["asset"], "fire_02.png")
        self.assertEqual(report["explained"][0]["distance"], 54)
        self.assertEqual(report["unlit_emitters"], [])

    def test_a_light_with_nothing_emitting_it_is_reported_with_its_position(self):
        report = scene.light_causes([light(1, 1200, 1200)], [obj(2, "furniture/tables/round_table_01.png", 1210, 1210)])
        self.assertFalse(report["ok"])
        self.assertEqual(report["emitters"], 0)
        self.assertEqual(report["unexplained"], [{"id": 1, "position": [1200.0, 1200.0], "range": 1.0}])

    def test_an_emitter_across_the_map_explains_nothing_and_is_itself_unlit(self):
        report = scene.light_causes([light(1, 1200, 1200)], [obj(2, "decor/lighting/lantern_01.png", 3900, 2900)])
        self.assertEqual([u["id"] for u in report["unexplained"]], [1])
        self.assertEqual(report["unlit_emitters"], [{"id": 2, "asset": "lantern_01.png", "position": [3900.0, 2900.0]}])
        self.assertFalse(report["ok"])

    def test_an_unlit_emitter_alone_does_not_fail_the_check(self):
        report = scene.light_causes([], [obj(2, "decor/lighting/lantern_01.png", 3900, 2900)])
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["unlit_emitters"]), 1)

    def test_the_nearest_emitter_is_credited_and_both_count_as_lit(self):
        report = scene.light_causes(
            [light(1, 1000, 1000)],
            [obj(2, "decor/lighting/wall_torch_01.png", 1100, 1000), obj(3, "decor/candle_01.png", 1020, 1000)])
        self.assertEqual(report["explained"][0]["by"], 3)
        self.assertEqual(report["unlit_emitters"], [])

    def test_emitter_reach_is_respected(self):
        far = [obj(2, "decor/lighting/lantern_01.png", 1500, 1000)]
        self.assertFalse(scene.light_causes([light(1, 1000, 1000)], far)["ok"])
        self.assertTrue(scene.light_causes([light(1, 1000, 1000)], far, emitter_reach=600)["ok"])

    def test_an_ambient_light_is_listed_not_flagged(self):
        report = scene.light_causes([light(1, 1200, 1200, range=6.0)], [])
        self.assertTrue(report["ok"])
        self.assertEqual([a["id"] for a in report["ambient"]], [1])
        self.assertEqual(report["unexplained"], [])

    def test_elements_may_give_x_and_y_instead_of_position(self):
        report = scene.light_causes([{"id": 1, "x": 100, "y": 100}],
                                    [{"id": 2, "x": 120, "y": 100, "asset": "res://x/torch_01.png"}])
        self.assertTrue(report["ok"])
        self.assertEqual(report["explained"][0]["distance"], 20)

    def test_no_lights_is_clean_rather_than_an_error(self):
        self.assertTrue(scene.light_causes([], [])["ok"])

    def test_bad_inputs_are_refused(self):
        with self.assertRaises(ValueError):
            scene.light_causes([light(1, 0, 0)], [], emitter_reach=0)
        with self.assertRaises(ValueError):
            scene.light_causes([{"id": 1}], [])


if __name__ == "__main__":
    unittest.main()
