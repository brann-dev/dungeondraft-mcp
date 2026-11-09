"""Offline tests for the layout checks: no Dungeondraft needed.

    .venv/bin/python -m unittest discover -s server/tests
"""

import math
import unittest

from dungeondraft_mcp import spatial


def box(cx, cy, w, h):
    return [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)]


def obj(id, cx, cy, w=256, h=256, layer=0, rotation=0.0, scale=(1.0, 1.0)):
    local = box(0, 0, w, h)
    return {
        "id": id, "asset": f"res://textures/objects/thing_{id}.png", "layer": layer,
        "position": [cx, cy], "scale": list(scale), "rotation": rotation, "local": local,
        "polygon": spatial.transform(local, (cx, cy), scale, rotation),
    }


def scene(**over):
    """A 20 x 20 tile map with one room: walls on x=1024..3072, y=1024..2560, a door on the top wall."""
    base = {
        "level_id": 0, "map_size": [5120, 5120], "regions": [], "unreadable": [],
        "objects": [],
        "walls": [{"id": 1, "loop": True, "half_width": 32,
                   "points": [[1024, 1024], [3072, 1024], [3072, 2560], [1024, 2560]]}],
        "portals": [{"id": 2, "wall_id": 1, "asset": "res://textures/portals/door_00.png",
                     "position": [2048, 1024], "direction": [1, 0], "radius": 128, "window": False}],
    }
    base.update(over)
    return base


def codes(conflicts):
    return sorted(c["code"] for c in conflicts)


class Geometry(unittest.TestCase):
    def test_overlap_area_of_offset_squares(self):
        self.assertAlmostEqual(spatial.overlap_area(box(0, 0, 2, 2), box(1, 0, 2, 2)), 2.0)

    def test_grid_aligned_squares_sharing_an_edge_do_not_overlap(self):
        self.assertAlmostEqual(spatial.overlap_area(box(0, 0, 2, 2), box(2, 0, 2, 2)), 0.0)

    def test_overlap_does_not_depend_on_winding(self):
        a, b = box(0, 0, 2, 2), box(1, 1, 2, 2)
        self.assertAlmostEqual(spatial.overlap_area(a, list(reversed(b))), 1.0)
        self.assertAlmostEqual(spatial.overlap_area(list(reversed(a)), b), 1.0)

    def test_concave_subject_is_clipped_piecewise(self):
        # A "U" 6 wide, 4 tall, with a 2-wide slot from the top down to y=1.
        u = [(0, 0), (6, 0), (6, 4), (4, 4), (4, 1), (2, 1), (2, 4), (0, 4)]
        self.assertAlmostEqual(spatial.area(u), 18.0)
        self.assertAlmostEqual(spatial.overlap_area(u, box(3, 3, 2, 2)), 0.0)    # sits in the slot
        self.assertAlmostEqual(spatial.overlap_area(u, box(3, 2, 6, 4)), 18.0)   # covers it all

    def test_rotated_square_overlap(self):
        diamond = spatial.transform(box(0, 0, 2, 2), (0, 0), (1, 1), 45)
        self.assertAlmostEqual(spatial.area(diamond), 4.0)
        self.assertAlmostEqual(spatial.overlap_area(diamond, box(0, 0, 2, 2)), 8 * (math.sqrt(2) - 1), places=6)

    def test_point_in_polygon_counts_the_outline(self):
        sq = box(0, 0, 2, 2)
        self.assertTrue(spatial.point_in_polygon((1, 0), sq))
        self.assertTrue(spatial.point_in_polygon((0.5, 0.5), sq))
        self.assertFalse(spatial.point_in_polygon((1.01, 0), sq))

    def test_distances(self):
        sq = box(0, 0, 2, 2)
        self.assertAlmostEqual(spatial.polygon_segment_distance(sq, (3, -5), (3, 5)), 2.0)
        self.assertEqual(spatial.polygon_segment_distance(sq, (-5, 0), (5, 0)), 0.0)    # passes through
        self.assertEqual(spatial.polygon_segment_distance(sq, (0, 0), (0.1, 0.1)), 0.0)  # fully inside
        self.assertAlmostEqual(spatial.polygon_distance(sq, box(4, 0, 2, 2)), 2.0)
        self.assertEqual(spatial.polygon_distance(sq, box(0, 0, 10, 10)), 0.0)           # contained
        self.assertAlmostEqual(spatial.polyline_polygon_distance([(5, 0)], sq), 4.0)
        self.assertEqual(spatial.polyline_polygon_distance([(0, 0)], sq), 0.0)

    def test_transform_scales_then_rotates_then_moves(self):
        pts = spatial.transform([(1, 0)], (10, 20), (2, 2), 90)
        self.assertAlmostEqual(pts[0][0], 10.0)
        self.assertAlmostEqual(pts[0][1], 22.0)


