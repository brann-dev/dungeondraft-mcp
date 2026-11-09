"""Offline tests for the arrangement maths and build_room's shared walls: no Dungeondraft needed.

    .venv/bin/python -m unittest discover -s server/tests
"""

import math
import unittest

from dungeondraft_mcp import layout, spatial

# Drawn on 409 x 169 of a 512 x 256 canvas, half a pixel off center (the raw asset_footprint shape).
SHELF = {"asset": "res://packs/fa/textures/objects/Shelf_Wood_Light_C.webp", "size": [512, 256], "visible": [52, 44, 409, 169]}
# A chair drawn with its backrest at the top, as get_asset_footprint reports it.
CHAIR = {"asset": "res://textures/objects/chair.webp", "footprint": [132, 155], "center_offset": [0, 1.5]}
BOOK = {"asset": "res://textures/objects/book.png", "width": 116, "height": 98}

# A 9 x 6 tile room with its top-left corner at the origin.
ROOM = {"id": 7, "loop": True, "half_width": 32, "points": [[0, 0], [2304, 0], [2304, 1536], [0, 1536]]}
TOP = [[0, 0], [2304, 0]]
LEFT = [[0, 0], [0, 1536]]


def facing_direction(rotation):
    """Where a 'down'-facing asset's front points after `rotation` degrees."""
    theta = math.radians(rotation)
    return (-math.sin(theta), math.cos(theta))


class Footprints(unittest.TestCase):
    def test_every_reported_shape_gives_the_same_footprint(self):
        raw = layout.footprint(SHELF)
        self.assertEqual((raw.width, raw.height, raw.offset), (409.0, 169.0, (0.5, 0.5)))
        tool = layout.footprint({"asset": SHELF["asset"], "footprint": [409, 169], "center_offset": [0.5, 0.5]})
        plain = layout.footprint({"asset": SHELF["asset"], "width": 409, "height": 169, "offset": [0.5, 0.5]})
        snapshot = layout.footprint({"asset": SHELF["asset"], "local": raw.local()})
        self.assertEqual(raw, tool)
        self.assertEqual(raw, plain)
        self.assertEqual(raw, snapshot)

    def test_scale_grows_the_footprint_but_not_its_stored_size(self):
        fp = layout.footprint(BOOK, scale=2.0)
        self.assertEqual((fp.width, fp.w, fp.height, fp.h), (116.0, 232.0, 98.0, 196.0))
        self.assertEqual((fp.across("down"), fp.depth("down")), (232.0, 196.0))
        self.assertEqual((fp.across("left"), fp.depth("right")), (196.0, 232.0))

    def test_refuses_nothing_visible_rather_than_using_the_canvas(self):
        with self.assertRaises(ValueError):
            layout.footprint({"asset": "x.png", "size": [256, 256], "visible": [0, 0, 0, 10]})
        with self.assertRaises(ValueError):
            layout.footprint({"asset": "x.png", "size": [256, 256]})
        with self.assertRaises(ValueError):
            layout.footprint(BOOK, scale=0)

    def test_placement_puts_the_visible_center_where_asked_at_any_rotation(self):
        fp = layout.footprint(SHELF)
        for rotation in (0.0, 90.0, -90.0, 180.0, 33.0):
            placed = layout.placement(fp, 1000.0, 500.0, rotation)
            cx, cy = spatial.centroid(layout.placement_footprint(fp, placed))
            self.assertAlmostEqual(cx, 1000.0, delta=0.01)
            self.assertAlmostEqual(cy, 500.0, delta=0.01)
        # Art drawn half a pixel low: the sprite's center goes half a pixel up.
        self.assertAlmostEqual(layout.placement(fp, 1000.0, 500.0, 0.0)["y"], 499.5)
        self.assertEqual(sorted(layout.placement(fp, 0, 0, 0)), ["asset", "rotation", "scale", "x", "y"])

    def test_rotation_turns_the_front_to_the_direction(self):
        cases = [("down", (0, 1), 0.0), ("down", (1, 0), -90.0), ("down", (-1, 0), 90.0), ("down", (0, -1), 180.0),
                 ("up", (0, -1), 0.0), ("right", (0, 1), 90.0)]
        for facing, direction, rotation in cases:
            self.assertAlmostEqual(layout.rotation_to_face(facing, direction), rotation)
        # The same sense as spatial.transform: a 'down' front turned -90 points +x.
        got = spatial.transform([(0, 1)], (0, 0), (1, 1), layout.rotation_to_face("down", (1, 0)))[0]
        self.assertAlmostEqual(got[0], 1.0)
        self.assertAlmostEqual(got[1], 0.0)


