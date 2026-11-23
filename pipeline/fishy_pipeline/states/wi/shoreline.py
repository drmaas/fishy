# SPDX-License-Identifier: GPL-3.0-only
"""Pull the dominant lake shoreline out of a bathymetry map raster.

Borders, PLSS grid lines, and legend blocks are suppressed before the shoreline
is chosen. The kept curve is the outer edge of the largest white region that
the ink fully encloses: that is the water inside the drawn shore. An inner
depth contour is a smaller hole, so it loses to the shore when the shore is
closed. A hole that covers the whole sheet is the map frame and is skipped.
Real DNR sheets that still fail this heuristic land in the review queue.
"""

from __future__ import annotations

import math
from array import array
from collections import deque

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
    holes = _hole_contours(small, small_w, small_h)
    rings: list[list[tuple[float, float]]] = []
    if holes:
        largest = _scale_ring(holes[0][1], factor)
        if len(largest) >= 8 and _span_fraction(largest, width, height) >= MIN_FIGURE_SPAN:
            rings.append(largest)
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
        if (rgb[offset] + rgb[offset + 1] + rgb[offset + 2]) // 3 < DARK_THRESHOLD:
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