class Placement(unittest.TestCase):
    def test_open_floor_is_fine(self):
        self.assertEqual(spatial.check_footprint(scene(), box(2048, 2000, 256, 256)), [])

    def test_through_a_wall(self):
        self.assertEqual(codes(spatial.check_footprint(scene(), box(1024, 1800, 256, 256))), ["wall"])

    def test_flush_against_a_wall_face_is_fine(self):
        # Wall centerline x=1024, half width 32: the box's left edge sits exactly on the face.
        self.assertEqual(spatial.check_footprint(scene(), box(1056 + 128, 1800, 256, 256)), [])

    def test_wall_clearance_pushes_furniture_off_the_wall(self):
        flush = box(1056 + 128, 1800, 256, 256)
        self.assertEqual(codes(spatial.check_footprint(scene(), flush, wall_clearance=64)), ["wall"])

    def test_in_a_doorway(self):
        # Just inside the door on the top wall, clear of the wall itself.
        got = spatial.check_footprint(scene(), box(2048, 1024 + 32 + 60, 120, 120))
        self.assertEqual(codes(got), ["door"])
        self.assertEqual(got[0]["portal_id"], 2)

    def test_door_clearance_zero_turns_the_door_check_off(self):
        self.assertEqual(spatial.check_footprint(scene(), box(2048, 1116, 120, 120), door_clearance=0), [])

    def test_windows_are_not_doors(self):
        s = scene()
        s["portals"][0]["window"] = True
        self.assertEqual(spatial.check_footprint(s, box(2048, 1116, 120, 120)), [])

    def test_outside_the_map(self):
        self.assertEqual(codes(spatial.check_footprint(scene(), box(5100, 4000, 256, 256))), ["outside_map"])

    def test_objects_overlap_only_on_the_same_layer(self):
        s = scene(objects=[obj(10, 2048, 2000)])
        self.assertEqual(codes(spatial.check_footprint(s, box(2100, 2000, 256, 256))), ["object"])
        self.assertEqual(spatial.check_footprint(s, box(2100, 2000, 256, 256), layer=100), [])
        self.assertEqual(spatial.check_footprint(s, box(2100, 2000, 256, 256), allow_overlap=True), [])

    def test_touching_objects_are_fine_unless_clearance_is_asked_for(self):
        s = scene(objects=[obj(10, 2048, 2000)])
        beside = box(2048 + 256, 2000, 256, 256)
        self.assertEqual(spatial.check_footprint(s, beside), [])
        self.assertEqual(codes(spatial.check_footprint(s, beside, object_clearance=32)), ["object"])

    def test_an_object_does_not_block_itself(self):
        s = scene(objects=[obj(10, 2048, 2000)])
        self.assertEqual(spatial.check_footprint(s, box(2060, 2000, 256, 256), ignore_id=10), [])

    def test_clear_zone_and_room(self):
        s = scene(regions=[
            {"name": "aisle", "kind": "clearance", "points": box(2048, 1800, 300, 1400)},
            {"name": "hall", "kind": "room", "points": box(2048, 1792, 1984, 1472)},
        ])
        self.assertEqual(codes(spatial.check_footprint(s, box(2048, 2000, 256, 256))), ["clearance"])
        self.assertEqual(spatial.check_footprint(s, box(1400, 2000, 256, 256), region="hall"), [])
        self.assertEqual(codes(spatial.check_footprint(s, box(4000, 4000, 256, 256), region="hall")), ["outside_room"])
        with self.assertRaises(ValueError):
            spatial.check_footprint(s, box(1400, 2000, 256, 256), region="aisle")   # not a room

    def test_a_table_across_a_concave_rooms_notch_is_not_inside(self):
        # L-shaped room: the box's four corners are all in the L, its middle is not.
        l_room = [(0, 0), (1000, 0), (1000, 400), (400, 400), (400, 1000), (0, 1000)]
        s = scene(walls=[], portals=[], regions=[{"name": "L", "kind": "room", "points": l_room}])
        across = [(350, 100), (900, 350), (890, 372), (100, 900)]
        self.assertTrue(all(spatial.point_in_polygon(p, l_room) for p in across))
        self.assertEqual(codes(spatial.check_footprint(s, across, region="L")), ["outside_room"])


