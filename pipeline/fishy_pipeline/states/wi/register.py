# SPDX-License-Identifier: GPL-3.0-only
"""Register a pixel shoreline to a hydro ring in EPSG:3071.

Initialization is a bounding-box scale plus four orientations (and a mirrored
copy). Refinement is affine point registration: arc-length phase search and
iterative closest points. The winning affine places a small set of ground
control points. A thin-plate spline through those points is what ``gdalwarp
-tps`` applies to the raster.

The reported IoU is the lower of the affine overlap and the TPS overlap. The
spline may bend a shoreline toward the hydro ring, and the affine score keeps
a mismatched waterbody below the review threshold.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_GCP_COUNT = 8
_SAMPLE_COUNT = 64
_DENSE_COUNT = 256


@dataclass(frozen=True)
class GroundControlPoint:
    pixel: float
    line: float
    easting: float
    northing: float

    def as_dict(self) -> dict[str, float]:
        return {
            "pixel": self.pixel,
            "line": self.line,
            "easting": self.easting,
            "northing": self.northing,
        }


@dataclass(frozen=True)
class Affine:
    """``easting = a*col + b*row + tx``, ``northing = c*col + d*row + ty``."""

    a: float
    b: float
    tx: float
    c: float
    d: float
    ty: float

    def apply(self, col: float, row: float) -> tuple[float, float]:
        return (
            self.a * col + self.b * row + self.tx,
            self.c * col + self.d * row + self.ty,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "a": self.a,
            "b": self.b,
            "tx": self.tx,
            "c": self.c,
            "d": self.d,
            "ty": self.ty,
        }


@dataclass
class ThinPlateSpline:
    sources: list[tuple[float, float]]
    easting_weights: list[float]
    northing_weights: list[float]
    easting_affine: tuple[float, float, float]
    northing_affine: tuple[float, float, float]

    def apply(self, col: float, row: float) -> tuple[float, float]:
        easting = self.easting_affine[0] + self.easting_affine[1] * col + self.easting_affine[2] * row
        northing = (
            self.northing_affine[0] + self.northing_affine[1] * col + self.northing_affine[2] * row
        )
        for (sx, sy), we, wn in zip(self.sources, self.easting_weights, self.northing_weights, strict=True):
            kernel = _tps_kernel(math.hypot(col - sx, row - sy))
            easting += we * kernel
            northing += wn * kernel
        return easting, northing


@dataclass(frozen=True)
class Registration:
    affine: Affine
    tps: ThinPlateSpline
    gcps: tuple[GroundControlPoint, ...]
    affine_iou: float
    tps_iou: float

    @property
    def iou(self) -> float:
        return min(self.affine_iou, self.tps_iou)


def signed_area(ring: list[tuple[float, float]]) -> float:
    area = 0.0
    for index, (x1, y1) in enumerate(ring):
        x2, y2 = ring[(index + 1) % len(ring)]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def ensure_orientation(ring: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Return a copy whose shoelace area is positive."""
    if signed_area(ring) < 0:
        return list(reversed(ring))
    return list(ring)


def resample_closed(ring: list[tuple[float, float]], count: int) -> list[tuple[float, float]]:
    """Evenly sample a closed ring by arc length. ``count`` >= 3."""
    if count < 3:
        raise ValueError("resample count must be at least 3")
    points = list(ring)
    if len(points) < 3:
        raise ValueError("ring must have at least 3 points")
    lengths: list[float] = []
    total = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        length = math.hypot(x2 - x1, y2 - y1)
        lengths.append(length)
        total += length
    if total <= 0:
        raise ValueError("ring has zero length")
    step = total / count
    samples: list[tuple[float, float]] = []
    segment = 0
    walked = 0.0
    x1, y1 = points[0]
    x2, y2 = points[1]
    seg_len = lengths[0]
    for sample_index in range(count):
        target = sample_index * step
        while walked + seg_len < target and segment < len(points) - 1:
            walked += seg_len
            segment += 1
            x1, y1 = points[segment]
            x2, y2 = points[(segment + 1) % len(points)]
            seg_len = lengths[segment]
        if seg_len <= 1e-12:
            samples.append((x1, y1))
            continue
        fraction = (target - walked) / seg_len
        samples.append((x1 + fraction * (x2 - x1), y1 + fraction * (y2 - y1)))
    return samples