class Walls(unittest.TestCase):
    def test_a_loop_contributes_its_closing_edge(self):
        segments = layout.wall_segments([{"id": 1, "loop": True, "points": [[0, 0], [1, 0], [1, 1]]},
                                         {"id": 2, "points": [[5, 5], [6, 6]]}])
        self.assertEqual([(s.wall_id, s.a, s.b) for s in segments],
                         [(1, (0, 0), (1, 0)), (1, (1, 0), (1, 1)), (1, (1, 1), (0, 0)), (2, (5, 5), (6, 6))])
        self.assertEqual(segments[-1].half_width, layout.DEFAULT_WALL_HALF_WIDTH)

    def test_choose_wall_segment_reports_a_double_wall(self):
        neighbour = {"id": 8, "loop": True, "points": [[2304, 0], [3304, 0], [3304, 1536], [2304, 1536]]}
        segment, others = layout.choose_wall_segment([ROOM, neighbour], [[2300, 200], [2300, 1300]])
        self.assertEqual((segment.wall_id, others), (7, [8]))
        segment, others = layout.choose_wall_segment([ROOM, neighbour], [[2300, 200], [2300, 1300]], wall_id=8)
        self.assertEqual((segment.wall_id, others), (8, [7]))

    def test_choose_wall_segment_refuses_a_line_far_from_any_wall_or_crossing_it(self):
        with self.assertRaisesRegex(ValueError, "no wall"):
            layout.choose_wall_segment([ROOM], [[500, 700], [1500, 700]])
        with self.assertRaisesRegex(ValueError, "no wall"):
            layout.choose_wall_segment([ROOM], [[100, 0], [100, 1400]])     # perpendicular to the top wall
        with self.assertRaisesRegex(ValueError, "wall 99"):
            layout.choose_wall_segment([ROOM], TOP, wall_id=99)

    def test_inward_normal_from_the_loop_or_from_toward(self):
        top, _ = layout.choose_wall_segment([ROOM], TOP)
        self.assertEqual(layout.inward_normal(top), (0.0, 1.0))
        left, _ = layout.choose_wall_segment([ROOM], LEFT)
        self.assertEqual(layout.inward_normal(left), (1.0, 0.0))
        open_run = {"id": 3, "points": [[0, 0], [1000, 0]]}
        segment, _ = layout.choose_wall_segment([open_run], [[0, 0], [1000, 0]])
        with self.assertRaisesRegex(ValueError, "toward"):
            layout.inward_normal(segment)
        self.assertEqual(layout.inward_normal(segment, [500, -300]), (0.0, -1.0))
        with self.assertRaises(ValueError):
            layout.inward_normal(segment, [500, 0])

    def test_free_stretches_clear_corners_and_this_walls_doors_only(self):
        top, _ = layout.choose_wall_segment([ROOM], TOP)
        door = {"id": 40, "wall_id": 7, "position": [1000, 0], "radius": 64}
        other = {"id": 41, "wall_id": 99, "position": [1500, 0], "radius": 64}
        far = {"id": 42, "wall_id": 7, "position": [1000, 1536], "radius": 64}
        window = {"id": 43, "wall_id": 7, "position": [1800, 0], "radius": 64, "window": True}
        stretches, kept = layout.free_stretches(top, TOP, [door, other, far, window], [ROOM], margin=32)
        self.assertEqual(kept, [40])
        self.assertEqual(stretches, [(64.0, 904.0), (1096.0, 2240.0)])     # corners: 32 half width + 32 margin

    def test_free_stretches_take_a_doors_width_when_it_has_no_radius(self):
        top, _ = layout.choose_wall_segment([ROOM], TOP)
        stretches, _ = layout.free_stretches(top, TOP, [{"id": 1, "position": [1000, 0], "width": 256}], [ROOM], margin=0)
        self.assertEqual(stretches, [(32.0, 872.0), (1128.0, 2272.0)])

    def test_free_stretches_keep_only_the_margin_at_a_free_end(self):
        wall = {"id": 3, "points": [[0, 0], [1000, 0]], "half_width": 32}
        segment, _ = layout.choose_wall_segment([wall], [[0, 0], [1000, 0]])
        stretches, _ = layout.free_stretches(segment, [[0, 0], [1000, 0]], [], [wall], margin=32)
        self.assertEqual(stretches, [(32.0, 968.0)])

    def test_free_stretches_block_a_partition_meeting_the_wall(self):
        partition = {"id": 9, "points": [[1152, 0], [1152, 1536]], "half_width": 32}
        top, _ = layout.choose_wall_segment([ROOM, partition], TOP)
        stretches, _ = layout.free_stretches(top, TOP, [], [ROOM, partition], margin=32)
        self.assertEqual(stretches, [(64.0, 1088.0), (1216.0, 2240.0)])

    def test_pack_row_spreads_or_centers_and_skips_what_does_not_fit(self):
        spread = layout.pack_row([(0.0, 1000.0)], [200.0, 200.0], gap=None, fill=False)
        self.assertEqual([round(s) for _, s in spread], [300, 700])
        centered = layout.pack_row([(0.0, 1000.0)], [200.0, 200.0], gap=0.0, fill=False)
        self.assertEqual([round(s) for _, s in centered], [400, 600])
        # The first stretch is too short for anything; both go to the second.
        split = layout.pack_row([(0.0, 100.0), (200.0, 700.0)], [200.0, 200.0], gap=0, fill=False)
        self.assertEqual([index for index, _ in split], [0, 1])
        self.assertTrue(all(s > 200 for _, s in split))
        filled = layout.pack_row([(0.0, 1000.0)], [300.0], gap=None, fill=True)
        self.assertEqual(len(filled), 3)


