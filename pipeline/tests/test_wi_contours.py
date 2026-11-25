# SPDX-License-Identifier: GPL-3.0-only
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fishy_pipeline.states.wi import contours
from fishy_pipeline.states.wi.depth_tag import (
    Label,
    Line,
    WaterPart,
    assign_depths,
    assignment_to_feet,
    cutline_collection,
    features_to_geojson,
    infer_interval,
    parse_depth_text,
    parse_depth_token,
    point_in_water,
    sheet_depth_unit,
    signed_area,
)
from fishy_pipeline.states.wi.hydro import load_hydro_geometries
from fishy_pipeline.states.wi.raster import fit_page_zoom, max_render_pixels
from fishy_pipeline.states.wi.recorded_labels import labels_for_trace
from fishy_pipeline.states.wi.register import Affine, GroundControlPoint
from fishy_pipeline.states.wi.trace import (
    blank_text_boxes,
    bridge_label_gaps,
    clip_raster,
    close_gap,
    filter_small_components,
    gdal_tools_available,
    gdalwarp_clip_args,
    ink_mask,
    neighbors,
    prune_spurs,
    trace_polylines,
)


def _box(x0: float, y0: float, x1: float, y1: float) -> tuple[tuple[float, float], ...]:
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def _shore() -> list[WaterPart]:
    return [WaterPart(exterior=_box(0, 0, 100, 100))]


def _depths(assignment) -> list[int]:
    return sorted(line.depth_ft for line in assignment.features if line.depth_ft is not None and line.source != "hydro")


class IntervalTest(unittest.TestCase):
    def test_tens_stay_ten_and_fives_become_five(self):
        self.assertEqual(infer_interval([10, 20, 30, 40]), 10)
        self.assertEqual(infer_interval([5, 10, 15, 20, 60]), 5)
        self.assertIsNone(infer_interval([]))

    def test_parse_depth_rejects_years_and_letters(self):
        self.assertEqual(parse_depth_text("10"), 10)
        self.assertEqual(parse_depth_text(" 5 "), 5)
        self.assertIsNone(parse_depth_text("1987"))
        self.assertIsNone(parse_depth_text("10FT"))
        self.assertIsNone(parse_depth_text("M"))
        self.assertIsNone(parse_depth_text("0"))


