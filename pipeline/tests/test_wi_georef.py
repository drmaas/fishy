# SPDX-License-Identifier: GPL-3.0-only
import json
import math
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fishy_pipeline.states.wi import georef, synthetic
from fishy_pipeline.states.wi.constants import DEFAULT_IOU_THRESHOLD
from fishy_pipeline.states.wi.hydro import load_hydro_polygons
from fishy_pipeline.states.wi.pngutil import RasterImage, read_png, write_png
from fishy_pipeline.states.wi.register import (
    Affine,
    Registration,
    ThinPlateSpline,
    fit_affine,
    polygon_iou,
    register_shoreline,
)
from fishy_pipeline.states.wi.shoreline import extract_shoreline, extract_shoreline_candidates

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
        chosen = registration.affine_iou if registration.method == "affine" else registration.tps_iou
        self.assertEqual(registration.iou, chosen)
        scale = float(spec["scale_m_per_px"])
        for col, row in pixels:
            easting, northing = expected.apply(col, row)
            got_e, got_n = registration.affine.apply(col, row)
            self.assertLess(math.hypot(got_e - easting, got_n - northing), 2.0 * scale)


class IouGateTest(unittest.TestCase):
    def test_reported_iou_follows_the_warp_that_is_applied(self):
        blank = ThinPlateSpline(
            sources=[],
            easting_weights=[],
            northing_weights=[],
            easting_affine=(0, 0, 0),
            northing_affine=(0, 0, 0),
        )
        affine = Registration(
            affine=Affine(a=1, b=0, tx=0, c=0, d=1, ty=0),
            tps=blank,
            gcps=(),
            affine_iou=0.95,
            tps_iou=0.80,
            method="affine",
        )
        self.assertEqual(affine.iou, 0.95)
        spline = Registration(
            affine=Affine(a=1, b=0, tx=0, c=0, d=1, ty=0),
            tps=blank,
            gcps=(),
            affine_iou=0.91,
            tps_iou=0.94,
            method="tps",
        )
        self.assertEqual(spline.iou, 0.94)


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

    def test_outer_shore_beats_an_inner_contour(self):
        width, height = 120, 120
        rgb = bytearray([255]) * (width * height * 3)
        def ink(x0, y0, x1, y1):
            for x in range(x0, x1 + 1):
                for y in (y0, y1):
                    rgb[(y * width + x) * 3 : (y * width + x) * 3 + 3] = b"\x00\x00\x00"
            for y in range(y0, y1 + 1):
                for x in (x0, x1):
                    rgb[(y * width + x) * 3 : (y * width + x) * 3 + 3] = b"\x00\x00\x00"
        ink(20, 20, 99, 99)
        ink(45, 45, 74, 74)
        contour = extract_shoreline(RasterImage(width=width, height=height, rgb=bytes(rgb)))
        self.assertIsNotNone(contour)
        assert contour is not None
        xs = [point[0] for point in contour]
        self.assertLess(min(xs), 30)
        self.assertGreater(max(xs), 90)

    def test_symbol_and_inner_contour_are_not_candidates(self):
        width, height = 200, 200
        rgb = bytearray([255]) * (width * height * 3)

        def ink(x0, y0, x1, y1):
            for x in range(x0, x1 + 1):
                for y in (y0, y1):
                    rgb[(y * width + x) * 3 : (y * width + x) * 3 + 3] = b"\x00\x00\x00"
            for y in range(y0, y1 + 1):
                for x in (x0, x1):
                    rgb[(y * width + x) * 3 : (y * width + x) * 3 + 3] = b"\x00\x00\x00"

        ink(8, 8, 18, 18)
        speck = RasterImage(width=width, height=height, rgb=bytes(rgb))
        self.assertEqual(extract_shoreline_candidates(speck), [])

        ink(36, 36, 150, 150)
        ink(80, 80, 110, 110)
        rings = extract_shoreline_candidates(RasterImage(width=width, height=height, rgb=bytes(rgb)))
        self.assertGreaterEqual(len(rings), 1)
        for ring in rings:
            xs = [point[0] for point in ring]
            self.assertLess(min(xs), 45)
            self.assertGreater(max(xs), 140)


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
        self.assertIn("-dstalpha", warp)
        self.assertNotIn("-dstnodata", warp)
        self.assertEqual(warp[warp.index("-t_srs") + 1], "EPSG:3071")
        affine = georef.gdal_translate_affine_args(
            "in.png",
            "warped.tif",
            {"a": 2.0, "b": 0.0, "tx": 500000.0, "c": 0.0, "d": -2.0, "ty": 300000.0},
        )
        self.assertEqual(affine[0], "gdal_translate")
        self.assertIn("-a_gt", affine)
        self.assertIn("EPSG:3071", affine)
        self.assertNotIn("-tps", affine)


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
        shared = [item for item in self.results["items"] if item["map_key"] == "1500000a"]
        self.assertEqual({item["wbic"] for item in shared}, {"1500000", "1500001"})
        self.assertEqual(len(self.results["items"]), 10)

    def test_known_raster_recovers_transform_above_threshold(self):
        spec = synthetic.load_spec(SPEC_PATH)
        expected = synthetic.affine_from_spec(spec)
        item = self.by_pair[("0417300", "0417300a")]
        self.assertEqual(item["qa_status"], "auto")
        self.assertGreaterEqual(item["iou"], DEFAULT_IOU_THRESHOLD)
        self.assertEqual(
            item["iou"],
            item["affine_iou"] if item["warp"]["method"] == "affine" else item["tps_iou"],
        )
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

    def test_low_iou_is_queued(self):
        item = self.by_pair[("0261100", "0261100a")]
        self.assertEqual(item["qa_status"], "needs_review")
        self.assertIn("iou_below_threshold", item["reasons"])
        self.assertLess(item["iou"], DEFAULT_IOU_THRESHOLD)
        queued = next(row for row in self.queue["items"] if row["wbic"] == "0261100")
        self.assertEqual(queued["map_key"], "0261100a")
        self.assertIn("iou_below_threshold", queued["reasons"])
        self.assertTrue(queued["gcp_path"].endswith("0261100/0261100a/gcps.json"))

    def test_similar_shared_sheet_queues_the_smaller_lake(self):
        smaller = self.by_pair[("1500001", "1500000a")]
        larger = self.by_pair[("1500000", "1500000a")]
        self.assertGreaterEqual(smaller["iou"], 0.90)
        self.assertIn("shared_sheet", smaller["reasons"])
        self.assertEqual(smaller["qa_status"], "needs_review")
        self.assertIn("shared_sheet", larger["reasons"])
        self.assertEqual(larger["qa_status"], "needs_review")
        queued = next(row for row in self.queue["items"] if row["wbic"] == "1500001")
        self.assertEqual(queued["map_key"], "1500000a")
        self.assertIn("shared_sheet", queued["reasons"])
        self.assertNotIn("iou_below_threshold", queued["reasons"])

    def test_flowage_is_queued_with_a_good_fit(self):
        item = self.by_pair[("1403200", "1403200a")]
        self.assertGreaterEqual(item["iou"], 0.90)
        self.assertIn("flowage", item["reasons"])
        self.assertEqual(item["qa_status"], "needs_review")
        self.assertEqual(item["hydrotype"], 707)

    def test_flowage_without_hydro_polygon_is_queued(self):
        item = self.by_pair[("1403299", "1403299a")]
        self.assertEqual(item["qa_status"], "needs_review")
        self.assertIn("flowage", item["reasons"])
        self.assertIsNone(item["iou"])
        queued = next(row for row in self.queue["items"] if row["wbic"] == "1403299")
        self.assertEqual(queued["map_key"], "1403299a")
        self.assertIn("flowage", queued["reasons"])

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

    @unittest.skipUnless(georef.gdal_tools_available(), "gdal")
    def test_warp_clip_keeps_ink(self):
        gdal = _gdal()
        if gdal is None:
            self.skipTest("osgeo")
        gdal.UseExceptions()
        item = self.by_pair[("0417300", "0417300a")]
        warped = Path(item["paths"]["warped"])
        source = gdal.Open(str(warped))
        self.assertIsNotNone(source)
        self.assertGreaterEqual(source.RasterCount, 4)
        self.assertIsNone(source.GetRasterBand(1).GetNoDataValue())
        spec = synthetic.load_spec(SPEC_PATH)
        expected = synthetic.affine_from_spec(spec)
        ring = [expected.apply(float(col), float(row)) for col, row in spec["pixel_ring"]]
        closed = ring + [ring[0]]
        cutline = self.work / "known-lake-cutline.geojson"
        cutline.write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3071"}},
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {},
                            "geometry": {"type": "Polygon", "coordinates": [[[x, y] for x, y in closed]]},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        clipped_path = self.work / "known-lake-clipped.tif"
        clipped = gdal.Warp(
            str(clipped_path),
            str(warped),
            format="GTiff",
            cutlineDSName=str(cutline),
            cropToCutline=True,
            dstSRS="EPSG:3071",
        )
        self.assertIsNotNone(clipped)
        color = clipped.GetRasterBand(1).ReadAsArray()
        alpha = clipped.GetRasterBand(clipped.RasterCount).ReadAsArray()
        valid_ink = int(((color == 0) & (alpha > 0)).sum())
        self.assertGreater(valid_ink, 50)
        source = None
        clipped = None