class AlongWall(unittest.TestCase):
    def test_backs_each_footprint_flush_against_the_wall_face(self):
        result = layout.arrange_along_wall([SHELF], [ROOM], TOP, count=3)
        self.assertEqual((result["skipped"], len(result["placements"]), result["wall"]["id"]), (0, 3, 7))
        for placed, fp in zip(result["placements"], result["footprints"]):
            self.assertEqual(placed["rotation"], 0.0)
            self.assertAlmostEqual(min(y for _, y in fp), 32.0, delta=0.06)   # flush to the face, not the canvas
        xs = [p["x"] for p in result["placements"]]
        self.assertEqual(xs, sorted(xs))
        self.assertEqual(result["notes"], [])

    def test_a_side_wall_turns_the_row_to_face_the_room(self):
        result = layout.arrange_along_wall([SHELF], [ROOM], LEFT, count=1, gap=0)
        placed, fp = result["placements"][0], result["footprints"][0]
        self.assertAlmostEqual(placed["rotation"], -90.0)
        self.assertAlmostEqual(min(x for x, _ in fp), 32.0, delta=0.06)
        self.assertEqual(result["wall"]["normal"], [1.0, 0.0])

    def test_wall_clearance_stands_the_row_off_the_face(self):
        result = layout.arrange_along_wall([SHELF], [ROOM], TOP, count=1, wall_clearance=20)
        self.assertAlmostEqual(min(y for _, y in result["footprints"][0]), 52.0, delta=0.06)

    def test_fills_the_stretch_when_no_count_is_given(self):
        result = layout.arrange_along_wall([SHELF], [ROOM], TOP)
        self.assertEqual(len(result["placements"]), 5)     # 2176 free woxels, 409 each
        self.assertEqual(result["wall"]["free"], [[64.0, 2240.0]])

    def test_the_row_passes_the_layout_checks(self):
        door = {"id": 40, "wall_id": 7, "asset": "res://textures/portals/door.png", "position": [1000, 0],
                "direction": [1, 0], "radius": 64, "window": False}
        scene = {"map_size": [4096, 4096], "regions": [], "unreadable": [], "objects": [],
                 "walls": [ROOM], "portals": [door]}
        result = layout.arrange_along_wall([SHELF, CHAIR], [ROOM], TOP, doors=[door], count=6)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["wall"]["doors_kept_clear"], [40])
        for fp in result["footprints"]:
            self.assertEqual(spatial.check_footprint(scene, fp, layer=100), [])
        for a, b in zip(result["footprints"], result["footprints"][1:]):
            self.assertLessEqual(spatial.overlap_area(a, b), spatial.MIN_OVERLAP)

    def test_assets_are_used_in_turn(self):
        result = layout.arrange_along_wall([SHELF, CHAIR], [ROOM], TOP, count=3)
        self.assertEqual([p["asset"] for p in result["placements"]], [SHELF["asset"], CHAIR["asset"], SHELF["asset"]])

    def test_reports_what_did_not_fit(self):
        wall = {"id": 1, "points": [[0, 0], [600, 0]], "half_width": 10}
        result = layout.arrange_along_wall([SHELF], [wall], [[0, 0], [600, 0]], toward=[300, 300], count=3, margin=0)
        self.assertEqual((len(result["placements"]), result["skipped"]), (1, 2))
        self.assertIn("2 of 3 did not fit", result["notes"][0])

    def test_notes_a_blocked_wall_and_a_double_wall(self):
        neighbour = {"id": 8, "loop": True, "half_width": 32, "points": [[2304, 0], [3304, 0], [3304, 1536], [2304, 1536]]}
        result = layout.arrange_along_wall([SHELF], [ROOM, neighbour], [[2304, 100], [2304, 1400]], count=1)
        self.assertTrue(any("double wall" in n for n in result["notes"]))
        # A 100-woxel edge of a loop: its two corners (32 half width + 32 margin each) eat it all.
        closet = {"id": 1, "loop": True, "half_width": 32, "points": [[0, 0], [100, 0], [100, 500], [0, 500]]}
        result = layout.arrange_along_wall([SHELF], [closet], [[0, 0], [100, 0]], count=1)
        self.assertEqual((result["placements"], result["wall"]["free"]), ([], []))
        self.assertTrue(any("no free stretch" in n for n in result["notes"]))

    def test_notes_an_asset_that_looks_drawn_side_on(self):
        tall = {"asset": "rack.png", "width": 100, "height": 300}
        self.assertTrue(any("side-on" in n for n in layout.arrange_along_wall([tall], [ROOM], TOP, count=1)["notes"]))
        self.assertEqual(layout.arrange_along_wall([tall], [ROOM], TOP, count=1, facing="left")["notes"], [])

    def test_refuses_bad_input_before_planning(self):
        with self.assertRaisesRegex(ValueError, "toward"):
            layout.arrange_along_wall([SHELF], [{"id": 3, "points": [[0, 0], [1000, 0]]}], [[0, 0], [1000, 0]])
        with self.assertRaises(ValueError):
            layout.arrange_along_wall([SHELF], [ROOM], TOP, facing="sideways")
        with self.assertRaises(ValueError):
            layout.arrange_along_wall([SHELF], [ROOM], TOP, count=0)
        with self.assertRaises(ValueError):
            layout.arrange_along_wall([], [ROOM], TOP)


