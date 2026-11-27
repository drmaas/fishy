# SPDX-License-Identifier: GPL-3.0-only
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fishy_pipeline.states.wi import contours
from fishy_pipeline.states.wi.depth_tag import (
    JOIN_GAP_MIN_M,
    JOIN_MAX_ANGLE_DEG,
    Label,
    Line,
    WaterPart,
    TaggedLine,
    assign_depths,
    join_gap_m_for_pixel,
    join_open_strokes,
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
            join_gap_m=0,
        )
        self.assertEqual(assignment.interval_ft, 5)
        self.assertEqual(_depths(assignment), [5, 10])
        self.assertFalse(any(line.needs_review for line in assignment.features))
        labeled = [line for line in assignment.features if line.source == "label"]
        self.assertTrue(all(line.confidence >= 0.8 for line in labeled))
        shore = [line for line in assignment.features if line.source == "hydro"]
        self.assertEqual([line.depth_ft for line in shore], [0])

    def test_carried_interval_steps_rings_that_have_no_labels(self):
        rings = [
            Line(coords=_box(10, 10, 90, 90), closed=True),
            Line(coords=_box(30, 30, 70, 70), closed=True),
        ]
        assignment = assign_depths(rings, [], _shore(), max_depth_ft=40, snap_m=5, interval_ft=5, join_gap_m=0)
        self.assertEqual(assignment.interval_ft, 5)
        self.assertEqual(_depths(assignment), [5, 10])
        self.assertFalse(any(line.needs_review for line in assignment.features))
        self.assertNotIn("interval_unknown", assignment.reasons)

    def test_unlabeled_rings_without_an_interval_stay_in_review(self):
        rings = [Line(coords=_box(10, 10, 90, 90), closed=True)]
        assignment = assign_depths(rings, [], _shore(), max_depth_ft=40, snap_m=5, join_gap_m=0)
        self.assertIsNone(assignment.interval_ft)
        self.assertIn("interval_unknown", assignment.reasons)
        tagged = [line for line in assignment.features if line.source != "hydro"]
        self.assertTrue(all(line.needs_review for line in tagged))

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
            join_gap_m=0,
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
        assignment = assign_depths([ring], labels, _shore(), max_depth_ft=40, snap_m=5, join_gap_m=0)
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
        assignment = assign_depths([ring], labels, _shore(), max_depth_ft=40, snap_m=5, join_gap_m=0)
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
            join_gap_m=0,
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
            join_gap_m=0,
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
            join_gap_m=0,
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
            join_gap_m=0,
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
            join_gap_m=0,
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
            join_gap_m=0,
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
            join_gap_m=0,
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
            join_gap_m=0,
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
            join_gap_m=0,
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
            join_gap_m=0,
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


