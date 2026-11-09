"""Offline tests for the Universal VTT reader: no Dungeondraft needed.

    .venv/bin/python -m unittest discover -s server/tests

The fixtures are built here in the shape Dungeondraft 1.2 writes (format
0.3), so no real export has to be checked in.
"""

import base64
import json
import math
import struct
import unittest
import zlib
from unittest import mock

from dungeondraft_mcp import uvtt_import as uvtt


def pt(x, y):
    return {"x": x, "y": y}


def png_bytes(width=640, height=512):
    """A PNG header with no pixel data: all the size reader looks at."""
    def chunk(kind, payload):
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")


def b64(data):
    return base64.b64encode(data).decode("ascii")


def export(**overrides):
    """A 10 x 8 tile export at 64 ppi: one room with a door on its top wall."""
    data = {
        "format": 0.3,
        "resolution": {"map_origin": pt(0, 0), "map_size": pt(10, 8), "pixels_per_grid": 64},
        # The room's wall, broken at the doorway 3.5..4.5 on y=2, as exported.
        "line_of_sight": [
            [pt(3.5, 2), pt(2, 2), pt(2, 6), pt(8, 6), pt(8, 2), pt(4.5, 2)],
        ],
        "objects_line_of_sight": [],
        "portals": [
            {
                "position": pt(4, 2),
                "bounds": [pt(3.5, 2), pt(4.5, 2)],
                "rotation": 0,
                "closed": True,
                "freestanding": False,
            }
        ],
        "environment": {"baked_lighting": True, "ambient_light": "ff203040"},
        "lights": [
            {"position": pt(5, 4), "range": 6, "intensity": 0.8, "color": "ffffb060", "shadows": True}
        ],
        "image": b64(png_bytes()),
    }
    data.update(overrides)
    return json.dumps(data).encode("utf-8")


def planned(**overrides):
    return uvtt.plan(uvtt.parse(export(**overrides)))


class Parsing(unittest.TestCase):
    def test_reads_the_export(self):
        parsed = uvtt.parse(export())
        self.assertEqual(parsed.tiles, (10, 8))
        self.assertEqual(parsed.pixels_per_grid, 64.0)
        self.assertEqual(parsed.format, 0.3)
        self.assertEqual(parsed.ambient_light, "#203040")
        self.assertTrue(parsed.baked_lighting)
        self.assertEqual(len(parsed.walls), 1)
        self.assertEqual(parsed.portals[0]["bounds"], ((3.5, 2.0), (4.5, 2.0)))
        self.assertEqual(parsed.lights[0]["color"], "#ffb060")
        self.assertTrue(parsed.image.startswith("iVBORw0K"))
        self.assertEqual(parsed.dropped, {})

    def test_a_fractional_region_needs_a_whole_tile(self):
        parsed = uvtt.parse(export(resolution={"map_size": pt(10.5, 7.2), "pixels_per_grid": 64}))
        self.assertEqual(parsed.tiles, (11, 8))
        self.assertEqual(parsed.origin, (0.0, 0.0))     # map_origin may be left out

    def test_not_json_is_refused(self):
        with self.assertRaisesRegex(uvtt.UvttError, "not valid JSON"):
            uvtt.parse(b"{not json")

    def test_json_without_a_resolution_is_refused(self):
        with self.assertRaisesRegex(uvtt.UvttError, "resolution"):
            uvtt.parse(json.dumps({"format": 0.3}).encode())
        with self.assertRaisesRegex(uvtt.UvttError, "resolution"):
            uvtt.parse(b"[1, 2, 3]")

    def test_bad_sizes_are_refused(self):
        with self.assertRaisesRegex(uvtt.UvttError, "positive"):
            uvtt.parse(export(resolution={"map_size": pt(0, 8), "pixels_per_grid": 64}))
        with self.assertRaisesRegex(uvtt.UvttError, "128"):
            uvtt.parse(export(resolution={"map_size": pt(500, 8), "pixels_per_grid": 64}))
        with self.assertRaisesRegex(uvtt.UvttError, "pixels_per_grid"):
            uvtt.parse(export(resolution={"map_size": pt(10, 8), "pixels_per_grid": "64"}))
        with self.assertRaisesRegex(uvtt.UvttError, "map_size"):
            uvtt.parse(export(resolution={"map_size": [10, 8], "pixels_per_grid": 64}))

    def test_absurd_counts_and_images_are_refused(self):
        with mock.patch.object(uvtt, "MAX_RUNS", 2):
            with self.assertRaisesRegex(uvtt.UvttError, "line_of_sight holds 3 runs"):
                uvtt.parse(export(line_of_sight=[[pt(0, 0), pt(1, 0)]] * 3))
        with mock.patch.object(uvtt, "MAX_RECORDS", 1):
            with self.assertRaisesRegex(uvtt.UvttError, "lights holds 2"):
                uvtt.parse(export(lights=[{"position": pt(1, 1)}, {"position": pt(2, 2)}]))
        with mock.patch.object(uvtt, "MAX_IMAGE_BYTES", 16):
            with self.assertRaisesRegex(uvtt.UvttError, "lower pixels-per-grid"):
                uvtt.parse(export())
        with mock.patch.object(uvtt, "MAX_FILE_BYTES", 100):
            with self.assertRaisesRegex(uvtt.UvttError, "this large"):
                uvtt.parse(export())

    def test_malformed_records_are_counted_not_fatal(self):
        parsed = uvtt.parse(export(
            portals=[{"closed": True}, "junk", {"position": pt(4, 2), "closed": False}],
            lights=[{"position": pt("x", 1)}, {"position": pt(5, 4), "range": "far", "color": 7}],
        ))
        self.assertEqual(parsed.dropped, {"portals": 2, "lights": 1})
        (portal,) = parsed.portals
        self.assertEqual((portal["bounds"], portal["rotation"], portal["freestanding"]), (None, 0.0, False))
        (light,) = parsed.lights
        self.assertEqual((light["range"], light["intensity"], light["color"], light["shadows"]),
                         (None, 1.0, None, True))

    def test_colours(self):
        self.assertEqual(uvtt.argb_to_hex("ffd9c3a0"), "#d9c3a0")
        self.assertEqual(uvtt.argb_to_hex("#D9C3A0"), "#d9c3a0")
        self.assertIsNone(uvtt.argb_to_hex("ff203"))
        self.assertIsNone(uvtt.argb_to_hex("gggggg"))
        self.assertIsNone(uvtt.argb_to_hex(None))


