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
shore. The georeferencer keeps whichever figure overlaps the hydro polygon
more.
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


def extract_shoreline(image: RasterImage) -> list[tuple[float, float]] | None:
    """Return an ordered outer shoreline in pixel coordinates, or None."""
    candidates = extract_shoreline_candidates(image)
    if not candidates:
        return None
    return candidates[0]


def extract_shoreline_candidates(image: RasterImage) -> list[list[tuple[float, float]]]:
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
    mask = _dark_mask(image)
    _clear_span_lines(mask, width, height, SPAN_RATIO)
    small, small_w, small_h, factor = _shrink_mask(mask, width, height, SHORE_MAX_SIDE)
    holes = _ranked_hole_contours(small, small_w, small_h)
    rings: list[list[tuple[float, float]]] = []
    for _area, contour in holes[:4]:
        scaled = _scale_ring(contour, factor)
        span = _span_fraction(scaled, width, height)
        if len(scaled) >= 8 and span >= MIN_FIGURE_SPAN:
            if span >= 0.35 and _road_like_ring(scaled, mask, image.width, image.height):
                continue
            rings.append(scaled)
    fallback = _component_shoreline(small, small_w, small_h)
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


def _component_shoreline(
    mask: bytearray, width: int, height: int
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
    """Fraction of perimeter that lies on long, nearly straight runs."""
    points = list(ring)
    count = len(points)
    if count < 8:
        return 0.0
    min_run = max(16.0, 0.05 * _ring_span(ring))
    straight = 0.0
    perimeter = 0.0
    index = 0
    while index < count:
        start = index
        j = (index + 1) % count
        dx = points[j][0] - points[index][0]
        dy = points[j][1] - points[index][1]
        seg_len = math.hypot(dx, dy)
        if seg_len < 1e-6:
            index += 1
            continue
        ux, uy = dx / seg_len, dy / seg_len
        run = seg_len
        step = j
        while run < min_run:
            nxt = (step + 1) % count
            if nxt == start:
                break
            edx = points[nxt][0] - points[step][0]
            edy = points[nxt][1] - points[step][1]
            elen = math.hypot(edx, edy)
            if elen < 1e-6:
                step = nxt
                continue
            cross = abs(ux * (edy / elen) - uy * (edx / elen))
            if cross > 0.1:
                break
            run += elen
            step = nxt
        if run >= min_run:
            straight += run
        perimeter += seg_len
        index += 1
    return straight / max(perimeter, 1e-6)


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
    mask: bytearray, width: int, height: int
) -> list[tuple[int, list[tuple[float, float]]]]:
    raw = _hole_contours(mask, width, height)
    scored: list[tuple[float, int, list[tuple[float, float]]]] = []
    for area, contour in raw:
        ink = _interior_ink_in_hole(mask, width, height, contour)
        straight = _ring_straightness(contour)
        if straight >= MAX_ROAD_STRAIGHTNESS and ink < MIN_LAKE_INK:
            continue
        score = float(area) * (0.015 + ink) / (1.0 + max(0.0, straight - 0.2) * 2.5)
        scored.append((score, area, contour))
    scored.sort(key=lambda item: -item[0])
    return [(area, contour) for _score, area, contour in scored]


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
    """Drop neatline and PLSS ink, but keep pixels where a shore crosses them."""
    row_need = ratio * width
    col_need = ratio * height
    span_rows = [y for y in range(height) if sum(mask[y * width : (y + 1) * width]) >= row_need]
    span_cols = [
        x for x in range(width) if sum(mask[y * width + x] for y in range(height)) >= col_need
    ]
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
    for y in span_rows:
        start = y * width
        for x in range(width):
            if not keep[start + x]:
                mask[start + x] = 0
    for x in span_cols:
        for y in range(height):
            index = y * width + x
            if not keep[index]:
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


def extract_shoreline_figures(image: RasterImage) -> list[ShoreFigure]:
    """Closed-shore candidates plus gap-closed and page-edge-cut figures.

    The list is small. Registration scores every figure and keeps the best
    overlap, so a tight inner contour loses to the outer shore when the outer
    shore is actually in the list.
    """
    figures: list[ShoreFigure] = []
    for ring in extract_shoreline_candidates(image):
        figures.append(ShoreFigure(tuple((float(x), float(y)) for x, y in ring), tuple(False for _ in ring)))
    figures.extend(_gap_closed_figures(image))
    return _dedup_figures(figures)[:8]


def _gap_closed_figures(image: RasterImage) -> list[ShoreFigure]:
    width = image.width
    height = image.height
    if width < 8 or height < 8:
        return []
    mask = _dark_mask(image)
    small, small_w, small_h, factor = _shrink_mask(mask, width, height, SHORE_WORK_SIDE)
    frame = _detect_frame(small, small_w, small_h)
    _clear_long_runs(
        small,
        small_w,
        small_h,
        max(8, int(0.16 * max(small_w, small_h))),
        keep=frame,
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


def _clear_long_runs(
    mask: bytearray,
    width: int,
    height: int,
    min_len: int,
    keep: tuple[int, int, int, int] | None = None,
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
            if _on_kept_frame(x, y, keep) or _run_has_side_ink(mask, width, height, x, y, axis="row"):
                continue
            mask[y * width + x] = 0
    for x, y0, y1 in vertical:
        for y in range(y0, y1):
            if _on_kept_frame(x, y, keep) or _run_has_side_ink(mask, width, height, x, y, axis="col"):
                continue
            mask[y * width + x] = 0


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
