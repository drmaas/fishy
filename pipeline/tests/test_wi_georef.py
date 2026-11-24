# SPDX-License-Identifier: GPL-3.0-only
import json
import math
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fishy_pipeline.states.wi import georef, synthetic
from fishy_pipeline.states.wi.constants import DEFAULT_IOU_THRESHOLD, NEAR_GATE_IOU
from fishy_pipeline.states.wi.hydro import load_hydro_polygons
from fishy_pipeline.states.wi.pngutil import RasterImage, read_png, write_png
from fishy_pipeline.states.wi.register import (
    MAX_ANISOTROPY,
    Affine,
    Registration,
    ThinPlateSpline,
    anisotropy,
    fit_affine,
    fit_similarity,
    polygon_iou,
    register_shoreline,
    union_polygon_iou,
)
from fishy_pipeline.states.wi import register as wi_register
from fishy_pipeline.states.wi.shoreline import (
    MAX_ROAD_STRAIGHTNESS,
    NEATLINE_SPAN,
    cleanup_shore_ring,
    extract_shoreline,
    extract_shoreline_candidates,
    extract_shoreline_figures,
    _has_reversal,
    _hole_shore_pixels,
    _ring_boxiness,
    _ring_straightness,
    _road_like_ring,
)

ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = ROOT / "fixtures" / "wi-georef" / "known-transform.json"


def _by_pair(results: dict) -> dict[tuple[str, str], dict]:
    return {(item["wbic"], item["map_key"]): item for item in results["items"]}


def _invert_affine(affine: Affine, easting: float, northing: float) -> tuple[float, float]:
    det = affine.a * affine.d - affine.b * affine.c
    ex = easting - affine.tx
    ny = northing - affine.ty
    col = (affine.d * ex - affine.b * ny) / det
    row = (-affine.c * ex + affine.a * ny) / det
    return (col, row)


def _angle_delta(left: float, right: float) -> float:
    delta = abs(left - right) % 360.0
    return min(delta, 360.0 - delta)


def _clip_open(
    ring: list[tuple[float, float]],
    cut_x: float,
) -> tuple[list[tuple[float, float]], list[bool]]:
    """Keep the part of ``ring`` with x >= cut_x. New edge vertices are on the frame."""

    def inside(point: tuple[float, float]) -> bool:
        return point[0] >= cut_x - 1e-9

    def cross(start: tuple[float, float], end: tuple[float, float]) -> tuple[float, float]:
        fraction = (cut_x - start[0]) / (end[0] - start[0])
        return (cut_x, start[1] + fraction * (end[1] - start[1]))

    output: list[tuple[float, float]] = []
    flags: list[bool] = []
    previous = ring[-1]
    previous_in = inside(previous)
    for point in ring:
        current_in = inside(point)
        if current_in:
            if not previous_in:
                output.append(cross(previous, point))
                flags.append(True)
            output.append(point)
            flags.append(abs(point[0] - cut_x) <= 1e-6)
        elif previous_in:
            output.append(cross(previous, point))
            flags.append(True)
        previous = point
        previous_in = current_in
    return output, flags


def _ink_polyline(
    rgb: bytearray,
    width: int,
    ring: list[tuple[int, int]],
    *,
    close: bool,
    skip_start: int = 0,
) -> None:
    points = list(ring)
    if close:
        points = points + [points[0]]
    first = True
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        pixels = _bresenham(x1, y1, x2, y2)
        if first and skip_start:
            pixels = pixels[skip_start:]
            first = False
        for x, y in pixels:
            index = (y * width + x) * 3
            rgb[index : index + 3] = b"\x00\x00\x00"