def fit_affine(
    source: list[tuple[float, float]],
    target: list[tuple[float, float]],
) -> Affine:
    """Least-squares affine from pixel pairs to map pairs."""
    if len(source) != len(target) or len(source) < 3:
        raise ValueError("affine fit needs at least 3 paired points")
    xs = [point[0] for point in source]
    ys = [point[1] for point in source]
    slope_e, row_e, easting0 = _fit_plane(xs, ys, [point[0] for point in target])
    slope_n, row_n, northing0 = _fit_plane(xs, ys, [point[1] for point in target])
    return Affine(a=slope_e, b=row_e, tx=easting0, c=slope_n, d=row_n, ty=northing0)


def polygon_iou(
    ring_a: list[tuple[float, float]],
    ring_b: list[tuple[float, float]],
    grid: int = 160,
) -> float:
    """Intersection over union by even-odd rasterization of cell centers."""
    if len(ring_a) < 3 or len(ring_b) < 3:
        return 0.0
    xs = [point[0] for ring in (ring_a, ring_b) for point in ring]
    ys = [point[1] for ring in (ring_a, ring_b) for point in ring]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max_x - min_x
    span_y = max_y - min_y
    if span_x <= 0 or span_y <= 0:
        return 0.0
    min_x -= span_x * 0.02
    max_x += span_x * 0.02
    min_y -= span_y * 0.02
    max_y += span_y * 0.02
    cell_x = (max_x - min_x) / grid
    cell_y = (max_y - min_y) / grid
    mask_a = _rasterize(ring_a, min_x, min_y, cell_x, cell_y, grid)
    mask_b = _rasterize(ring_b, min_x, min_y, cell_x, cell_y, grid)
    intersection = 0
    union = 0
    for left, right in zip(mask_a, mask_b, strict=True):
        if left or right:
            union += 1
        if left and right:
            intersection += 1
    if union == 0:
        return 0.0
    return intersection / union


def register_shoreline(
    pixel_ring: list[tuple[float, float]],
    hydro_ring: list[tuple[float, float]],
    *,
    gcp_count: int = DEFAULT_GCP_COUNT,
) -> Registration:
    """Fit pixel shoreline → hydro ring and score both the affine and the TPS."""
    if gcp_count < 4:
        raise ValueError("gcp_count must be at least 4")
    source = resample_closed(ensure_orientation(pixel_ring), _SAMPLE_COUNT)
    hydro = resample_closed(ensure_orientation(list(hydro_ring)), _DENSE_COUNT)
    affine = _best_affine(source, hydro)
    gcps = _control_points(pixel_ring, hydro, affine, gcp_count)
    tps = fit_tps(gcps)
    affine_ring = [affine.apply(col, row) for col, row in pixel_ring]
    tps_ring = [tps.apply(col, row) for col, row in pixel_ring]
    hydro_list = list(hydro_ring)
    return Registration(
        affine=affine,
        tps=tps,
        gcps=tuple(gcps),
        affine_iou=polygon_iou(affine_ring, hydro_list),
        tps_iou=polygon_iou(tps_ring, hydro_list),
    )


def fit_tps(gcps: list[GroundControlPoint]) -> ThinPlateSpline:
    """Solve a 2D thin-plate spline. Kernel is ``r^2 log r``, matching GDAL."""
    count = len(gcps)
    if count < 3:
        raise ValueError("TPS needs at least 3 control points")
    size = count + 3
    matrix = [[0.0] * size for _ in range(size)]
    for i in range(count):
        for j in range(count):
            if i == j:
                matrix[i][j] = 0.0
                continue
            distance = math.hypot(gcps[i].pixel - gcps[j].pixel, gcps[i].line - gcps[j].line)
            matrix[i][j] = _tps_kernel(distance)
        matrix[i][count] = 1.0
        matrix[i][count + 1] = gcps[i].pixel
        matrix[i][count + 2] = gcps[i].line
        matrix[count][i] = 1.0
        matrix[count + 1][i] = gcps[i].pixel
        matrix[count + 2][i] = gcps[i].line
    easting = _solve(matrix, [gcp.easting for gcp in gcps] + [0.0, 0.0, 0.0])
    northing = _solve(matrix, [gcp.northing for gcp in gcps] + [0.0, 0.0, 0.0])
    return ThinPlateSpline(
        sources=[(gcp.pixel, gcp.line) for gcp in gcps],
        easting_weights=easting[:count],
        northing_weights=northing[:count],
        easting_affine=(easting[count], easting[count + 1], easting[count + 2]),
        northing_affine=(northing[count], northing[count + 1], northing[count + 2]),
    )


