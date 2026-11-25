# SPDX-License-Identifier: GPL-3.0-only
"""Pull the dominant lake shoreline out of a bathymetry map raster.

Borders, PLSS grid lines, and legend blocks are suppressed before the shoreline
is chosen. The kept curve is the outer edge of the largest white region that
the ink fully encloses: that is the water inside the drawn shore. An inner
depth contour is a smaller hole, so it loses to the shore when the shore is
closed. A hole that covers the whole sheet is the map frame and is skipped.

Real sheets often leave a gap in that outer line, or the line runs off the
page. A second pass closes small gaps, keeps a detected neatline as a wall,
and returns those water polygons too. Vertices that sit on the neatline or the
page edge are marked ``on_frame`` so registration can treat them as unknown
shore. In the near-gate band, ink and area decide when the overlap gap is
within 0.10. A wider gap keeps the higher overlap.

A long straight run is usually a section line or a road, and those pixels are
cleared before tracing. A cased road is two long parallel lines with a narrow
white gap. Those lines are cleared too, including when they run diagonally,
unless they are the seal of the basin. A shore can be straight for a long
stretch, and a span-wide shore is the same kind of line. The seal test runs
before either clear. Only the pixels whose deletion would open that basin are
kept, and only for the basin that opens. A line that merely touches the water,
and the border of a separate rectangle, are still removed. A road must not
become the wall that cuts a bay off the basin, and it must not be the stroke
that opens the basin onto the page.

``clear_projected_casings`` is the second pass. Georef calls it after the
affine, with road centerlines inverse-projected into the sheet. A narrow pair
of ink lines beside that centerline is cleared. A single shore line is left
in place. The straight-pair detector above still runs on the first trace.

``clear_cartographic_lines`` runs on every sheet after that. It clears long
straight ink that shares the sheet grid: dashed or slightly rotated section
lines, the neatline, and border ticks. Georef keeps the new trace only when
the overlap rises and an irregular basin stays sealed.
"""

from __future__ import annotations

import math
from array import array
from collections import deque
from dataclasses import dataclass

from fishy_pipeline.states.wi.pngutil import RasterImage

# A row or column this full of ink is a neatline or a PLSS line, not a shore.
SPAN_RATIO = 0.80
DARK_THRESHOLD = 128
MIN_INTERIOR = 40
# Components whose box covers the whole sheet are the map frame, not a lake.
MAX_FRAME_FRACTION = 0.95
# A white hole this much of the sheet is the neatline interior, not a lake.
FRAME_HOLE_AREA = 0.45
# Shore tracing does not need the full scan. Coordinates are scaled back.
SHORE_MAX_SIDE = 1800
# A ring this small on the long side of the sheet is a symbol or a letter, not the lake.
MIN_FIGURE_SPAN = 0.12
# Gap-closing works on a smaller mask. Coordinates are scaled back to the scan.
SHORE_WORK_SIDE = 720
# A white region smaller than this fraction of the work mask is not the lake.
MIN_HOLE_FRACTION = 0.025
# Land inside the neatline has most of its boundary on the frame. A cut lake does not.
MAX_FRAME_BOUNDARY = 0.62
# A long, nearly straight boundary with almost no enclosed depth ink is a road or grid.
MAX_ROAD_STRAIGHTNESS = 0.48
MIN_LAKE_INK = 0.012
# A sheet-spanning ring with this much boundary on the neatline or page edge is not the lake.
NEATLINE_SPAN = 0.88
NEATLINE_FRAME_BOUNDARY = 0.12
# Neatlines are nearly straight; irregular lake shores can share the span band.
NEATLINE_MIN_STRAIGHTNESS = 0.40
# A PLSS line or neatline crosses a real share of the sheet. 12% is under the
# 14% axis-aligned run clear, so a line that is only nearly horizontal still
# qualifies, and a short straight beach does not.
CARTO_MIN_FRACTION = 0.12
# Section lines share a grid angle. 6° keeps a scanned PLSS stroke and drops a
# shore that is straight for only part of a bay.
CARTO_ANGLE_DEG = 6.0
# Dashed section ink breaks. A gap under 4% of the long side, within 2.5 work
# pixels of the same line, is still one stroke.
CARTO_GAP_FRACTION = 0.04
CARTO_LATERAL_PX = 2.5
# Crossings add side ink. A shore that depth lines leave along its length does not.
CARTO_MAX_SIDE_FRACTION = 0.30
# Ticks are stubs on the neatline: longer than a speck, shorter than a shore.
CARTO_TICK_MIN_PX = 4
CARTO_TICK_MAX_FRACTION = 0.045
# A hole this straight is a section cell or a frame, not a lake basin.
CARTO_CELL_STRAIGHTNESS = 0.72


def extract_shoreline(image: RasterImage) -> list[tuple[float, float]] | None:
    """Return an ordered outer shoreline in pixel coordinates, or None."""
    candidates = extract_shoreline_candidates(image)
    if not candidates:
        return None
    return candidates[0]


def extract_shoreline_candidates(
    image: RasterImage,
    road_mask: bytearray | None = None,
) -> list[list[tuple[float, float]]]:
    """The dominant enclosed shore, and the dominant ink outline when it is lake-scale.

    A small hole can be affine-stretched onto the hydro polygon and score a
    higher overlap than the real shore. Those rings are not candidates. The
    georeferencer keeps whichever of these two figures overlaps the hydro
    polygon more.
    """
    width = image.width
    height = image.height
    if width < 8 or height < 8:
        return []
    if road_mask is None:
        mask = _dark_mask(image)
        _clear_cased_roads(mask, width, height)
    else:
        mask = road_mask
    small, small_w, small_h, factor, frame = _trace_work_mask(mask, width, height, SHORE_MAX_SIDE)
    holes = _ranked_hole_contours(small, small_w, small_h, frame)
    rings: list[list[tuple[float, float]]] = []
    for _area, contour in holes[:4]:
        scaled = _scale_ring(contour, factor)
        span = _span_fraction(scaled, width, height)
        if len(scaled) >= 8 and span >= MIN_FIGURE_SPAN:
            if _neatline_enclosure(scaled, width, height, frame, factor):
                continue
            # An out-and-back spur is a reversal, not a road. Dropping it here
            # means the spur cleanup never sees the shore it is meant to repair.
            if (
                span >= 0.22
                and _road_like_ring(scaled, mask, image.width, image.height)
                and not _has_reversal(scaled)
            ):
                continue
            rings.append(scaled)
    fallback = _component_shoreline(small, small_w, small_h, frame)
    if fallback is not None:
        outline = _scale_ring(fallback, factor)
        outline_span = _span_fraction(outline, width, height)
        if len(outline) >= 8 and outline_span >= MIN_FIGURE_SPAN:
            if not rings or outline_span >= 0.5 * _span_fraction(rings[0], width, height):
                rings.append(outline)
    return rings


def _span_fraction(ring: list[tuple[float, float]], width: int, height: int) -> float:
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    long_side = max(width, height)
    if long_side <= 0:
        return 0.0
    return max(max(xs) - min(xs), max(ys) - min(ys)) / long_side


def _trace_work_mask(
    mask: bytearray, width: int, height: int, max_side: int
) -> tuple[bytearray, int, int, int, tuple[int, int, int, int] | None]:
    """Downsampled ink mask with neatline and long straight runs suppressed."""
    work = bytearray(mask)
    _clear_span_lines(work, width, height, SPAN_RATIO)
    small, small_w, small_h, factor = _shrink_mask(work, width, height, max_side)
    frame = _detect_frame(small, small_w, small_h)
    if max(width, height) >= 400:
        run_len = max(8, int(0.14 * max(small_w, small_h)))
        _clear_long_runs(
            small,
            small_w,
            small_h,
            run_len,
            keep=frame,
            protect=_hole_shore_pixels(small, small_w, small_h, run_len, frame),
        )
    return small, small_w, small_h, factor, frame


def _neatline_enclosure(
    ring: list[tuple[float, float]],
    width: int,
    height: int,
    frame: tuple[int, int, int, int] | None,
    factor: int,
) -> bool:
    span = _span_fraction(ring, width, height)
    if span < NEATLINE_SPAN:
        return False
    work_w = max(1, math.ceil(width / factor))
    work_h = max(1, math.ceil(height / factor))
    tol = 3.0 * factor
    hits = 0
    for point in ring:
        on_edge = (
            point[0] <= tol
            or point[1] <= tol
            or point[0] >= width - 1 - tol
            or point[1] >= height - 1 - tol
        )
        on_neatline = False
        if frame is not None:
            on_neatline = _vertex_on_frame(
                point[0] / factor,
                point[1] / factor,
                work_w,
                work_h,
                frame,
                tol=3.0,
            )
        if on_edge or on_neatline:
            hits += 1
    if hits / max(len(ring), 1) < NEATLINE_FRAME_BOUNDARY:
        return False
    return _ring_straightness(ring) >= NEATLINE_MIN_STRAIGHTNESS


def _component_shoreline(
    mask: bytearray,
    width: int,
    height: int,
    frame: tuple[int, int, int, int] | None = None,
) -> list[tuple[float, float]] | None:
    """Older path: the dark stroke that encloses the most water."""
    best_fill, best_area = _best_enclosed(mask, width, height)
    if best_fill is None:
        grown = _dilate(_dilate(mask, width, height), width, height)
        best_fill, best_area = _best_enclosed(grown, width, height)
    if best_fill is None or best_area < MIN_INTERIOR:
        return None
    contour = _trace_outer(best_fill, width, height)
    if len(contour) < 8:
        return None
    return contour