class AssignDepthTest(unittest.TestCase):
    def test_nested_labels_step_in_from_the_shore(self):
        outer = Line(coords=_box(10, 10, 90, 90), closed=True)
        inner = Line(coords=_box(30, 30, 70, 70), closed=True)
        assignment = assign_depths(
            [outer, inner],
            [
                Label(x=10, y=50, depth_ft=5, confidence=0.9),
                Label(x=30, y=50, depth_ft=10, confidence=0.8),
            ],
            _shore(),
            max_depth_ft=20,
            snap_m=5,
        )
        self.assertEqual(assignment.interval_ft, 5)
        self.assertEqual(_depths(assignment), [5, 10])
        self.assertFalse(any(line.needs_review for line in assignment.features))
        labeled = [line for line in assignment.features if line.source == "label"]
        self.assertTrue(all(line.confidence >= 0.8 for line in labeled))
        shore = [line for line in assignment.features if line.source == "hydro"]
        self.assertEqual([line.depth_ft for line in shore], [0])

    def test_unlabeled_rings_propagate_until_max_depth(self):
        rings = [
            Line(coords=_box(10, 10, 90, 90), closed=True),
            Line(coords=_box(25, 25, 75, 75), closed=True),
            Line(coords=_box(40, 40, 60, 60), closed=True),
        ]
        assignment = assign_depths(
            rings,
            [Label(x=10, y=50, depth_ft=5, confidence=0.95)],
            _shore(),
            max_depth_ft=8,
            snap_m=5,
        )
        by_depth = {line.depth_ft: line for line in assignment.features if line.source != "hydro"}
        self.assertFalse(by_depth[5].needs_review)
        self.assertTrue(by_depth[10].needs_review)
        self.assertEqual(by_depth[10].reason, "above_max_depth")
        self.assertTrue(by_depth[15].needs_review)

    def test_conflicting_labels_need_review(self):
        ring = Line(coords=_box(10, 10, 90, 90), closed=True)
        labels = [
            Label(x=10, y=40, depth_ft=10, confidence=0.9),
            Label(x=10, y=60, depth_ft=10, confidence=0.9),
            Label(x=50, y=10, depth_ft=20, confidence=0.9),
            Label(x=50, y=90, depth_ft=20, confidence=0.9),
        ]
        assignment = assign_depths([ring], labels, _shore(), max_depth_ft=40, snap_m=5)
        tagged = next(line for line in assignment.features if line.source == "label")
        self.assertTrue(tagged.needs_review)
        self.assertEqual(tagged.reason, "label_conflict")
        self.assertLessEqual(tagged.confidence, 0.4)

    def test_one_stray_label_does_not_conflict(self):
        ring = Line(coords=_box(10, 10, 90, 90), closed=True)
        labels = [
            Label(x=10, y=30, depth_ft=10, confidence=0.9),
            Label(x=10, y=50, depth_ft=10, confidence=0.9),
            Label(x=10, y=70, depth_ft=10, confidence=0.9),
            Label(x=90, y=50, depth_ft=15, confidence=0.4),
        ]
        assignment = assign_depths([ring], labels, _shore(), max_depth_ft=40, snap_m=5)
        tagged = next(line for line in assignment.features if line.source == "label")
        self.assertEqual(tagged.depth_ft, 10)
        self.assertFalse(tagged.needs_review)

    def test_sibling_basins_share_a_depth(self):
        left = Line(coords=_box(10, 10, 40, 40), closed=True)
        right = Line(coords=_box(60, 10, 90, 40), closed=True)
        assignment = assign_depths(
            [left, right],
            [
                Label(x=10, y=20, depth_ft=10, confidence=0.9),
                Label(x=60, y=20, depth_ft=10, confidence=0.9),
            ],
            _shore(),
            max_depth_ft=30,
            snap_m=5,
        )
        self.assertEqual(_depths(assignment), [10, 10])
        self.assertEqual(assignment.interval_ft, 10)
        self.assertFalse(any(line.needs_review for line in assignment.features if line.source == "label"))

    def test_island_collar_without_a_label_is_not_a_deeper_basin(self):
        parts = [WaterPart(exterior=_box(0, 0, 100, 100), holes=(_box(40, 40, 60, 60),))]
        basin = Line(coords=_box(10, 10, 90, 90), closed=True)
        collar = Line(coords=_box(36, 36, 64, 64), closed=True)
        assignment = assign_depths(
            [basin, collar],
            [Label(x=10, y=50, depth_ft=5, confidence=0.9)],
            parts,
            max_depth_ft=30,
            snap_m=5,
        )
        self.assertIn("island_unlabeled", assignment.reasons)
        collar_tag = next(line for line in assignment.features if line.reason == "island_unlabeled")
        self.assertIsNone(collar_tag.depth_ft)
        basin_tag = next(line for line in assignment.features if line.source == "label")
        self.assertEqual(basin_tag.depth_ft, 5)
        self.assertFalse(basin_tag.needs_review)
        shores = [line for line in assignment.features if line.source == "hydro"]
        self.assertEqual(len(shores), 2)

    def test_labeled_island_collar_keeps_the_label(self):
        parts = [WaterPart(exterior=_box(0, 0, 100, 100), holes=(_box(40, 40, 60, 60),))]
        collar = Line(coords=_box(36, 36, 64, 64), closed=True)
        assignment = assign_depths(
            [collar],
            [Label(x=36, y=50, depth_ft=5, confidence=0.88)],
            parts,
            max_depth_ft=30,
            snap_m=5,
        )
        tagged = next(line for line in assignment.features if line.source == "label")
        self.assertEqual(tagged.depth_ft, 5)
        self.assertFalse(tagged.needs_review)

    def test_line_inside_an_island_is_dropped(self):
        parts = [WaterPart(exterior=_box(0, 0, 100, 100), holes=(_box(40, 40, 70, 70),))]
        land = Line(coords=_box(48, 48, 62, 62), closed=True)
        assignment = assign_depths(
            [land],
            [Label(x=48, y=55, depth_ft=10, confidence=0.9)],
            parts,
            max_depth_ft=30,
            snap_m=5,
        )
        self.assertEqual(_depths(assignment), [])
        self.assertFalse(point_in_water(50, 50, parts))
        self.assertTrue(point_in_water(20, 20, parts))

    def test_section_line_is_not_a_contour(self):
        section = Line(coords=((0, 50), (25, 50), (50, 50), (75, 50), (100, 50)), closed=False)
        ring = Line(coords=_box(20, 20, 80, 80), closed=True)
        assignment = assign_depths(
            [section, ring],
            [Label(x=20, y=50, depth_ft=5, confidence=0.9)],
            _shore(),
            max_depth_ft=20,
            snap_m=3,
        )
        self.assertEqual(_depths(assignment), [5])

    def test_open_line_keeps_its_label(self):
        arc = Line(coords=((20, 40), (40, 30), (60, 40)), closed=False)
        assignment = assign_depths(
            [arc],
            [Label(x=40, y=30, depth_ft=10, confidence=0.7)],
            _shore(),
            max_depth_ft=40,
            snap_m=5,
        )
        tagged = next(line for line in assignment.features if line.source == "label")
        self.assertEqual(tagged.depth_ft, 10)
        self.assertFalse(tagged.closed)

    def test_label_that_breaks_the_step_needs_review(self):
        outer = Line(coords=_box(10, 10, 90, 90), closed=True)
        inner = Line(coords=_box(30, 30, 70, 70), closed=True)
        assignment = assign_depths(
            [outer, inner],
            [
                Label(x=10, y=40, depth_ft=10, confidence=0.9),
                Label(x=10, y=60, depth_ft=10, confidence=0.9),
                Label(x=30, y=50, depth_ft=5, confidence=0.9),
            ],
            _shore(),
            max_depth_ft=40,
            snap_m=5,
        )
        inner_tag = next(line for line in assignment.features if line.depth_ft == 5)
        self.assertTrue(inner_tag.needs_review)
        self.assertEqual(inner_tag.reason, "label_breaks_propagation")

    def test_far_label_does_not_snap(self):
        ring = Line(coords=_box(10, 10, 40, 40), closed=True)
        assignment = assign_depths(
            [ring],
            [Label(x=80, y=80, depth_ft=10, confidence=0.99)],
            _shore(),
            max_depth_ft=40,
            snap_m=5,
        )
        self.assertEqual(_depths(assignment), [])
        self.assertIn("interval_unknown", assignment.reasons)


