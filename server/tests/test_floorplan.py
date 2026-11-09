"""Offline tests for the floorplan analysis: synthetic snapshots, no Dungeondraft.

    .venv/bin/python -m unittest discover -s server/tests
"""

import unittest

from dungeondraft_mcp import floorplan

MAP = [4096, 3072]   # 16 x 12 tiles


def loop(id, x0, y0, x1, y1, half_width=32):
    """A closed rectangular wall, as the snapshot reports one."""
    return {"id": id, "loop": True, "half_width": half_width,
            "points": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}


def segment(id, points, half_width=32):
    return {"id": id, "loop": False, "half_width": half_width, "points": points}


def door(id, wall_id, x, y, direction, radius=128, window=False):
    return {"id": id, "wall_id": wall_id, "asset": "res://textures/portals/door_00.png",
            "position": [x, y], "direction": list(direction), "radius": radius, "window": window}


def scene(walls=(), portals=()):
    return {"level_id": 0, "map_size": MAP, "objects": [], "unreadable": [], "regions": [],
            "walls": list(walls), "portals": list(portals)}


def codes(report):
    return sorted(f["code"] for f in report["findings"])


ROOM = loop(1, 512, 512, 2560, 2048)                  # 8 x 6 tiles, walls on cell boundaries
PARTITION = segment(2, [[1536, 512], [1536, 2048]])   # splits it into two 4 x 6 halves
FRONT_DOOR = door(10, 1, 512, 1280, (0, 1))           # in the left wall
INNER_DOOR = door(11, 2, 1536, 1280, (0, 1))          # through the partition