def _shrink_mask(
    mask: bytearray, width: int, height: int, max_side: int
) -> tuple[bytearray, int, int, int]:
    """Block any-ink downsample. ``factor`` maps small pixels back to source pixels."""
    long_side = max(width, height)
    if long_side <= max_side:
        return mask, width, height, 1
    factor = math.ceil(long_side / max_side)
    small_w = math.ceil(width / factor)
    small_h = math.ceil(height / factor)
    out = bytearray(small_w * small_h)
    for y in range(height):
        row = y * width
        out_row = (y // factor) * small_w
        for x in range(width):
            if mask[row + x]:
                out[out_row + (x // factor)] = 1
    return out, small_w, small_h, factor


def interior_ink_fraction(
    image: RasterImage,
    ring: tuple[tuple[float, float], ...] | list[tuple[float, float]],
) -> float:
    """Share of pixels inside ``ring`` that are bathymetry ink (dark strokes)."""
    width = image.width
    height = image.height
    mask = _dark_mask(image)
    small, small_w, small_h, factor = _shrink_mask(mask, width, height, SHORE_WORK_SIDE)
    indices = _ring_pixel_indices(ring, small_w, small_h, factor, width, height)
    if not indices:
        return 0.0
    ink = sum(small[index] for index in indices)
    return ink / len(indices)


def _ring_pixel_indices(
    ring: tuple[tuple[float, float], ...] | list[tuple[float, float]],
    work_w: int,
    work_h: int,
    factor: int,
    source_w: int,
    source_h: int,
) -> list[int]:
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    min_x = max(0, int(min(xs) / factor))
    max_x = min(work_w - 1, int(max(xs) / factor))
    min_y = max(0, int(min(ys) / factor))
    max_y = min(work_h - 1, int(max(ys) / factor))
    if max_x < min_x or max_y < min_y:
        return []
    points = list(ring)
    picked: list[int] = []

    def inside(x: float, y: float) -> bool:
        inside_flag = False
        previous = points[-1]
        for current in points:
            y1, y2 = previous[1], current[1]
            if (y1 > y) != (y2 > y):
                x_cross = (current[0] - previous[0]) * (y - y1) / (y2 - y1 + 1e-12) + previous[0]
                if x < x_cross:
                    inside_flag = not inside_flag
            previous = current
        return inside_flag

    step = 2 if (max_x - min_x + 1) * (max_y - min_y + 1) > 8000 else 1
    for y in range(min_y, max_y + 1, step):
        row = y * work_w
        for x in range(min_x, max_x + 1, step):
            if inside((x + 0.5) * factor, (y + 0.5) * factor):
                picked.append(row + x)
    return picked


def _ring_straightness(ring: tuple[tuple[float, float], ...] | list[tuple[float, float]]) -> float:
    """Fraction of perimeter that lies on long, nearly straight runs.

    Each edge is counted once. Counting the same run again at every vertex
    saturated ordinary lake shores at 1, so the road filter dropped them.
    """
    points = list(ring)
    count = len(points)
    if count < 8:
        return 0.0
    lengths: list[float] = []
    directions: list[tuple[float, float]] = []
    perimeter = 0.0
    for index in range(count):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % count]
        length = math.hypot(x2 - x1, y2 - y1)
        lengths.append(length)
        perimeter += length
        if length < 1e-9:
            directions.append((0.0, 0.0))
        else:
            directions.append(((x2 - x1) / length, (y2 - y1) / length))
    if perimeter < 1e-6:
        return 0.0
    min_run = max(16.0, 0.05 * _ring_span(ring))
    straight = 0.0
    index = 0
    while index < count:
        if lengths[index] < 1e-9:
            index += 1
            continue
        ux, uy = directions[index]
        run = 0.0
        step = 0
        while step < count:
            edge = (index + step) % count
            if lengths[edge] < 1e-9:
                step += 1
                continue
            vx, vy = directions[edge]
            # A 180-degree reversal has no cross product. It is a corner: an
            # out-and-back road spur must not extend the straight run.
            if step > 0 and (
                abs(ux * vy - uy * vx) > 0.12 or ux * vx + uy * vy < 0.0
            ):
                break
            run += lengths[edge]
            step += 1
        if run >= min_run and step > 0:
            straight += run
            index += step
        else:
            index += 1
    return min(1.0, straight / perimeter)


def _has_reversal(ring: tuple[tuple[float, float], ...] | list[tuple[float, float]]) -> bool:
    """True when the ring doubles back. That turn is a spur, not a straight edge."""
    points = list(ring)
    count = len(points)
    if count < 4:
        return False
    previous: tuple[float, float] | None = None
    for index in range(count):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % count]
        length = math.hypot(x2 - x1, y2 - y1)
        if length < 1e-6:
            continue
        direction = ((x2 - x1) / length, (y2 - y1) / length)
        if previous is not None and previous[0] * direction[0] + previous[1] * direction[1] < 0.0:
            return True
        previous = direction
    return False


def _ring_boxiness(ring: tuple[tuple[float, float], ...] | list[tuple[float, float]]) -> float:
    """Filled area divided by the axis-aligned box. A legend rectangle is near 1."""
    if len(ring) < 4:
        return 0.0
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    if width < 1.0 or height < 1.0:
        return 0.0
    return _ring_area(ring) / (width * height)


def cleanup_shore_ring(ring: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """Filled-shore alternates with thin road spurs opened away.

    The tracer often follows the shore and then runs out and back along a
    road. That spur wrecks the polygon fill. A one- or two-pixel open, erode,
    or dilate on the filled ring drops the spur and leaves the lake body.
    Callers keep a variant only when it overlaps hydro more than the original.
    """
    base = [(float(point[0]), float(point[1])) for point in ring]
    if len(base) < 12:
        return []
    mask, width, height, geo = _rasterize_ring(base, 360)
    base_area = sum(mask)
    if base_area < 40:
        return []
    variants: list[bytearray] = [
        _dilate_mask(_erode_mask(mask, width, height, 2), width, height, 2),
        _erode_mask(mask, width, height, 1),
        _dilate_mask(mask, width, height, 1),
        _dilate_mask(mask, width, height, 2),
    ]
    cleaned: list[list[tuple[float, float]]] = []
    seen: set[tuple[int, int, int]] = set()
    for variant in variants:
        area = sum(variant)
        if area < 0.72 * base_area or area > 1.28 * base_area:
            continue
        traced = _trace_outer(variant, width, height)
        if len(traced) < 12:
            continue
        scaled = _unmap_ring(traced, geo)
        key = (
            round(min(point[0] for point in scaled)),
            round(max(point[0] for point in scaled)),
            round(area),
        )
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(scaled)
    return cleaned


def _rasterize_ring(
    ring: list[tuple[float, float]], grid: int
) -> tuple[bytearray, int, int, tuple[float, float, float, float]]:
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)
    min_x -= span_x * 0.04
    max_x += span_x * 0.04
    min_y -= span_y * 0.04
    max_y += span_y * 0.04
    cell_x = (max_x - min_x) / grid
    cell_y = (max_y - min_y) / grid
    cells = bytearray(grid * grid)
    count = len(ring)
    for iy in range(grid):
        y = min_y + (iy + 0.5) * cell_y
        crossings: list[float] = []
        for index in range(count):
            x1, y1 = ring[index]
            x2, y2 = ring[(index + 1) % count]
            if y1 == y2 or (y1 > y) == (y2 > y):
                continue
            fraction = (y - y1) / (y2 - y1)
            crossings.append(x1 + fraction * (x2 - x1))
        crossings.sort()
        for pair in range(0, len(crossings) - 1, 2):
            start = math.ceil((crossings[pair] - min_x) / cell_x - 0.5)
            end = math.floor((crossings[pair + 1] - min_x) / cell_x - 0.5)
            if start < 0:
                start = 0
            if end >= grid:
                end = grid - 1
            row = iy * grid
            for ix in range(start, end + 1):
                cells[row + ix] = 1
    return cells, grid, grid, (min_x, min_y, cell_x, cell_y)


def _unmap_ring(
    ring: list[tuple[float, float]], geo: tuple[float, float, float, float]
) -> list[tuple[float, float]]:
    min_x, min_y, cell_x, cell_y = geo
    return [((point[0] + 0.5) * cell_x + min_x, (point[1] + 0.5) * cell_y + min_y) for point in ring]


def _road_like_ring(
    ring: list[tuple[float, float]],
    mask: bytearray,
    width: int,
    height: int,
) -> bool:
    straight = _ring_straightness(ring)
    if straight < MAX_ROAD_STRAIGHTNESS:
        return False
    work_mask, work_w, work_h, factor = _shrink_mask(mask, width, height, SHORE_WORK_SIDE)
    indices = _ring_pixel_indices(ring, work_w, work_h, factor, width, height)
    if not indices:
        return False
    ink = sum(work_mask[index] for index in indices) / len(indices)
    return ink < MIN_LAKE_INK


def _ranked_hole_contours(
    mask: bytearray,
    width: int,
    height: int,
    frame: tuple[int, int, int, int] | None = None,
) -> list[tuple[int, list[tuple[float, float]]]]:
    raw = _hole_contours(mask, width, height)
    scored: list[tuple[float, int, list[tuple[float, float]]]] = []
    for area, contour in raw:
        if _contour_on_frame(contour, width, height, frame, tol=3.0):
            continue
        ink = _interior_ink_in_hole(mask, width, height, contour)
        straight = _ring_straightness(contour)
        if straight >= MAX_ROAD_STRAIGHTNESS and ink < MIN_LAKE_INK and not _has_reversal(contour):
            continue
        score = float(area) * (0.015 + ink) / (1.0 + max(0.0, straight - 0.2) * 2.5)
        scored.append((score, area, contour))
    scored.sort(key=lambda item: -item[0])
    return [(area, contour) for _score, area, contour in scored]


def _contour_on_frame(
    contour: list[tuple[float, float]],
    width: int,
    height: int,
    frame: tuple[int, int, int, int] | None,
    *,
    tol: float,
) -> bool:
    if not contour:
        return False
    long_side = max(width, height)
    if long_side <= 0:
        return False
    span = max(
        max(point[0] for point in contour) - min(point[0] for point in contour),
        max(point[1] for point in contour) - min(point[1] for point in contour),
    )
    if span / long_side < NEATLINE_SPAN:
        return False
    hits = sum(
        1 for point in contour if _vertex_on_frame(point[0], point[1], width, height, frame, tol=tol)
    )
    if hits / len(contour) < NEATLINE_FRAME_BOUNDARY:
        return False
    return _ring_straightness(contour) >= NEATLINE_MIN_STRAIGHTNESS


def _interior_ink_in_hole(
    mask: bytearray,
    width: int,
    height: int,
    ring: list[tuple[float, float]],
) -> float:
    indices = _ring_pixel_indices(ring, width, height, 1, width, height)
    if not indices:
        return 0.0
    ink = sum(mask[index] for index in indices)
    return ink / len(indices)


def _hole_contours(
    mask: bytearray, width: int, height: int
) -> list[tuple[int, list[tuple[float, float]]]]:
    """White regions the border cannot reach, largest first, frame holes dropped."""
    outside = bytearray(width * height)
    stack: deque[int] = deque()
    for x in range(width):
        for y in (0, height - 1):
            index = y * width + x
            if not mask[index] and not outside[index]:
                outside[index] = 1
                stack.append(index)
    for y in range(height):
        for x in (0, width - 1):
            index = y * width + x
            if not mask[index] and not outside[index]:
                outside[index] = 1
                stack.append(index)
    while stack:
        index = stack.pop()
        y, x = divmod(index, width)
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if nx < 0 or ny < 0 or nx >= width or ny >= height:
                continue
            neighbor = ny * width + nx
            if mask[neighbor] or outside[neighbor]:
                continue
            outside[neighbor] = 1
            stack.append(neighbor)
    seen = bytearray(width * height)
    holes: list[tuple[int, list[tuple[float, float]]]] = []
    for start in range(width * height):
        if mask[start] or outside[start] or seen[start]:
            continue
        component = _flood_component(seen, mask, outside, width, height, start)
        if len(component) < MIN_INTERIOR:
            continue
        if len(component) >= FRAME_HOLE_AREA * width * height:
            continue
        if _component_covers_frame(component, width, height, MAX_FRAME_FRACTION):
            continue
        filled = bytearray(width * height)
        for index in component:
            filled[index] = 1
        contour = _trace_outer(filled, width, height)
        if len(contour) >= 8:
            holes.append((len(component), contour))
    holes.sort(key=lambda item: -item[0])
    return holes


def _flood_component(
    seen: bytearray,
    mask: bytearray,
    outside: bytearray,
    width: int,
    height: int,
    start: int,
) -> list[int]:
    component = [start]
    seen[start] = 1
    stack = [start]
    while stack:
        index = stack.pop()
        y, x = divmod(index, width)
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if nx < 0 or ny < 0 or nx >= width or ny >= height:
                continue
            neighbor = ny * width + nx
            if mask[neighbor] or outside[neighbor] or seen[neighbor]:
                continue
            seen[neighbor] = 1
            stack.append(neighbor)
            component.append(neighbor)
    return component


def _component_covers_frame(
    component: list[int], width: int, height: int, fraction: float
) -> bool:
    min_x = min_y = 10**9
    max_x = max_y = -1
    for index in component:
        y, x = divmod(index, width)
        if x < min_x:
            min_x = x
        if y < min_y:
            min_y = y
        if x > max_x:
            max_x = x
        if y > max_y:
            max_y = y
    return (max_x - min_x + 1) >= fraction * width and (max_y - min_y + 1) >= fraction * height


def _scale_ring(ring: list[tuple[float, float]], factor: int) -> list[tuple[float, float]]:
    if factor == 1:
        return ring
    offset = (factor - 1) / 2.0
    return [(x * factor + offset, y * factor + offset) for x, y in ring]


def _dark_mask(image: RasterImage) -> bytearray:
    mask = bytearray(image.width * image.height)
    rgb = image.rgb
    for index in range(image.width * image.height):
        offset = index * 3
        red = rgb[offset]
        green = rgb[offset + 1]
        blue = rgb[offset + 2]
        luminance = (red + green + blue) // 3
        dark = luminance < DARK_THRESHOLD
        blue_ink = blue > 80 and blue > red + 30 and blue > green + 10 and luminance < 220
        if dark or blue_ink:
            mask[index] = 1
    return mask


def _clear_span_lines(mask: bytearray, width: int, height: int, ratio: float) -> None:
    """Drop neatline and PLSS ink, but keep a span line that seals a basin.

    The seal test runs before any span pixel is deleted. A shore across 87
    percent of the page is still a shore when removing it would open the water.
    """
    row_need = ratio * width
    col_need = ratio * height
    span_rows = [y for y in range(height) if sum(mask[y * width : (y + 1) * width]) >= row_need]
    span_cols = [
        x for x in range(width) if sum(mask[y * width + x] for y in range(height)) >= col_need
    ]
    if not span_rows and not span_cols:
        return
    span_row_set = set(span_rows)
    span_col_set = set(span_cols)
    keep = bytearray(width * height)
    for y in span_rows:
        for x in range(width):
            index = y * width + x
            if mask[index] and _off_line_neighbor(
                mask, width, height, x, y, span_row_set, axis="row"
            ):
                keep[index] = 1
    for x in span_cols:
        for y in range(height):
            index = y * width + x
            if mask[index] and _off_line_neighbor(
                mask, width, height, x, y, span_col_set, axis="col"
            ):
                keep[index] = 1
    doomed = bytearray(width * height)
    for y in span_rows:
        start = y * width
        for x in range(width):
            index = start + x
            if mask[index] and not keep[index]:
                doomed[index] = 1
    for x in span_cols:
        for y in range(height):
            index = y * width + x
            if mask[index] and not keep[index]:
                doomed[index] = 1
    seal = _basin_seal_pixels(mask, width, height, doomed)
    for index, flag in enumerate(doomed):
        if flag and not seal[index]:
            mask[index] = 0


def _off_line_neighbor(
    mask: bytearray,
    width: int,
    height: int,
    x: int,
    y: int,
    span_set: set[int],
    *,
    axis: str,
) -> bool:
    """True when ink just off this grid line continues the shoreline."""
    if axis == "row":
        for dy in (-2, -1, 1, 2):
            ny = y + dy
            if ny < 0 or ny >= height or ny in span_set:
                continue
            for dx in range(-2, 3):
                nx = x + dx
                if 0 <= nx < width and mask[ny * width + nx]:
                    return True
        return False
    for dx in (-2, -1, 1, 2):
        nx = x + dx
        if nx < 0 or nx >= width or nx in span_set:
            continue
        for dy in range(-2, 3):
            ny = y + dy
            if 0 <= ny < height and mask[ny * width + nx]:
                return True
    return False


def _best_enclosed(mask: bytearray, width: int, height: int) -> tuple[bytearray | None, int]:
    _labels, components = _label_components(mask, width, height)
    best_fill: bytearray | None = None
    best_area = 0
    ranked = sorted(components, key=lambda comp: -_bbox_area(comp, width))
    for component in ranked[:12]:
        if _covers_frame(component, width, height):
            continue
        area, filled = _enclosed_fill(component, width, height)
        if area > best_area and filled is not None:
            best_area = area
            best_fill = filled
    return best_fill, best_area


def _dilate(mask: bytearray, width: int, height: int) -> bytearray:
    out = bytearray(width * height)
    for y in range(height):
        row = y * width
        for x in range(width):
            if not mask[row + x]:
                continue
            for dy in (-1, 0, 1):
                ny = y + dy
                if ny < 0 or ny >= height:
                    continue
                nrow = ny * width
                for dx in (-1, 0, 1):
                    nx = x + dx
                    if 0 <= nx < width:
                        out[nrow + nx] = 1
    return out


def _label_components(mask: bytearray, width: int, height: int) -> tuple[array, list[list[int]]]:
    labels: array = array("I", [0]) * (width * height)
    components: list[list[int]] = []
    neighbors = (
        (-1, -1),
        (0, -1),
        (1, -1),
        (-1, 0),
        (1, 0),
        (-1, 1),
        (0, 1),
        (1, 1),
    )
    for start in range(width * height):
        if not mask[start] or labels[start]:
            continue
        comp_id = len(components) + 1
        component = [start]
        labels[start] = comp_id
        stack = [start]
        while stack:
            index = stack.pop()
            y, x = divmod(index, width)
            for dx, dy in neighbors:
                nx = x + dx
                ny = y + dy
                if nx < 0 or ny < 0 or nx >= width or ny >= height:
                    continue
                neighbor = ny * width + nx
                if mask[neighbor] and not labels[neighbor]:
                    labels[neighbor] = comp_id
                    stack.append(neighbor)
                    component.append(neighbor)
        components.append(component)
    return labels, components


def _bbox_area(component: list[int], width: int) -> int:
    min_x = min_y = 10**9
    max_x = max_y = -1
    for index in component:
        y, x = divmod(index, width)
        if x < min_x:
            min_x = x
        if y < min_y:
            min_y = y
        if x > max_x:
            max_x = x
        if y > max_y:
            max_y = y
    return (max_x - min_x + 1) * (max_y - min_y + 1)


def _covers_frame(component: list[int], width: int, height: int) -> bool:
    min_x = min_y = 10**9
    max_x = max_y = -1
    for index in component:
        y, x = divmod(index, width)
        if x < min_x:
            min_x = x
        if y < min_y:
            min_y = y
        if x > max_x:
            max_x = x
        if y > max_y:
            max_y = y
    return (max_x - min_x + 1) >= MAX_FRAME_FRACTION * width and (
        max_y - min_y + 1
    ) >= MAX_FRAME_FRACTION * height


def _enclosed_fill(component: list[int], width: int, height: int) -> tuple[int, bytearray | None]:
    """Interior pixel count of this component alone, plus the filled mask."""
    mask = bytearray(width * height)
    for index in component:
        mask[index] = 1
    outside = bytearray(width * height)
    stack: deque[int] = deque()
    for x in range(width):
        for y in (0, height - 1):
            index = y * width + x
            if not mask[index] and not outside[index]:
                outside[index] = 1
                stack.append(index)
    for y in range(height):
        for x in (0, width - 1):
            index = y * width + x
            if not mask[index] and not outside[index]:
                outside[index] = 1
                stack.append(index)
    while stack:
        index = stack.pop()
        y, x = divmod(index, width)
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if nx < 0 or ny < 0 or nx >= width or ny >= height:
                continue
            neighbor = ny * width + nx
            if mask[neighbor] or outside[neighbor]:
                continue
            outside[neighbor] = 1
            stack.append(neighbor)
    area = 0
    filled = bytearray(mask)
    for index in range(width * height):
        if not mask[index] and not outside[index]:
            filled[index] = 1
            area += 1
    if area < MIN_INTERIOR:
        return area, None
    return area, filled


def _trace_outer(filled: bytearray, width: int, height: int) -> list[tuple[float, float]]:
    """Moore neighborhood walk of the outer boundary. Coordinates are pixel centers."""
    start = None
    for y in range(height):
        row = y * width
        for x in range(width):
            if filled[row + x]:
                start = (x, y)
                break
        if start is not None:
            break
    if start is None:
        return []
    # Clockwise from north: N, NE, E, SE, S, SW, W, NW.
    dx = (0, 1, 1, 1, 0, -1, -1, -1)
    dy = (-1, -1, 0, 1, 1, 1, 0, -1)
    x, y = start
    # Entered from the empty pixel above the topmost start.
    prev_dir = 0
    contour: list[tuple[float, float]] = []
    seen: set[tuple[int, int, int]] = set()
    for _ in range(width * height * 2):
        state = (x, y, prev_dir)
        if state in seen:
            break
        seen.add(state)
        contour.append((x + 0.5, y + 0.5))
        moved = False
        for step in range(8):
            direction = (prev_dir + 1 + step) % 8
            nx = x + dx[direction]
            ny = y + dy[direction]
            if 0 <= nx < width and 0 <= ny < height and filled[ny * width + nx]:
                x, y = nx, ny
                prev_dir = (direction + 4) % 8
                moved = True
                break
        if not moved:
            break
    return contour


@dataclass(frozen=True)
class ShoreFigure:
    """A candidate water polygon. ``on_frame`` is aligned with ``ring`` vertices."""

    ring: tuple[tuple[float, float], ...]
    on_frame: tuple[bool, ...]


def extract_shoreline_figures(
    image: RasterImage,
    road_mask: bytearray | None = None,
) -> list[ShoreFigure]:
    """Closed-shore candidates plus gap-closed and page-edge-cut figures.

    The list is small. Registration scores every figure and keeps the best
    overlap, so a tight inner contour loses to the outer shore when the outer
    shore is actually in the list. ``road_mask`` is a dark mask that already
    had road ink cleared. When it is omitted, the straight cased-road pass runs.
    """
    if road_mask is None:
        mask = _dark_mask(image)
        _clear_cased_roads(mask, image.width, image.height)
    else:
        mask = road_mask
    figures: list[ShoreFigure] = []
    for ring in extract_shoreline_candidates(image, mask):
        figures.append(ShoreFigure(tuple((float(x), float(y)) for x, y in ring), tuple(False for _ in ring)))
    figures.extend(_gap_closed_figures(image, mask))
    return _dedup_figures(figures)[:8]


def _gap_closed_figures(image: RasterImage, road_mask: bytearray | None = None) -> list[ShoreFigure]:
    width = image.width
    height = image.height
    if width < 8 or height < 8:
        return []
    if road_mask is None:
        mask = _dark_mask(image)
        _clear_cased_roads(mask, width, height)
    else:
        mask = road_mask
    small, small_w, small_h, factor = _shrink_mask(mask, width, height, SHORE_WORK_SIDE)
    frame = _detect_frame(small, small_w, small_h)
    run_len = max(8, int(0.16 * max(small_w, small_h)))
    _clear_long_runs(
        small,
        small_w,
        small_h,
        run_len,
        keep=frame,
        protect=_hole_shore_pixels(small, small_w, small_h, run_len, frame),
    )
    walled = _burn_walls(small, small_w, small_h, frame)
    figures: list[ShoreFigure] = []
    for radius in (2, 3, 5):
        closed = _dilate_mask(walled, small_w, small_h, radius)
        holes = _large_holes(closed, small_w, small_h, frame)
        if not holes:
            continue
        grow = max(1, radius - 1)
        for hole, _area in holes[:3]:
            expanded = _dilate_mask(hole, small_w, small_h, grow)
            ring = _trace_outer(expanded, small_w, small_h)
            figure = _figure_from_work_ring(ring, factor, small_w, small_h, frame, width, height)
            if figure is not None:
                figures.append(figure)
        if len(holes) >= 2:
            union = bytearray(small_w * small_h)
            for hole, _area in holes[:3]:
                for index, value in enumerate(hole):
                    if value:
                        union[index] = 1
            bridged = _erode_mask(_dilate_mask(union, small_w, small_h, 6), small_w, small_h, 6)
            ring = _trace_outer(bridged, small_w, small_h)
            figure = _figure_from_work_ring(ring, factor, small_w, small_h, frame, width, height)
            if figure is not None:
                figures.append(figure)
    return figures


def _figure_from_work_ring(
    ring: list[tuple[float, float]],
    factor: int,
    work_w: int,
    work_h: int,
    frame: tuple[int, int, int, int] | None,
    width: int,
    height: int,
) -> ShoreFigure | None:
    if len(ring) < 12:
        return None
    scaled = _scale_ring(ring, factor)
    if _span_fraction(scaled, width, height) < MIN_FIGURE_SPAN:
        return None
    # Flags are decided in work pixels, before the scale, so the tolerance
    # stays a couple of mask pixels rather than a couple of scan pixels.
    flags = tuple(
        _vertex_on_frame(point[0], point[1], work_w, work_h, frame, tol=3.0) for point in ring
    )
    if sum(1 for flag in flags if flag) / len(flags) > MAX_FRAME_BOUNDARY:
        return None
    return ShoreFigure(tuple((float(x), float(y)) for x, y in scaled), flags)


def _vertex_on_frame(
    x: float,
    y: float,
    width: int,
    height: int,
    frame: tuple[int, int, int, int] | None,
    tol: float,
) -> bool:
    if x <= tol or y <= tol or x >= width - 1 - tol or y >= height - 1 - tol:
        return True
    if frame is None:
        return False
    left, top, right, bottom = frame
    on_horizontal = (abs(y - top) <= tol or abs(y - bottom) <= tol) and left - tol <= x <= right + tol
    on_vertical = (abs(x - left) <= tol or abs(x - right) <= tol) and top - tol <= y <= bottom + tol
    return on_horizontal or on_vertical


def _detect_frame(mask: bytearray, width: int, height: int) -> tuple[int, int, int, int] | None:
    """Outermost heavy axis-aligned neatline, or None when the sheet has no rectangle."""
    y0 = max(2, int(height * 0.05))
    y1 = min(height - 2, int(height * 0.95))
    x0 = max(2, int(width * 0.05))
    x1 = min(width - 2, int(width * 0.95))
    if y1 <= y0 or x1 <= x0:
        return None
    col_need = 0.35 * (y1 - y0)
    row_need = 0.35 * (x1 - x0)
    columns = [
        x
        for x in range(2, width - 2)
        if sum(mask[y * width + x] for y in range(y0, y1)) >= col_need
    ]
    rows = [
        y
        for y in range(2, height - 2)
        if sum(mask[y * width + x] for x in range(x0, x1)) >= row_need
    ]
    if len(columns) < 2 or len(rows) < 2:
        return None
    left, right = min(columns), max(columns)
    top, bottom = min(rows), max(rows)
    if right - left < 0.45 * width or bottom - top < 0.45 * height:
        return None
    return (left, top, right, bottom)


def _burn_walls(
    mask: bytearray,
    width: int,
    height: int,
    frame: tuple[int, int, int, int] | None,
) -> bytearray:
    """Page edge, and the neatline when one was found, so a cut shore can close."""
    out = bytearray(mask)
    for x in range(width):
        out[x] = 1
        out[(height - 1) * width + x] = 1
    for y in range(height):
        out[y * width] = 1
        out[y * width + width - 1] = 1
    if frame is not None:
        left, top, right, bottom = frame
        for x in range(max(0, left), min(width, right + 1)):
            out[top * width + x] = 1
            out[bottom * width + x] = 1
        for y in range(max(0, top), min(height, bottom + 1)):
            out[y * width + left] = 1
            out[y * width + right] = 1
    return out


def _hole_shore_pixels(
    mask: bytearray,
    width: int,
    height: int,
    min_len: int,
    keep: tuple[int, int, int, int] | None = None,
) -> bytearray:
    """Pixels whose deletion would open one large hole.

    ``_clear_long_runs`` deletes section lines. The same test matches a long
    straight piece of shoreline. On Trout sheet 1 and Fence sheet 2 that piece
    is the only seal: removing a few dozen pixels opens the water to the page
    border. Only those pixels are kept, and only for the hole that opens. A
    section line that merely touches the basin, and the border of a separate
    rectangle, are not kept. Lakes whose largest hole already survives the
    clear are left alone, so a passing trace is not redrawn.
    """
    protect = bytearray(width * height)
    if width < 8 or height < 8:
        return protect
    trial = bytearray(mask)
    _clear_long_runs(trial, width, height, min_len, keep=keep)
    doomed = bytearray(width * height)
    for index, value in enumerate(mask):
        if value and not trial[index]:
            doomed[index] = 1
    if not any(doomed):
        return protect
    return _basin_seal_pixels(mask, width, height, doomed)


def _basin_seal_pixels(
    mask: bytearray,
    width: int,
    height: int,
    doomed: bytearray,
) -> bytearray:
    """Doomed pixels that form the inner seal of the large hole that opens.

    A pixel counts only when it touches that hole and a path through doomed
    pixels reaches the outside from it. The border of a different hole is not
    on that path. A section line that only touches the basin is not the inner
    seal, so it is not marked.
    """
    protect = bytearray(width * height)
    if width < 8 or height < 8 or not any(doomed):
        return protect
    outside = _outside_mask(mask, width, height)
    holes = _contained_holes(mask, width, height, outside)
    if not holes:
        return protect
    largest = holes[0]
    opened = _outside_mask(mask, width, height, also_passable=doomed)
    if not any(opened[index] for index in largest):
        return protect
    hole = bytearray(width * height)
    for index in largest:
        hole[index] = 1
    candidates: list[int] = []
    for index, flag in enumerate(doomed):
        if not flag:
            continue
        y, x = divmod(index, width)
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height and hole[ny * width + nx]:
                candidates.append(index)
                break
    if not candidates:
        return protect
    reachable = _doomed_reaching_outside(doomed, width, height, outside)
    for index in candidates:
        if reachable[index]:
            protect[index] = 1
    return protect


def _outside_mask(
    mask: bytearray,
    width: int,
    height: int,
    *,
    also_passable: bytearray | None = None,
) -> bytearray:
    """White pixels connected to the border. ``also_passable`` pixels are open too."""
    outside = bytearray(width * height)
    stack: deque[int] = deque()

    def push(index: int) -> None:
        if outside[index]:
            return
        if mask[index] and not (also_passable is not None and also_passable[index]):
            return
        outside[index] = 1
        stack.append(index)

    for x in range(width):
        push(x)
        push((height - 1) * width + x)
    for y in range(height):
        push(y * width)
        push(y * width + width - 1)
    while stack:
        index = stack.pop()
        y, x = divmod(index, width)
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if nx < 0 or ny < 0 or nx >= width or ny >= height:
                continue
            push(ny * width + nx)
    return outside


def _contained_holes(
    mask: bytearray,
    width: int,
    height: int,
    outside: bytearray,
) -> list[list[int]]:
    """White components the border cannot reach, largest acceptable hole first."""
    seen = bytearray(width * height)
    holes: list[list[int]] = []
    minimum = max(MIN_INTERIOR, int(0.04 * width * height))
    frame_limit = FRAME_HOLE_AREA * width * height
    for start in range(width * height):
        if mask[start] or outside[start] or seen[start]:
            continue
        component = _flood_component(seen, mask, outside, width, height, start)
        if len(component) < minimum or len(component) >= frame_limit:
            continue
        if _component_covers_frame(component, width, height, MAX_FRAME_FRACTION):
            continue
        holes.append(component)
    holes.sort(key=len, reverse=True)
    return holes


def _doomed_reaching_outside(
    doomed: bytearray,
    width: int,
    height: int,
    outside: bytearray,
) -> bytearray:
    """Doomed pixels that can reach the outside by walking other doomed pixels."""
    reached = bytearray(width * height)
    stack: deque[int] = deque()
    for index, flag in enumerate(doomed):
        if not flag:
            continue
        y, x = divmod(index, width)
        on_border = x == 0 or y == 0 or x == width - 1 or y == height - 1
        touches_outside = on_border
        if not touches_outside:
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < width and 0 <= ny < height and outside[ny * width + nx]:
                    touches_outside = True
                    break
        if touches_outside:
            reached[index] = 1
            stack.append(index)
    while stack:
        index = stack.pop()
        y, x = divmod(index, width)
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if nx < 0 or ny < 0 or nx >= width or ny >= height:
                continue
            neighbor = ny * width + nx
            if not doomed[neighbor] or reached[neighbor]:
                continue
            reached[neighbor] = 1
            stack.append(neighbor)
    return reached


# Center-to-center gap of a cased road on the work mask, in pixels.
_ROAD_GAP_MIN = 2.0
_ROAD_GAP_MAX = 8.0
_ROAD_ALIGN = 0.96


def _clear_cased_roads(mask: bytearray, width: int, height: int) -> None:
    """Clear cased-road pixels on the source mask.

    Detection runs on the shoreline work grid. A cleared work pixel clears the
    source block it came from, so the gap-closed pass sees the same roads.
    """
    small, small_w, small_h, factor = _shrink_mask(mask, width, height, SHORE_MAX_SIDE)
    frame = _detect_frame(small, small_w, small_h)
    before = bytearray(small)
    _mask_cased_roads(small, small_w, small_h, frame)
    if factor == 1:
        mask[:] = small
        return
    for y in range(small_h):
        row = y * small_w
        for x in range(small_w):
            index = row + x
            if not before[index] or small[index]:
                continue
            y0 = y * factor
            x0 = x * factor
            for yy in range(y0, min(height, y0 + factor)):
                dest = yy * width
                for xx in range(x0, min(width, x0 + factor)):
                    mask[dest + xx] = 0


def _mask_cased_roads(
    mask: bytearray,
    width: int,
    height: int,
    frame: tuple[int, int, int, int] | None = None,
) -> None:
    """Clear double-line roads. Keep a line that is the basin's only seal.

    A cased road is two straight ink lines with a narrow white gap. Depth
    contours and a single shore are not pairs, so they stay. The hydro
    polygon is not consulted: the pair is an image pattern, and the seal test
    decides which of those pixels still close the water.
    """
    if width < 24 or height < 24:
        return
    min_len = max(28, int(0.08 * max(width, height)))
    chains = [
        chain
        for chain in _straight_ink_chains(mask, width, height, min_len)
        if not _chain_follows_border(chain, width, height)
    ]
    if len(chains) < 2:
        return
    # No enclosed basin means the lines may be the shore. Leave them.
    outside = _outside_mask(mask, width, height)
    holes = _contained_holes(mask, width, height, outside)
    if not holes:
        return
    doomed = bytearray(width * height)
    paired = False
    for index, left in enumerate(chains):
        for right in chains[index + 1 :]:
            if not _road_pair(left, right, mask, width, height):
                continue
            paired = True
            for chain in (left, right):
                for x, y in chain:
                    if _on_kept_frame(x, y, frame):
                        continue
                    doomed[y * width + x] = 1
    if not paired or not any(doomed):
        return
    # Keep the casing pixels that close any basin. A road across the water
    # touches water on both sides, so it is not that seal, and clearing it
    # joins the basin. Protecting only the largest hole opens the other one.
    protect = bytearray(width * height)
    for hole in holes:
        part = _casing_seal_pixels(width, height, doomed, outside, hole)
        for index, flag in enumerate(part):
            if flag:
                protect[index] = 1
    for index, flag in enumerate(doomed):
        if flag and not protect[index]:
            mask[index] = 0


def _casing_seal_pixels(
    width: int,
    height: int,
    doomed: bytearray,
    outside: bytearray,
    largest_hole: list[int],
) -> bytearray:
    """Casing pixels that sit between the enclosed water and the outside."""
    protect = bytearray(width * height)
    if width < 8 or height < 8 or not any(doomed) or not largest_hole:
        return protect
    hole = bytearray(width * height)
    for index in largest_hole:
        hole[index] = 1
    for index, flag in enumerate(doomed):
        if not flag:
            continue
        y, x = divmod(index, width)
        touches_hole = False
        touches_outside = x == 0 or y == 0 or x == width - 1 or y == height - 1
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if nx < 0 or ny < 0 or nx >= width or ny >= height:
                touches_outside = True
                continue
            neighbor = ny * width + nx
            if hole[neighbor]:
                touches_hole = True
            if outside[neighbor]:
                touches_outside = True
        if touches_hole and touches_outside:
            protect[index] = 1
    return protect


def cleared_hits_basin_seal(
    before: bytearray,
    after: bytearray,
    width: int,
    height: int,
) -> bool:
    """True when a cleared pixel was the seal of any enclosed basin.

    The first shore is that seal. A later trace can score higher after the
    basin opens, and that gain is not a road removal.
    """
    if width < 8 or height < 8 or len(before) != width * height or len(after) != width * height:
        return False
    doomed = bytearray(width * height)
    for index, (was_ink, still_ink) in enumerate(zip(before, after, strict=True)):
        if was_ink and not still_ink:
            doomed[index] = 1
    if not any(doomed):
        return False
    outside = _outside_mask(before, width, height)
    for hole in _contained_holes(before, width, height, outside):
        if any(_casing_seal_pixels(width, height, doomed, outside, hole)):
            return True
    return False


def clear_projected_casings(
    mask: bytearray,
    width: int,
    height: int,
    polylines: list[list[tuple[float, float]]],
    *,
    search: float = 48.0,
) -> int:
    """Clear narrow ink pairs beside projected road centerlines.

    ``polylines`` are in source pixels. A pair is two thin ink runs separated
    by a white gap of a few pixels. One run, the usual shore stroke, stays.
    Pixels that are the only seal of an enclosed basin stay too. Returns how
    many ink pixels were cleared.
    """
    if width < 16 or height < 16 or not polylines or search < 2:
        return 0
    doomed = bytearray(width * height)
    for line in polylines:
        _mark_casing_along(mask, width, height, line, doomed, search)
    if not any(doomed):
        return 0
    outside = _outside_mask(mask, width, height)
    holes = _contained_holes(mask, width, height, outside)
    protect = bytearray(width * height)
    # A road that splits the basin makes two holes. Each shore crossing is a
    # seal. Keeping only the largest hole opens the other side onto the page.
    for hole in holes:
        part = _casing_seal_pixels(width, height, doomed, outside, hole)
        for index, flag in enumerate(part):
            if flag:
                protect[index] = 1
    cleared = 0
    for index, flag in enumerate(doomed):
        if not flag or protect[index] or not mask[index]:
            continue
        # A shore or contour that crosses the casing is ink we are not marking
        # as the pair. Leave the crossing pixel so the basin stays closed.
        if _touches_kept_ink(mask, doomed, width, height, index):
            continue
        mask[index] = 0
        cleared += 1
    return cleared


def _touches_kept_ink(
    mask: bytearray,
    doomed: bytearray,
    width: int,
    height: int,
    index: int,
) -> bool:
    y, x = divmod(index, width)
    for dy in (-1, 0, 1):
        yy = y + dy
        if yy < 0 or yy >= height:
            continue
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            xx = x + dx
            if xx < 0 or xx >= width:
                continue
            neighbor = yy * width + xx
            if mask[neighbor] and not doomed[neighbor]:
                return True
    return False


def _mark_casing_along(
    mask: bytearray,
    width: int,
    height: int,
    line: list[tuple[float, float]],
    doomed: bytearray,
    search: float,
) -> None:
    samples = _resample_polyline(line, 1.0)
    if len(samples) < 2:
        return
    gap_min = 2
    gap_max = 14
    for index in range(len(samples)):
        x, y = samples[index]
        if index + 1 < len(samples):
            dx = samples[index + 1][0] - x
            dy = samples[index + 1][1] - y
        else:
            dx = x - samples[index - 1][0]
            dy = y - samples[index - 1][1]
        length = math.hypot(dx, dy)
        if length < 1e-6:
            continue
        nx, ny = -dy / length, dx / length
        half = int(search)
        ink = [
            _probe_ink(mask, width, height, x + nx * offset, y + ny * offset)
            for offset in range(-half, half + 1)
        ]
        runs: list[tuple[int, int]] = []
        cursor = 0
        while cursor < len(ink):
            if not ink[cursor]:
                cursor += 1
                continue
            end = cursor
            while end < len(ink) and ink[end]:
                end += 1
            if 1 <= end - cursor <= 8:
                runs.append((cursor, end))
            cursor = end
        best: tuple[int, int, int, int] | None = None
        best_key: tuple[int, int] | None = None
        for left, right in zip(runs, runs[1:], strict=False):
            gap = right[0] - left[1]
            if gap < gap_min or gap > gap_max:
                continue
            left_off = ((left[0] + left[1] - 1) / 2.0) - half
            right_off = ((right[0] + right[1] - 1) / 2.0) - half
            center = (left_off + right_off) / 2.0
            # Snap to the casing nearest the projected line. A first affine that
            # still follows the road can miss the ink by tens of meters.
            if abs(center) > search * 0.9:
                continue
            key = (int(abs(center) * 10), gap)
            if best_key is None or key < best_key:
                best_key = key
                best = (left[0], left[1], right[0], right[1])
        if best is None:
            continue
        left_start, left_end, right_start, right_end = best
        for slot in _casing_slots(left_start, left_end, left_side=True):
            _mark_ink(mask, doomed, width, height, x + nx * (slot - half), y + ny * (slot - half))
        for slot in _casing_slots(right_start, right_end, left_side=False):
            _mark_ink(mask, doomed, width, height, x + nx * (slot - half), y + ny * (slot - half))


def _casing_slots(start: int, end: int, *, left_side: bool) -> range:
    """The outer one or two pixels of a run. A shore stuck to the gap stays."""
    thickness = end - start
    if thickness <= 2:
        return range(start, end)
    if left_side:
        return range(start, start + 2)
    return range(end - 2, end)


def _resample_polyline(
    line: list[tuple[float, float]],
    step: float,
) -> list[tuple[float, float]]:
    if len(line) < 2 or step <= 0:
        return []
    samples = [line[0]]
    previous = line[0]
    carry = 0.0
    for nxt in line[1:]:
        dx = nxt[0] - previous[0]
        dy = nxt[1] - previous[1]
        dist = math.hypot(dx, dy)
        if dist < 1e-9:
            previous = nxt
            continue
        ux, uy = dx / dist, dy / dist
        traveled = carry
        while traveled + step <= dist + 1e-9:
            traveled += step
            if traveled <= dist + 1e-6:
                samples.append((previous[0] + ux * traveled, previous[1] + uy * traveled))
        carry = dist - traveled
        previous = nxt
    return samples


def _probe_ink(mask: bytearray, width: int, height: int, x: float, y: float) -> bool:
    ix = int(round(x))
    iy = int(round(y))
    if ix < 0 or iy < 0 or ix >= width or iy >= height:
        return False
    return bool(mask[iy * width + ix])


def _mark_ink(
    mask: bytearray,
    doomed: bytearray,
    width: int,
    height: int,
    x: float,
    y: float,
) -> None:
    ix = int(round(x))
    iy = int(round(y))
    if ix < 0 or iy < 0 or ix >= width or iy >= height:
        return
    index = iy * width + ix
    if mask[index]:
        doomed[index] = 1


def _straight_ink_chains(
    mask: bytearray,
    width: int,
    height: int,
    min_len: int,
) -> list[list[tuple[int, int]]]:
    """Long, nearly straight ink chains. A bend starts a new chain."""
    visited = bytearray(width * height)
    chains: list[list[tuple[int, int]]] = []
    for start in range(width * height):
        if not mask[start] or visited[start]:
            continue
        y, x = divmod(start, width)
        chain = _grow_straight_chain(mask, visited, width, height, x, y)
        for cx, cy in chain:
            visited[cy * width + cx] = 1
        if len(chain) >= min_len and _chain_is_straight(chain, min_len):
            chains.append(chain)
    return chains


def _grow_straight_chain(
    mask: bytearray,
    visited: bytearray,
    width: int,
    height: int,
    x: int,
    y: int,
) -> list[tuple[int, int]]:
    """Walk ink both ways until the line bends or the ink stops."""
    forward = _walk_ink(mask, visited, width, height, x, y, None)
    heading = None
    if len(forward) >= 2:
        heading = (forward[0][0] - forward[1][0], forward[0][1] - forward[1][1])
    backward = _walk_ink(mask, visited, width, height, x, y, heading, blocked=forward)
    if len(backward) <= 1:
        return forward
    return list(reversed(backward[1:])) + forward


def _walk_ink(
    mask: bytearray,
    visited: bytearray,
    width: int,
    height: int,
    x: int,
    y: int,
    heading: tuple[int, int] | None,
    blocked: list[tuple[int, int]] | None = None,
) -> list[tuple[int, int]]:
    local = {(x, y)}
    if blocked:
        local.update(blocked)
    chain = [(x, y)]
    hx, hy = heading if heading is not None else (0, 0)
    while True:
        nxt = _next_ink_step(mask, visited, width, height, chain[-1][0], chain[-1][1], hx, hy, local)
        if nxt is None:
            return chain
        nx, ny, dx, dy = nxt
        if len(chain) >= 6:
            ax = chain[-1][0] - chain[0][0]
            ay = chain[-1][1] - chain[0][1]
            along = ax * dx + ay * dy
            if along <= 0:
                return chain
            scale = math.hypot(dx, dy) * math.hypot(hx, hy)
            if scale > 0 and (dx * hx + dy * hy) < 0.45 * scale:
                return chain
        chain.append((nx, ny))
        local.add((nx, ny))
        if len(chain) >= 4:
            px, py = chain[-4]
            hx, hy = nx - px, ny - py
        else:
            hx, hy = dx, dy


_INK_STEPS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))


