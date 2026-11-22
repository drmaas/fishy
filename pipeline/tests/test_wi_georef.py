# SPDX-License-Identifier: GPL-3.0-only
import json
import math
import subprocess
import tempfile
import unittest
from pathlib import Path

from fishy_pipeline.states.wi import georef, synthetic
from fishy_pipeline.states.wi.hydro import load_hydro_polygons
from fishy_pipeline.states.wi.pngutil import RasterImage, read_png, write_png
from fishy_pipeline.states.wi.register import fit_affine, polygon_iou, register_shoreline
from fishy_pipeline.states.wi.shoreline import extract_shoreline

ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = ROOT / "fixtures" / "wi-georef" / "known-transform.json"


def _by_pair(results: dict) -> dict[tuple[str, str], dict]:
    return {(item["wbic"], item["map_key"]): item for item in results["items"]}


class PngTest(unittest.TestCase):
    def test_roundtrip(self):
        image = RasterImage(width=2, height=2, rgb=bytes([0, 0, 0, 255, 0, 0, 0, 255, 0, 255, 255, 255]))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tiny.png"
            write_png(path, image)
            self.assertEqual(read_png(path), image)


class RegisterMathTest(unittest.TestCase):
    def test_identical_polygons_have_iou_one(self):
        square = [(0.0, 0.0), (10.0, 0.0), (10.0, 4.0), (0.0, 4.0)]
        self.assertEqual(polygon_iou(square, list(square)), 1.0)

    def test_affine_recovers_known_pairs(self):
        spec = synthetic.load_spec(SPEC_PATH)
        expected = synthetic.affine_from_spec(spec)
        pixels = [(float(x), float(y)) for x, y in spec["pixel_ring"]]
        mapped = [expected.apply(x, y) for x, y in pixels]
        fit = fit_affine(pixels, mapped)
        for (col, row), (easting, northing) in zip(pixels, mapped, strict=True):
            got_e, got_n = fit.apply(col, row)
            self.assertAlmostEqual(got_e, easting, places=6)
            self.assertAlmostEqual(got_n, northing, places=6)

    def test_point_rings_recover_transform(self):
        spec = synthetic.load_spec(SPEC_PATH)
        expected = synthetic.affine_from_spec(spec)
        pixels = [(float(x), float(y)) for x, y in spec["pixel_ring"]]
        hydro = [expected.apply(x, y) for x, y in pixels]
        registration = register_shoreline(pixels, hydro, gcp_count=8)
        self.assertGreaterEqual(registration.iou, 0.90)
        scale = float(spec["scale_m_per_px"])
        for col, row in pixels:
            easting, northing = expected.apply(col, row)
            got_e, got_n = registration.affine.apply(col, row)
            self.assertLess(math.hypot(got_e - easting, got_n - northing), 2.0 * scale)


class ShorelineTest(unittest.TestCase):
    def test_ignores_border_grid_and_legend(self):
        spec = synthetic.load_spec(SPEC_PATH)
        image = synthetic.render_map(spec)
        contour = extract_shoreline(image)
        self.assertIsNotNone(contour)
        assert contour is not None
        cx = sum(point[0] for point in contour) / len(contour)
        cy = sum(point[1] for point in contour) / len(contour)
        ring = spec["pixel_ring"]
        lake_x = sum(point[0] for point in ring) / len(ring)
        lake_y = sum(point[1] for point in ring) / len(ring)
        self.assertLess(math.hypot(cx - lake_x, cy - lake_y), 8.0)
        legend_x = spec["legend"][0] + spec["legend"][2] / 2
        legend_y = spec["legend"][1] + spec["legend"][3] / 2
        self.assertGreater(math.hypot(cx - legend_x, cy - legend_y), 30.0)

    def test_blank_image_has_no_shoreline(self):
        image = RasterImage(width=32, height=32, rgb=bytes([255]) * (32 * 32 * 3))
        self.assertIsNone(extract_shoreline(image))