class GeoJsonTest(unittest.TestCase):
    def test_properties_are_positive_feet(self):
        assignment = assign_depths(
            [Line(coords=_box(10, 10, 90, 90), closed=True)],
            [Label(x=10, y=50, depth_ft=5, confidence=0.9)],
            _shore(),
            max_depth_ft=20,
            snap_m=5,
        )
        collection = features_to_geojson(
            assignment.features,
            wbic="0417300",
            interval_ft=assignment.interval_ft,
            max_depth_ft=20,
            qa_status="auto",
        )
        self.assertEqual(collection["qa_status"], "auto")
        props = [feature["properties"] for feature in collection["features"]]
        self.assertTrue(props)
        for prop in props:
            self.assertEqual(prop["wbic"], "0417300")
            self.assertGreaterEqual(prop["depth_ft"], 0)
            self.assertEqual(prop["interval_ft"], 5)
            self.assertGreaterEqual(prop["confidence"], 0)
            self.assertLessEqual(prop["confidence"], 1)
            self.assertIn("needs_review", prop)
        self.assertIn(5, [prop["depth_ft"] for prop in props])
        self.assertIn(0, [prop["depth_ft"] for prop in props])

    def test_cutline_keeps_island_orientation(self):
        parts = [WaterPart(exterior=_box(0, 0, 100, 100), holes=(_box(40, 40, 60, 60),))]
        collection = cutline_collection(parts)
        rings = collection["features"][0]["geometry"]["coordinates"]
        exterior = [(point[0], point[1]) for point in rings[0][:-1]]
        hole = [(point[0], point[1]) for point in rings[1][:-1]]
        self.assertGreater(signed_area(exterior), 0)
        self.assertLess(signed_area(hole), 0)
        self.assertEqual(collection["crs"]["properties"]["name"], "urn:ogc:def:crs:EPSG::3071")