class AroundTarget(unittest.TestCase):
    TABLE = {"asset": "res://textures/objects/Table_Rectangle_H.webp", "x": 1000, "y": 1000, "width": 390, "height": 203}

    def test_target_box_from_a_quad_or_a_center(self):
        box = layout.target_box(self.TABLE)
        self.assertEqual((box.cx, box.cy, box.half_w, box.half_h, box.rotation), (1000, 1000, 195, 101.5, 0))
        turned = layout.target_box({**self.TABLE, "rotation": 30})
        from_quad = layout.target_box({"polygon": turned.corners()})
        for got, want in zip((from_quad.cx, from_quad.cy, from_quad.half_w, from_quad.half_h, from_quad.rotation),
                             (1000, 1000, 195, 101.5, 30)):
            self.assertAlmostEqual(got, want, places=6)
        snapshot_like = layout.target_box({"position": [10, 20], "size": [100, 50], "rotation": 5})
        self.assertEqual((snapshot_like.cx, snapshot_like.half_w, snapshot_like.rotation), (10, 50, 5))
        with self.assertRaises(ValueError):
            layout.target_box({"x": 1, "y": 2})
        with self.assertRaises(ValueError):
            layout.target_box({"polygon": [[0, 0], [1, 0], [1, 1]]})

    def test_a_rectangular_table_seats_by_side_length_and_faces_it(self):
        result = layout.arrange_around_target([CHAIR], self.TABLE, count=6, clearance=0, jitter=False)
        self.assertEqual((result["skipped"], len(result["placements"]), result["shape"]), (0, 6, "rect"))
        table = layout.target_box(self.TABLE)
        centers = [spatial.centroid(fp) for fp in result["footprints"]]
        self.assertEqual(sum(1 for _, cy in centers if cy < 1000 - 101.5), 2)    # two a side, one at each end
        for (cx, cy), placed, fp in zip(centers, result["placements"], result["footprints"]):
            front = facing_direction(placed["rotation"])
            self.assertGreater(front[0] * (1000 - cx) + front[1] * (1000 - cy), 0)   # faces the table
            self.assertLessEqual(spatial.overlap_area(table.corners(), fp), spatial.MIN_OVERLAP)   # not on it
        self.assertEqual(result["notes"], [])

    def test_tucked_seats_overlap_the_table_and_say_so(self):
        result = layout.arrange_around_target([CHAIR], self.TABLE, count=4, jitter=False)
        table = layout.target_box(self.TABLE).corners()
        self.assertTrue(all(spatial.overlap_area(table, fp) > spatial.MIN_OVERLAP for fp in result["footprints"]))
        self.assertTrue(any("allow_overlap" in n for n in result["notes"]))

    def test_an_end_narrower_than_a_seat_still_takes_one(self):
        armchair = {"asset": "armchair.png", "width": 216, "height": 191}
        result = layout.arrange_around_target([armchair], self.TABLE, count=4, clearance=0, jitter=False)
        self.assertEqual((len(result["placements"]), result["skipped"]), (4, 0))

    def test_more_seats_than_the_table_holds_are_reported(self):
        result = layout.arrange_around_target([CHAIR], self.TABLE, count=12, clearance=0, jitter=False)
        self.assertGreater(result["skipped"], 0)
        self.assertEqual(len(result["placements"]) + result["skipped"], 12)
        self.assertTrue(any("did not fit" in n for n in result["notes"]))

    def test_a_round_table_rings_evenly_and_jitter_stays_small(self):
        table = {"asset": "res://x/round_table_01.png", "x": 0, "y": 0, "width": 266, "height": 266}
        plain = layout.arrange_around_target([CHAIR], table, count=5, clearance=0, jitter=False, seed=1)
        jittered = layout.arrange_around_target([CHAIR], table, count=5, clearance=0, jitter=True, seed=1)
        self.assertEqual(plain["shape"], "round")
        self.assertAlmostEqual(plain["placements"][0]["rotation"], 0.0)      # the first seat, at the top, faces down
        for a, b in zip(plain["placements"], jittered["placements"]):
            self.assertLessEqual(abs(a["rotation"] - b["rotation"]), layout.SEAT_TURN_JITTER + 0.01)
            self.assertLessEqual(math.hypot(a["x"] - b["x"], a["y"] - b["y"]), 2 * layout.SEAT_SHIFT_JITTER)
        radii = [math.hypot(*spatial.centroid(fp)) for fp in plain["footprints"]]
        self.assertAlmostEqual(max(radii) - min(radii), 0.0, delta=0.05)

    def test_a_turned_table_turns_its_seats(self):
        result = layout.arrange_around_target([CHAIR], {**self.TABLE, "rotation": 90}, count=2, clearance=0, jitter=False)
        centers = [spatial.centroid(fp) for fp in result["footprints"]]
        # The long sides now run up and down the screen: the seats sit left and right of the table.
        self.assertTrue(all(abs(cy - 1000) < 1 and abs(cx - 1000) > 101.5 for cx, cy in centers))

    def test_the_seed_reproduces_the_jitter(self):
        one = layout.arrange_around_target([CHAIR], self.TABLE, count=4, seed=7)
        two = layout.arrange_around_target([CHAIR], self.TABLE, count=4, seed=7)
        other = layout.arrange_around_target([CHAIR], self.TABLE, count=4, seed=8)
        self.assertEqual(one, two)
        self.assertEqual(one["seed"], 7)
        self.assertNotEqual(one["placements"], other["placements"])
        self.assertIsInstance(layout.arrange_around_target([CHAIR], self.TABLE)["seed"], int)

    def test_table_shape_comes_from_the_name(self):
        self.assertEqual(layout.table_shape("res://x/round_table_01.png"), "round")
        self.assertEqual(layout.table_shape("res://x/Table_Circular.webp"), "round")
        self.assertEqual(layout.table_shape("res://x/Table_Rectangle_H.webp"), "rect")
        forced = layout.arrange_around_target([CHAIR], self.TABLE, count=4, shape="round", jitter=False)
        self.assertEqual(forced["shape"], "round")
        with self.assertRaises(ValueError):
            layout.arrange_around_target([CHAIR], self.TABLE, shape="oval")