class HydroPolygonTest(unittest.TestCase):
    def test_rejects_non_3071(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hydro.geojson"
            path.write_text(
                '{"type":"FeatureCollection","crs":{"type":"name","properties":{"name":"EPSG:4326"}},'
                '"features":[]}',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_hydro_polygons(path)

    def test_keeps_largest_ring_for_one_wbic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hydro.geojson"
            path.write_text(
                json.dumps(
                    {
                        "type": "FeatureCollection",
                        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3071"}},
                        "features": [
                            {
                                "type": "Feature",
                                "properties": {"WATERBODY_WBIC": 417300, "HYDROTYPE": 706},
                                "geometry": {
                                    "type": "Polygon",
                                    "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                                },
                            },
                            {
                                "type": "Feature",
                                "properties": {"WATERBODY_WBIC": "0417300", "HYDROTYPE": 706},
                                "geometry": {
                                    "type": "Polygon",
                                    "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            index = load_hydro_polygons(path)
            self.assertEqual(set(index), {"0417300"})
            self.assertGreater(index["0417300"].ring[1][0], 5)


class GdalCommandTest(unittest.TestCase):
    def test_tps_warp_targets_3071(self):
        gcps = [
            {"pixel": 0, "line": 0, "easting": 545000.0, "northing": 511000.0},
            {"pixel": 10, "line": 0, "easting": 545100.0, "northing": 511000.0},
            {"pixel": 10, "line": 8, "easting": 545100.0, "northing": 510900.0},
            {"pixel": 0, "line": 8, "easting": 545000.0, "northing": 510900.0},
        ]
        translate = georef.gdal_translate_gcp_args("in.png", "gcps.vrt", gcps)
        warp = georef.gdalwarp_tps_args("gcps.vrt", "warped.tif")
        self.assertEqual(translate[:5], ["gdal_translate", "-of", "VRT", "-a_srs", "EPSG:3071"])
        self.assertIn("-gcp", translate)
        self.assertEqual(warp[0], "gdalwarp")
        self.assertIn("-tps", warp)
        self.assertEqual(warp[warp.index("-t_srs") + 1], "EPSG:3071")


class FixtureGeorefTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.work = Path(cls.tmp.name)
        manifest_path, hydro_path = synthetic.materialize_fixture(
            cls.work / "fixture-input",
            synthetic.load_spec(SPEC_PATH),
        )
        cls.results = georef.run_georef(
            manifest_path=manifest_path,
            hydro_path=hydro_path,
            work_dir=cls.work,
        )
        cls.by_pair = _by_pair(cls.results)
        queue_path = cls.work / "georef" / "review_queue.json"
        cls.queue = json.loads(queue_path.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_shared_map_key_is_not_a_primary_key(self):
        shared = [item for item in self.results["items"] if item["map_key"] == "0417300a"]
        self.assertEqual({item["wbic"] for item in shared}, {"0417300", "0261100"})
        self.assertEqual(len(self.results["items"]), 7)

    def test_known_raster_recovers_transform_above_threshold(self):
        spec = synthetic.load_spec(SPEC_PATH)
        expected = synthetic.affine_from_spec(spec)
        item = self.by_pair[("0417300", "0417300a")]
        self.assertEqual(item["qa_status"], "auto")
        self.assertGreaterEqual(item["iou"], 0.90)
        self.assertEqual(item["reasons"], [])
        self.assertNotIn(("0417300", "0417300a"), {(row["wbic"], row["map_key"]) for row in self.queue["items"]})
        scale = spec["scale_m_per_px"]
        for col, row in spec["pixel_ring"]:
            easting, northing = expected.apply(float(col), float(row))
            got = item["affine"]
            got_e = got["a"] * col + got["b"] * row + got["tx"]
            got_n = got["c"] * col + got["d"] * row + got["ty"]
            self.assertLess(
                math.hypot(got_e - easting, got_n - northing),
                4.0 * scale,
                f"vertex {(col, row)} off by more than 4 px",
            )

    def test_low_iou_shared_map_is_queued(self):
        item = self.by_pair[("0261100", "0417300a")]
        self.assertEqual(item["qa_status"], "needs_review")
        self.assertIn("iou_below_threshold", item["reasons"])
        self.assertLess(item["iou"], 0.90)
        queued = next(row for row in self.queue["items"] if row["wbic"] == "0261100")
        self.assertEqual(queued["map_key"], "0417300a")
        self.assertIn("iou_below_threshold", queued["reasons"])
        self.assertTrue(queued["gcp_path"].endswith("0261100/0417300a/gcps.json"))

    def test_flowage_is_queued_with_a_good_fit(self):
        item = self.by_pair[("1403200", "1403200a")]
        self.assertGreaterEqual(item["iou"], 0.90)
        self.assertIn("flowage", item["reasons"])
        self.assertEqual(item["qa_status"], "needs_review")
        self.assertEqual(item["hydrotype"], 707)

    def test_multi_sheet_is_queued_even_when_iou_is_high(self):
        for map_key in ("2271600a", "2271600b"):
            item = self.by_pair[("2271600", map_key)]
            self.assertGreaterEqual(item["iou"], 0.90)
            self.assertIn("multi_sheet", item["reasons"])
            self.assertEqual(item["qa_status"], "needs_review")
        queued = [row for row in self.queue["items"] if row["wbic"] == "2271600"]
        self.assertEqual({row["map_key"] for row in queued}, {"2271600a", "2271600b"})

    def test_skipped_sources_keep_their_reason(self):
        shapefile = self.by_pair[("0850800", "0850800b")]
        unknown = self.by_pair[("3000001", "3000001a")]
        self.assertEqual(shapefile["qa_status"], "skipped")
        self.assertEqual(shapefile["skip_reason"], "unsupported_media_type: shapefile")
        self.assertEqual(unknown["qa_status"], "skipped")
        self.assertEqual(unknown["skip_reason"], "unsupported_media_type: unknown")
        queued = {(row["wbic"], row["map_key"]) for row in self.queue["items"]}
        self.assertNotIn(("0850800", "0850800b"), queued)
        self.assertNotIn(("3000001", "3000001a"), queued)

    @unittest.skipUnless(georef.gdal_tools_available(), "gdal")
    def test_gdalwarp_tps_writes_3071_geotiff(self):
        item = self.by_pair[("0417300", "0417300a")]
        self.assertEqual(item["warp"]["status"], "ok")
        warped = Path(item["paths"]["warped"])
        self.assertTrue(warped.is_file())
        info = json.loads(subprocess.check_output(["gdalinfo", "-json", str(warped)], text=True))
        wkt = json.dumps(info.get("coordinateSystem", {}))
        self.assertIn("3071", wkt)
        spec = synthetic.load_spec(SPEC_PATH)
        expected = synthetic.affine_from_spec(spec)
        eastings = []
        northings = []
        for col, row in spec["pixel_ring"]:
            easting, northing = expected.apply(float(col), float(row))
            eastings.append(easting)
            northings.append(northing)
        center_e = sum(eastings) / len(eastings)
        center_n = sum(northings) / len(northings)
        upper_left = info["cornerCoordinates"]["upperLeft"]
        lower_right = info["cornerCoordinates"]["lowerRight"]
        min_e = min(upper_left[0], lower_right[0])
        max_e = max(upper_left[0], lower_right[0])
        min_n = min(upper_left[1], lower_right[1])
        max_n = max(upper_left[1], lower_right[1])
        self.assertGreater(center_e, min_e)
        self.assertLess(center_e, max_e)
        self.assertGreater(center_n, min_n)
        self.assertLess(center_n, max_n)


class FlowageNameTest(unittest.TestCase):
    def test_name_flowage_is_queued_when_hydrotype_is_a_lake(self):
        self.assertTrue(georef.is_flowage(706, "Petenwell Flowage"))
        self.assertFalse(georef.is_flowage(706, "Ada Lake"))
        self.assertTrue(georef.is_flowage(707, "Ada Lake"))


class CliTest(unittest.TestCase):
    def test_fixtures_flag_writes_review_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            georef.main(["--fixtures", "--work-dir", str(work)])
            queue = json.loads((work / "georef" / "review_queue.json").read_text(encoding="utf-8"))
            results = json.loads((work / "georef" / "results.json").read_text(encoding="utf-8"))
        good = next(item for item in results["items"] if item["wbic"] == "0417300")
        self.assertGreaterEqual(good["iou"], 0.90)
        self.assertEqual(good["qa_status"], "auto")
        self.assertTrue(queue["items"])
        self.assertEqual(queue["crs"], "EPSG:3071")


if __name__ == "__main__":
    unittest.main()