class CarriedIntervalMergeTest(unittest.TestCase):
    def _donor(self) -> dict:
        return {
            "wbic": "0980900",
            "page": "0980900a-p0.png",
            "qa_status": "auto",
            "labels_applied": True,
            "label_page_rejected": False,
            "interval_ft": 5,
            "depths_ft": [0, 5, 10],
            "reasons": [],
            "unassigned_count": 0,
            "_features": [],
            "label_count": 2,
            "recorded_labels": True,
            "label_source": "recorded",
        }

    def _rejected(self, *, reasons: list[str], lines: list[Line], max_depth: float) -> dict:
        return {
            "wbic": "0980900",
            "page": "0980900a-p1.png",
            "qa_status": "needs_review",
            "labels_applied": False,
            "label_page_rejected": True,
            "interval_ft": None,
            "depths_ft": [0],
            "reasons": reasons,
            "unassigned_count": 0,
            "_features": [],
            "_lines": lines,
            "_assign_labels": [],
            "_parts": _shore(),
            "_assign_max": max_depth,
            "_snap_m": 5.0,
            "max_depth_ft": max_depth,
            "label_count": 0,
            "recorded_labels": True,
            "label_source": "none",
        }

    def _merge(self, pieces: list[dict]):
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "fishy_pipeline.states.wi.contours.publish_gate",
                return_value=(True, "auto", []),
            ) as gate:
                result = contours._merge_sheet_traces(
                    {
                        "wbic": "0980900",
                        "map_key": "0980900a",
                        "iou": 0.91,
                        "georef_qa_status": "auto",
                        "georef_reasons": [],
                        "warp_source": "auto",
                    },
                    pieces,
                    out_dir=Path(tmp),
                    entry={"max_depth_ft": 40},
                )
        return result, gate

    def test_rejected_page_takes_the_label_interval_and_the_gate_runs_once(self):
        rings = [
            Line(coords=_box(10, 10, 90, 90), closed=True),
            Line(coords=_box(30, 30, 70, 70), closed=True),
        ]
        donor = self._donor()
        other = self._rejected(
            reasons=["interval_unknown", "recorded_depth_missing", "no_contour_lines"],
            lines=rings,
            max_depth=40,
        )
        contours._carry_label_interval([donor, other])
        self.assertEqual(other["qa_status"], "auto")
        self.assertEqual(other["depths_ft"], [0, 5, 10])
        self.assertEqual(other["reasons"], [])
        result, gate = self._merge([donor, other])
        self.assertEqual(gate.call_count, 1)
        self.assertEqual(gate.call_args.kwargs["depths_ft"], [5, 10])
        self.assertEqual(result["qa_status"], "auto")
        self.assertNotIn("recorded_depth_missing", result["reasons"])
        self.assertNotIn("no_contour_lines", result["reasons"])

    def test_ambiguous_carried_rings_stay_in_review(self):
        rings = [
            Line(coords=_box(10, 10, 90, 90), closed=True),
            Line(coords=_box(30, 30, 70, 70), closed=True),
        ]
        other = self._rejected(reasons=["interval_unknown"], lines=rings, max_depth=8)
        contours._carry_label_interval([self._donor(), other])
        self.assertEqual(other["qa_status"], "needs_review")
        self.assertTrue(any(line.needs_review for line in other["_features"] if line.depth_ft))
        result, _gate = self._merge([self._donor(), other])
        self.assertEqual(result["qa_status"], "needs_review")

    def test_missing_interval_does_not_copy_label_rejection_reasons(self):
        donor = self._donor()
        donor["interval_ft"] = None
        donor["_features"] = [
            TaggedLine(
                coords=_box(10, 10, 90, 90),
                closed=True,
                depth_ft=5,
                interval_ft=None,
                confidence=0.9,
                needs_review=False,
                reason=None,
                source="label",
            )
        ]
        other = self._rejected(
            reasons=["interval_unknown", "recorded_depth_missing", "no_contour_lines"],
            lines=[Line(coords=_box(10, 10, 90, 90), closed=True)],
            max_depth=40,
        )
        contours._carry_label_interval([donor, other])
        self.assertIsNone(other["interval_ft"])
        result, gate = self._merge([donor, other])
        self.assertEqual(gate.call_count, 1)
        self.assertIn("interval_unknown", result["reasons"])
        self.assertNotIn("recorded_depth_missing", result["reasons"])
        self.assertNotIn("no_contour_lines", result["reasons"])
        self.assertEqual(result["qa_status"], "needs_review")