class HydroHoleTest(unittest.TestCase):
    def test_largest_feature_keeps_its_island(self):
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
                                "properties": {"WATERBODY_WBIC": 417300, "WATERBODY_NAME": "Ada Lake", "HYDROTYPE": 706},
                                "geometry": {
                                    "type": "Polygon",
                                    "coordinates": [
                                        [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
                                        [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]],
                                    ],
                                },
                            },
                            {
                                "type": "Feature",
                                "properties": {"WATERBODY_WBIC": 417300, "WATERBODY_NAME": "Ada Lake", "HYDROTYPE": 706},
                                "geometry": {
                                    "type": "Polygon",
                                    "coordinates": [[[0, 0], [3, 0], [3, 3], [0, 3], [0, 0]]],
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            geometry = load_hydro_geometries(path)["0417300"]
        self.assertEqual(len(geometry.parts), 1)
        self.assertEqual(len(geometry.parts[0].holes), 1)
        self.assertEqual(geometry.hydrotype, 706)


class TraceGraphTest(unittest.TestCase):
    def test_rectangle_skeleton_is_one_closed_ring(self):
        points = set()
        for x in range(0, 11):
            points.add((x, 0))
            points.add((x, 10))
        for y in range(0, 11):
            points.add((0, y))
            points.add((10, y))
        self.assertEqual(len(neighbors((0, 0), points)), 2)
        polylines = trace_polylines(points)
        self.assertEqual(len(polylines), 1)
        _path, closed = polylines[0]
        self.assertTrue(closed)

    def test_diagonal_steps_stay_one_line(self):
        points = {(0, 0), (1, 1), (2, 2), (3, 3), (4, 4)}
        polylines = trace_polylines(points)
        self.assertEqual(len(polylines), 1)
        path, closed = polylines[0]
        self.assertFalse(closed)
        self.assertEqual(len(path), 5)

    def test_short_spur_is_pruned(self):
        points = {(x, 0) for x in range(0, 41)}
        points.update((20, y) for y in range(1, 4))
        pruned = prune_spurs(points, max_len=12)
        self.assertNotIn((20, 3), pruned)
        self.assertIn((20, 0), pruned)
        self.assertIn((0, 0), pruned)
        polylines = trace_polylines(pruned)
        self.assertEqual(len(polylines), 1)

    def test_close_gap_joins_a_near_ring(self):
        path = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 2)]
        closed_path, closed = close_gap(path, gap_px=3)
        self.assertTrue(closed)
        self.assertEqual(closed_path[0], closed_path[-1])