class Rooms(unittest.TestCase):
    def test_a_map_with_no_walls_has_no_rooms(self):
        report = floorplan.analyse(scene())
        self.assertEqual(report["rooms"], [])
        self.assertTrue(report["ok"])
        self.assertTrue(report["outside"])
        self.assertTrue(any("no enclosed rooms" in n for n in report["notes"]))

    def test_a_room_with_a_door_is_reachable_and_the_door_is_an_entrance(self):
        report = floorplan.analyse(scene([ROOM], [FRONT_DOOR]))
        self.assertEqual(len(report["rooms"]), 1)
        room = report["rooms"][0]
        self.assertTrue(room["reachable_from_outside"])
        self.assertEqual(room["portal_ids"], [10])
        self.assertEqual(report["entrances"], [10])
        self.assertEqual(report["findings"], [])
        self.assertTrue(report["ok"])

    def test_a_room_with_no_door_is_sealed(self):
        report = floorplan.analyse(scene([ROOM]))
        self.assertEqual(len(report["rooms"]), 1)
        self.assertFalse(report["rooms"][0]["reachable_from_outside"])
        self.assertEqual(codes(report), ["no_entrance", "sealed_room"])
        self.assertEqual(report["findings"][0]["room_id"], 1)
        self.assertFalse(report["ok"])

    def test_area_bounds_and_center_are_in_sensible_units(self):
        room = floorplan.analyse(scene([ROOM], [FRONT_DOOR]))["rooms"][0]
        # 8 x 6 tiles between the wall centerlines; the walls' thickness comes off that.
        self.assertTrue(36 < room["area_tiles"] < 48, room["area_tiles"])
        x0, y0, x1, y1 = room["bounds"]
        self.assertTrue(512 <= x0 < 700 and 2400 < x1 <= 2560)
        self.assertTrue(512 <= y0 < 700 and 1900 < y1 <= 2048)
        cx, cy = room["center"]
        self.assertTrue(x0 < cx < x1 and y0 < cy < y1)

    def test_two_rooms_joined_by_a_door_are_both_reachable(self):
        report = floorplan.analyse(scene([ROOM, PARTITION], [FRONT_DOOR, INNER_DOOR]))
        self.assertEqual([r["id"] for r in report["rooms"]], [1, 2])
        self.assertTrue(all(r["reachable_from_outside"] for r in report["rooms"]))
        inner = next(p for p in report["portals"] if p["id"] == 11)
        self.assertEqual(sorted(inner["sides"]), [1, 2])
        self.assertIsNone(inner["problem"])
        self.assertEqual(report["entrances"], [10])
        self.assertTrue(report["ok"])

    def test_rooms_are_numbered_in_reading_order(self):
        report = floorplan.analyse(scene([ROOM, PARTITION], [FRONT_DOOR, INNER_DOOR]))
        left, right = report["rooms"]
        self.assertLess(left["bounds"][0], right["bounds"][0])
        self.assertEqual(left["portal_ids"], [10, 11])
        self.assertEqual(right["portal_ids"], [11])

    def test_removing_the_connecting_door_seals_only_the_inner_room(self):
        report = floorplan.analyse(scene([ROOM, PARTITION], [FRONT_DOOR]))
        self.assertEqual(codes(report), ["sealed_room"])
        self.assertEqual(report["findings"][0]["room_id"], 2)
        self.assertEqual([r["reachable_from_outside"] for r in report["rooms"]], [True, False])
        self.assertEqual(report["entrances"], [10])

    def test_a_room_whose_only_door_opens_onto_a_wall_is_unreachable(self):
        # A thick wall runs just outside the front door.
        behind = segment(6, [[400, 1000], [400, 1500]], half_width=64)
        report = floorplan.analyse(scene([ROOM, behind], [FRONT_DOOR]))
        self.assertEqual(codes(report), ["no_entrance", "portal_to_nothing", "unreachable_room"])
        portal = report["portals"][0]
        self.assertIn(floorplan.WALL, portal["sides"])
        self.assertIn("opens onto a wall", portal["problem"])
        self.assertEqual(report["entrances"], [])

    def test_slivers_are_counted_in_notes_not_reported_as_rooms(self):
        # A 1 x 2 tile loop: with the walls' thickness, under a tile of floor is left.
        report = floorplan.analyse(scene([loop(1, 1024, 512, 1280, 1024)]))
        self.assertEqual(report["rooms"], [])
        self.assertTrue(report["ok"])
        self.assertTrue(any("1 enclosed area under 1 tile ignored" in n for n in report["notes"]), report["notes"])

    def test_wall_thickness_closes_a_gap_narrower_than_the_walls(self):
        # The loop stops 100 woxels short of closing on its top edge.
        def almost_closed(half_width):
            return segment(1, [[1636, 512], [2560, 512], [2560, 2048], [512, 2048], [512, 512], [1536, 512]],
                           half_width=half_width)
        thick = floorplan.analyse(scene([almost_closed(64)]))
        self.assertEqual(len(thick["rooms"]), 1)
        thin = floorplan.analyse(scene([almost_closed(2)]))
        self.assertEqual(thin["rooms"], [])