class Walls(unittest.TestCase):
    def test_the_doorway_is_rejoined_into_a_closed_wall(self):
        result = planned()
        self.assertEqual(result["summary"]["doorways_joined"], 1)
        (wall,) = result["walls"]
        self.assertTrue(wall["loop"])
        # The doorway edges were straight-line vertices and are gone: 4 corners.
        self.assertEqual(sorted(map(tuple, wall["points"])),
                         sorted([(512.0, 512.0), (512.0, 1536.0), (2048.0, 1536.0), (2048.0, 512.0)]))

    def test_two_doors_side_by_side_still_join_the_wall(self):
        """No wall run is left between adjacent doors; their doorways chain."""
        runs = [[(0, 0), (2, 0)], [(4, 0), (6, 0)]]
        portals = [
            {"position": (2.5, 0), "bounds": ((2, 0), (3, 0))},
            {"position": (3.5, 0), "bounds": ((3, 0), (4, 0))},
        ]
        (wall,) = uvtt.join_at_portals(runs, portals)
        self.assertEqual(wall.points, [(0, 0), (6, 0)])
        self.assertEqual((wall.loop, wall.doorways), (False, 2))

    def test_a_door_in_a_corner_keeps_the_corner(self):
        runs = [[(0, 4), (0, 1)], [(1, 0), (4, 0)]]
        portals = [{"position": (0.5, 0.5), "bounds": ((0, 1), (1, 0))}]
        (wall,) = uvtt.join_at_portals(runs, portals)
        self.assertEqual(wall.points, [(0, 4), (0, 1), (1, 0), (4, 0)])

    def test_a_t_junction_is_not_merged(self):
        runs = [[(0, 0), (2, 0)], [(2, 0), (4, 0)], [(2, 0), (2, 3)]]
        self.assertEqual(len(uvtt.join_at_portals(runs, [])), 3)

    def test_a_doorway_with_no_wall_is_dropped(self):
        self.assertEqual(uvtt.join_at_portals([], [{"position": (0.5, 0), "bounds": ((0, 0), (1, 0))}]), [])

    def test_a_freestanding_portal_joins_nothing(self):
        runs = [[(0, 0), (2, 0)], [(3, 0), (5, 0)]]
        portals = [{"position": (2.5, 0), "bounds": ((2, 0), (3, 0)), "freestanding": True}]
        self.assertEqual(len(uvtt.join_at_portals(runs, portals)), 2)

    def test_runs_meeting_within_tolerance_join(self):
        runs = [[(0, 0), (2, 0)], [(2.000001, 0.000001), (2, 3)]]
        (wall,) = uvtt.join_at_portals(runs, [])
        self.assertEqual(len(wall.points), 3)
        self.assertFalse(wall.loop)

    def test_a_u_shaped_wall_stays_open(self):
        result = planned(line_of_sight=[[pt(2, 2), pt(2, 6), pt(8, 6), pt(8, 2)]], portals=[])
        (wall,) = result["walls"]
        self.assertFalse(wall["loop"])
        self.assertEqual(len(wall["points"]), 4)

    def test_real_bends_survive_the_straightening(self):
        # A wall with a 0.5-tile jog: three real corners between the ends.
        result = planned(line_of_sight=[[pt(1, 1), pt(4, 1), pt(4, 1.5), pt(7, 1.5)]], portals=[])
        self.assertEqual(len(result["walls"][0]["points"]), 4)

    def test_wall_coordinates_are_woxels(self):
        result = planned(line_of_sight=[[pt(1, 1), pt(1.5, 1)]], portals=[])
        self.assertEqual(result["walls"][0]["points"], [[256.0, 256.0], [384.0, 256.0]])