def _tps_kernel(distance: float) -> float:
    if distance <= 1e-12:
        return 0.0
    return distance * distance * math.log(distance)


def _best_affine(
    source: list[tuple[float, float]],
    hydro_dense: list[tuple[float, float]],
) -> Affine:
    phase_source = resample_closed(source, _SAMPLE_COUNT)
    phase_target = resample_closed(hydro_dense, _SAMPLE_COUNT)
    candidates = _bbox_orientation_affines(phase_source, phase_target)
    candidates.extend(_phase_affines(phase_source, phase_target))
    candidates.extend(_phase_affines(list(reversed(phase_source)), phase_target))
    refined = list(candidates)
    for candidate in candidates:
        refined.append(_icp(phase_source, hydro_dense, candidate))
    best = refined[0]
    best_error = _nn_rms(phase_source, hydro_dense, best)
    for candidate in refined[1:]:
        error = _nn_rms(phase_source, hydro_dense, candidate)
        if error < best_error:
            best = candidate
            best_error = error
    return best


def _bbox_orientation_affines(
    source: list[tuple[float, float]],
    target: list[tuple[float, float]],
) -> list[Affine]:
    """Scale source bbox to the target bbox at 0/90/180/270, plus a mirror."""
    affines: list[Affine] = []
    for mirrored in (False, True):
        points = _mirror_y(source) if mirrored else source
        for quarter in range(4):
            theta = quarter * math.pi / 2.0
            affines.append(_compose_mirror(mirrored, source, _similarity_from_bbox(points, target, theta)))
    return affines


def _similarity_from_bbox(
    source: list[tuple[float, float]],
    target: list[tuple[float, float]],
    theta: float,
) -> Affine:
    sx, sy = _centroid(source)
    tx, ty = _centroid(target)
    source_diag = _bbox_diagonal(source)
    target_diag = _bbox_diagonal(target)
    scale = target_diag / source_diag if source_diag > 0 else 1.0
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    a = scale * cos_t
    b = -scale * sin_t
    c = scale * sin_t
    d = scale * cos_t
    return Affine(
        a=a,
        b=b,
        tx=tx - (a * sx + b * sy),
        c=c,
        d=d,
        ty=ty - (c * sx + d * sy),
    )