class Portals(unittest.TestCase):
    def test_a_door_in_a_wall_that_encloses_nothing(self):
        fence = segment(5, [[3000, 2600], [3800, 2600]])
        gate = door(12, 5, 3400, 2600, (1, 0))
        report = floorplan.analyse(scene([ROOM, fence], [FRONT_DOOR, gate]))
        self.assertEqual(codes(report), ["portal_to_nothing"])
        self.assertEqual(report["findings"][0]["portal_id"], 12)
        portal = next(p for p in report["portals"] if p["id"] == 12)
        self.assertEqual(portal["sides"], [floorplan.OUTSIDE, floorplan.OUTSIDE])
        self.assertIn("both sides", portal["problem"])
        self.assertEqual(report["entrances"], [10])   # the real door is unaffected

    def test_a_door_at_the_map_edge_leads_off_the_map(self):
        edge_wall = segment(7, [[32, 1000], [32, 2000]])
        report = floorplan.analyse(scene([edge_wall], [door(13, 7, 32, 1500, (0, 1))]))
        self.assertIn(floorplan.OFF_MAP, report["portals"][0]["sides"])
        self.assertEqual(codes(report), ["portal_to_nothing"])

    def test_a_window_is_attached_to_its_sides_but_is_not_a_way_in(self):
        window = door(20, 1, 1280, 512, (1, 0), window=True)
        report = floorplan.analyse(scene([ROOM], [window]))
        portal = report["portals"][0]
        self.assertEqual(portal["kind"], "window")
        self.assertEqual(sorted(map(str, portal["sides"])), ["1", floorplan.OUTSIDE])
        self.assertIsNone(portal["problem"])
        self.assertEqual(report["rooms"][0]["portal_ids"], [20])
        self.assertEqual(report["entrances"], [])
        self.assertEqual(codes(report), ["no_entrance", "sealed_room"])
        self.assertIn("1 window only", report["findings"][0]["message"])

    def test_a_door_at_a_corner_still_finds_the_room(self):
        # The probe at the door's middle, offset toward the corner, lands in the top wall;
        # the probes along the door do not.
        report = floorplan.analyse(scene([ROOM], [door(10, 1, 512, 700, (0, 1))]))
        self.assertEqual(sorted(map(str, report["portals"][0]["sides"])), ["1", floorplan.OUTSIDE])
        self.assertTrue(report["ok"])

    def test_a_portal_on_an_unknown_wall_uses_the_default_thickness(self):
        stray = dict(FRONT_DOOR, wall_id=999)
        report = floorplan.analyse(scene([ROOM], [stray]))
        self.assertEqual(report["entrances"], [10])
        self.assertTrue(report["ok"])


class Grid(unittest.TestCase):
    def test_a_big_grid_is_coarsened_to_the_cap_and_says_so(self):
        report = floorplan.analyse(scene([ROOM]), cell_woxels=16, max_cells=10_000)
        self.assertGreater(report["cell_woxels"], 16)
        self.assertLessEqual(report["grid"][0] * report["grid"][1], 10_000)
        self.assertTrue(any("cell raised from 16" in n for n in report["notes"]))
        self.assertEqual(len(report["rooms"]), 1)   # still found at the coarser cell

    def test_the_default_cell_fits_a_200_tile_map(self):
        self.assertEqual(floorplan.choose_cell(51200, 51200, floorplan.DEFAULT_CELL), floorplan.DEFAULT_CELL)

    def test_a_map_walled_to_its_edge_has_no_outside_to_reach(self):
        report = floorplan.analyse({"map_size": [1024, 1024], "walls": [loop(1, 0, 0, 1024, 1024)], "portals": []})
        self.assertFalse(report["outside"])
        self.assertEqual(len(report["rooms"]), 1)
        self.assertIsNone(report["rooms"][0]["reachable_from_outside"])
        self.assertEqual(codes(report), ["sealed_room"])   # no_entrance / unreachable are not judged
        self.assertTrue(any("not judged" in n for n in report["notes"]))

    def test_bad_inputs_are_refused(self):
        with self.assertRaises(ValueError):
            floorplan.analyse(scene(), cell_woxels=0)
        with self.assertRaises(ValueError):
            floorplan.analyse({"map_size": [0, 1024], "walls": [], "portals": []})
        with self.assertRaises(ValueError):
            floorplan.analyse(scene(), min_room_tiles=-1)

    def test_the_raster_blocks_a_wall_and_leaves_the_floor_open(self):
        grid = floorplan.rasterise([ROOM], MAP[0], MAP[1], 64)
        self.assertTrue(grid.blocked[grid.index(512, 1280)])      # on the left wall
        self.assertFalse(grid.blocked[grid.index(1536, 1280)])    # the middle of the room
        self.assertIsNone(grid.index(-1, 0))
        self.assertIsNone(grid.index(MAP[0], 0))


if __name__ == "__main__":
    unittest.main()