class OnSurface(unittest.TestCase):
    DESK = {"asset": "desk.png", "x": 500, "y": 500, "width": 394, "height": 203, "rotation": 20, "layer": 100}

    def test_items_stay_inside_and_apart(self):
        result = layout.arrange_on_surface([BOOK], self.DESK, count=2, margin=12, seed=3)
        self.assertEqual((result["skipped"], len(result["placements"])), (0, 2))
        desk = layout.target_box(self.DESK).corners()
        for fp in result["footprints"]:
            self.assertTrue(all(spatial.point_in_polygon(corner, desk) for corner in fp))
        for i, a in enumerate(result["footprints"]):
            for b in result["footprints"][i + 1:]:
                self.assertLessEqual(spatial.overlap_area(a, b), spatial.MIN_OVERLAP)
        for placed in result["placements"]:
            self.assertLessEqual(abs(placed["rotation"] - 20), layout.SURFACE_TURN + 0.01)
        self.assertTrue(any("layer 100" in n for n in result["notes"]))

    def test_reports_items_with_no_room(self):
        tiny = {"x": 0, "y": 0, "width": 140, "height": 120}
        result = layout.arrange_on_surface([BOOK], tiny, count=3, margin=4, seed=3)
        self.assertEqual((len(result["placements"]), result["skipped"]), (1, 2))
        self.assertTrue(any("no room" in n for n in result["notes"]))
        with self.assertRaisesRegex(ValueError, "too small"):
            layout.arrange_on_surface([BOOK], tiny, count=1, margin=80)

    def test_the_seed_reproduces_the_scatter(self):
        one = layout.arrange_on_surface([BOOK], self.DESK, seed=5)
        self.assertEqual(one, layout.arrange_on_surface([BOOK], self.DESK, seed=5))
        self.assertNotEqual(one["placements"], layout.arrange_on_surface([BOOK], self.DESK, seed=6)["placements"])