def _bresenham(x1: int, y1: int, x2: int, y2: int) -> list[tuple[int, int]]:
    pixels: list[tuple[int, int]] = []
    dx = abs(x2 - x1)
    dy = -abs(y2 - y1)
    sx = 1 if x1 < x2 else -1
    sy = 1 if y1 < y2 else -1
    err = dx + dy
    x, y = x1, y1
    while True:
        pixels.append((x, y))
        if x == x2 and y == y2:
            break
        step = 2 * err
        if step >= dy:
            err += dy
            x += sx
        if step <= dx:
            err += dx
            y += sy
    return pixels


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

    def test_continuous_rotation_recovers_an_off_axis_pose(self):
        hydro = [(0.0, 0.0), (90.0, 8.0), (110.0, 55.0), (60.0, 100.0), (12.0, 70.0), (-8.0, 24.0)]
        theta_deg = 37.0
        scale = 3.0
        theta = math.radians(theta_deg)
        forward = Affine(
            a=scale * math.cos(theta),
            b=-scale * math.sin(theta),
            tx=1000.0,
            c=-scale * math.sin(theta),
            d=-scale * math.cos(theta),
            ty=2000.0,
        )
        pixels = [_invert_affine(forward, easting, northing) for easting, northing in hydro]
        registration = register_shoreline(pixels, hydro, gcp_count=8)
        self.assertGreaterEqual(registration.iou, 0.90)
        self.assertLess(_angle_delta(registration.rotation_deg, theta_deg), 8.0)
        for (col, row), (easting, northing) in zip(pixels, hydro, strict=True):
            got_e, got_n = registration.affine.apply(col, row)
            self.assertLess(math.hypot(got_e - easting, got_n - northing), 2.0 * scale)

    def test_edge_cut_ring_matches_the_visible_portion(self):
        hydro = [(0.0, 0.0), (120.0, 6.0), (140.0, 70.0), (80.0, 130.0), (10.0, 90.0), (-15.0, 30.0)]
        cut_x = 50.0
        visible, flags = _clip_open(hydro, cut_x)
        theta_deg = 37.0
        scale = 4.0
        theta = math.radians(theta_deg)
        forward = Affine(
            a=scale * math.cos(theta),
            b=-scale * math.sin(theta),
            tx=500.0,
            c=-scale * math.sin(theta),
            d=-scale * math.cos(theta),
            ty=800.0,
        )
        pixels = [_invert_affine(forward, easting, northing) for easting, northing in visible]
        registration = register_shoreline(pixels, hydro, gcp_count=8, on_frame=flags)
        self.assertGreaterEqual(registration.iou, DEFAULT_IOU_THRESHOLD)
        self.assertLess(_angle_delta(registration.rotation_deg, theta_deg), 12.0)

    def test_similarity_roundtrip_direct_and_reflected(self):
        source = [(0.0, 0.0), (4.0, 0.5), (5.0, 3.0), (1.0, 4.0), (-1.0, 2.0)]
        theta = math.radians(20.0)
        scale = 2.5
        forward = Affine(
            a=scale * math.cos(theta),
            b=-scale * math.sin(theta),
            tx=10.0,
            c=scale * math.sin(theta),
            d=scale * math.cos(theta),
            ty=-4.0,
        )
        target = [forward.apply(x, y) for x, y in source]
        fit = fit_similarity(source, target)
        for (x, y), (u, v) in zip(source, target, strict=True):
            got_u, got_v = fit.apply(x, y)
            self.assertLess(math.hypot(got_u - u, got_v - v), 1e-6)
        reflected = Affine(
            a=-forward.a,
            b=forward.b,
            tx=forward.tx + 2.0 * forward.a * 2.0,
            c=-forward.c,
            d=forward.d,
            ty=forward.ty + 2.0 * forward.c * 2.0,
        )
        reflected_target = [reflected.apply(x, y) for x, y in source]
        reflected_fit = fit_similarity(source, reflected_target)
        for (x, y), (u, v) in zip(source, reflected_target, strict=True):
            got_u, got_v = reflected_fit.apply(x, y)
            self.assertLess(math.hypot(got_u - u, got_v - v), 1e-6)


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

    def test_gap_in_the_outer_line_still_closes(self):
        width, height = 160, 140
        rgb = bytearray([255]) * (width * height * 3)
        # An irregular shore with a two-pixel gap. A rectangle would look like a neatline.
        ring = [(24, 40), (70, 22), (120, 36), (132, 78), (100, 112), (48, 108), (22, 74)]
        _ink_polyline(rgb, width, ring, close=True, skip_start=3)
        figures = extract_shoreline_figures(RasterImage(width=width, height=height, rgb=bytes(rgb)))
        self.assertGreaterEqual(len(figures), 1)
        xs = [point[0] for point in figures[0].ring]
        ys = [point[1] for point in figures[0].ring]
        self.assertLess(min(xs), 40)
        self.assertGreater(max(xs), 110)
        self.assertLess(min(ys), 40)
        self.assertGreater(max(ys), 95)

    def test_straight_shore_seal_is_kept(self):
        # A long straight shore used to be cleared as a section line. That
        # opened the water onto the page, so the tracer never saw the lake.
        width, height = 240, 120
        rgb = bytearray([255]) * (width * height * 3)
        left = [(40 + (6 if index % 2 == 0 else 0), y) for index, y in enumerate(range(28, 80, 5))]
        right = [(190 + (6 if index % 2 == 0 else 0), y) for index, y in enumerate(range(80, 26, -5))]
        # The bottom is one long horizontal shore. It is the seal of the water.
        ring = left + [(left[-1][0], 96), (190, 96)] + right
        _ink_polyline(rgb, width, ring, close=True)
        for x in range(width):
            index = (6 * width + x) * 3
            rgb[index : index + 3] = b"\x00\x00\x00"
        rings = extract_shoreline_candidates(RasterImage(width=width, height=height, rgb=bytes(rgb)))
        self.assertTrue(
            any(max(point[0] for point in ring) - min(point[0] for point in ring) > 140 for ring in rings)
        )

    def test_long_road_enclosure_is_not_the_outer_shore_candidate(self):
        width, height = 220, 180
        rgb = bytearray([255]) * (width * height * 3)
        road = [(20, 70), (200, 70), (200, 82), (20, 82), (20, 70)]
        lake = [(70, 50), (150, 44), (165, 100), (120, 125), (60, 110), (55, 75)]
        _ink_polyline(rgb, width, road, close=True)
        _ink_polyline(rgb, width, lake, close=True)
        for cx, cy in ((95, 70), (115, 88), (90, 98)):
            index = (cy * width + cx) * 3
            rgb[index : index + 3] = b"\x00\x00\x00"
        image = RasterImage(width=width, height=height, rgb=bytes(rgb))
        rings = extract_shoreline_candidates(image)
        self.assertGreaterEqual(len(rings), 1)
        self.assertLess(_ring_centroid_distance(rings[0], lake), 35.0)

    def test_neatline_touching_lake_skips_map_frame_enclosure(self):
        width, height = 260, 210
        rgb = bytearray([255]) * (width * height * 3)
        inset = 10
        neatline = [
            (inset, inset),
            (width - 1 - inset, inset),
            (width - 1 - inset, height - 1 - inset),
            (inset, height - 1 - inset),
        ]
        lake = [(70, 70), (155, 62), (175, 118), (125, 148), (68, 130), (62, 92)]
        _ink_polyline(rgb, width, neatline, close=True)
        _ink_polyline(rgb, width, lake, close=True)
        for cx, cy in ((95, 88), (120, 105), (100, 118)):
            index = (cy * width + cx) * 3
            rgb[index : index + 3] = b"\x00\x00\x00"
        image = RasterImage(width=width, height=height, rgb=bytes(rgb))
        rings = extract_shoreline_candidates(image)
        self.assertGreaterEqual(len(rings), 1)
        long_side = max(width, height)
        lake_x = sum(point[0] for point in lake) / len(lake)
        lake_y = sum(point[1] for point in lake) / len(lake)
        best = min(
            rings,
            key=lambda ring: math.hypot(
                sum(point[0] for point in ring) / len(ring) - lake_x,
                sum(point[1] for point in ring) / len(ring) - lake_y,
            ),
        )
        xs = [point[0] for point in best]
        ys = [point[1] for point in best]
        span = max(max(xs) - min(xs), max(ys) - min(ys)) / long_side
        self.assertLess(span, 0.72)
        self.assertLess(span, NEATLINE_SPAN)

    def test_dual_disconnected_basins_both_surface_as_figures(self):
        width, height = 200, 120
        rgb = bytearray([255]) * (width * height * 3)
        left = [(25, 30), (70, 25), (80, 70), (30, 75)]
        right = [(115, 35), (165, 32), (170, 78), (120, 82)]
        _ink_polyline(rgb, width, left, close=True)
        _ink_polyline(rgb, width, right, close=True)
        figures = extract_shoreline_figures(RasterImage(width=width, height=height, rgb=bytes(rgb)))
        centroids = [
            (
                sum(point[0] for point in figure.ring) / len(figure.ring),
                sum(point[1] for point in figure.ring) / len(figure.ring),
            )
            for figure in figures
        ]
        self.assertGreaterEqual(len(centroids), 2)
        gap = abs(centroids[0][0] - centroids[1][0])
        self.assertGreater(gap, 35.0)

    def test_shore_that_hits_the_page_edge_is_marked(self):
        width, height = 120, 100
        rgb = bytearray([255]) * (width * height * 3)
        # Leaves the right edge, so the water polygon is closed by the page border.
        ring = [(20, 30), (55, 18), (width - 1, 28), (width - 1, 72), (50, 84), (18, 60)]
        _ink_polyline(rgb, width, ring, close=True)
        figures = extract_shoreline_figures(RasterImage(width=width, height=height, rgb=bytes(rgb)))
        self.assertGreaterEqual(len(figures), 1)
        marked = [figure for figure in figures if any(figure.on_frame)]
        self.assertGreaterEqual(len(marked), 1)
        self.assertLess(sum(1 for flag in marked[0].on_frame if flag) / len(marked[0].on_frame), 0.62)

    def test_span_wide_rectangle_stays_a_figure(self):
        # The top and bottom edges cover 87% of the width, over the 80% span
        # clear. Those pixels seal the basin, so the rectangle is still traced.
        width, height = 200, 80
        rgb = bytearray([255]) * (width * height * 3)
        left, right = 13, 186
        top, bottom = 20, 50
        self.assertGreaterEqual((right - left + 1) / width, 0.87)
        _ink_polyline(
            rgb,
            width,
            [(left, top), (right, top), (right, bottom), (left, bottom)],
            close=True,
        )
        for x in range(left + 8, right - 8, 7):
            for y in range(top + 6, bottom - 4, 8):
                index = (y * width + x) * 3
                rgb[index : index + 3] = b"\x00\x00\x00"
        figures = extract_shoreline_figures(RasterImage(width=width, height=height, rgb=bytes(rgb)))
        self.assertTrue(figures)
        spans = []
        for figure in figures:
            xs = [point[0] for point in figure.ring]
            spans.append((max(xs) - min(xs)) / width)
        self.assertTrue(any(span >= 0.80 for span in spans))

    def test_seal_keeps_only_the_hole_that_opens(self):
        width, height = 120, 90
        mask = bytearray(width * height)

        def ink_box(x0: int, y0: int, x1: int, y1: int) -> None:
            for x in range(x0, x1 + 1):
                mask[y0 * width + x] = 1
                mask[y1 * width + x] = 1
            for y in range(y0, y1 + 1):
                mask[y * width + x0] = 1
                mask[y * width + x1] = 1

        ink_box(12, 18, 70, 58)
        for x in range(40, 115):
            mask[17 * width + x] = 1
        ink_box(78, 64, 116, 86)
        protect = _hole_shore_pixels(mask, width, height, 36)
        lake_seal = sum(protect[58 * width + x] for x in range(22, 60))
        self.assertGreater(lake_seal, 10)
        self.assertEqual(sum(protect[17 * width + x] for x in range(40, 115)), 0)
        other = sum(protect[64 * width + x] for x in range(86, 110))
        other += sum(protect[y * width + 116] for y in range(70, 82))
        self.assertEqual(other, 0)

    def test_reversed_spur_is_not_only_a_road(self):
        # A collinear out-and-back is road-straight and empty, and it reverses.
        # The candidate filter keeps that ring so spur cleanup can run.
        points: list[tuple[float, float]] = []
        x = 20.0
        while x < 160.0:
            points.append((x, 30.0))
            x += 4.0
        y = 30.0
        while y < 100.0:
            points.append((160.0, y))
            y += 4.0
        x = 160.0
        while x > 90.0:
            points.append((x, 100.0))
            x -= 4.0
        points.append((90.0, 100.0))
        points.append((90.0, 110.0))
        points.append((90.0, 100.0))
        x = 86.0
        while x > 20.0:
            points.append((x, 100.0))
            x -= 4.0
        y = 100.0
        while y > 30.0:
            points.append((20.0, y))
            y -= 4.0
        width, height = 200, 140
        mask = bytearray(width * height)
        self.assertTrue(_has_reversal(points))
        self.assertGreater(_ring_straightness(points), MAX_ROAD_STRAIGHTNESS)
        self.assertTrue(_road_like_ring(points, mask, width, height))
        self.assertFalse(_road_like_ring(points, mask, width, height) and not _has_reversal(points))


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

    def test_single_sheet_auto_lake_uses_min_gate_iou(self):
        """Regression pin for the single-sheet min(per-sheet gate_iou) publish path."""
        item = self.by_pair[("0417300", "0417300a")]
        self.assertFalse(item.get("multi_sheet"))
        self.assertEqual(len(item["sheets"]), 1)
        sheet = item["sheets"][0]
        self.assertEqual(item["iou"], sheet["iou"])
        self.assertEqual(item["qa_status"], "auto")
        self.assertEqual(item["reasons"], [])
        self.assertAlmostEqual(item["iou"], 0.946772, places=6)
        self.assertAlmostEqual(item["rotation_deg"], 13.862, places=3)

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

    def test_multi_sheet_publishes_when_every_sheet_passes(self):
        combined = []
        for map_key in ("2271600a", "2271600b"):
            item = self.by_pair[("2271600", map_key)]
            self.assertGreaterEqual(item["iou"], 0.90)
            self.assertNotIn("multi_sheet", item["reasons"])
            self.assertEqual(item["qa_status"], "auto")
            self.assertGreaterEqual(item["combined_iou"], 0.90)
            combined.append(item["combined_iou"])
        self.assertEqual(combined[0], combined[1])
        queued = [row for row in self.queue["items"] if row["wbic"] == "2271600"]
        self.assertEqual(queued, [])

    def test_multi_sheet_blocks_when_a_sheet_misses_the_gate(self):
        items = [
            {
                "wbic": "2271600",
                "qa_status": "auto",
                "multi_sheet": True,
                "iou": 0.95,
                "sheets": [{"iou": 0.95}],
                "reasons": [],
            },
            {
                "wbic": "2271600",
                "qa_status": "needs_review",
                "multi_sheet": True,
                "iou": 0.40,
                "sheets": [{"iou": 0.40}],
                "reasons": ["iou_below_threshold"],
            },
        ]
        georef._fail_closed_multi_sheet(items, {}, DEFAULT_IOU_THRESHOLD)
        self.assertIn("multi_sheet", items[0]["reasons"])
        self.assertEqual(items[0]["qa_status"], "needs_review")
        self.assertIn("multi_sheet", items[1]["reasons"])

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

            def wrapped(pixel_ring, hydro_ring, gcp_count=8, on_frame=None):
                # The first sheet's figures fail. Later lakes still register.
                calls["n"] += 1
                if calls["n"] <= 2:
                    raise ValueError("singular linear system")
                return real(pixel_ring, hydro_ring, gcp_count=gcp_count, on_frame=on_frame)

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