def _mirror_y(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    _cx, cy = _centroid(points)
    return [(x, 2 * cy - y) for x, y in points]


def _compose_mirror(mirrored: bool, original: list[tuple[float, float]], affine: Affine) -> Affine:
    """Express an affine that was fit on y-mirrored points in the original pixels."""
    if not mirrored:
        return affine
    _cx, cy = _centroid(original)
    # y' = 2*cy - y
    return Affine(
        a=affine.a,
        b=-affine.b,
        tx=affine.tx + 2 * affine.b * cy,
        c=affine.c,
        d=-affine.d,
        ty=affine.ty + 2 * affine.d * cy,
    )


def _phase_affines(
    source: list[tuple[float, float]],
    target: list[tuple[float, float]],
) -> list[Affine]:
    count = len(source)
    if len(target) != count:
        raise ValueError("phase search needs equal sample counts")
    # Coarse shifts, then a local refinement around the best. Enough for a closed shore.
    step = max(1, count // 16)
    shifts = list(range(0, count, step))
    best_shift = 0
    best_rms = math.inf
    best: Affine | None = None
    for shift in shifts:
        paired = [target[(index + shift) % count] for index in range(count)]
        affine = fit_affine(source, paired)
        rms = _pair_rms(source, paired, affine)
        if rms < best_rms:
            best_rms = rms
            best_shift = shift
            best = affine
    for shift in range(best_shift - step, best_shift + step + 1):
        wrapped = shift % count
        paired = [target[(index + wrapped) % count] for index in range(count)]
        affine = fit_affine(source, paired)
        rms = _pair_rms(source, paired, affine)
        if rms < best_rms:
            best_rms = rms
            best = affine
    if best is None:
        raise ValueError("phase search produced no affine")
    return [best]


def _icp(
    source: list[tuple[float, float]],
    target: list[tuple[float, float]],
    affine: Affine,
    iterations: int = 8,
) -> Affine:
    current = affine
    for _ in range(iterations):
        paired = [_nearest(current.apply(x, y), target) for x, y in source]
        current = fit_affine(source, paired)
    return current


def _control_points(
    pixel_ring: list[tuple[float, float]],
    hydro_dense: list[tuple[float, float]],
    affine: Affine,
    gcp_count: int,
) -> list[GroundControlPoint]:
    samples = resample_closed(pixel_ring, gcp_count)
    gcps: list[GroundControlPoint] = []
    for col, row in samples:
        easting, northing = _nearest(affine.apply(col, row), hydro_dense)
        gcps.append(GroundControlPoint(pixel=col, line=row, easting=easting, northing=northing))
    return gcps


def _nn_rms(
    source: list[tuple[float, float]],
    target: list[tuple[float, float]],
    affine: Affine,
) -> float:
    error = 0.0
    for x, y in source:
        matched = _nearest(affine.apply(x, y), target)
        mapped = affine.apply(x, y)
        error += (mapped[0] - matched[0]) ** 2 + (mapped[1] - matched[1]) ** 2
    return math.sqrt(error / len(source))


def _pair_rms(
    source: list[tuple[float, float]],
    target: list[tuple[float, float]],
    affine: Affine,
) -> float:
    error = 0.0
    for (x, y), (tx, ty) in zip(source, target, strict=True):
        mx, my = affine.apply(x, y)
        error += (mx - tx) ** 2 + (my - ty) ** 2
    return math.sqrt(error / len(source))


def _nearest(
    point: tuple[float, float],
    cloud: list[tuple[float, float]],
) -> tuple[float, float]:
    best = cloud[0]
    best_d = math.inf
    px, py = point
    for cx, cy in cloud:
        dist = (cx - px) ** 2 + (cy - py) ** 2
        if dist < best_d:
            best_d = dist
            best = (cx, cy)
    return best


def _centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    count = len(points)
    return (
        sum(point[0] for point in points) / count,
        sum(point[1] for point in points) / count,
    )


def _bbox_diagonal(points: list[tuple[float, float]]) -> float:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return math.hypot(max(xs) - min(xs), max(ys) - min(ys))


def _fit_plane(
    xs: list[float],
    ys: list[float],
    values: list[float],
) -> tuple[float, float, float]:
    sxx = syy = sxy = sx = sy = 0.0
    sxv = syv = sv = 0.0
    count = float(len(xs))
    for x, y, value in zip(xs, ys, values, strict=True):
        sxx += x * x
        syy += y * y
        sxy += x * y
        sx += x
        sy += y
        sxv += x * value
        syv += y * value
        sv += value
    matrix = [
        [sxx, sxy, sx],
        [sxy, syy, sy],
        [sx, sy, count],
    ]
    slope_x, slope_y, intercept = _solve(matrix, [sxv, syv, sv])
    return slope_x, slope_y, intercept


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-12:
            raise ValueError("singular linear system")
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        scale = augmented[col][col]
        for column in range(col, size + 1):
            augmented[col][column] /= scale
        for row in range(size):
            if row == col:
                continue
            factor = augmented[row][col]
            if factor == 0:
                continue
            for column in range(col, size + 1):
                augmented[row][column] -= factor * augmented[col][column]
    return [augmented[index][size] for index in range(size)]


def _rasterize(
    ring: list[tuple[float, float]],
    min_x: float,
    min_y: float,
    cell_x: float,
    cell_y: float,
    grid: int,
) -> bytearray:
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
    return cells