class StrokeJoinTest(unittest.TestCase):
    def test_aligned_gap_joins_when_both_sides_share_a_depth(self):
        left = Line(coords=((20, 50), (40, 50), (55, 50)), closed=False)
        right = Line(coords=((68, 50), (90, 50), (110, 50)), closed=False)
        assignment = assign_depths(
            [left, right],
            [
                Label(x=40, y=50, depth_ft=10, confidence=0.9),
                Label(x=90, y=50, depth_ft=10, confidence=0.9),
            ],
            _shore(),
            max_depth_ft=40,
            snap_m=5,
            join_gap_m=20,
        )
        tagged = [line for line in assignment.features if line.source == "label"]
        self.assertEqual(len(tagged), 1)
        self.assertEqual(tagged[0].depth_ft, 10)
        self.assertFalse(tagged[0].closed)
        self.assertGreater(len(tagged[0].coords), 4)
        unlabeled_partner = join_open_strokes(
            [left, right],
            [Label(x=40, y=50, depth_ft=10, confidence=0.9)],
            _shore(),
            max_gap_m=20,
            max_angle_deg=JOIN_MAX_ANGLE_DEG,
            snap_m=5,
        )
        self.assertEqual(len(unlabeled_partner), 2)

    def test_a_corner_and_a_depth_conflict_stay_apart(self):
        east = Line(coords=((20, 50), (40, 50)), closed=False)
        north = Line(coords=((48, 50), (48, 80)), closed=False)
        parted = join_open_strokes(
            [east, north],
            [],
            _shore(),
            max_gap_m=20,
            max_angle_deg=JOIN_MAX_ANGLE_DEG,
            snap_m=5,
        )
        self.assertEqual(len(parted), 2)
        deep = Line(coords=((20, 20), (40, 20)), closed=False)
        deeper = Line(coords=((52, 20), (80, 20)), closed=False)
        conflicted = join_open_strokes(
            [deep, deeper],
            [Label(x=30, y=20, depth_ft=10, confidence=0.9), Label(x=70, y=20, depth_ft=20, confidence=0.9)],
            _shore(),
            max_gap_m=20,
            max_angle_deg=JOIN_MAX_ANGLE_DEG,
            snap_m=5,
        )
        self.assertEqual(len(conflicted), 2)

    def test_joining_does_not_invent_a_depth_for_unlabeled_strokes(self):
        left = Line(coords=((0, 40), (45, 40)), closed=False)
        right = Line(coords=((55, 40), (100, 40)), closed=False)
        assignment = assign_depths(
            [left, right],
            [],
            _shore(),
            max_depth_ft=30,
            snap_m=5,
            join_gap_m=20,
        )
        self.assertEqual(assignment.unassigned_count, 2)
        self.assertTrue(all(line.depth_ft is None or line.source == "hydro" for line in assignment.features))
        ten = Line(coords=((10, 50), (28, 50)), closed=False)
        middle = Line(coords=((40, 50), (58, 50)), closed=False)
        twenty = Line(coords=((70, 50), (88, 50)), closed=False)
        chain = assign_depths(
            [ten, middle, twenty],
            [
                Label(x=20, y=50, depth_ft=10, confidence=0.9),
                Label(x=80, y=50, depth_ft=20, confidence=0.9),
            ],
            _shore(),
            max_depth_ft=40,
            snap_m=5,
            join_gap_m=20,
        )
        self.assertEqual(len(join_open_strokes(
            [ten, middle, twenty],
            [
                Label(x=20, y=50, depth_ft=10, confidence=0.9),
                Label(x=80, y=50, depth_ft=20, confidence=0.9),
            ],
            _shore(),
            max_gap_m=20,
            max_angle_deg=JOIN_MAX_ANGLE_DEG,
            snap_m=5,
        )), 3)
        depths = _depths(chain)
        self.assertEqual(depths, [10, 20])
        self.assertNotIn("propagated", [line.source for line in chain.features])
        outer = Line(coords=_box(5, 5, 95, 95), closed=True)
        labeled = Line(coords=_box(18, 18, 82, 82), closed=True)
        almost = Line(
            coords=((35, 35), (65, 35), (65, 65), (35, 65), (35, 41)),
            closed=False,
        )
        loop = Line(coords=((10, 10), (40, 10), (40, 40), (10, 40), (10, 16)), closed=False)
        joined = join_open_strokes(
            [loop],
            [Label(x=25, y=10, depth_ft=10, confidence=0.9)],
            _shore(),
            max_gap_m=20,
            max_angle_deg=JOIN_MAX_ANGLE_DEG,
            snap_m=5,
        )
        self.assertEqual(len(joined), 1)
        self.assertFalse(joined[0].closed)
        closed_ring = assign_depths(
            [outer, labeled, almost],
            [
                Label(x=6, y=50, depth_ft=10, confidence=0.9),
                Label(x=18, y=50, depth_ft=20, confidence=0.9),
            ],
            _shore(),
            max_depth_ft=40,
            snap_m=5,
            join_gap_m=20,
        )
        self.assertFalse(any(line.closed and line.source == "propagated" for line in closed_ring.features))
        self.assertNotIn(30, _depths(closed_ring))

    def test_a_chord_across_a_peninsula_does_not_join(self):
        # The notch between y=40 and y=60, x>40, is land. The chord crosses it.
        shore = [
            WaterPart(
                exterior=(
                    (0, 0),
                    (100, 0),
                    (100, 40),
                    (40, 40),
                    (40, 60),
                    (100, 60),
                    (100, 100),
                    (0, 100),
                )
            )
        ]
        lower = Line(coords=((45, 20), (45, 32)), closed=False)
        upper = Line(coords=((45, 80), (45, 68)), closed=False)
        labels = [
            Label(x=45, y=26, depth_ft=10, confidence=0.9),
            Label(x=45, y=74, depth_ft=10, confidence=0.9),
        ]
        parted = join_open_strokes(
            [lower, upper],
            labels,
            shore,
            max_gap_m=40,
            max_angle_deg=JOIN_MAX_ANGLE_DEG,
            snap_m=5,
        )
        self.assertEqual(len(parted), 2)
        full = join_open_strokes(
            [lower, upper],
            labels,
            _shore(),
            max_gap_m=40,
            max_angle_deg=JOIN_MAX_ANGLE_DEG,
            snap_m=5,
        )
        self.assertEqual(len(full), 1)

    def test_a_narrow_bar_just_inside_an_end_does_not_join(self):
        # A 0.6 m island sits 0.2 m past the left end. The rest of the chord is water.
        shore = [
            WaterPart(
                exterior=_box(0, 0, 100, 100),
                holes=(((10.2, 49.0), (10.8, 49.0), (10.8, 51.0), (10.2, 51.0)),),
            )
        ]
        left = Line(coords=((0, 50), (10, 50)), closed=False)
        right = Line(coords=((30, 50), (40, 50)), closed=False)
        labels = [
            Label(x=5, y=50, depth_ft=10, confidence=0.9),
            Label(x=35, y=50, depth_ft=10, confidence=0.9),
        ]
        parted = join_open_strokes(
            [left, right],
            labels,
            shore,
            max_gap_m=40,
            max_angle_deg=JOIN_MAX_ANGLE_DEG,
            snap_m=5,
        )
        self.assertEqual(len(parted), 2)
        full = join_open_strokes(
            [left, right],
            labels,
            _shore(),
            max_gap_m=40,
            max_angle_deg=JOIN_MAX_ANGLE_DEG,
            snap_m=5,
        )
        self.assertEqual(len(full), 1)

    def test_pixel_gap_tracks_the_sheet_not_a_metre_cap(self):
        self.assertEqual(join_gap_m_for_pixel(0.2), 8.0)
        self.assertAlmostEqual(join_gap_m_for_pixel(1.0), 22.0)
        # 1:24000 at 300 dpi is 2.03 m/px and 1:31680 at 300 dpi is 2.68 m/px.
        self.assertAlmostEqual(join_gap_m_for_pixel(2.03), 2.03 * 22.0)
        self.assertAlmostEqual(join_gap_m_for_pixel(2.68), 2.68 * 22.0)
        # Only the pixel size is clamped, so a bad georeference stays bounded.
        self.assertEqual(join_gap_m_for_pixel(3.5), 77.0)
        self.assertEqual(join_gap_m_for_pixel(50.0), 77.0)
        self.assertEqual(join_gap_m_for_pixel(0), JOIN_GAP_MIN_M)
        self.assertEqual(join_gap_m_for_pixel(float("nan")), JOIN_GAP_MIN_M)

    def test_a_break_past_the_old_35_m_cap_still_joins(self):
        # The same 40 m break on a 1:24000 sheet at 300 dpi. The 35 m cap used
        # to leave it apart; the sheet's own 22 px window closes it.
        left = Line(coords=((10, 50), (30, 50)), closed=False)
        right = Line(coords=((70, 50), (90, 50)), closed=False)
        labels = [
            Label(x=20, y=50, depth_ft=10, confidence=0.9),
            Label(x=80, y=50, depth_ft=10, confidence=0.9),
        ]
        capped = join_open_strokes(
            [left, right],
            labels,
            _shore(),
            max_gap_m=35.0,
            max_angle_deg=JOIN_MAX_ANGLE_DEG,
            snap_m=5,
        )
        self.assertEqual(len(capped), 2)
        sheet_gap = join_gap_m_for_pixel(2.03)
        self.assertGreater(sheet_gap, 35.0)
        joined = join_open_strokes(
            [left, right],
            labels,
            _shore(),
            max_gap_m=sheet_gap,
            max_angle_deg=JOIN_MAX_ANGLE_DEG,
            snap_m=5,
        )
        self.assertEqual(len(joined), 1)
        self.assertFalse(joined[0].closed)

    def test_assign_depths_requires_an_explicit_gap(self):
        with self.assertRaises(TypeError):
            assign_depths(
                [Line(coords=((0, 0), (10, 0)), closed=False)],
                [],
                _shore(),
                max_depth_ft=10,
                snap_m=5,
            )


if __name__ == "__main__":
    unittest.main()