def _next_ink_step(
    mask: bytearray,
    visited: bytearray,
    width: int,
    height: int,
    x: int,
    y: int,
    hx: int,
    hy: int,
    local: set[tuple[int, int]],
) -> tuple[int, int, int, int] | None:
    best: tuple[int, int, int, int] | None = None
    best_score = 0.34
    hlen = math.hypot(hx, hy)
    for dx, dy in _INK_STEPS:
        nx = x + dx
        ny = y + dy
        if nx < 0 or ny < 0 or nx >= width or ny >= height:
            continue
        if (nx, ny) in local or visited[ny * width + nx] or not mask[ny * width + nx]:
            continue
        if hlen < 1e-6:
            score = 1.0
        else:
            score = (dx * hx + dy * hy) / (math.hypot(dx, dy) * hlen)
        if score > best_score:
            best_score = score
            best = (nx, ny, dx, dy)
    if best is not None:
        return best
    if hlen >= 1e-6:
        return None
    for dx, dy in _INK_STEPS:
        nx = x + dx
        ny = y + dy
        if nx < 0 or ny < 0 or nx >= width or ny >= height:
            continue
        if (nx, ny) in local or visited[ny * width + nx] or not mask[ny * width + nx]:
            continue
        return (nx, ny, dx, dy)
    return None