def _sheet(iou: float, cut: float, name: str, ring: list[tuple[float, float]] | None = None) -> dict:
    affine = Affine(a=1.0, b=0.0, tx=0.0, c=0.0, d=1.0, ty=0.0)
    blank = ThinPlateSpline(
        sources=[],
        easting_weights=[],
        northing_weights=[],
        easting_affine=(0.0, 0.0, 0.0),
        northing_affine=(0.0, 0.0, 0.0),
    )
    registration = Registration(
        affine=affine,
        tps=blank,
        gcps=(),
        affine_iou=iou,
        tps_iou=iou,
        method="affine",
    )
    sheet = {
        "page": Path(name),
        "cut_fraction": cut,
        "registration": registration,
        "ring": ring if ring is not None else [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
    }
    return sheet


def _ring_centroid_distance(
    ring: list[tuple[float, float]],
    target: list[tuple[int, int]],
) -> float:
    cx = sum(point[0] for point in ring) / len(ring)
    cy = sum(point[1] for point in ring) / len(ring)
    tx = sum(point[0] for point in target) / len(target)
    ty = sum(point[1] for point in target) / len(target)
    return math.hypot(cx - tx, cy - ty)


class NearGateIouTest(unittest.TestCase):
    def test_near_gate_is_derived_and_not_publish_threshold(self):
        self.assertEqual(NEAR_GATE_IOU, DEFAULT_IOU_THRESHOLD - 0.01)
        self.assertAlmostEqual(NEAR_GATE_IOU, DEFAULT_IOU_THRESHOLD - 0.01)
        self.assertLess(NEAR_GATE_IOU, DEFAULT_IOU_THRESHOLD)

    def test_prefer_sheet_uses_near_gate_for_iou_tie_break(self):
        at_band = _sheet(NEAR_GATE_IOU, 0.0, "a.png")
        below_band = _sheet(NEAR_GATE_IOU - 0.02, 0.0, "b.png")
        below_band["ink"] = 0.5
        at_band["ink"] = 0.01
        self.assertTrue(georef._prefer_sheet(at_band, below_band))


class PublishGateTest(unittest.TestCase):
    def test_high_combined_iou_does_not_clear_gate_when_sheets_miss(self):
        hydro = [(0.0, 0.0), (200.0, 0.0), (200.0, 100.0), (0.0, 100.0)]
        left = [(0.0, 0.0), (95.0, 0.0), (95.0, 100.0), (0.0, 100.0)]
        right = [(105.0, 0.0), (200.0, 0.0), (200.0, 100.0), (105.0, 100.0)]
        sheets = [_sheet(0.72, 0.0, "p0.png", ring=left), _sheet(0.50, 0.0, "p0.png", ring=right)]
        for sheet in sheets:
            sheet["gate_iou"] = sheet["registration"].iou
        rings = [list(sheet["ring"]) for sheet in sheets]
        combined = union_polygon_iou(rings, hydro)
        gate = min(sheet["gate_iou"] for sheet in sheets)
        self.assertGreaterEqual(combined, DEFAULT_IOU_THRESHOLD)
        self.assertLess(gate, DEFAULT_IOU_THRESHOLD)
        low_iou = gate < DEFAULT_IOU_THRESHOLD
        reasons = georef._reasons(
            flowage=False,
            shared_sheet=False,
            low_iou=low_iou,
            multi_sheet=False,
            shoreline_found=True,
            registration_error=False,
            warp_failed=False,
        )
        self.assertIn("iou_below_threshold", reasons)
        self.assertNotEqual([], reasons)

    def test_fail_closed_does_not_clear_weak_on_combined_iou(self):
        hydro_ring = [(0.0, 0.0), (200.0, 0.0), (200.0, 100.0), (0.0, 100.0)]
        left = [(0.0, 0.0), (95.0, 0.0), (95.0, 100.0), (0.0, 100.0)]
        right = [(105.0, 0.0), (200.0, 0.0), (200.0, 100.0), (105.0, 100.0)]
        items = [
            {
                "wbic": "2271600",
                "qa_status": "auto",
                "multi_sheet": False,
                "iou": 0.91,
                "sheets": [
                    {"page": "p0.png", "iou": 0.72},
                    {"page": "p0.png", "iou": 0.50},
                ],
                "reasons": [],
                "_sheet_rings": [left, right],
            }
        ]
        polygons = {
            "2271600": type("Water", (), {"ring": hydro_ring})(),
        }
        georef._fail_closed_multi_sheet(items, polygons, DEFAULT_IOU_THRESHOLD)
        self.assertGreaterEqual(items[0]["combined_iou"], DEFAULT_IOU_THRESHOLD)
        self.assertIn("multi_sheet", items[0]["reasons"])
        self.assertEqual(items[0]["qa_status"], "needs_review")

    def test_pick_page_basins_keeps_plausible_same_page_pair(self):
        hydro = [(0.0, 0.0), (200.0, 0.0), (200.0, 100.0), (0.0, 100.0)]
        left_ring = [(5.0, 5.0), (90.0, 5.0), (90.0, 95.0), (5.0, 95.0)]
        right_ring = [(110.0, 5.0), (195.0, 5.0), (195.0, 95.0), (110.0, 95.0)]
        identity = Affine(1, 0, 0, 0, 1, 0)
        blank = ThinPlateSpline([], [], [], (0, 0, 0), (0, 0, 0))
        left_iou = polygon_iou(left_ring, hydro)
        right_iou = polygon_iou(right_ring, hydro)
        reg_left = Registration(
            affine=identity,
            tps=blank,
            gcps=(),
            affine_iou=left_iou,
            tps_iou=left_iou,
            method="affine",
        )
        reg_right = Registration(
            affine=identity,
            tps=blank,
            gcps=(),
            affine_iou=right_iou,
            tps_iou=right_iou,
            method="affine",
        )
        candidates = [
            {
                "page": Path("page.png"),
                "registration": reg_left,
                "ring": left_ring,
                "area": 1000.0,
                "cut_fraction": 0.0,
                "ink": 0.2,
            },
            {
                "page": Path("page.png"),
                "registration": reg_right,
                "ring": right_ring,
                "area": 900.0,
                "cut_fraction": 0.0,
                "ink": 0.18,
            },
        ]
        picked = georef._pick_page_basins(candidates, hydro)
        self.assertEqual(len(picked), 2)

    def test_pick_page_basins_rejects_foreign_basin(self):
        hydro = [(0.0, 0.0), (100.0, 0.0), (100.0, 80.0), (0.0, 80.0)]
        lake = [(10.0, 15.0), (45.0, 12.0), (48.0, 55.0), (12.0, 58.0)]
        foreign = [(300.0, 300.0), (360.0, 300.0), (360.0, 360.0), (300.0, 360.0)]
        reg_lake = register_shoreline(lake, hydro)
        reg_foreign = register_shoreline(foreign, [(280.0, 280.0), (380.0, 280.0), (380.0, 380.0), (280.0, 380.0)])
        candidates = [
            {
                "page": Path("page.png"),
                "registration": reg_lake,
                "ring": lake,
                "area": 1000.0,
                "cut_fraction": 0.0,
                "ink": 0.2,
            },
            {
                "page": Path("page.png"),
                "registration": reg_foreign,
                "ring": foreign,
                "area": 900.0,
                "cut_fraction": 0.0,
                "ink": 0.2,
            },
        ]
        picked = georef._pick_page_basins(candidates, hydro)
        self.assertEqual(len(picked), 1)

    def test_figure_quality_prefers_bathymetry_ink_on_near_ties(self):
        high_ink = {"registration": Registration(
            affine=Affine(1, 0, 0, 0, 1, 0),
            tps=ThinPlateSpline([], [], [], (0, 0, 0), (0, 0, 0)),
            gcps=(),
            affine_iou=0.58,
            tps_iou=0.58,
            method="affine",
        ), "ink": 0.18, "area": 10}
        low_ink = {"registration": Registration(
            affine=Affine(1, 0, 0, 0, 1, 0),
            tps=ThinPlateSpline([], [], [], (0, 0, 0), (0, 0, 0)),
            gcps=(),
            affine_iou=0.62,
            tps_iou=0.62,
            method="affine",
        ), "ink": 0.004, "area": 10}
        self.assertTrue(georef._prefer_sheet(high_ink, low_ink))
        strong = {"registration": Registration(
            affine=Affine(1, 0, 0, 0, 1, 0),
            tps=ThinPlateSpline([], [], [], (0, 0, 0), (0, 0, 0)),
            gcps=(),
            affine_iou=0.91,
            tps_iou=0.91,
            method="affine",
        ), "ink": 0.004, "area": 10}
        weak = {"registration": Registration(
            affine=Affine(1, 0, 0, 0, 1, 0),
            tps=ThinPlateSpline([], [], [], (0, 0, 0), (0, 0, 0)),
            gcps=(),
            affine_iou=0.62,
            tps_iou=0.62,
            method="affine",
        ), "ink": 0.22, "area": 10}
        self.assertTrue(georef._prefer_sheet(strong, weak))

    def test_false_shore_ring_flags_neatline_touching_boxes(self):
        ring = [(2.0, 2.0), (318.0, 2.0), (318.0, 408.0), (2.0, 408.0)]
        flags = (True, True, True, True)
        self.assertTrue(georef._false_shore_ring(ring, 320, 410, georef._flag_fraction(flags)))
        lake = [(40.0, 50.0), (120.0, 45.0), (130.0, 95.0), (70.0, 110.0)]
        self.assertFalse(georef._false_shore_ring(lake, 320, 410, 0.0))


class FailClosedGateTest(unittest.TestCase):
    def test_cut_page_is_not_dropped_for_a_near_best_closed_ring(self):
        closed = _sheet(0.86, 0.0, "a.png")
        cut = _sheet(0.40, 0.20, "b.png")
        pages = [Path("a.png"), Path("b.png"), Path("c.png")]
        kept, missed = georef._select_lake_sheets([closed, cut], pages)
        self.assertEqual({sheet["page"].name for sheet in kept}, {"a.png", "b.png"})
        self.assertEqual([page.name for page in missed], ["c.png"])

    def test_closed_pages_collapse_to_the_best_sheet(self):
        pages = [Path("a.png"), Path("b.png")]
        kept, missed = georef._select_lake_sheets(
            [_sheet(0.91, 0.0, "a.png"), _sheet(0.55, 0.0, "b.png")],
            pages,
        )
        self.assertEqual([sheet["page"].name for sheet in kept], ["a.png"])
        self.assertEqual(missed, [])

    def test_several_passing_pages_are_queued_and_each_is_warped(self):
        first = _sheet(0.92, 0.0, "north.png")
        second = _sheet(0.90, 0.0, "south.png")
        for sheet in (first, second):
            sheet["gate_iou"] = sheet["registration"].iou
        plan = georef._warp_plan_for([first, second], first, DEFAULT_IOU_THRESHOLD)
        self.assertEqual([name for _sheet_row, name in plan], ["warped.tif", "warped-south.tif"])
        reasons = georef._reasons(
            flowage=False,
            shared_sheet=False,
            low_iou=False,
            multi_sheet=False,
            shoreline_found=True,
            registration_error=False,
            warp_failed=False,
            several_pages_passed=True,
        )
        self.assertIn("multi_sheet", reasons)
        self.assertNotIn("iou_below_threshold", reasons)

    def test_partial_fragment_cannot_clear_the_gate_on_footprint_iou(self):
        hydro = [(0.0, 0.0), (100.0, 0.0), (100.0, 80.0), (0.0, 80.0)]
        fragment = [(0.0, 0.0), (20.0, 0.0), (20.0, 16.0), (0.0, 16.0)]
        sheet = _sheet(0.95, 0.25, "cut.png", ring=fragment)
        full = georef._full_hydro_iou(sheet, hydro)
        gate = min(sheet["registration"].iou, full)
        self.assertGreaterEqual(sheet["registration"].iou, DEFAULT_IOU_THRESHOLD)
        self.assertLess(full, 0.5)
        self.assertLess(gate, DEFAULT_IOU_THRESHOLD)
        covered = _sheet(0.90, 0.20, "whole.png", ring=list(hydro))
        self.assertGreaterEqual(
            min(covered["registration"].iou, georef._full_hydro_iou(covered, hydro)),
            DEFAULT_IOU_THRESHOLD,
        )

    def test_sheared_sweep_fallback_is_rejected(self):
        shear = Affine(a=5.0, b=0.0, tx=0.0, c=0.0, d=1.0, ty=0.0)
        similar = Affine(a=2.0, b=0.0, tx=0.0, c=0.0, d=-2.0, ty=0.0)
        self.assertGreater(anisotropy(shear), MAX_ANISOTROPY)
        self.assertLessEqual(anisotropy(similar), MAX_ANISOTROPY)
        self.assertIs(wi_register._sweep_fallback([shear, similar]), similar)
        with self.assertRaises(ValueError):
            wi_register._sweep_fallback([shear])


class InkWaiverBandTest(unittest.TestCase):
    def test_low_ink_passes_inside_near_gate_band(self):
        hydro = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
        ring = [(10.0, 10.0), (90.0, 10.0), (90.0, 90.0), (10.0, 90.0)]
        identity = Affine(1, 0, 0, 0, 1, 0)
        blank = ThinPlateSpline([], [], [], (0, 0, 0), (0, 0, 0))
        reg = Registration(
            affine=identity,
            tps=blank,
            gcps=(),
            affine_iou=NEAR_GATE_IOU,
            tps_iou=NEAR_GATE_IOU,
            method="affine",
        )
        ink = 0.002
        skip_ink = ink < georef.MIN_BASIN_INK and reg.iou < NEAR_GATE_IOU
        self.assertFalse(skip_ink)
        span = 0.80
        skip_large = (
            span > georef.LARGE_RING_SPAN
            and ink < georef.LARGE_RING_MIN_INK
            and reg.iou < NEAR_GATE_IOU
        )
        self.assertFalse(skip_large)
        self.assertTrue(
            georef._hydro_footprint_matches(reg, ring, hydro, cut_fraction=0.0)
        )


class NeatlineSpanEdgeTest(unittest.TestCase):
    def test_wavy_lake_below_neatline_span_is_not_false_shore(self):
        width, height = 400, 320
        ring = []
        for index in range(80):
            angle = index / 80 * 2 * math.pi
            radius = 140 + 25 * math.sin(angle * 5)
            ring.append((200 + radius * math.cos(angle), 160 + radius * math.sin(angle)))
        span = max(max(p[0] for p in ring) - min(p[0] for p in ring), max(p[1] for p in ring) - min(p[1] for p in ring))
        span /= max(width, height)
        self.assertGreater(span, 0.70)
        self.assertLess(span, NEATLINE_SPAN)
        self.assertFalse(georef._false_shore_ring(ring, width, height, 0.20))

    def test_straight_neatline_touching_frame_is_false_shore(self):
        width, height = 400, 320
        inset = 8
        ring = [
            (inset, inset),
            (width - inset, inset),
            (width - inset, height - inset),
            (inset, height - inset),
        ]
        self.assertTrue(georef._false_shore_ring(ring, width, height, 0.55))


class DistinctLowDpiSheetTest(unittest.TestCase):
    def test_distinct_low_dpi_bathymetry_stays_in_the_minimum(self):
        low = _sheet(0.35, 0.0, "scan.png")
        low["gate_iou"] = low["registration"].iou
        sharp = _sheet(0.82, 0.0, "letter.png")
        sharp["gate_iou"] = sharp["registration"].iou
        # The pages are different drawings. A dpi gap does not remove the scan,
        # so the lake minimum stays on the low side of the gate.
        sheets = [low, sharp]
        gate = min(sheet["gate_iou"] for sheet in sheets)
        self.assertEqual({sheet["page"].name for sheet in sheets}, {"scan.png", "letter.png"})
        self.assertAlmostEqual(gate, 0.35)
        self.assertLess(gate, DEFAULT_IOU_THRESHOLD)


class HydroFootprintMatchesTest(unittest.TestCase):
    def test_strong_overlap_skips_span_ratio_check(self):
        hydro = [(0.0, 0.0), (50.0, 0.0), (50.0, 50.0), (0.0, 50.0)]
        ring = [(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0)]
        sheet = _sheet(0.70, 0.0, "a.png", ring=ring)
        self.assertTrue(
            georef._hydro_footprint_matches(
                sheet["registration"],
                ring,
                hydro,
                cut_fraction=0.0,
            )
        )

    def test_partial_cut_relaxes_minimum_span_ratio(self):
        hydro = [(0.0, 0.0), (200.0, 0.0), (200.0, 120.0), (0.0, 120.0)]
        ring = [(0.0, 0.0), (50.0, 0.0), (50.0, 35.0), (0.0, 35.0)]
        sheet = _sheet(0.52, 0.15, "partial.png", ring=ring)
        self.assertTrue(
            georef._hydro_footprint_matches(
                sheet["registration"],
                ring,
                hydro,
                cut_fraction=0.12,
            )
        )
        self.assertFalse(
            georef._hydro_footprint_matches(
                sheet["registration"],
                ring,
                hydro,
                cut_fraction=0.0,
            )
        )


def _registration(iou: float) -> Registration:
    blank = ThinPlateSpline([], [], [], (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    return Registration(
        affine=Affine(1, 0, 0, 0, 1, 0),
        tps=blank,
        gcps=(),
        affine_iou=iou,
        tps_iou=iou,
        method="affine",
    )


def _oval_ring(width: int, height: int) -> list[tuple[int, int]]:
    cx, cy = width / 2, height / 2
    rx, ry = width * 0.16, height * 0.11
    ring: list[tuple[int, int]] = []
    for index in range(96):
        angle = index / 96 * 2 * math.pi
        ring.append((int(round(cx + rx * math.cos(angle))), int(round(cy + ry * math.sin(angle)))))
    return ring


def _dam_ring() -> list[tuple[int, int]]:
    """A large basin closed by one straight dam and a shallow bow.

    The outline is near a rectangle, so only the area cap keeps it out of the
    legend test. A smaller oval covers the boxiness cap.
    """
    chord = 120.0
    body = 50.0
    bow = 6.0
    x0, y0 = 30.0, 40.0
    points: list[tuple[int, int]] = []
    steps = 40
    for step in range(steps + 1):
        fraction = step / steps
        x = x0 + chord * fraction
        # Circular-ish bow above the rectangle's top edge.
        arch = math.sin(math.pi * fraction)
        y = y0 - bow * arch
        points.append((int(round(x)), int(round(y))))
    points.append((int(round(x0 + chord)), int(round(y0 + body))))
    points.append((int(round(x0)), int(round(y0 + body))))
    return points


def _traced_outline(
    ring: list[tuple[int, int]], width: int, height: int
) -> tuple[RasterImage, list[tuple[float, float]]]:
    rgb = bytearray([255]) * (width * height * 3)
    _ink_polyline(rgb, width, ring, close=True)
    image = RasterImage(width=width, height=height, rgb=bytes(rgb))
    figures = extract_shoreline_figures(image)
    if not figures:
        raise AssertionError("traced outline produced no figure")
    target_x = sum(point[0] for point in ring) / len(ring)
    target_y = sum(point[1] for point in ring) / len(ring)
    best = min(
        figures,
        key=lambda figure: math.hypot(
            sum(point[0] for point in figure.ring) / len(figure.ring) - target_x,
            sum(point[1] for point in figure.ring) / len(figure.ring) - target_y,
        ),
    )
    return image, list(best.ring)


def _outline_metrics(image: RasterImage, ring: list[tuple[float, float]]) -> dict[str, float]:
    area = abs(georef._polygon_area(ring))
    image_area = float(image.width * image.height)
    return {
        "area": area / image_area if image_area else 0.0,
        "box": _ring_boxiness(ring),
        "straight": _ring_straightness(ring),
        "ink": georef.interior_ink_fraction(image, ring),
    }


def _dense_rectangle(x0: float, y0: float, x1: float, y1: float, step: float = 4.0) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    x = x0
    while x < x1:
        points.append((x, y0))
        x += step
    y = y0
    while y < y1:
        points.append((x1, y))
        y += step
    x = x1
    while x > x0:
        points.append((x, y1))
        x -= step
    y = y1
    while y > y0:
        points.append((x0, y))
        y -= step
    return points


class ShorelineStraightnessTest(unittest.TestCase):
    def test_each_edge_is_counted_once(self):
        rectangle = _dense_rectangle(0.0, 0.0, 200.0, 120.0)
        self.assertGreater(_ring_straightness(rectangle), 0.85)
        wavy: list[tuple[float, float]] = []
        for index in range(180):
            angle = index / 180 * 2 * math.pi
            radius = 80 + 26 * math.sin(angle * 5)
            wavy.append((120 + radius * math.cos(angle), 120 + radius * math.sin(angle)))
        self.assertLess(_ring_straightness(wavy), 0.45)

    def test_opening_removes_a_thin_road_spur(self):
        ring = [
            (0.0, 0.0),
            (100.0, 0.0),
            (100.0, 39.6),
            (150.0, 39.6),
            (150.0, 40.2),
            (100.0, 40.2),
            (100.0, 80.0),
            (0.0, 80.0),
        ]
        # Repeat vertices so the rectangle edges are long enough to rasterize.
        dense: list[tuple[float, float]] = []
        for start, end in zip(ring, ring[1:] + ring[:1], strict=True):
            dense.append(start)
            for step in range(1, 8):
                fraction = step / 8
                dense.append(
                    (
                        start[0] + (end[0] - start[0]) * fraction,
                        start[1] + (end[1] - start[1]) * fraction,
                    )
                )
        cleaned = cleanup_shore_ring(dense)
        self.assertTrue(cleaned)
        self.assertTrue(any(max(point[0] for point in variant) < 120.0 for variant in cleaned))

    def test_out_and_back_reversal_is_a_corner(self):
        # Each spur leg is 10 px. The straight-run minimum on this ring is 16.
        # Counting the reversal as one run would make the whole perimeter straight.
        points: list[tuple[float, float]] = []
        x = 0.0
        while x < 200.0:
            points.append((x, 0.0))
            x += 4.0
        y = 0.0
        while y < 80.0:
            points.append((200.0, y))
            y += 4.0
        x = 200.0
        while x > 100.0:
            points.append((x, 80.0))
            x -= 4.0
        points.append((100.0, 80.0))
        points.append((100.0, 90.0))
        points.append((100.0, 80.0))
        x = 96.0
        while x > 0.0:
            points.append((x, 80.0))
            x -= 4.0
        y = 80.0
        while y > 0.0:
            points.append((0.0, y))
            y -= 4.0
        self.assertTrue(_has_reversal(points))
        straight = _ring_straightness(points)
        self.assertGreater(straight, 0.90)
        self.assertLess(straight, 0.99)
        self.assertGreater(straight, MAX_ROAD_STRAIGHTNESS)

    def test_spur_cleanup_is_not_limited_to_the_iou_winner(self):
        # The thin spur scores under a plain rectangle. Cleanup has to see
        # that ring even though another closed candidate currently leads.
        width, height = 180, 120
        image = RasterImage(width=width, height=height, rgb=bytes([255]) * (width * height * 3))
        outline = [
            (0.0, 0.0),
            (100.0, 0.0),
            (100.0, 39.6),
            (150.0, 39.6),
            (150.0, 40.2),
            (100.0, 40.2),
            (100.0, 80.0),
            (0.0, 80.0),
        ]
        dense: list[tuple[float, float]] = []
        for start, end in zip(outline, outline[1:] + outline[:1], strict=True):
            dense.append(start)
            for step in range(1, 8):
                fraction = step / 8
                dense.append(
                    (
                        start[0] + (end[0] - start[0]) * fraction,
                        start[1] + (end[1] - start[1]) * fraction,
                    )
                )
        hydro = [(0.0, 0.0), (100.0, 0.0), (100.0, 80.0), (0.0, 80.0)]
        spur = georef._score_ring(
            image,
            dense,
            None,
            hydro_ring=hydro,
            gcp_count=8,
            page=Path("spur.png"),
            _allow_spur_cleanup=False,
        )
        self.assertIsNotNone(spur)
        assert spur is not None
        self.assertGreater(_ring_straightness(dense), MAX_ROAD_STRAIGHTNESS)
        winner = {
            "registration": _registration(spur["registration"].iou + 0.04),
            "cut_fraction": 0.0,
            "ring": list(hydro),
            "ink": 0.2,
            "page": Path("winner.png"),
            "area": 8000.0,
            "image": image,
        }
        cleaned = georef._spur_cleaned_candidates(
            [winner, spur],
            image=image,
            hydro_ring=hydro,
            gcp_count=8,
            page=Path("spur.png"),
        )
        self.assertTrue(
            any(
                row["registration"].iou >= spur["registration"].iou + 0.02
                and max(point[0] for point in row["ring"]) < 120.0
                for row in cleaned
            )
        )


class AnchorAndLegendTest(unittest.TestCase):
    def test_near_gate_shore_beats_an_inkier_road(self):
        hydro = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        shore = {"registration": _registration(0.88), "ink": 0.06, "cut_fraction": 0.0, "ring": hydro}
        road = {"registration": _registration(0.62), "ink": 0.30, "cut_fraction": 0.0, "ring": hydro}
        self.assertIs(georef._choose_anchor([road, shore], hydro), shore)

    def test_ink_still_breaks_a_below_gate_near_tie(self):
        hydro = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        high_ink = {"registration": _registration(0.58), "ink": 0.18, "cut_fraction": 0.0, "ring": hydro}
        low_ink = {"registration": _registration(0.62), "ink": 0.004, "cut_fraction": 0.0, "ring": hydro}
        self.assertIs(georef._choose_anchor([low_ink, high_ink], hydro), high_ink)

    def test_partial_footprint_cannot_outrank_a_closed_shore(self):
        hydro = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
        closed_ring = list(hydro)
        fragment = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
        closed = {
            "registration": _registration(0.73),
            "ink": 0.2,
            "cut_fraction": 0.0,
            "ring": closed_ring,
        }
        partial = {
            "registration": _registration(0.85),
            "ink": 0.01,
            "cut_fraction": 0.25,
            "ring": fragment,
        }
        self.assertIs(georef._choose_anchor([partial, closed], hydro), closed)

    def test_legend_rectangle_is_not_a_shore(self):
        ring = _dense_rectangle(4.0, 4.0, 80.0, 50.0, step=3.0)
        straight = _ring_straightness(ring)
        self.assertTrue(georef._legend_box(ring, 0.004, straight, area_fraction=0.05))
        self.assertFalse(georef._legend_box(ring, 0.2, straight, area_fraction=0.05))

    def test_small_rectangle_is_a_legend_and_a_large_one_is_not(self):
        ring = _dense_rectangle(10.0, 10.0, 90.0, 70.0, step=6.0)
        straight = _ring_straightness(ring)
        self.assertGreater(_ring_boxiness(ring), 0.90)
        self.assertTrue(georef._legend_box(ring, 0.005, straight, area_fraction=0.05))
        self.assertFalse(georef._legend_box(ring, 0.005, straight, area_fraction=0.20))

    def test_traced_oval_and_dammed_basin_stay_candidates(self):
        oval_image, oval = _traced_outline(_oval_ring(200, 200), 200, 200)
        oval_metrics = _outline_metrics(oval_image, oval)
        self.assertLess(oval_metrics["area"], 0.08)
        self.assertGreater(oval_metrics["box"], 0.70)
        self.assertLess(oval_metrics["box"], 0.90)
        self.assertLess(oval_metrics["ink"], 0.012)
        self.assertFalse(
            georef._legend_box(
                oval,
                oval_metrics["ink"],
                oval_metrics["straight"],
                area_fraction=oval_metrics["area"],
            )
        )
        oval_row = georef._score_ring(
            oval_image,
            oval,
            [False] * len(oval),
            hydro_ring=oval,
            gcp_count=8,
            page=Path("oval.png"),
        )
        self.assertIsNotNone(oval_row)

        dam_image, dam = _traced_outline(_dam_ring(), 220, 160)
        dam_metrics = _outline_metrics(dam_image, dam)
        self.assertGreater(dam_metrics["area"], 0.08)
        self.assertGreaterEqual(dam_metrics["straight"], 0.40)
        self.assertGreaterEqual(dam_metrics["box"], 0.90)
        self.assertLess(dam_metrics["ink"], georef._LEGEND_MAX_INK)
        self.assertFalse(
            georef._legend_box(
                dam,
                dam_metrics["ink"],
                dam_metrics["straight"],
                area_fraction=dam_metrics["area"],
            )
        )
        dam_row = georef._score_ring(
            dam_image,
            dam,
            [False] * len(dam),
            hydro_ring=dam,
            gcp_count=8,
            page=Path("dam.png"),
        )
        self.assertIsNotNone(dam_row)


class CitationPageTest(unittest.TestCase):
    def test_blank_citation_is_not_a_map_sheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blank = root / "cite.png"
            write_png(blank, RasterImage(80, 100, bytes([250]) * (80 * 100 * 3)))
            ink = bytearray([255]) * (80 * 100 * 3)
            for y in range(15, 70):
                for x in range(10, 70):
                    if (x + y) % 5 == 0:
                        offset = (y * 80 + x) * 3
                        ink[offset : offset + 3] = b"\x10\x10\x10"
            drawn = root / "map.png"
            write_png(drawn, RasterImage(80, 100, bytes(ink)))
            self.assertTrue(georef._is_citation_raster(blank))
            self.assertTrue(georef._is_citation_raster(blank, long_inches=11.0))
            self.assertFalse(georef._is_citation_raster(drawn))
            self.assertFalse(georef._is_citation_raster(blank, long_inches=36.0))

    def test_light_map_sheet_is_not_a_citation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            width, height = 360, 280
            rgb = bytearray([255]) * (width * height * 3)
            _ink_polyline(rgb, width, _oval_ring(width, height), close=True)
            light = root / "light-map.png"
            write_png(light, RasterImage(width=width, height=height, rgb=bytes(rgb)))
            self.assertLess(georef._sampled_dark_fraction(read_png(light)), georef._CITATION_DARK_FRACTION)
            self.assertFalse(georef._is_citation_raster(light))
            self.assertFalse(georef._is_citation_raster(light, long_inches=42.0))

            blank = root / "blank-large.png"
            write_png(blank, RasterImage(width=width, height=height, rgb=bytes([250]) * (width * height * 3)))
            darker = root / "darker.png"
            dark_rgb = bytearray([255]) * (width * height * 3)
            for index in range(0, width * height, 4):
                offset = index * 3
                dark_rgb[offset : offset + 3] = b"\x10\x10\x10"
            write_png(darker, RasterImage(width=width, height=height, rgb=bytes(dark_rgb)))
            entry = {
                "raster_pages": [
                    {"path": str(light), "page": 0, "pixels": [width, height], "dpi": 8},
                    {"path": str(darker), "page": 1, "pixels": [width, height], "dpi": 8},
                ]
            }
            kept = georef._page_rasters(entry, light)
            self.assertIn(light, kept)

            letter = root / "cite.png"
            write_png(letter, RasterImage(width=80, height=100, rgb=bytes([250]) * (80 * 100 * 3)))
            letter_entry = {
                "raster_pages": [
                    {"path": str(light), "page": 0, "pixels": [width, height], "dpi": 8},
                    {"path": str(letter), "page": 1, "pixels": [110, 85], "dpi": 10},
                ]
            }
            maps = georef._page_rasters(letter_entry, light)
            self.assertEqual(maps, [light])


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