# --------------------------------------------------------------------------
# Shared walls
# --------------------------------------------------------------------------

A = [[0, 0], [512, 0], [512, 512], [0, 512]]
B = [[512, 0], [1024, 0], [1024, 512], [512, 512]]


def loop(points, wall_id=7, **extra):
    return {"id": wall_id, "loop": True, "points": points, "type": 0, **extra}


def unordered(edge):
    return tuple(sorted(tuple(p) for p in edge))


class SharedWalls(unittest.TestCase):
    def test_a_room_touching_no_wall_stays_one_loop(self):
        result = layout.shared_walls(B, [{"id": 1, "type": 0, "points": [[2000, 0], [3000, 0]]}])
        self.assertEqual(result["runs"], [B])
        self.assertTrue(result["closed"])
        self.assertFalse(result["enclosed"])
        self.assertEqual((result["shared_wall_ids"], result["shared_edges"], result["notes"]), ([], [], []))

    def test_the_edge_two_rooms_share_is_left_out(self):
        result = layout.shared_walls(B, [loop(A)])
        self.assertEqual([unordered(e) for e in result["shared_edges"]], [((512, 0), (512, 512))])
        self.assertEqual(result["runs"], [[[512, 0], [1024, 0], [1024, 512], [512, 512]]])
        self.assertEqual(result["shared_wall_ids"], [7])
        self.assertFalse(result["closed"])
        self.assertFalse(result["enclosed"])
        self.assertIn("1 open run", result["notes"][0])

    def test_a_partly_shared_edge_keeps_its_uncovered_part(self):
        tall = [[512, 0], [1024, 0], [1024, 1024], [512, 1024]]
        result = layout.shared_walls(tall, [loop(A)])
        self.assertEqual([unordered(e) for e in result["shared_edges"]], [((512, 0), (512, 512))])
        self.assertEqual(result["runs"], [[[512, 0], [1024, 0], [1024, 1024], [512, 1024], [512, 512]]])

    def test_a_shared_edge_in_the_middle_joins_the_runs_around_the_corner(self):
        left = [[-512, 0], [0, 0], [0, 512], [-512, 512]]
        result = layout.shared_walls(left, [loop(A)])
        self.assertEqual([unordered(e) for e in result["shared_edges"]], [((0, 0), (0, 512))])
        self.assertEqual(result["runs"], [[[0, 512], [-512, 512], [-512, 0], [0, 0]]])

    def test_a_wall_covering_the_middle_of_an_edge_leaves_two_runs(self):
        stub = {"id": 3, "type": 0, "points": [[600, 0], [900, 0]]}
        result = layout.shared_walls(B, [stub])
        self.assertEqual(result["runs"], [[[900, 0], [1024, 0], [1024, 512], [512, 512], [512, 0], [600, 0]]])
        self.assertEqual(result["shared_edges"], [[[600, 0], [900, 0]]])

    def test_two_walls_covering_one_edge_are_both_shared(self):
        halves = [{"id": 3, "type": 0, "points": [[512, 0], [512, 256]]}, {"id": 4, "type": 0, "points": [[512, 256], [512, 512]]}]
        result = layout.shared_walls(B, halves)
        self.assertEqual(result["shared_wall_ids"], [3, 4])
        self.assertEqual(len(result["shared_edges"]), 1)

    def test_a_wall_within_a_woxel_counts_as_shared(self):
        result = layout.shared_walls(B, [{"id": 1, "type": 0, "points": [[512.5, -1], [512.5, 600]]}])
        self.assertEqual(len(result["shared_edges"]), 1)

    def test_a_parallel_wall_two_woxels_away_is_not_shared(self):
        result = layout.shared_walls(B, [{"id": 1, "type": 0, "points": [[510, 0], [510, 512]]}])
        self.assertEqual(result["shared_edges"], [])
        self.assertTrue(result["closed"])
        wider = layout.shared_walls(B, [{"id": 1, "type": 0, "points": [[510, 0], [510, 512]]}], tolerance=4)
        self.assertEqual(len(wider["shared_edges"]), 1)

    def test_a_boundary_already_walled_all_round_needs_only_a_floor(self):
        result = layout.shared_walls(A, [loop(A)])
        self.assertEqual(result["runs"], [])
        self.assertTrue(result["enclosed"])
        self.assertFalse(result["closed"])
        self.assertEqual(len(result["shared_edges"]), 4)
        self.assertIn("only a floor", result["notes"][0])

    def test_a_wall_of_another_type_is_not_shared(self):
        cave = layout.shared_walls(B, [loop(A, type=2)])
        self.assertTrue(cave["closed"])
        self.assertEqual(cave["shared_wall_ids"], [])
        manual = layout.shared_walls(B, [loop(A, type=1)], wall_type=1)
        self.assertEqual(manual["shared_wall_ids"], [7])
        # The key battlemap-mcp's walls carry is accepted too.
        other_key = {"id": 7, "loop": True, "points": A, "wall_type": 1}
        self.assertEqual(layout.shared_walls(B, [other_key], wall_type=1)["shared_wall_ids"], [7])

    def test_a_wall_of_another_asset_is_not_shared_when_the_asset_is_named(self):
        stone = loop(A, asset="res://walls/stone.png")
        self.assertTrue(layout.shared_walls(B, [stone], wall_asset="res://walls/wood.png")["closed"])
        self.assertEqual(layout.shared_walls(B, [stone], wall_asset="res://walls/stone.png")["shared_wall_ids"], [7])
        self.assertEqual(layout.shared_walls(B, [stone])["shared_wall_ids"], [7])     # no asset named: any matches

    def test_a_wall_without_a_type_is_left_alone_and_noted(self):
        result = layout.shared_walls(B, [{"id": 7, "loop": True, "points": A}])
        self.assertTrue(result["closed"])
        self.assertEqual(len(result["notes"]), 1)
        self.assertIn("no type", result["notes"][0])

    def test_refuses_a_degenerate_boundary(self):
        with self.assertRaises(ValueError):
            layout.shared_walls([[0, 0], [1, 1]], [])
        with self.assertRaises(ValueError):
            layout.shared_walls(A, [], tolerance=0)


class Serialisation(unittest.TestCase):
    def test_every_result_is_plain_json(self):
        import json
        table = {"asset": "table.png", "x": 1000, "y": 1000, "width": 390, "height": 203, "layer": 100}
        results = [
            layout.arrange_along_wall([SHELF], [ROOM], TOP, doors=[{"id": 1, "position": [1000, 0], "radius": 64}]),
            layout.arrange_around_target([CHAIR], table, seed=1),
            layout.arrange_on_surface([BOOK], table, seed=1),
            layout.shared_walls(B, [loop(A)]),
            layout.shared_walls(A, []),
        ]
        for result in results:
            self.assertEqual(json.loads(json.dumps(result)), json.loads(json.dumps(result)))
            self.assertIsInstance(result["notes"], list)


if __name__ == "__main__":
    unittest.main()