class InkTest(unittest.TestCase):
    def test_alpha_and_blue_ink(self):
        # 2x2: dark, white, blue, dark-but-transparent
        rgb = bytes(
            [
                0, 0, 0,
                250, 250, 250,
                20, 40, 200,
                0, 0, 0,
            ]
        )
        alpha = bytes([255, 255, 255, 0])
        mask = ink_mask(rgb, 2, 2, alpha)
        self.assertEqual(list(mask), [1, 0, 1, 0])

    def test_blank_and_component_filter(self):
        rgb = bytearray([0, 0, 0] * 16)
        blanked = blank_text_boxes(bytes(rgb), 4, 4, [(1, 1, 3, 3)])
        self.assertEqual(blanked[5 * 3 : 6 * 3], b"\xff\xff\xff")
        width = 80
        height = 4
        mask = bytearray(width * height)
        for x in range(60):
            mask[x] = 1
        for y in range(2):
            for x in range(2):
                mask[(2 + y) * width + x] = 1
        kept = filter_small_components(mask, width, height)
        self.assertEqual(sum(kept[:60]), 60)
        self.assertEqual(sum(kept[2 * width :]), 0)


class ClipCommandTest(unittest.TestCase):
    def test_warp_args_keep_alpha_and_cutline(self):
        args = gdalwarp_clip_args("in.tif", "out.tif", "cut.geojson")
        self.assertIn("-cutline", args)
        self.assertIn("cut.geojson", args)
        self.assertIn("-dstalpha", args)
        self.assertIn("CUTLINE_ALL_TOUCHED=TRUE", args)
        self.assertNotIn("-dstnodata", args)


class ClipRasterTest(unittest.TestCase):
    @unittest.skipUnless(gdal_tools_available(), "gdal")
    def test_hole_is_masked_and_interior_ink_remains(self):
        gdal = _gdal()
        if gdal is None:
            self.skipTest("osgeo")
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            source = work / "sheet.tif"
            _write_sheet(gdal, source)
            cutline = work / "cut.geojson"
            parts = [
                WaterPart(
                    exterior=((500080, 500080), (500240, 500080), (500240, 500240), (500080, 500240)),
                    holes=(((500180, 500140), (500220, 500140), (500220, 500200), (500180, 500200)),),
                )
            ]
            cutline.write_text(json.dumps(cutline_collection(parts)), encoding="utf-8")
            clipped = work / "clipped.tif"
            clip_raster(source, clipped, cutline)
            dataset = gdal.Open(str(clipped))
            self.assertIsNotNone(dataset)
            transform = dataset.GetGeoTransform()
            width = dataset.RasterXSize
            height = dataset.RasterYSize
            color = dataset.GetRasterBand(1).ReadRaster(0, 0, width, height, width, height, gdal.GDT_Byte)
            alpha = dataset.GetRasterBand(dataset.RasterCount).ReadRaster(
                0, 0, width, height, width, height, gdal.GDT_Byte
            )
            inside_ink = 0
            hole_ink = 0
            for row in range(height):
                for col in range(width):
                    index = row * width + col
                    if color[index] > 10 or alpha[index] == 0:
                        continue
                    x = transform[0] + (col + 0.5) * transform[1] + (row + 0.5) * transform[2]
                    y = transform[3] + (col + 0.5) * transform[4] + (row + 0.5) * transform[5]
                    if point_in_water(x, y, parts):
                        inside_ink += 1
                    else:
                        hole_ink += 1
            dataset = None
        self.assertGreater(inside_ink, 0)
        self.assertEqual(hole_ink, 0)


def _gdal():
    try:
        from osgeo import gdal
    except ImportError:
        return None
    return gdal


def _write_sheet(gdal, path: Path) -> None:
    from osgeo import osr

    driver = gdal.GetDriverByName("GTiff")
    width = height = 40
    dataset = driver.Create(str(path), width, height, 3, gdal.GDT_Byte)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3071)
    dataset.SetProjection(srs.ExportToWkt())
    dataset.SetGeoTransform((500000, 10, 0, 500400, 0, -10))
    white = bytes([255]) * (width * height)
    for band_index in range(1, 4):
        dataset.GetRasterBand(band_index).WriteRaster(0, 0, width, height, white)
    red = bytearray(white)
    # Inside the water, inside the hole, and outside the polygon.
    # Row/col follow the geotransform (500000, 10, 0, 500400, 0, -10).
    red[28 * width + 12] = 0
    red[22 * width + 20] = 0
    red[2 * width + 2] = 0
    dataset.GetRasterBand(1).WriteRaster(0, 0, width, height, bytes(red))
    dataset.FlushCache()
    dataset = None