class Portals(unittest.TestCase):
    def test_a_closed_portal_is_a_door_mounted_on_the_wall(self):
        (portal,) = planned()["portals"]
        self.assertEqual(portal["kind"], "door")
        self.assertTrue(portal["closed"])
        self.assertEqual((portal["x"], portal["y"]), (1024.0, 512.0))
        self.assertEqual((portal["width_woxels"], portal["radius"]), (256.0, 128.0))
        self.assertEqual(portal["mount"], "wall")
        self.assertEqual(portal["direction"], [1.0, 0.0])
        self.assertEqual(portal["rotation_degrees"], 0.0)

    def test_an_open_portal_is_a_window(self):
        portal = dict(json.loads(export())["portals"][0], closed=False)
        (window,) = planned(portals=[portal])["portals"]
        self.assertEqual((window["kind"], window["closed"]), ("window", False))

    def test_a_door_faces_the_way_the_export_says_whichever_way_its_wall_runs(self):
        """The bridge faces a mounted door along its wall segment; flip corrects it."""
        room = json.loads(export())
        forwards = planned()["portals"][0]
        reversed_wall = [list(reversed(room["line_of_sight"][0]))]
        backwards = planned(line_of_sight=reversed_wall)["portals"][0]
        self.assertNotEqual(forwards["flip"], backwards["flip"])
        # A door the export recorded as flipped (rotation + pi) flips the other way round.
        turned = dict(room["portals"][0], rotation=math.pi)
        self.assertEqual(planned(portals=[turned])["portals"][0]["flip"], not forwards["flip"])
        self.assertEqual(planned(portals=[turned])["portals"][0]["rotation_degrees"], 180.0)

    def test_a_freestanding_portal_keeps_its_rotation_in_degrees(self):
        portal = {"position": pt(6, 4), "bounds": [pt(6, 3.5), pt(6, 4.5)], "rotation": math.pi / 2,
                  "closed": True, "freestanding": True}
        (free,) = planned(portals=[portal])["portals"]
        self.assertEqual((free["mount"], free["flip"]), ("free", False))
        self.assertEqual(free["rotation_degrees"], 90.0)
        self.assertEqual(free["direction"], [0.0, 1.0])
        self.assertEqual(free["width_woxels"], 256.0)

    def test_a_portal_without_bounds_is_a_one_tile_door_facing_its_rotation(self):
        portal = {"position": pt(6, 4), "rotation": math.pi / 2, "closed": True, "freestanding": True}
        (free,) = planned(portals=[portal])["portals"]
        self.assertEqual((free["width_woxels"], free["radius"]), (256.0, 128.0))
        self.assertEqual(free["direction"], [0.0, 1.0])

    def test_a_mounted_portal_off_every_wall_is_reported(self):
        portal = {"position": pt(6, 4), "bounds": [pt(5.5, 4), pt(6.5, 4)], "rotation": 0,
                  "closed": True, "freestanding": False}
        result = planned(portals=[portal])
        self.assertEqual(result["portals"][0]["mount"], "wall")
        self.assertTrue(any("no planned wall" in note for note in result["notes"]))