class Proposals(unittest.TestCase):
    def setUp(self):
        self.scene = scene(objects=[obj(10, 2000, 2000, w=200, h=100, layer=100, rotation=90)],
                           footprint={"asset": "x", "size": [300, 300], "visible": [50, 100, 200, 100],
                                      "local": box(0, 0, 200, 100)})

    def test_new_object_uses_the_assets_trimmed_box(self):
        p = spatial.proposed_footprint(self.scene, "place_object", {"asset": "x", "x": 500, "y": 600, "scale": 2, "layer": 200})
        self.assertEqual(spatial.bounds(p.polygon), (300.0, 500.0, 700.0, 700.0))
        self.assertEqual((p.layer, p.ignore_id), (200, None))

    def test_new_object_defaults_to_the_map_center(self):
        p = spatial.proposed_footprint(self.scene, "place_object", {"asset": "x"})
        self.assertEqual(spatial.centroid(p.polygon), (2560.0, 2560.0))

    def test_move_keeps_rotation_and_ignores_itself(self):
        p = spatial.proposed_footprint(self.scene, "move_element", {"id": 10, "x": 3000, "y": 3000})
        x0, y0, x1, y1 = spatial.bounds(p.polygon)
        self.assertAlmostEqual(x1 - x0, 100.0)    # rotated 90 degrees: 200 x 100 stands upright
        self.assertAlmostEqual(y1 - y0, 200.0)
        self.assertEqual((p.layer, p.ignore_id), (100, 10))

    def test_modify_rescales_and_turns(self):
        p = spatial.proposed_footprint(self.scene, "modify_object", {"id": 10, "scale": 2, "rotation": 0})
        x0, y0, x1, y1 = spatial.bounds(p.polygon)
        self.assertAlmostEqual(x1 - x0, 400.0)
        self.assertAlmostEqual(y1 - y0, 200.0)

    def test_duplicate_may_overlap_its_source(self):
        p = spatial.proposed_footprint(self.scene, "duplicate_object", {"id": 10, "dx": 10, "dy": 0})
        self.assertIsNone(p.ignore_id)
        self.assertEqual(codes(spatial.check_footprint(self.scene, p.polygon, layer=p.layer)), ["object"])

    def test_not_an_object(self):
        self.assertIsNone(spatial.proposed_footprint(self.scene, "move_element", {"id": 999, "x": 0, "y": 0}))


class Audit(unittest.TestCase):
    def test_reports_problems_and_overlaps_separately(self):
        s = scene(objects=[
            obj(10, 1024, 1800),          # through the left wall
            obj(11, 2048, 2000),
            obj(12, 2100, 2000),          # overlaps 11
            obj(13, 2100, 2000, layer=100),   # another layer: not an overlap
        ])
        report = spatial.audit(s)
        self.assertFalse(report["ok"])
        self.assertEqual([(p["id"], p["code"]) for p in report["problems"]], [(10, "wall")])
        self.assertEqual([o["ids"] for o in report["overlaps"]], [[11, 12]])
        self.assertEqual(report["counts"], {"wall": 1, "overlap": 1})

    def test_overlaps_alone_do_not_fail_the_audit(self):
        self.assertTrue(spatial.audit(scene(objects=[obj(11, 2048, 2000), obj(12, 2100, 2000)]))["ok"])

    def test_region_limits_the_audit(self):
        s = scene(objects=[obj(10, 1024, 1800), obj(11, 4000, 4000)],
                  regions=[{"name": "yard", "kind": "clearance", "points": box(4000, 4000, 1000, 1000)}])
        report = spatial.audit(s, region="yard")
        self.assertEqual(report["objects_checked"], 1)
        self.assertEqual([p["code"] for p in report["problems"]], ["clearance"])


class Protected(unittest.TestCase):
    regions = [{"name": "inn", "kind": "protected", "points": box(2000, 2000, 1000, 1000)},
               {"name": "aisle", "kind": "clearance", "points": box(0, 0, 100, 100)}]

    def test_polygon_edits(self):
        self.assertEqual(spatial.protected_hits(self.regions, polygon=box(2400, 2400, 100, 100)), ["inn"])
        self.assertEqual(spatial.protected_hits(self.regions, polygon=box(4000, 4000, 100, 100)), [])
        self.assertEqual(spatial.protected_hits(self.regions, polygon=box(2600, 2000, 190, 100)), [])
        self.assertEqual(spatial.protected_hits(self.regions, polygon=box(2600, 2000, 190, 100), radius=64), ["inn"])

    def test_brush_strokes(self):
        self.assertEqual(spatial.protected_hits(self.regions, path=[(1000, 2000), (1400, 2000)], radius=96), [])
        self.assertEqual(spatial.protected_hits(self.regions, path=[(1000, 2000), (1450, 2000)], radius=96), ["inn"])
        self.assertEqual(spatial.protected_hits(self.regions, path=[(2000, 2000)], radius=10), ["inn"])

    def test_whole_level_edits_hit_every_protected_region(self):
        self.assertEqual(spatial.protected_hits(self.regions, whole_level=True), ["inn"])
        self.assertEqual(spatial.protected_hits([self.regions[1]], whole_level=True), [])


if __name__ == "__main__":
    unittest.main()