class FixtureCliTest(unittest.TestCase):
    def test_fixtures_write_positive_depths(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            contours.main(["--fixtures", "--work-dir", str(work)])
            results = json.loads((work / "contours" / "results.json").read_text(encoding="utf-8"))
            item = results["items"][0]
            self.assertEqual(item["qa_status"], "auto")
            self.assertEqual(item["interval_ft"], 5)
            self.assertEqual(item["depths_ft"], [0, 5, 10])
            collection = json.loads(Path(item["path"]).read_text(encoding="utf-8"))
        self.assertEqual(collection["wbic"], "0000101")
        self.assertTrue(all(feature["properties"]["depth_ft"] >= 0 for feature in collection["features"]))


class MeterSheetTest(unittest.TestCase):
    def test_meter_suffix_and_legend_word(self):
        self.assertEqual(parse_depth_token("10m"), (10.0, "m"))
        self.assertEqual(parse_depth_token("10"), (10.0, "ft"))
        self.assertIsNone(parse_depth_text("10m"))
        self.assertIsNone(parse_depth_token("10FT"))
        self.assertEqual(sheet_depth_unit(["MAX. DEPTH 10.3 METER", "4"]), "m")
        self.assertEqual(sheet_depth_unit(["5", "10", "15"]), "ft")

    def test_meter_labels_become_integer_feet(self):
        shore = [WaterPart(exterior=_box(0, 0, 200, 200))]
        rings = [
            Line(coords=_box(20, 20, 180, 180), closed=True),
            Line(coords=_box(60, 60, 140, 140), closed=True),
        ]
        assignment = assign_depths(
            rings,
            [
                Label(x=20, y=100, depth_ft=2, confidence=0.9),
                Label(x=60, y=100, depth_ft=4, confidence=0.9),
            ],
            shore,
            max_depth_ft=10.3 * 0.3048,
            snap_m=8,
        )
        converted = assignment_to_feet(assignment)
        self.assertEqual(converted.interval_ft, 6.56)
        self.assertEqual(
            sorted(line.depth_ft for line in converted.features if line.source != "hydro"),
            [7, 13],
        )


class LabelGapTest(unittest.TestCase):
    def test_bridge_joins_the_two_sides_of_a_label(self):
        width, height = 40, 11
        mask = bytearray(width * height)
        for x in range(4, 14):
            mask[5 * width + x] = 1
        for x in range(26, 36):
            mask[5 * width + x] = 1
        bridged = bridge_label_gaps(mask, width, height, [(14, 2, 26, 9)])
        self.assertTrue(any(bridged[5 * width + x] for x in range(14, 26)))


class RenderCapTest(unittest.TestCase):
    def test_oversized_page_scales_into_the_cap(self):
        max_w, max_h = max_render_pixels(300)
        zoom, actual_dpi, within_cap = fit_page_zoom(
            page_width_pt=46 * 72,
            page_height_pt=36 * 72,
            dpi=300,
            max_w=max_w,
            max_h=max_h,
            native_zoom=None,
        )
        self.assertFalse(within_cap)
        self.assertLessEqual(46 * 72 * zoom, max_w + 1e-6)
        self.assertLessEqual(36 * 72 * zoom, max_h + 1e-6)
        self.assertLess(actual_dpi, 300)

    def test_coarse_scan_is_not_upsampled(self):
        _zoom, actual_dpi, within_cap = fit_page_zoom(
            page_width_pt=38 * 72,
            page_height_pt=34 * 72,
            dpi=300,
            max_w=11700,
            max_h=10500,
            native_zoom=1.0,
        )
        self.assertTrue(within_cap)
        self.assertAlmostEqual(actual_dpi, 72.0)


class RecordedLabelPageTest(unittest.TestCase):
    def test_labels_apply_only_on_the_digitized_page(self):
        body = {
            "page": "0980900a-p0.png",
            "unit": "ft",
            "labels": [{"pixel": [10.0, 10.0], "value": 5, "confidence": 0.9}],
        }
        gcps = [
            GroundControlPoint(pixel=0.0, line=0.0, easting=0.0, northing=0.0),
            GroundControlPoint(pixel=20.0, line=0.0, easting=20.0, northing=0.0),
            GroundControlPoint(pixel=0.0, line=20.0, easting=0.0, northing=20.0),
        ]
        affine = Affine(a=1.0, b=0.0, tx=0.0, c=0.0, d=1.0, ty=0.0)
        transform = (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        with patch("fishy_pipeline.states.wi.recorded_labels.load_recorded_labels", return_value=body):
            matched, _boxes, _texts, _unit = labels_for_trace(
                "0980900",
                gcps=gcps,
                method="affine",
                affine=affine,
                geotransform=transform,
                clipped_width=50,
                clipped_height=50,
                page="0980900a-p0.png",
            )
            other, other_boxes, _texts, _unit = labels_for_trace(
                "0980900",
                gcps=gcps,
                method="affine",
                affine=affine,
                geotransform=transform,
                clipped_width=50,
                clipped_height=50,
                page="0980900a-p1.png",
            )
        self.assertEqual(len(matched), 1)
        self.assertEqual(other, [])
        self.assertEqual(other_boxes, [])

    def test_every_kept_warp_is_traced_with_its_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            for name in ("0980900a-p0.png", "0980900a-p1.png"):
                path = root / f"{name}.tif"
                path.write_bytes(b"tif")
                paths.append(path)
            item = {
                "wbic": "0980900",
                "map_key": "0980900a",
                "qa_status": "auto",
                "iou": 0.91,
                "reasons": [],
                "warp_source": "auto",
                "paths": {
                    "warps": [
                        {"page": "0980900a-p0.png", "status": "ok", "path": str(paths[0]), "gcps": str(root / "a.json")},
                        {"page": "0980900a-p1.png", "status": "ok", "path": str(paths[1]), "gcps": str(root / "b.json")},
                    ]
                },
            }
            seen: list[str | None] = []

            def fake_trace(*_args, **kwargs):
                seen.append(kwargs.get("page"))
                return {
                    "qa_status": "auto" if kwargs.get("page") == "0980900a-p0.png" else "needs_review",
                    "reasons": [] if kwargs.get("page") == "0980900a-p0.png" else ["label_page_mismatch"],
                    "depths_ft": [5],
                    "interval_ft": 5,
                    "label_count": 1 if kwargs.get("page") == "0980900a-p0.png" else 0,
                    "labels_applied": kwargs.get("page") == "0980900a-p0.png",
                    "recorded_labels": True,
                    "label_source": "recorded" if kwargs.get("page") == "0980900a-p0.png" else "none",
                    "unassigned_count": 0,
                    "path": str(root / "piece.geojson"),
                    "_features": [],
                }

            with patch("fishy_pipeline.states.wi.contours._trace_sheet", side_effect=fake_trace):
                result = contours._trace_item(
                    item,
                    entry={"max_depth_ft": 20},
                    geometry=SimpleNamespace(parts=[object()]),
                    work_dir=root,
                    ocr_fallback=False,
                    compare_ocr=False,
                )
        self.assertEqual(seen, ["0980900a-p0.png", "0980900a-p1.png"])
        self.assertEqual(result["qa_status"], "needs_review")
        self.assertEqual(result["sheet_count"], 2)
        self.assertTrue(result["sheets"][0]["labels_applied"])
        self.assertFalse(result["sheets"][1]["labels_applied"])


if __name__ == "__main__":
    unittest.main()