class Lights(unittest.TestCase):
    def test_lights_are_converted_for_add_light(self):
        (light,) = planned()["lights"]
        # The export's range is the editor's Range, the unit add_light takes.
        self.assertEqual(light, {"x": 1280.0, "y": 1024.0, "range": 6.0, "energy": 0.8,
                                 "color": "#ffb060", "shadows": True})

    def test_missing_fields_take_add_lights_defaults(self):
        (light,) = planned(lights=[{"position": pt(5, 4)}])["lights"]
        self.assertEqual((light["range"], light["energy"], light["color"], light["shadows"]),
                         (2.0, 1.0, "", True))

    def test_the_ambient_light_is_a_level_colour(self):
        result = planned()
        self.assertEqual(result["ambient_light"], "#203040")
        self.assertEqual(result["summary"]["ambient_light"], "#203040")
        self.assertIsNone(planned(environment={"baked_lighting": True})["ambient_light"])


class Regions(unittest.TestCase):
    def test_a_region_export_is_moved_to_the_origin_and_cropped(self):
        """Region exports keep the source map's coordinates and list all of it."""
        result = planned(
            resolution={"map_origin": pt(20, 30), "map_size": pt(10, 8), "pixels_per_grid": 64},
            line_of_sight=[[pt(22, 32), pt(28, 32)], [pt(60, 70), pt(61, 70)]],
            portals=[],
            lights=[
                {"position": pt(25, 34), "range": 4, "intensity": 1, "color": "ffffffff"},
                {"position": pt(2, 2), "range": 4, "intensity": 1, "color": "ffffffff"},
            ],
            objects_line_of_sight=[[pt(21, 31), pt(22, 31), pt(22, 32), pt(21, 32)],
                                   [pt(90, 90), pt(91, 90), pt(91, 91)]],
        )
        self.assertEqual([w["points"] for w in result["walls"]], [[[512.0, 512.0], [2048.0, 512.0]]])
        self.assertEqual([(l["x"], l["y"]) for l in result["lights"]], [(1280.0, 1024.0)])
        self.assertEqual([(b["x"], b["y"]) for b in result["blocking_objects"]], [(384.0, 384.0)])
        self.assertEqual(result["map_origin"], [20.0, 30.0])
        self.assertEqual(result["summary"]["outside_region"], {"walls": 1, "lights": 1, "blocking_objects": 1})
        self.assertTrue(any("region export" in note for note in result["notes"]))
        self.assertTrue(any("left out" in note for note in result["notes"]))

    def test_a_wall_along_the_region_edge_is_kept(self):
        result = planned(line_of_sight=[[pt(0, 0), pt(10, 0)]], portals=[])
        self.assertEqual(len(result["walls"]), 1)
        self.assertEqual(result["summary"]["outside_region"], {})


class Blockers(unittest.TestCase):
    def test_a_blocker_is_fitted_to_its_outline(self):
        # A 2 x 1 tile rectangle centred on (3, 3), turned 30 degrees.
        turn = math.radians(30)
        corners = [(-1, -0.5), (1, -0.5), (1, 0.5), (-1, 0.5)]
        outline = [(3 + x * math.cos(turn) - y * math.sin(turn), 3 + x * math.sin(turn) + y * math.cos(turn))
                   for x, y in corners]
        fit = uvtt.fit_blocker(outline + outline[:1])
        self.assertEqual((fit["x"], fit["y"]), (768.0, 768.0))
        self.assertAlmostEqual(fit["rotation_degrees"], 30.0, delta=0.1)
        self.assertAlmostEqual(fit["length_woxels"], 512.0, delta=0.5)
        self.assertAlmostEqual(fit["width_woxels"], 256.0, delta=0.5)
        self.assertEqual(fit["points"], 4)

    def test_blockers_reach_the_plan(self):
        result = planned(objects_line_of_sight=[[pt(5, 5), pt(6, 5), pt(6, 6), pt(5, 6)]])
        (box,) = result["blocking_objects"]
        self.assertEqual((box["x"], box["y"], box["length_woxels"], box["width_woxels"]),
                         (1408.0, 1408.0, 256.0, 256.0))
        self.assertEqual(result["summary"]["blocking_objects"], 1)


