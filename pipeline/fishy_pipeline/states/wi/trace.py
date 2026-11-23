# SPDX-License-Identifier: GPL-3.0-only
"""Trace depth lines out of a georeferenced Wisconsin map raster.

The clip, the ink test, and the polyline walk are ordinary Python. Reading a
GeoTIFF needs the GDAL Python bindings. Inpaint, component filtering on a
full sheet, skeletonizing, and OCR import OpenCV, scikit-image, and EasyOCR
from the ``wi`` extra when a real sheet is traced. Unit tests cover the
stdlib pieces and never import those libraries.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Frozen from the Ada Lake scan: paper is near 255 and ink is near 0.
# Do not change these to lift a single lake over the pilot gate.
INK_LUMINANCE_MAX = 160
BLUE_DELTA = 30
SPECK_AREA = 40
COMPACT_AREA = 450
COMPACT_ELONGATION = 2.8
COMPACT_MAX_SIDE = 36
MAX_TRACE_PX = 4500
SPUR_PX = 12
SIMPLIFY_PX = 1.25
CLOSE_GAP_PX = 18
MIN_LINE_PX = 20
OCR_MIN_CONFIDENCE = 0.35
SNAP_PX = 28
WI_CRS = "EPSG:3071"

_ORTH = ((0, -1), (1, 0), (0, 1), (-1, 0))
_DIAG = ((-1, -1), (1, -1), (1, 1), (-1, 1))


@dataclass(frozen=True)
class TracedLine:
    coords: tuple[tuple[float, float], ...]
    closed: bool


@dataclass(frozen=True)
class TracedLabel:
    x: float
    y: float
    depth_ft: float
    confidence: float


@dataclass(frozen=True)
class TracedSheet:
    lines: tuple[TracedLine, ...]
    labels: tuple[TracedLabel, ...]
    pixel_size_m: float
    snap_m: float
    unit: str = "ft"


def gdal_tools_available() -> bool:
    return shutil.which("gdalwarp") is not None and shutil.which("gdal_translate") is not None


def gdalwarp_clip_args(source: str, dest: str, cutline: str) -> list[str]:
    """Clip a warped sheet to the hydro polygon, including island holes.

    ``-dstalpha`` keeps the outside of the polygon (and the inside of each
    island) at alpha 0 so black ink stays a real color instead of nodata.
    """
    return [
        "gdalwarp",
        "-cutline",
        cutline,
        "-crop_to_cutline",
        "-dstalpha",
        "-r",
        "near",
        "-t_srs",
        WI_CRS,
        "-wo",
        "CUTLINE_ALL_TOUCHED=TRUE",
        "-overwrite",
        source,
        dest,
    ]


def gdal_translate_cap_args(source: str, dest: str, *, width: int, height: int, max_px: int = MAX_TRACE_PX) -> list[str] | None:
    """Nearest-neighbor downscale when a sheet is larger than ``max_px``."""
    long_side = max(width, height)
    if long_side <= max_px:
        return None
    if width >= height:
        return ["gdal_translate", "-r", "nearest", "-outsize", str(max_px), "0", source, dest]
    return ["gdal_translate", "-r", "nearest", "-outsize", "0", str(max_px), source, dest]


def ink_mask(
    rgb: bytes,
    width: int,
    height: int,
    alpha: bytes | None,
    *,
    luminance_max: int = INK_LUMINANCE_MAX,
) -> bytearray:
    """Dark ink or blue ink on the valid sheet. 1 means ink."""
    count = width * height
    if len(rgb) != count * 3:
        raise ValueError("rgb length does not match the raster size")
    if alpha is not None and len(alpha) != count:
        raise ValueError("alpha length does not match the raster size")
    mask = bytearray(count)
    for index in range(count):
        if alpha is not None and alpha[index] == 0:
            continue
        offset = index * 3
        red = rgb[offset]
        green = rgb[offset + 1]
        blue = rgb[offset + 2]
        luminance = (red + green + blue) // 3
        dark = luminance < luminance_max
        blue_ink = blue > 80 and blue > red + BLUE_DELTA and blue > green + 15 and luminance < 210
        if dark or blue_ink:
            mask[index] = 1
    return mask


def blank_text_boxes(
    rgb: bytes,
    width: int,
    height: int,
    boxes: list[tuple[int, int, int, int]],
) -> bytes:
    """Paint OCR boxes white. Used when OpenCV inpaint is not installed."""
    out = bytearray(rgb)
    for x0, y0, x1, y1 in boxes:
        x0 = max(0, x0)
        y0 = max(0, y0)
        x1 = min(width, x1)
        y1 = min(height, y1)
        for y in range(y0, y1):
            start = (y * width + x0) * 3
            end = (y * width + x1) * 3
            out[start:end] = b"\xff" * (end - start)
    return bytes(out)


def filter_small_components(mask: bytearray, width: int, height: int) -> bytearray:
    """Drop specks and letter-sized blobs. Long contour strokes stay."""
    try:
        return _filter_cv(mask, width, height)
    except ImportError:
        if width * height > 250_000:
            raise RuntimeError("component filtering needs opencv from the wi extra") from None
        return _filter_py(mask, width, height)


def skeleton_points(mask: bytearray, width: int, height: int) -> set[tuple[int, int]]:
    try:
        import numpy as np
        from skimage.morphology import skeletonize
    except ImportError as error:
        raise RuntimeError("skeletonize needs scikit-image from the wi extra") from error
    image = np.frombuffer(mask, dtype=np.uint8).reshape(height, width)
    skel = skeletonize(image.astype(bool))
    ys, xs = np.nonzero(skel)
    return set(zip(xs.tolist(), ys.tolist(), strict=True))


def neighbors(point: tuple[int, int], points: set[tuple[int, int]]) -> list[tuple[int, int]]:
    """8-connected neighbors, without the diagonal shortcut across a corner."""
    x, y = point
    found: list[tuple[int, int]] = []
    for dx, dy in _ORTH:
        candidate = (x + dx, y + dy)
        if candidate in points:
            found.append(candidate)
    for dx, dy in _DIAG:
        candidate = (x + dx, y + dy)
        if candidate not in points:
            continue
        if (x + dx, y) in points or (x, y + dy) in points:
            continue
        found.append(candidate)
    return found


def prune_spurs(points: set[tuple[int, int]], max_len: int = SPUR_PX) -> set[tuple[int, int]]:
    """Remove short branches that leave a junction. A free arc is kept."""
    points = set(points)
    for _ in range(max(1, len(points))):
        degrees = {point: len(neighbors(point, points)) for point in points}
        removed = False
        for endpoint in [point for point, degree in degrees.items() if degree == 1]:
            chain = [endpoint]
            previous = None
            current = endpoint
            hit_junction = False
            while len(chain) <= max_len:
                nxt = [point for point in neighbors(current, points) if point != previous]
                if current != endpoint and degrees.get(current, 0) >= 3:
                    hit_junction = True
                    break
                if not nxt:
                    break
                if len(nxt) > 1:
                    hit_junction = True
                    break
                step = nxt[0]
                if degrees.get(step, 0) >= 3:
                    hit_junction = True
                    break
                chain.append(step)
                previous, current = current, step
            if hit_junction and 0 < len(chain) <= max_len:
                for point in chain:
                    points.discard(point)
                removed = True
                break
        if not removed:
            break
    return points


def trace_polylines(points: set[tuple[int, int]]) -> list[tuple[list[tuple[int, int]], bool]]:
    """Walk a skeleton into open paths and closed rings."""
    if not points:
        return []
    adjacency = {point: neighbors(point, points) for point in points}
    unused: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for point, nbrs in adjacency.items():
        for other in nbrs:
            unused.add(_edge(point, other))
    polylines: list[tuple[list[tuple[int, int]], bool]] = []

    def walk(start: tuple[int, int], first: tuple[int, int]) -> tuple[list[tuple[int, int]], bool]:
        path = [start, first]
        unused.discard(_edge(start, first))
        previous = start
        current = first
        guard = 0
        limit = len(points) + 2
        while guard < limit:
            guard += 1
            if current == start and len(path) > 2:
                return path, True
            options = [point for point in adjacency[current] if _edge(current, point) in unused and point != previous]
            if not options:
                return path, False
            if len(adjacency[current]) >= 3 and len(path) > 2:
                return path, False
            step = options[0]
            unused.discard(_edge(current, step))
            path.append(step)
            previous, current = current, step
        return path, current == start and len(path) > 2

    starters = [point for point in points if len(adjacency[point]) != 2]
    if not starters:
        starters = [next(iter(points))]
    for start in list(starters):
        for other in list(adjacency[start]):
            if _edge(start, other) in unused:
                path, closed = walk(start, other)
                if len(path) >= 2:
                    polylines.append((path, closed))
    safety = 0
    while unused and safety < len(points) + 2:
        safety += 1
        start, first = next(iter(unused))
        path, closed = walk(start, first)
        if len(path) >= 2:
            polylines.append((path, closed))
        else:
            unused.discard(_edge(start, first))
    return polylines


def simplify(path: list[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    """Douglas-Peucker. ``epsilon`` is in the same units as ``path``."""
    if len(path) < 3 or epsilon <= 0:
        return list(path)
    end = len(path) - 1
    keep = {0, end}
    stack = [(0, end)]
    while stack:
        start, stop = stack.pop()
        if stop <= start + 1:
            continue
        ax, ay = path[start]
        bx, by = path[stop]
        worst = -1.0
        worst_index = None
        for index in range(start + 1, stop):
            dist = _segment_distance(path[index], (ax, ay), (bx, by))
            if dist > worst:
                worst = dist
                worst_index = index
        if worst_index is not None and worst > epsilon:
            keep.add(worst_index)
            stack.append((start, worst_index))
            stack.append((worst_index, stop))
    return [path[index] for index in sorted(keep)]


def bridge_label_gaps(
    mask: bytearray,
    width: int,
    height: int,
    boxes: list[tuple[int, int, int, int]],
) -> bytearray:
    """Reconnect a contour whose label was inpainted out.

    A depth number sits on the line. After the glyphs are removed, the stroke
    has two ink clusters on the box edge. Draw the short segment between them.
    More than two clusters is ambiguous and is left alone.
    """
    if not boxes:
        return mask
    out = bytearray(mask)
    for x0, y0, x1, y1 in boxes:
        left = max(0, x0)
        top = max(0, y0)
        right = min(width, x1)
        bottom = min(height, y1)
        if right - left < 2 or bottom - top < 2:
            continue
        seeds: list[tuple[int, int]] = []
        for y in range(max(0, top - 3), min(height, bottom + 3)):
            row = y * width
            for x in range(max(0, left - 3), min(width, right + 3)):
                if left <= x < right and top <= y < bottom:
                    continue
                if out[row + x]:
                    seeds.append((x, y))
        clusters = _cluster_seeds(seeds)
        if len(clusters) != 2:
            continue
        start, end = _closest_seeds(clusters[0], clusters[1])
        _stamp_line(out, width, height, start, end)
    return out


def close_gap(path: list[tuple[int, int]], gap_px: float = CLOSE_GAP_PX) -> tuple[list[tuple[int, int]], bool]:
    if len(path) < 4:
        return path, False
    if path[0] == path[-1]:
        return path, True
    gap = math.hypot(path[0][0] - path[-1][0], path[0][1] - path[-1][1])
    length = 0.0
    for index in range(len(path) - 1):
        length += math.hypot(path[index + 1][0] - path[index][0], path[index + 1][1] - path[index][1])
    if gap <= gap_px and length >= gap * 4:
        return path + [path[0]], True
    return path, False


def clip_raster(source: Path, dest: Path, cutline: Path) -> None:
    if not gdal_tools_available():
        raise RuntimeError("gdalwarp and gdal_translate are required to clip a sheet")
    _run(gdalwarp_clip_args(str(source), str(dest), str(cutline)))


def trace_sheet(
    raster_path: Path,
    work_dir: Path,
    *,
    wbic: str | None = None,
    sheet_gcp_path: Path | None = None,
    ocr_fallback: bool = False,
) -> TracedSheet:
    """Inpaint recorded labels when present, optionally OCR, then trace lines in map coordinates."""
    prepared = _cap_raster(raster_path, work_dir / "trace-raster.tif")
    rgb, alpha, transform, width, height = read_geotiff(prepared)
    labels_px: list[tuple[float, float, float, float]] = []
    boxes: list[tuple[int, int, int, int]] = []
    texts: list[str] = []
    unit = "ft"
    if wbic and sheet_gcp_path and sheet_gcp_path.is_file():
        from fishy_pipeline.states.wi.recorded_labels import labels_for_trace, load_gcps_from_work

        body = json.loads(sheet_gcp_path.read_text(encoding="utf-8"))
        gcps, _default_method, _affine_dict = load_gcps_from_work(sheet_gcp_path)
        method = str(body.get("method") or _default_method)
        affine = None
        if isinstance(body.get("affine"), dict):
            from fishy_pipeline.states.wi.register import Affine

            aff = body["affine"]
            affine = Affine(
                a=float(aff["a"]),
                b=float(aff["b"]),
                tx=float(aff["tx"]),
                c=float(aff["c"]),
                d=float(aff["d"]),
                ty=float(aff["ty"]),
            )
        labels_px, boxes, texts, unit = labels_for_trace(
            wbic,
            gcps=gcps,
            method=method,
            affine=affine,
            geotransform=transform,
            clipped_width=width,
            clipped_height=height,
        )
    if not labels_px and ocr_fallback:
        labels_px, boxes, texts = _ocr_labels(rgb, width, height)
        from fishy_pipeline.states.wi.depth_tag import sheet_depth_unit

        unit = sheet_depth_unit(texts)
    repaired = _inpaint(rgb, width, height, boxes)
    mask = ink_mask(repaired, width, height, alpha)
    mask = bridge_label_gaps(mask, width, height, boxes)
    mask = _close_mask(mask, width, height)
    mask = filter_small_components(mask, width, height)
    points = prune_spurs(skeleton_points(mask, width, height), SPUR_PX)
    pixel_lines = _pixel_lines(points)
    lines: list[TracedLine] = []
    for path, closed in pixel_lines:
        coords = tuple(_pixel_to_map(col + 0.5, row + 0.5, transform) for col, row in path)
        lines.append(TracedLine(coords=coords, closed=closed))
    labels = []
    for col, row, depth, confidence in labels_px:
        easting, northing = _pixel_to_map(col, row, transform)
        labels.append(TracedLabel(x=easting, y=northing, depth_ft=depth, confidence=confidence))
    pixel_size = math.hypot(transform[1], transform[5])
    if wbic and labels_px:
        from fishy_pipeline.states.wi.pilot_data import label_file_unit, load_recorded_labels

        recorded = load_recorded_labels(wbic)
        if recorded is not None:
            unit = label_file_unit(recorded)

    return TracedSheet(
        lines=tuple(lines),
        labels=tuple(labels),
        pixel_size_m=pixel_size,
        snap_m=max(12.0, SNAP_PX * pixel_size),
        unit=unit,
    )


def read_geotiff(path: Path) -> tuple[bytes, bytes | None, tuple[float, ...], int, int]:
    """Return RGB bytes, optional alpha, and the GDAL geotransform."""
    try:
        from osgeo import gdal
    except ImportError as error:
        raise RuntimeError("reading a GeoTIFF needs python3-gdal") from error
    dataset = gdal.Open(str(path))
    if dataset is None:
        raise RuntimeError(f"could not open {path}")
    width = dataset.RasterXSize
    height = dataset.RasterYSize
    transform = tuple(float(value) for value in dataset.GetGeoTransform())
    count = dataset.RasterCount
    if count < 1:
        raise RuntimeError(f"no bands in {path}")
    # ReadRaster stays on the GDAL bindings. ReadAsArray pulls in gdal_array,
    # which is tied to the system NumPy and breaks when another wheel upgrades it.
    planes = [_read_band(dataset.GetRasterBand(index + 1), width, height) for index in range(count)]
    dataset = None
    if count == 1:
        return _gray_to_rgb(planes[0]), None, transform, width, height
    if count == 2:
        return _interleave(planes[0], planes[0], planes[0]), planes[1], transform, width, height
    alpha = planes[-1] if count >= 4 else None
    return _interleave(planes[0], planes[1], planes[2]), alpha, transform, width, height


def _read_band(band, width: int, height: int) -> bytes:
    from osgeo import gdal

    data = band.ReadRaster(0, 0, width, height, width, height, gdal.GDT_Byte)
    if not data or len(data) != width * height:
        raise RuntimeError("GDAL ReadRaster returned no bytes")
    return data


def _gray_to_rgb(gray: bytes) -> bytes:
    rgb = bytearray(len(gray) * 3)
    for index, value in enumerate(gray):
        rgb[index * 3 : index * 3 + 3] = bytes((value, value, value))
    return bytes(rgb)


def _interleave(red: bytes, green: bytes, blue: bytes) -> bytes:
    try:
        import numpy as np
    except ImportError:
        rgb = bytearray(len(red) * 3)
        for index, (r, g, b) in enumerate(zip(red, green, blue, strict=True)):
            rgb[index * 3] = r
            rgb[index * 3 + 1] = g
            rgb[index * 3 + 2] = b
        return bytes(rgb)
    stacked = np.stack(
        (
            np.frombuffer(red, dtype=np.uint8),
            np.frombuffer(green, dtype=np.uint8),
            np.frombuffer(blue, dtype=np.uint8),
        ),
        axis=-1,
    )
    return stacked.tobytes()


def _cap_raster(source: Path, dest: Path) -> Path:
    try:
        from osgeo import gdal
    except ImportError as error:
        raise RuntimeError("reading a GeoTIFF needs python3-gdal") from error
    dataset = gdal.Open(str(source))
    if dataset is None:
        raise RuntimeError(f"could not open {source}")
    width = dataset.RasterXSize
    height = dataset.RasterYSize
    dataset = None
    args = gdal_translate_cap_args(str(source), str(dest), width=width, height=height)
    if args is None:
        return source
    _run(args)
    return dest


def _ocr_labels(
    rgb: bytes, width: int, height: int
) -> tuple[list[tuple[float, float, float, float]], list[tuple[int, int, int, int]], list[str]]:
    from fishy_pipeline.states.wi.depth_tag import parse_depth_token

    try:
        import numpy as np
    except ImportError as error:
        raise RuntimeError("OCR needs the wi extra") from error
    reader = _reader()
    image = np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3).copy()
    raw = reader.readtext(image)
    labels: list[tuple[float, float, float, float]] = []
    boxes: list[tuple[int, int, int, int]] = []
    texts: list[str] = []
    for box, text, confidence in raw:
        texts.append(str(text))
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        x0 = int(math.floor(min(xs))) - 2
        y0 = int(math.floor(min(ys))) - 2
        x1 = int(math.ceil(max(xs))) + 2
        y1 = int(math.ceil(max(ys))) + 2
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        boxes.append((x0, y0, x1, y1))
        parsed = parse_depth_token(str(text))
        if parsed is None or float(confidence) < OCR_MIN_CONFIDENCE:
            continue
        labels.append((sum(xs) / len(xs), sum(ys) / len(ys), parsed[0], float(confidence)))
    return labels, boxes, texts


def _reader():
    global _READER
    if _READER is None:
        try:
            import easyocr
        except ImportError as error:
            raise RuntimeError("OCR needs easyocr from the wi extra: pip install -e 'pipeline/.[wi]'") from error
        _READER = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _READER


_READER = None


def _inpaint(rgb: bytes, width: int, height: int, boxes: list[tuple[int, int, int, int]]) -> bytes:
    if not boxes:
        return rgb
    try:
        import cv2
        import numpy as np
    except ImportError:
        return blank_text_boxes(rgb, width, height, boxes)
    image = np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3).copy()
    mask = np.zeros((height, width), dtype=np.uint8)
    for x0, y0, x1, y1 in boxes:
        mask[max(0, y0) : min(height, y1), max(0, x0) : min(width, x1)] = 255
    repaired = cv2.inpaint(image, mask, 3, cv2.INPAINT_TELEA)
    return repaired.tobytes()


def _close_mask(mask: bytearray, width: int, height: int) -> bytearray:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return mask
    image = np.frombuffer(mask, dtype=np.uint8).reshape(height, width)
    kernel = np.ones((3, 3), dtype=np.uint8)
    closed = cv2.morphologyEx(image, cv2.MORPH_CLOSE, kernel)
    return bytearray(closed.tobytes())


def _filter_cv(mask: bytearray, width: int, height: int) -> bytearray:
    import cv2
    import numpy as np

    image = np.frombuffer(bytes(mask), dtype=np.uint8).reshape(height, width)
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(image, connectivity=8)
    keep = np.zeros_like(image)
    for index in range(1, count):
        if _keep_component(
            int(stats[index, cv2.CC_STAT_AREA]),
            int(stats[index, cv2.CC_STAT_WIDTH]),
            int(stats[index, cv2.CC_STAT_HEIGHT]),
        ):
            keep[labels == index] = 1
    return bytearray(keep.tobytes())


def _filter_py(mask: bytearray, width: int, height: int) -> bytearray:
    seen = bytearray(width * height)
    keep = bytearray(width * height)
    for start in range(width * height):
        if mask[start] == 0 or seen[start]:
            continue
        stack = [start]
        seen[start] = 1
        component = [start]
        min_x = max_x = start % width
        min_y = max_y = start // width
        while stack:
            index = stack.pop()
            x = index % width
            y = index // width
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx = x + dx
                    ny = y + dy
                    if nx < 0 or ny < 0 or nx >= width or ny >= height:
                        continue
                    nxt = ny * width + nx
                    if mask[nxt] == 0 or seen[nxt]:
                        continue
                    seen[nxt] = 1
                    stack.append(nxt)
                    component.append(nxt)
                    min_x = min(min_x, nx)
                    max_x = max(max_x, nx)
                    min_y = min(min_y, ny)
                    max_y = max(max_y, ny)
        if _keep_component(len(component), max_x - min_x + 1, max_y - min_y + 1):
            for index in component:
                keep[index] = 1
    return keep


def _keep_component(area: int, box_w: int, box_h: int) -> bool:
    if area <= SPECK_AREA:
        return False
    long_side = max(box_w, box_h)
    short_side = max(1, min(box_w, box_h))
    elongation = long_side / short_side
    if area <= COMPACT_AREA and elongation < COMPACT_ELONGATION and long_side < COMPACT_MAX_SIDE:
        return False
    return True


def _pixel_lines(points: set[tuple[int, int]]) -> list[tuple[list[tuple[int, int]], bool]]:
    traced = []
    for path, closed in trace_polylines(points):
        path, closed = close_gap(path, CLOSE_GAP_PX)
        if _pixel_length(path) < MIN_LINE_PX:
            continue
        simplified = simplify([(float(x), float(y)) for x, y in path], SIMPLIFY_PX)
        rounded = [(int(round(x)), int(round(y))) for x, y in simplified]
        if closed and rounded[0] != rounded[-1]:
            rounded.append(rounded[0])
        traced.append((rounded, closed))
    return traced


def _pixel_length(path: list[tuple[int, int]]) -> float:
    total = 0.0
    for index in range(len(path) - 1):
        total += math.hypot(path[index + 1][0] - path[index][0], path[index + 1][1] - path[index][1])
    return total


def _pixel_to_map(col: float, row: float, transform: tuple[float, ...]) -> tuple[float, float]:
    x = transform[0] + col * transform[1] + row * transform[2]
    y = transform[3] + col * transform[4] + row * transform[5]
    return x, y


def _cluster_seeds(seeds: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    remaining = set(seeds)
    clusters: list[list[tuple[int, int]]] = []
    while remaining:
        start = remaining.pop()
        cluster = [start]
        stack = [start]
        while stack:
            x, y = stack.pop()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nxt = (x + dx, y + dy)
                    if nxt in remaining:
                        remaining.remove(nxt)
                        cluster.append(nxt)
                        stack.append(nxt)
        clusters.append(cluster)
    return clusters


def _closest_seeds(
    left: list[tuple[int, int]], right: list[tuple[int, int]]
) -> tuple[tuple[int, int], tuple[int, int]]:
    best = (left[0], right[0])
    best_d = math.inf
    for a in left:
        for b in right:
            dist = (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2
            if dist < best_d:
                best_d = dist
                best = (a, b)
    return best


def _stamp_line(
    mask: bytearray,
    width: int,
    height: int,
    start: tuple[int, int],
    end: tuple[int, int],
) -> None:
    x0, y0 = start
    x1, y1 = end
    steps = max(abs(x1 - x0), abs(y1 - y0), 1)
    for step in range(steps + 1):
        x = int(round(x0 + (x1 - x0) * step / steps))
        y = int(round(y0 + (y1 - y0) * step / steps))
        if 0 <= x < width and 0 <= y < height:
            mask[y * width + x] = 1


def _edge(a: tuple[int, int], b: tuple[int, int]) -> tuple[tuple[int, int], tuple[int, int]]:
    return (a, b) if a <= b else (b, a)


def _segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    px, py = point
    x1, y1 = start
    x2, y2 = end
    dx = x2 - x1
    dy = y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(px - x1, py - y1)
    tee = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
    return math.hypot(px - (x1 + tee * dx), py - (y1 + tee * dy))


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"{command[0]} failed: {detail[:500]}")
