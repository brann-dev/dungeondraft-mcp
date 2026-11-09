import unittest

from dungeondraft_mcp import cropping


W = cropping.window_woxels((2, 3, 10, 10))   # tiles 2..12 x 3..13


class Window(unittest.TestCase):
    def test_window_in_woxels(self):
        self.assertEqual(W, (512, 768, 3072, 3328))


class Points(unittest.TestCase):
    def test_all_inside(self):
        self.assertEqual(cropping.place_points([[600, 800], [700, 900]], False, W), "inside")

    def test_all_outside_and_not_touching(self):
        self.assertEqual(cropping.place_points([[0, 0], [100, 100]], False, W), "outside")

    def test_one_point_inside_is_crossing(self):
        self.assertEqual(cropping.place_points([[0, 0], [1000, 1000]], False, W), "crossing")

    def test_segment_passing_through_is_crossing(self):
        self.assertEqual(cropping.place_points([[0, 2000], [5000, 2000]], False, W), "crossing")

    def test_loop_surrounding_the_window_is_crossing(self):
        big = [[0, 0], [5000, 0], [5000, 5000], [0, 5000]]
        self.assertEqual(cropping.place_points(big, True, W), "crossing")

    def test_empty_points_count_as_inside(self):
        self.assertEqual(cropping.place_points([], False, W), "inside")


class Classify(unittest.TestCase):
    def snapshot(self):
        return {
            "objects": [
                {"id": 1, "polygon": [[600, 800], [700, 800], [700, 900], [600, 900]]},
                {"id": 2, "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]]},
                {"id": 3, "polygon": [[400, 700], [600, 700], [600, 900], [400, 900]]},
            ],
            "walls": [
                {"id": 10, "points": [[0, 0], [100, 0]], "loop": False, "asset": "res://textures/walls/wood.png"},
                {"id": 11, "points": [[600, 800], [5000, 800]], "loop": False},
                {"id": 12, "points": [[0, 0], [100, 0]], "loop": False, "asset": "res://textures/walls/caves/rock.png"},
            ],
            "portals": [
                {"id": 20, "wall_id": 10, "position": [50, 0]},
                {"id": 21, "wall_id": 11, "position": [1000, 800]},
                {"id": 22, "wall_id": 99, "position": [10, 10]},
            ],
        }

    def test_outside_crossing_and_cave_walls(self):
        found = cropping.classify(self.snapshot(), {"lights": [{"id": 30, "position": [5, 5]}, {"id": 31, "position": [1000, 1000]}]}, W)
        self.assertEqual(found["outside"], {"objects": [2], "walls": [10], "portals": [22], "lights": [30]})
        self.assertEqual(found["crossing"], {"objects": [3], "walls": [11]})

    def test_a_door_on_a_dropped_wall_is_not_listed_twice(self):
        found = cropping.classify(self.snapshot(), {}, W)
        self.assertNotIn(20, found["outside"].get("portals", []))


if __name__ == "__main__":
    unittest.main()