def _gdal():
    try:
        from osgeo import gdal
    except ImportError:
        return None
    return gdal


class FlowageNameTest(unittest.TestCase):
    def test_name_flowage_is_queued_when_hydrotype_is_a_lake(self):
        self.assertTrue(georef.is_flowage(706, "Petenwell Flowage"))
        self.assertFalse(georef.is_flowage(706, "Ada Lake"))
        self.assertTrue(georef.is_flowage(707, "Ada Lake"))


class ManifestIdentityTest(unittest.TestCase):
    def test_unpadded_wbic_matches_hydro_and_one_failure_does_not_abort(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            manifest_path, hydro_path = synthetic.materialize_fixture(work / "fixture-input")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for entry in manifest["entries"]:
                if entry["wbic"] == "0417300":
                    entry["wbic"] = "417300"
            manifest["entries"].append(
                {
                    "map_key": "0000000a",
                    "wbic": "not-a-wbic",
                    "name": "Bad Id",
                    "source_media_type": "png",
                    "raster_path": "missing.png",
                    "hydro_matched": True,
                }
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            real = georef.register_shoreline
            calls = {"n": 0}

            def wrapped(pixel_ring, hydro_ring, gcp_count=8):
                # The first sheet has two lake-scale figures. Both fail, and the
                # batch still registers the later lakes.
                calls["n"] += 1
                if calls["n"] <= 2:
                    raise ValueError("singular linear system")
                return real(pixel_ring, hydro_ring, gcp_count=gcp_count)

            with patch("fishy_pipeline.states.wi.georef.register_shoreline", side_effect=wrapped):
                results = georef.run_georef(
                    manifest_path=manifest_path,
                    hydro_path=hydro_path,
                    work_dir=work,
                )
            by_pair = _by_pair(results)
            known = by_pair[("0417300", "0417300a")]
            self.assertEqual(known["wbic"], "0417300")
            self.assertIn("registration_error", known["reasons"])
            self.assertEqual(known["qa_status"], "needs_review")
            self.assertTrue(str(known.get("error", "")).startswith("singular linear system"))
            invalid = next(item for item in results["items"] if item["map_key"] == "0000000a")
            self.assertEqual(invalid["qa_status"], "skipped")
            self.assertEqual(invalid["skip_reason"], "invalid_wbic")
            flowage = by_pair[("1403200", "1403200a")]
            self.assertGreaterEqual(flowage["iou"], 0.90)
            queue = json.loads((work / "georef" / "review_queue.json").read_text(encoding="utf-8"))
            queued = next(row for row in queue["items"] if row["wbic"] == "0417300")
            self.assertIn("registration_error", queued["reasons"])


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