class Trace(unittest.TestCase):
    def test_the_image_decodes_with_its_format_and_size(self):
        image = uvtt.decode_image(uvtt.parse(export()))
        self.assertEqual((image.format, image.suffix), ("png", ".png"))
        self.assertEqual((image.width, image.height), (640, 512))
        self.assertEqual(image.data, png_bytes())

    def test_the_trace_scale_covers_the_region(self):
        result = planned()
        # 64 pixels per tile: each pixel is 4 woxels; centred on the 10 x 8 region.
        self.assertEqual(result["trace"], {"scale": 4.0, "x": 1280.0, "y": 1024.0, "opacity": 0.5})
        self.assertTrue(result["summary"]["trace_image"])

    def test_an_export_without_an_image_has_no_trace(self):
        parsed = uvtt.parse(export(image=""))
        self.assertIsNone(uvtt.plan(parsed)["trace"])
        with self.assertRaisesRegex(uvtt.UvttError, "no image"):
            uvtt.decode_image(parsed)

    def test_broken_images_are_refused(self):
        with self.assertRaisesRegex(uvtt.UvttError, "base64"):
            uvtt.decode_image(uvtt.parse(export(image="not base64!!")))
        with self.assertRaisesRegex(uvtt.UvttError, "PNG, WEBP or JPEG"):
            uvtt.decode_image(uvtt.parse(export(image=b64(b"GIF89a....."))))
        with mock.patch.object(uvtt, "MAX_IMAGE_EDGE", 600):
            with self.assertRaisesRegex(uvtt.UvttError, "640 x 512"):
                uvtt.decode_image(uvtt.parse(export()))

    def test_webp_and_jpeg_headers_are_read(self):
        vp8x = (b"RIFF" + struct.pack("<I", 30) + b"WEBP" + b"VP8X" + struct.pack("<I", 10)
                + b"\x00\x00\x00\x00" + (639).to_bytes(3, "little") + (511).to_bytes(3, "little"))
        image = uvtt.decode_image(uvtt.parse(export(image=b64(vp8x))))
        self.assertEqual((image.format, image.width, image.height), ("webp", 640, 512))
        sof = b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"JF" + b"\xff\xc0" + struct.pack(">HBHH", 11, 8, 512, 640)
        image = uvtt.decode_image(uvtt.parse(export(image=b64(sof))))
        self.assertEqual((image.format, image.suffix, image.width, image.height), ("jpeg", ".jpg", 640, 512))
        # A JPEG whose header stops short still decodes, without a size.
        image = uvtt.decode_image(uvtt.parse(export(image=b64(b"\xff\xd8\xff\xe0"))))
        self.assertEqual((image.format, image.width), ("jpeg", None))


class MapSize(unittest.TestCase):
    def test_a_matching_map_needs_no_resize(self):
        result = uvtt.plan(uvtt.parse(export()), current_map_size_tiles=(10, 8))
        self.assertFalse(result["needs_resize"])
        self.assertEqual(result["current_map_size_tiles"], [10, 8])
        self.assertEqual(result["map_size_tiles"], [10, 8])
        self.assertEqual(result["notes"], [])

    def test_a_different_map_is_flagged(self):
        bigger = uvtt.plan(uvtt.parse(export()), current_map_size_tiles=[30.0, 20.0])
        self.assertTrue(bigger["needs_resize"])
        self.assertTrue(any("top-left corner" in note for note in bigger["notes"]))
        smaller = uvtt.plan(uvtt.parse(export()), current_map_size_tiles=[5, 5])
        self.assertTrue(smaller["needs_resize"])
        self.assertTrue(any("outside the map" in note for note in smaller["notes"]))

    def test_without_the_map_size_nothing_is_said(self):
        result = planned()
        self.assertIsNone(result["needs_resize"])
        self.assertIsNone(result["current_map_size_tiles"])


class Summary(unittest.TestCase):
    def test_counts_for_a_dry_run(self):
        result = planned()
        self.assertEqual(result["summary"], {
            "map_size_tiles": [10, 8], "walls": 1, "doorways_joined": 1, "portals": 1, "doors": 1,
            "windows": 0, "freestanding_portals": 0, "lights": 1, "blocking_objects": 0,
            "ambient_light": "#203040", "trace_image": True, "outside_region": {}, "malformed": {},
        })

    def test_the_plan_is_json(self):
        result = uvtt.plan(uvtt.parse(export(
            resolution={"map_origin": pt(1, 1), "map_size": pt(10, 8), "pixels_per_grid": 64},
            objects_line_of_sight=[[pt(5, 5), pt(6, 5), pt(6, 6)]],
        )), current_map_size_tiles=(10, 8))
        self.assertEqual(json.loads(json.dumps(result)), result)


if __name__ == "__main__":
    unittest.main()