def _chain_is_straight(chain: list[tuple[int, int]], min_len: int) -> bool:
    x0, y0 = chain[0]
    x1, y1 = chain[-1]
    dx = x1 - x0
    dy = y1 - y0
    length = math.hypot(dx, dy)
    if length < min_len * 0.75:
        return False
    worst = 0.0
    for x, y in chain:
        worst = max(worst, abs((x - x0) * dy - (y - y0) * dx) / length)
    return worst <= 2.25


def _road_pair(
    left: list[tuple[int, int]],
    right: list[tuple[int, int]],
    mask: bytearray,
    width: int,
    height: int,
) -> bool:
    """True when the chains are parallel, close, overlapping, and white between."""
    if not _chains_parallel(left, right):
        return False
    gap = _chain_gap(left, right, mask, width, height)
    if gap is None:
        return False
    median, samples, ink_hits = gap
    if not (_ROAD_GAP_MIN <= median <= _ROAD_GAP_MAX):
        return False
    if samples < 4:
        return False
    return ink_hits / samples <= 0.35


def _chain_follows_border(chain: list[tuple[int, int]], width: int, height: int) -> bool:
    """A neatline or the page edge is not a road casing."""
    hits = 0
    for x, y in chain:
        if x <= 2 or y <= 2 or x >= width - 3 or y >= height - 3:
            hits += 1
    return hits / len(chain) >= 0.5


def _chains_parallel(left: list[tuple[int, int]], right: list[tuple[int, int]]) -> bool:
    ax = left[-1][0] - left[0][0]
    ay = left[-1][1] - left[0][1]
    bx = right[-1][0] - right[0][0]
    by = right[-1][1] - right[0][1]
    la = math.hypot(ax, ay)
    lb = math.hypot(bx, by)
    if la < 1 or lb < 1:
        return False
    return abs(ax * bx + ay * by) / (la * lb) >= _ROAD_ALIGN


def _chain_gap(
    left: list[tuple[int, int]],
    right: list[tuple[int, int]],
    mask: bytearray,
    width: int,
    height: int,
) -> tuple[float, int, int] | None:
    """Median distance, overlap sample count, and how many gap samples are ink."""
    x0, y0 = right[0]
    bx = right[-1][0] - x0
    by = right[-1][1] - y0
    length_sq = bx * bx + by * by
    if length_sq <= 0:
        return None
    length = math.sqrt(length_sq)
    casing = set(left)
    casing.update(right)
    step = max(1, len(left) // 24)
    distances: list[float] = []
    ink_hits = 0
    for x, y in left[::step]:
        t = ((x - x0) * bx + (y - y0) * by) / length_sq
        if t < -0.02 or t > 1.02:
            continue
        dist = abs((x - x0) * by - (y - y0) * bx) / length
        distances.append(dist)
        mid_x = int(round((x + x0 + t * bx) / 2))
        mid_y = int(round((y + y0 + t * by) / 2))
        if (mid_x, mid_y) in casing:
            continue
        if 0 <= mid_x < width and 0 <= mid_y < height and mask[mid_y * width + mid_x]:
            ink_hits += 1
    if len(distances) < 4:
        return None
    distances.sort()
    return distances[len(distances) // 2], len(distances), ink_hits


def _clear_long_runs(
    mask: bytearray,
    width: int,
    height: int,
    min_len: int,
    keep: tuple[int, int, int, int] | None = None,
    protect: bytearray | None = None,
) -> None:
    """Drop long straight ink (section lines) and keep pixels where a shore leaves the run."""
    horizontal: list[tuple[int, int, int]] = []
    for y in range(height):
        x = 0
        row = y * width
        while x < width:
            if mask[row + x]:
                x0 = x
                while x < width and mask[row + x]:
                    x += 1
                if x - x0 >= min_len:
                    horizontal.append((y, x0, x))
            else:
                x += 1
    vertical: list[tuple[int, int, int]] = []
    for x in range(width):
        y = 0
        while y < height:
            if mask[y * width + x]:
                y0 = y
                while y < height and mask[y * width + x]:
                    y += 1
                if y - y0 >= min_len:
                    vertical.append((x, y0, y))
            else:
                y += 1
    for y, x0, x1 in horizontal:
        for x in range(x0, x1):
            index = y * width + x
            if (protect is not None and protect[index]) or _on_kept_frame(x, y, keep) or _run_has_side_ink(
                mask, width, height, x, y, axis="row"
            ):
                continue
            mask[index] = 0
    for x, y0, y1 in vertical:
        for y in range(y0, y1):
            index = y * width + x
            if (protect is not None and protect[index]) or _on_kept_frame(x, y, keep) or _run_has_side_ink(
                mask, width, height, x, y, axis="col"
            ):
                continue
            mask[index] = 0


def _on_kept_frame(x: int, y: int, frame: tuple[int, int, int, int] | None) -> bool:
    if frame is None:
        return False
    left, top, right, bottom = frame
    if abs(y - top) <= 2 or abs(y - bottom) <= 2:
        return left - 2 <= x <= right + 2
    if abs(x - left) <= 2 or abs(x - right) <= 2:
        return top - 2 <= y <= bottom + 2
    return False


def _run_has_side_ink(
    mask: bytearray,
    width: int,
    height: int,
    x: int,
    y: int,
    *,
    axis: str,
) -> bool:
    if axis == "row":
        for dy in (-2, -1, 1, 2):
            ny = y + dy
            if ny < 0 or ny >= height:
                continue
            for dx in range(-1, 2):
                nx = x + dx
                if 0 <= nx < width and mask[ny * width + nx]:
                    return True
        return False
    for dx in (-2, -1, 1, 2):
        nx = x + dx
        if nx < 0 or nx >= width:
            continue
        for dy in range(-1, 2):
            ny = y + dy
            if 0 <= ny < height and mask[ny * width + nx]:
                return True
    return False


def _dilate_mask(mask: bytearray, width: int, height: int, radius: int) -> bytearray:
    if radius <= 0:
        return bytearray(mask)
    return _filter_cols(_filter_rows(mask, width, height, radius, erode=False), width, height, radius, erode=False)


def _erode_mask(mask: bytearray, width: int, height: int, radius: int) -> bytearray:
    if radius <= 0:
        return bytearray(mask)
    return _filter_cols(_filter_rows(mask, width, height, radius, erode=True), width, height, radius, erode=True)


def _filter_rows(mask: bytearray, width: int, height: int, radius: int, *, erode: bool) -> bytearray:
    out = bytearray(width * height)
    for y in range(height):
        row = y * width
        prefix = [0] * (width + 1)
        for x in range(width):
            prefix[x + 1] = prefix[x] + mask[row + x]
        for x in range(width):
            start = x - radius
            end = x + radius + 1
            if start < 0:
                start = 0
            if end > width:
                end = width
            count = prefix[end] - prefix[start]
            if erode:
                if count == end - start:
                    out[row + x] = 1
            elif count:
                out[row + x] = 1
    return out


def _filter_cols(mask: bytearray, width: int, height: int, radius: int, *, erode: bool) -> bytearray:
    out = bytearray(width * height)
    for x in range(width):
        prefix = [0] * (height + 1)
        for y in range(height):
            prefix[y + 1] = prefix[y] + mask[y * width + x]
        for y in range(height):
            start = y - radius
            end = y + radius + 1
            if start < 0:
                start = 0
            if end > height:
                end = height
            count = prefix[end] - prefix[start]
            index = y * width + x
            if erode:
                if count == end - start:
                    out[index] = 1
            elif count:
                out[index] = 1
    return out


def _large_holes(
    mask: bytearray,
    width: int,
    height: int,
    frame: tuple[int, int, int, int] | None,
) -> list[tuple[bytearray, int]]:
    """White regions the border cannot reach, largest acceptable holes first."""
    outside = bytearray(width * height)
    stack: deque[int] = deque()
    for x in range(width):
        for y in (0, height - 1):
            index = y * width + x
            if not mask[index] and not outside[index]:
                outside[index] = 1
                stack.append(index)
    for y in range(height):
        for x in (0, width - 1):
            index = y * width + x
            if not mask[index] and not outside[index]:
                outside[index] = 1
                stack.append(index)
    while stack:
        index = stack.pop()
        y, x = divmod(index, width)
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if nx < 0 or ny < 0 or nx >= width or ny >= height:
                continue
            neighbor = ny * width + nx
            if mask[neighbor] or outside[neighbor]:
                continue
            outside[neighbor] = 1
            stack.append(neighbor)
    seen = bytearray(width * height)
    holes: list[tuple[bytearray, int]] = []
    minimum = max(MIN_INTERIOR, int(MIN_HOLE_FRACTION * width * height))
    for start in range(width * height):
        if mask[start] or outside[start] or seen[start]:
            continue
        component = _flood_component(seen, mask, outside, width, height, start)
        if len(component) < minimum:
            continue
        if len(component) >= FRAME_HOLE_AREA * width * height:
            continue
        filled = bytearray(width * height)
        for index in component:
            filled[index] = 1
        contour = _trace_outer(filled, width, height)
        if len(contour) < 12:
            continue
        frame_hits = sum(
            1 for point in contour if _vertex_on_frame(point[0], point[1], width, height, frame, tol=3.0)
        )
        if frame_hits / len(contour) > MAX_FRAME_BOUNDARY:
            continue
        holes.append((filled, len(component)))
    holes.sort(key=lambda item: -item[1])
    return holes


def _dedup_figures(figures: list[ShoreFigure]) -> list[ShoreFigure]:
    kept: list[ShoreFigure] = []
    for figure in figures:
        if any(_same_figure(figure, other) for other in kept):
            continue
        kept.append(figure)
    kept.sort(key=lambda figure: -_ring_area(figure.ring))
    return kept


def _same_figure(left: ShoreFigure, right: ShoreFigure) -> bool:
    if not left.ring or not right.ring:
        return False
    lx = sum(point[0] for point in left.ring) / len(left.ring)
    ly = sum(point[1] for point in left.ring) / len(left.ring)
    rx = sum(point[0] for point in right.ring) / len(right.ring)
    ry = sum(point[1] for point in right.ring) / len(right.ring)
    span = max(_ring_span(left.ring), _ring_span(right.ring), 1.0)
    if math.hypot(lx - rx, ly - ry) > 0.08 * span:
        return False
    area_l = _ring_area(left.ring)
    area_r = _ring_area(right.ring)
    if area_l <= 0 or area_r <= 0:
        return False
    return min(area_l, area_r) >= 0.75 * max(area_l, area_r)


def _ring_area(ring: tuple[tuple[float, float], ...] | list[tuple[float, float]]) -> float:
    area = 0.0
    count = len(ring)
    for index, (x1, y1) in enumerate(ring):
        x2, y2 = ring[(index + 1) % count]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _ring_span(ring: tuple[tuple[float, float], ...] | list[tuple[float, float]]) -> float:
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    return max(max(xs) - min(xs), max(ys) - min(ys))


def clear_cartographic_lines(
    mask: bytearray,
    width: int,
    height: int,
    shore_rings: list[list[tuple[float, float]]] | None = None,
) -> tuple[int, bool]:
    """Clear PLSS lines, neatline strokes, and grid ticks on ``mask``.

    Detection runs on the shoreline work grid and is the same for every sheet.
    A long straight stroke counts when it shares the sheet's grid angle and
    either spans opposite borders or has a parallel companion. Dashed pieces
    of one line are joined first. A tick is a short stub that leaves the
    neatline and stops. A shore of that length that continues inland stays.

    Returns how many source pixels were cleared, and whether the clear was
    rejected. Any contained hole that opens rejects the clear unless the
    cleared pixels are disjoint from ``shore_rings``. A rectangular shore is
    that hole's boundary, so its pixels stay. The caller keeps the previous
    trace when the flag is set.
    """
    if width < 24 or height < 24 or len(mask) != width * height:
        return 0, False
    small, small_w, small_h, factor = _shrink_mask(mask, width, height, SHORE_MAX_SIDE)
    before = bytearray(small)
    frame = _detect_frame(small, small_w, small_h)
    doomed = _cartographic_pixels(small, small_w, small_h, frame)
    if not any(doomed):
        return 0, False
    # A shore that crosses the line is ink off the line. Keep that pixel so
    # the basin does not open at the crossing. The rest of the line goes.
    _keep_crossing_pixels(small, doomed, small_w, small_h)
    _protect_short_irregular_seals(small, small_w, small_h, doomed)
    after = bytearray(small)
    for index, flag in enumerate(doomed):
        if flag:
            after[index] = 0
    hole_opened = _contained_hole_opened(before, after, small_w, small_h)
    hits_shore = _cleared_hits_rings(doomed, small_w, small_h, factor, shore_rings or [])
    # No ring was supplied, so a hole that opens cannot be shown to miss the shore.
    disjoint = bool(shore_rings) and not hits_shore
    if hole_opened and not disjoint:
        return 0, True
    small = after
    opened = False
    cleared = 0
    if factor == 1:
        mask[:] = small
        return sum(1 for was, now in zip(before, small, strict=True) if was and not now), opened
    for y in range(small_h):
        row = y * small_w
        for x in range(small_w):
            index = row + x
            if before[index] and not small[index]:
                y0 = y * factor
                x0 = x * factor
                for yy in range(y0, min(height, y0 + factor)):
                    dest = yy * width
                    for xx in range(x0, min(width, x0 + factor)):
                        if mask[dest + xx]:
                            mask[dest + xx] = 0
                            cleared += 1
    return cleared, opened


def irregular_basin_opened(
    before: bytearray,
    after: bytearray,
    width: int,
    height: int,
) -> bool:
    """True when a cleared pixel connected an irregular hole to the page.

    A section cell is a straight hole. Opening one joins the lake. An
    irregular hole that reaches the page border was a real basin seal.
    """
    if width < 8 or height < 8 or len(before) != width * height or len(after) != width * height:
        return False
    if not any(was and not now for was, now in zip(before, after, strict=True)):
        return False
    outside = _outside_mask(before, width, height)
    holes = _contained_holes(before, width, height, outside)
    if not holes:
        return False
    opened = _outside_mask(after, width, height)
    for hole in holes:
        if not any(opened[index] for index in hole):
            continue
        if _hole_straightness(hole, width, height) >= CARTO_CELL_STRAIGHTNESS:
            continue
        return True
    return False


def _contained_hole_opened(
    before: bytearray,
    after: bytearray,
    width: int,
    height: int,
) -> bool:
    """True when any contained hole, straight or not, reaches the page."""
    if width < 8 or height < 8 or len(before) != width * height or len(after) != width * height:
        return False
    if not any(was and not now for was, now in zip(before, after, strict=True)):
        return False
    outside = _outside_mask(before, width, height)
    holes = _sealed_white(before, width, height, outside)
    if not holes:
        return False
    opened = _outside_mask(after, width, height)
    return any(any(opened[index] for index in hole) for hole in holes)


def _sealed_white(
    mask: bytearray,
    width: int,
    height: int,
    outside: bytearray,
) -> list[list[int]]:
    """White the border cannot reach, including a basin the frame-hole cap would skip."""
    seen = bytearray(width * height)
    holes: list[list[int]] = []
    for start in range(width * height):
        if mask[start] or outside[start] or seen[start]:
            continue
        component = _flood_component(seen, mask, outside, width, height, start)
        if len(component) < MIN_INTERIOR:
            continue
        holes.append(component)
    return holes


def _cleared_hits_rings(
    doomed: bytearray,
    width: int,
    height: int,
    factor: int,
    rings: list[list[tuple[float, float]]],
) -> bool:
    """True when a doomed pixel lies on a current shore ring."""
    if factor < 1:
        return True
    for ring in rings:
        if len(ring) < 2:
            continue
        points = list(ring) + [ring[0]]
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            for x, y in _line_pixels(x1 / factor, y1 / factor, x2 / factor, y2 / factor):
                if 0 <= x < width and 0 <= y < height and doomed[y * width + x]:
                    return True
    return False


def _line_pixels(x1: float, y1: float, x2: float, y2: float):
    steps = max(int(math.hypot(x2 - x1, y2 - y1)), 1)
    for index in range(steps + 1):
        fraction = index / steps
        yield round(x1 + (x2 - x1) * fraction), round(y1 + (y2 - y1) * fraction)


def _cartographic_pixels(
    mask: bytearray,
    width: int,
    height: int,
    frame: tuple[int, int, int, int] | None,
) -> bytearray:
    doomed = bytearray(width * height)
    long_side = max(width, height)
    min_len = max(10, int(0.06 * long_side))
    chains = _straight_ink_chains(mask, width, height, min_len)
    # Ticks are shorter than that minimum (4 px up to 4.5% of the side), so
    # they never enter ``chains``. Collect them on their own.
    stubs = _straight_ink_chains(mask, width, height, CARTO_TICK_MIN_PX)
    groups = _collinear_groups(chains, max(4.0, CARTO_GAP_FRACTION * long_side))
    if frame is not None:
        _mark_frame_stroke(doomed, width, height, frame)
    if not groups and not any(doomed):
        _mark_ticks(doomed, mask, width, height, frame, stubs)
        return doomed
    grid = _dominant_grid_angle(groups)
    min_group = CARTO_MIN_FRACTION * long_side
    chosen: list[int] = []
    for index, group in enumerate(groups):
        if grid is None or _fold_dist(group["fold"], grid) > math.radians(CARTO_ANGLE_DEG):
            continue
        if group["length"] < min_group:
            continue
        if _group_side_fraction(group, mask, width, height) > CARTO_MAX_SIDE_FRACTION:
            continue
        if _group_spans_opposite(group, width, height, frame) or _has_parallel_companion(groups, index):
            chosen.append(index)
    for index in chosen:
        for chain in groups[index]["chains"]:
            for x, y in chain:
                doomed[y * width + x] = 1
    _mark_ticks(doomed, mask, width, height, frame, stubs)
    return doomed


def _collinear_groups(
    chains: list[list[tuple[int, int]]],
    gap_max: float,
) -> list[dict]:
    """Union dashed pieces that lie on one straight line."""
    parent = list(range(len(chains)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    metrics = [_chain_metrics(chain) for chain in chains]
    for left in range(len(chains)):
        for right in range(left + 1, len(chains)):
            if _chains_collinear(metrics[left], metrics[right], gap_max):
                parent[find(left)] = find(right)
    buckets: dict[int, list[int]] = {}
    for index in range(len(chains)):
        buckets.setdefault(find(index), []).append(index)
    groups: list[dict] = []
    for members in buckets.values():
        member_chains = [chains[index] for index in members]
        length = sum(metrics[index]["length"] for index in members)
        # The longest piece sets the line angle. A short dash must not rotate it.
        lead = max(members, key=lambda index: metrics[index]["length"])
        groups.append(
            {
                "chains": member_chains,
                "length": length,
                "fold": metrics[lead]["fold"],
                "origin": metrics[lead]["origin"],
                "direction": metrics[lead]["direction"],
                "points": [point for chain in member_chains for point in chain],
            }
        )
    return groups


def _chain_metrics(chain: list[tuple[int, int]]) -> dict:
    x0, y0 = chain[0]
    x1, y1 = chain[-1]
    dx = float(x1 - x0)
    dy = float(y1 - y0)
    length = math.hypot(dx, dy)
    if length < 1e-6:
        direction = (1.0, 0.0)
        length = float(len(chain))
    else:
        direction = (dx / length, dy / length)
    return {
        "length": length,
        "origin": (float(x0), float(y0)),
        "direction": direction,
        "fold": _fold_grid(math.atan2(dy, dx)),
        "points": chain,
    }


def _chains_collinear(left: dict, right: dict, gap_max: float) -> bool:
    if _fold_dist(left["fold"], right["fold"]) > math.radians(CARTO_ANGLE_DEG):
        return False
    ox, oy = left["origin"]
    ux, uy = left["direction"]
    laterals = [
        abs((x - ox) * uy - (y - oy) * ux) for x, y in (right["points"][0], right["points"][-1])
    ]
    if max(laterals) > CARTO_LATERAL_PX:
        return False
    left_along = [
        (x - ox) * ux + (y - oy) * uy for x, y in (left["points"][0], left["points"][-1])
    ]
    right_along = [
        (x - ox) * ux + (y - oy) * uy for x, y in (right["points"][0], right["points"][-1])
    ]
    left_lo, left_hi = min(left_along), max(left_along)
    right_lo, right_hi = min(right_along), max(right_along)
    gap = max(left_lo, right_lo) - min(left_hi, right_hi)
    return gap <= gap_max


def _fold_grid(angle: float) -> float:
    """Line angle folded into ``[0, pi/2)``. Perpendicular strokes share a bin."""
    half = math.pi / 2.0
    folded = angle % half
    if folded < 0:
        folded += half
    return folded


def _fold_dist(left: float, right: float) -> float:
    half = math.pi / 2.0
    delta = abs(left - right) % half
    return min(delta, half - delta)


def _dominant_grid_angle(groups: list[dict]) -> float | None:
    if not groups:
        return None
    bin_width = math.radians(CARTO_ANGLE_DEG)
    bins: dict[int, float] = {}
    for group in groups:
        slot = int(group["fold"] / bin_width)
        bins[slot] = bins.get(slot, 0.0) + group["length"]
    slot = max(bins, key=lambda key: bins[key])
    # The longest bin is the sheet grid. A stroke still has to sit inside the
    # angle tolerance, so one long dam does not pull the other lines with it.
    return (slot + 0.5) * bin_width


def _group_side_fraction(group: dict, mask: bytearray, width: int, height: int) -> float:
    points = group["points"]
    if len(points) < 4:
        return 1.0
    ux, uy = group["direction"]
    px, py = -uy, ux
    step = max(1, len(points) // 40)
    hits = 0
    samples = 0
    owned = set(points)
    for x, y in points[::step]:
        samples += 1
        for sign in (-3, 3):
            nx = int(round(x + px * sign))
            ny = int(round(y + py * sign))
            if (nx, ny) in owned or nx < 0 or ny < 0 or nx >= width or ny >= height:
                continue
            if mask[ny * width + nx]:
                hits += 1
                break
    if samples == 0:
        return 1.0
    return hits / samples


def _group_spans_opposite(
    group: dict,
    width: int,
    height: int,
    frame: tuple[int, int, int, int] | None,
) -> bool:
    xs = [point[0] for point in group["points"]]
    ys = [point[1] for point in group["points"]]
    tol = 3
    near_left = min(xs) <= tol or (frame is not None and min(xs) <= frame[0] + tol)
    near_right = max(xs) >= width - 1 - tol or (frame is not None and max(xs) >= frame[2] - tol)
    near_top = min(ys) <= tol or (frame is not None and min(ys) <= frame[1] + tol)
    near_bottom = max(ys) >= height - 1 - tol or (frame is not None and max(ys) >= frame[3] - tol)
    return (near_left and near_right) or (near_top and near_bottom)


def _has_parallel_companion(groups: list[dict], index: int) -> bool:
    group = groups[index]
    ox, oy = group["origin"]
    ux, uy = group["direction"]
    for other_index, other in enumerate(groups):
        if other_index == index:
            continue
        if _fold_dist(group["fold"], other["fold"]) > math.radians(CARTO_ANGLE_DEG):
            continue
        laterals = [
            abs((x - ox) * uy - (y - oy) * ux) for x, y in (other["points"][0], other["points"][-1])
        ]
        separation = min(laterals)
        if separation < 6.0:
            continue
        return True
    return False


def _mark_frame_stroke(
    doomed: bytearray,
    width: int,
    height: int,
    frame: tuple[int, int, int, int],
) -> None:
    """The neatline itself. A two-pixel band covers a double-ruled border."""
    left, top, right, bottom = frame
    for x in range(max(0, left), min(width, right + 1)):
        for band in range(-1, 2):
            for y in (top + band, bottom + band):
                if 0 <= y < height:
                    doomed[y * width + x] = 1
    for y in range(max(0, top), min(height, bottom + 1)):
        for band in range(-1, 2):
            for x in (left + band, right + band):
                if 0 <= x < width:
                    doomed[y * width + x] = 1


def _mark_ticks(
    doomed: bytearray,
    mask: bytearray,
    width: int,
    height: int,
    frame: tuple[int, int, int, int] | None,
    chains: list[list[tuple[int, int]]],
) -> None:
    """Short strokes that leave the neatline and stop. A shore does not stop there."""
    long_side = max(width, height)
    max_len = max(CARTO_TICK_MIN_PX + 1, int(CARTO_TICK_MAX_FRACTION * long_side))
    for chain in chains:
        if not (CARTO_TICK_MIN_PX <= len(chain) <= max_len):
            continue
        ends = (chain[0], chain[-1])
        border = [end for end in ends if _on_sheet_border(end[0], end[1], width, height, frame)]
        if len(border) != 1:
            continue
        dx = chain[-1][0] - chain[0][0]
        dy = chain[-1][1] - chain[0][1]
        if math.hypot(dx, dy) < CARTO_TICK_MIN_PX:
            continue
        anchor = border[0]
        free = chain[-1] if anchor == chain[0] else chain[0]
        # A shore of tick length keeps going inland. A tick stops.
        if _ink_leaves_chain(mask, width, height, chain, free):
            continue
        # The stub has to leave the border, not run along it.
        on_vertical = anchor[0] <= 3 or anchor[0] >= width - 4 or (
            frame is not None and (abs(anchor[0] - frame[0]) <= 3 or abs(anchor[0] - frame[2]) <= 3)
        )
        if on_vertical:
            if abs(dy) > abs(dx):
                continue
        elif abs(dx) > abs(dy):
            continue
        for x, y in chain:
            if mask[y * width + x]:
                doomed[y * width + x] = 1


def _ink_leaves_chain(
    mask: bytearray,
    width: int,
    height: int,
    chain: list[tuple[int, int]],
    end: tuple[int, int],
) -> bool:
    """True when ink continues past ``end`` off this chain."""
    occupied = set(chain)
    x, y = end
    for dx, dy in _INK_STEPS:
        nx = x + dx
        ny = y + dy
        if nx < 0 or ny < 0 or nx >= width or ny >= height:
            continue
        if (nx, ny) in occupied:
            continue
        if mask[ny * width + nx]:
            return True
    return False


def _on_sheet_border(
    x: int,
    y: int,
    width: int,
    height: int,
    frame: tuple[int, int, int, int] | None,
) -> bool:
    if x <= 3 or y <= 3 or x >= width - 4 or y >= height - 4:
        return True
    if frame is None:
        return False
    left, top, right, bottom = frame
    on_horizontal = (abs(y - top) <= 3 or abs(y - bottom) <= 3) and left - 3 <= x <= right + 3
    on_vertical = (abs(x - left) <= 3 or abs(x - right) <= 3) and top - 3 <= y <= bottom + 3
    return on_horizontal or on_vertical


def _keep_crossing_pixels(mask: bytearray, doomed: bytearray, width: int, height: int) -> None:
    keep = bytearray(width * height)
    for index, flag in enumerate(doomed):
        if not flag or not mask[index]:
            continue
        y, x = divmod(index, width)
        for dy in (-1, 0, 1):
            yy = y + dy
            if yy < 0 or yy >= height:
                continue
            for dx in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                xx = x + dx
                if xx < 0 or xx >= width:
                    continue
                neighbor = yy * width + xx
                if mask[neighbor] and not doomed[neighbor]:
                    keep[index] = 1
                    break
    for index, flag in enumerate(keep):
        if flag:
            doomed[index] = 0


def _protect_short_irregular_seals(
    mask: bytearray,
    width: int,
    height: int,
    doomed: bytearray,
) -> None:
    """Keep a short straight shore. A section line is longer than this and stays doomed."""
    if not any(doomed):
        return
    outside = _outside_mask(mask, width, height)
    holes = _contained_holes(mask, width, height, outside)
    limit = CARTO_MIN_FRACTION * max(width, height)
    for hole in holes:
        if _hole_straightness(hole, width, height) >= CARTO_CELL_STRAIGHTNESS:
            continue
        seal = _seal_pixels_for_hole(mask, width, height, doomed, outside, hole)
        for component in _mask_components(seal, width, height):
            if len(component) >= limit:
                continue
            for index in component:
                doomed[index] = 0


def _hole_straightness(hole: list[int], width: int, height: int) -> float:
    filled = bytearray(width * height)
    for index in hole:
        filled[index] = 1
    ring = _trace_outer(filled, width, height)
    if len(ring) < 8:
        return 0.0
    return _ring_straightness(ring)


def _seal_pixels_for_hole(
    mask: bytearray,
    width: int,
    height: int,
    doomed: bytearray,
    outside: bytearray,
    hole: list[int],
) -> bytearray:
    """Doomed pixels of this hole that also reach the page through doomed ink."""
    protect = bytearray(width * height)
    if not hole or not any(doomed):
        return protect
    hole_mask = bytearray(width * height)
    for index in hole:
        hole_mask[index] = 1
    opened = _outside_mask(mask, width, height, also_passable=doomed)
    if not any(opened[index] for index in hole):
        return protect
    candidates: list[int] = []
    for index, flag in enumerate(doomed):
        if not flag:
            continue
        y, x = divmod(index, width)
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height and hole_mask[ny * width + nx]:
                candidates.append(index)
                break
    reachable = _doomed_reaching_outside(doomed, width, height, outside)
    for index in candidates:
        if reachable[index]:
            protect[index] = 1
    return protect


def _mask_components(mask: bytearray, width: int, height: int) -> list[list[int]]:
    _labels, components = _label_components(mask, width, height)
    return components
