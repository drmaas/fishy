# SPDX-License-Identifier: GPL-3.0-only
"""Register a pixel shoreline to a hydro ring in EPSG:3071.

Initialization is a similarity sweep over continuous rotation (every few
degrees, plus a mirror) and the older four-orientation bbox seeds. Refinement
is affine point registration: arc-length phase search and iterative closest
points. The affine is the candidate with the best polygon overlap, not the
lowest nearest-point residual. A low residual can sit on the wrong phase of a
lobed shore. An affine that stretches much more in one axis than the other is
dropped; a scanned sheet is close to a similarity, and a stretched inner
contour can otherwise outscore the real shore.

A shoreline that runs off the sheet is only partly the lake. Those frame
vertices are marked and the score is the overlap inside the sheet footprint
(hydro clipped to the matched portion), not the whole waterbody.

Ground control points stay on that affine unless the nearest hydro vertex is
close. A thin-plate spline through those points is used only when it does not
pull the shore farther than the affine residual. Otherwise the warp is the
affine itself. The reported IoU is the overlap of the warp that is applied.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_GCP_COUNT = 8
_SAMPLE_COUNT = 64
_DENSE_COUNT = 256
# Coarse pose search. 6° is enough for ICP to finish the alignment.
ROTATION_STEP_DEG = 6
# Singular-value ratio above this is a stretched false match, not a scan.
MAX_ANISOTROPY = 1.40
# A frame edge this long, relative to the ring, is a sheet cut.
_PARTIAL_CUT_FRACTION = 0.08
# Two control points closer than this in map meters are one ground location.
# 1 m is under the spacing of shore samples on a 24K lake and still merges a
# hydro vertex that two pixels snapped to.
GCP_MAP_EPS_M = 1.0
# Pixel twins closer than this are one sample. Shore control points are spread
# around the ring, so 1.5 px does not merge a real pair.
GCP_PIXEL_EPS = 1.5
# An outlier has to beat both a multiple of the median residual and a share of
# the control cloud. A consistent fit stays inside both.
GCP_OUTLIER_MEDIAN_FACTOR = 8.0
GCP_OUTLIER_SPAN_FRACTION = 0.15
# Smaller pixel spread over the larger. Below this the points do not determine
# an affine: they are collinear or piled on one spot.
GCP_MIN_SPREAD_RATIO = 1.0e-3
GCP_MIN_COUNT = 3


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
class PreparedControl:
    """Control points after duplicate, outlier, and degeneracy checks.

    ``reason`` is set when fewer than three non-collinear points remain. The
    caller fails closed instead of warping that set.
    """

    points: tuple[GroundControlPoint, ...]
    dropped: int
    reason: str | None

    @property
    def usable(self) -> bool:
        return self.reason is None


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
    method: str = "tps"

    @property
    def iou(self) -> float:
        if self.method == "affine":
            return self.affine_iou
        return self.tps_iou

    @property
    def rotation_deg(self) -> float:
        """Clockwise degrees from map north to image-up. 0 means north is up."""
        return rotation_deg_of(self.affine)


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


def union_polygon_iou(
    rings: list[list[tuple[float, float]]],
    hydro: list[tuple[float, float]],
    grid: int = 160,
) -> float:
    """IoU of the union of ``rings`` against ``hydro`` on one raster."""
    usable = [ring for ring in rings if len(ring) >= 3]
    if not usable or len(hydro) < 3:
        return 0.0
    xs = [point[0] for ring in (*usable, hydro) for point in ring]
    ys = [point[1] for ring in (*usable, hydro) for point in ring]
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
    mask_u = bytearray(grid * grid)
    for ring in usable:
        part = _rasterize(ring, min_x, min_y, cell_x, cell_y, grid)
        for index, value in enumerate(part):
            if value:
                mask_u[index] = 1
    mask_h = _rasterize(hydro, min_x, min_y, cell_x, cell_y, grid)
    intersection = 0
    union = 0
    for left, right in zip(mask_u, mask_h, strict=True):
        if left or right:
            union += 1
        if left and right:
            intersection += 1
    if union == 0:
        return 0.0
    return intersection / union


def rotation_deg_of(affine: Affine) -> float:
    """Clockwise degrees from map north to image-up.

    Image-up is pixel direction ``(0, -1)``. Its map vector is ``(-b, -d)``.
    """
    return math.degrees(math.atan2(-affine.b, -affine.d)) % 360.0


def anisotropy(affine: Affine) -> float:
    """Ratio of the larger singular value of the linear map to the smaller."""
    aa = affine.a * affine.a + affine.c * affine.c
    bb = affine.b * affine.b + affine.d * affine.d
    ab = affine.a * affine.b + affine.c * affine.d
    mid = (aa + bb) / 2.0
    rad = math.sqrt(max(0.0, mid * mid - (aa * bb - ab * ab)))
    major = math.sqrt(max(0.0, mid + rad))
    minor = math.sqrt(max(0.0, mid - rad))
    if minor < 1e-9:
        return math.inf
    return major / minor


def register_shoreline(
    pixel_ring: list[tuple[float, float]],
    hydro_ring: list[tuple[float, float]],
    gcp_count: int = DEFAULT_GCP_COUNT,
    on_frame: list[bool] | None = None,
) -> Registration:
    """Fit pixel shoreline → hydro ring and score both the affine and the TPS.

    ``on_frame`` marks vertices that lie on the sheet edge rather than the
    shore. When enough of the ring is frame, the score is partial: hydro is
    clipped to the sheet footprint before the overlap is measured.
    """
    if gcp_count < 4:
        raise ValueError("gcp_count must be at least 4")
    # A page-edge cut is not shoreline. Matching it as a closed ring lets a
    # wrong pose cover the whole lake and outscore the real fragment.
    if on_frame is not None and _cut_fraction(on_frame) >= _PARTIAL_CUT_FRACTION:
        partial = _register_partial(pixel_ring, hydro_ring, on_frame, gcp_count=gcp_count)
        if partial is not None:
            return partial
    return _register_closed(pixel_ring, hydro_ring, gcp_count=gcp_count, on_frame=on_frame)


def _register_closed(
    pixel_ring: list[tuple[float, float]],
    hydro_ring: list[tuple[float, float]],
    *,
    gcp_count: int,
    on_frame: list[bool] | None,
) -> Registration:
    source = resample_closed(ensure_orientation(pixel_ring), _SAMPLE_COUNT)
    hydro = resample_closed(ensure_orientation(list(hydro_ring)), _DENSE_COUNT)
    affine = _best_affine(source, hydro)
    return _registration_from_affine(
        pixel_ring,
        hydro_ring,
        hydro,
        affine,
        gcp_count=gcp_count,
        on_frame=on_frame,
    )


def prepare_control_points(gcps: list[GroundControlPoint]) -> PreparedControl:
    """Drop duplicate, near-coincident, and outlier control points.

    The same rules run for every warp. Near-coincident pixel coordinates or
    near-coincident map coordinates are one observation. A residual that is
    many times the median and a large share of the cloud is dropped. The set
    is unusable when fewer than three non-collinear points remain.
    """
    finite = [
        gcp
        for gcp in gcps
        if all(math.isfinite(value) for value in (gcp.pixel, gcp.line, gcp.easting, gcp.northing))
    ]
    unique: list[GroundControlPoint] = []
    for gcp in finite:
        if any(_control_points_coincide(gcp, kept) for kept in unique):
            continue
        unique.append(gcp)
    cleaned = _drop_outlier_controls(unique)
    dropped = len(gcps) - len(cleaned)
    if len(cleaned) < GCP_MIN_COUNT:
        return PreparedControl(
            points=tuple(cleaned),
            dropped=dropped,
            reason=f"degenerate control points: {len(cleaned)} remain, need {GCP_MIN_COUNT} non-collinear",
        )
    if _control_spread_ratio(cleaned) < GCP_MIN_SPREAD_RATIO:
        return PreparedControl(
            points=tuple(cleaned),
            dropped=dropped,
            reason="degenerate control points: collinear",
        )
    return PreparedControl(points=tuple(cleaned), dropped=dropped, reason=None)


def _control_points_coincide(left: GroundControlPoint, right: GroundControlPoint) -> bool:
    if math.hypot(left.pixel - right.pixel, left.line - right.line) <= GCP_PIXEL_EPS:
        return True
    return math.hypot(left.easting - right.easting, left.northing - right.northing) <= GCP_MAP_EPS_M


def _drop_outlier_controls(points: list[GroundControlPoint]) -> list[GroundControlPoint]:
    """Drop a point the other points cannot predict.

    Leave-one-out keeps one wild residual from pulling the fit and hiding
    itself. A consistent set predicts each held-out point inside the limit,
    so a normal warp does not lose a control point.
    """
    if len(points) < GCP_MIN_COUNT + 1:
        return points
    worst: int | None = None
    worst_residual = 0.0
    for index in range(len(points)):
        others = points[:index] + points[index + 1 :]
        if len(others) < GCP_MIN_COUNT:
            continue
        try:
            affine = fit_affine(
                [(point.pixel, point.line) for point in others],
                [(point.easting, point.northing) for point in others],
            )
        except ValueError:
            continue
        other_residuals = []
        for point in others:
            easting, northing = affine.apply(point.pixel, point.line)
            other_residuals.append(math.hypot(easting - point.easting, northing - point.northing))
        other_residuals.sort()
        median = other_residuals[len(other_residuals) // 2]
        xs = [point.easting for point in others]
        ys = [point.northing for point in others]
        span = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
        limit = max(GCP_OUTLIER_MEDIAN_FACTOR * median, GCP_OUTLIER_SPAN_FRACTION * span, 1.0)
        held = points[index]
        easting, northing = affine.apply(held.pixel, held.line)
        residual = math.hypot(easting - held.easting, northing - held.northing)
        if residual > limit and residual > worst_residual:
            worst = index
            worst_residual = residual
    if worst is None:
        return points
    kept = points[:worst] + points[worst + 1 :]
    if len(kept) <= GCP_MIN_COUNT:
        return kept
    return _drop_outlier_controls(kept)


def _control_spread_ratio(points: list[GroundControlPoint]) -> float:
    count = len(points)
    if count < 2:
        return 0.0
    cx = sum(point.pixel for point in points) / count
    cy = sum(point.line for point in points) / count
    xx = yy = xy = 0.0
    for point in points:
        dx = point.pixel - cx
        dy = point.line - cy
        xx += dx * dx
        yy += dy * dy
        xy += dx * dy
    trace = xx + yy
    det = xx * yy - xy * xy
    disc = math.sqrt(max(0.0, trace * trace / 4.0 - det))
    major = trace / 2.0 + disc
    minor = trace / 2.0 - disc
    if major <= 1e-12:
        return 0.0
    return max(0.0, minor) / major


def registration_from_reviewed(
    pixel_ring: list[tuple[float, float]],
    hydro_ring: list[tuple[float, float]],
    gcps: list[GroundControlPoint],
    *,
    method: str = "tps",
) -> Registration:
    """Score a shoreline using human-placed control points."""
    prepared = prepare_control_points(gcps)
    if not prepared.usable:
        raise ValueError(prepared.reason or "degenerate control points")
    gcps = list(prepared.points)
    if len(gcps) < 3:
        raise ValueError("reviewed registration needs at least 3 GCPs")
    sources = [(gcp.pixel, gcp.line) for gcp in gcps]
    targets = [(gcp.easting, gcp.northing) for gcp in gcps]
    affine = fit_affine(sources, targets)
    tps = fit_tps(gcps)
    affine_ring = [affine.apply(col, row) for col, row in pixel_ring]
    tps_ring = [tps.apply(col, row) for col, row in pixel_ring]
    hydro_list = list(hydro_ring)
    affine_iou = polygon_iou(affine_ring, hydro_list)
    tps_iou = polygon_iou(tps_ring, hydro_list)
    chosen = "affine" if method == "affine" else "tps"
    return Registration(
        affine=affine,
        tps=tps,
        gcps=tuple(gcps),
        affine_iou=affine_iou,
        tps_iou=tps_iou,
        method=chosen,
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


def _registration_from_affine(
    pixel_ring: list[tuple[float, float]],
    hydro_ring: list[tuple[float, float]],
    hydro_dense: list[tuple[float, float]],
    affine: Affine,
    *,
    gcp_count: int,
    on_frame: list[bool] | None,
) -> Registration:
    affine_error = _nn_rms(
        resample_closed(ensure_orientation(pixel_ring), _SAMPLE_COUNT),
        hydro_dense,
        affine,
    )
    gcps = _control_points(
        pixel_ring,
        hydro_dense,
        affine,
        gcp_count,
        snap_limit=max(affine_error * 3.0, 1.0),
        on_frame=on_frame,
    )
    prepared = prepare_control_points(gcps)
    gcps = list(prepared.points)
    affine_ring = [affine.apply(col, row) for col, row in pixel_ring]
    hydro_list = list(hydro_ring)
    affine_iou = _overlap(affine_ring, on_frame, hydro_list)
    try:
        if not prepared.usable:
            raise ValueError(prepared.reason or "degenerate control points")
        tps = fit_tps(gcps)
        tps_ring = [tps.apply(col, row) for col, row in pixel_ring]
        tps_iou = _overlap(tps_ring, on_frame, hydro_list)
        source = resample_closed(ensure_orientation(pixel_ring), _SAMPLE_COUNT)
        shift = _model_shift(source, affine, tps)
        span = _bbox_diagonal(affine_ring)
        walked_off = span > 0 and shift > 0.06 * span
        if tps_iou + 1e-9 < affine_iou or shift > max(affine_error, 1e-6) * 2.5 or walked_off:
            method = "affine"
        else:
            method = "tps"
    except ValueError:
        # A nearly straight shore makes the spline matrix singular. The affine stands.
        tps = ThinPlateSpline(
            sources=[(gcp.pixel, gcp.line) for gcp in gcps],
            easting_weights=[0.0] * len(gcps),
            northing_weights=[0.0] * len(gcps),
            easting_affine=(0.0, affine.a, affine.b),
            northing_affine=(0.0, affine.c, affine.d),
        )
        tps_iou = affine_iou
        method = "affine"
    return Registration(
        affine=affine,
        tps=tps,
        gcps=tuple(gcps),
        affine_iou=affine_iou,
        tps_iou=tps_iou,
        method=method,
    )


def _overlap(
    mapped: list[tuple[float, float]],
    on_frame: list[bool] | None,
    hydro: list[tuple[float, float]],
) -> float:
    if on_frame is not None and len(on_frame) == len(mapped) and _cut_fraction(on_frame) >= _PARTIAL_CUT_FRACTION:
        return _partial_iou(mapped, on_frame, hydro)
    return polygon_iou(mapped, hydro)


def _cut_fraction(on_frame: list[bool]) -> float:
    if not on_frame:
        return 0.0
    return sum(1 for flag in on_frame if flag) / len(on_frame)


def _best_affine(
    source: list[tuple[float, float]],
    hydro_dense: list[tuple[float, float]],
) -> Affine:
    phase_source = resample_closed(source, _SAMPLE_COUNT)
    phase_target = resample_closed(hydro_dense, _SAMPLE_COUNT)
    sweep = _rotation_sweep_similarities(phase_source, phase_target)
    sweep.sort(key=lambda aff: _coarse_overlap(aff, phase_source, phase_target), reverse=True)
    candidates = list(sweep[:5])
    candidates.extend(_bbox_orientation_affines(phase_source, phase_target))
    candidates.extend(_phase_affines(phase_source, phase_target))
    candidates.extend(_phase_affines(list(reversed(phase_source)), phase_target))
    refined: list[Affine] = []
    for candidate in candidates:
        refined.append(candidate)
        try:
            refined.append(_icp(phase_source, hydro_dense, candidate))
        except ValueError:
            continue
    best: Affine | None = None
    best_key: tuple[float, float] | None = None
    for candidate in refined:
        if anisotropy(candidate) > MAX_ANISOTROPY:
            continue
        key = _affine_rank(candidate, phase_source, phase_target, hydro_dense)
        if best is None or best_key is None or key > best_key:
            best = candidate
            best_key = key
    if best is None:
        # The sweep is already ranked by overlap. Skip a sheared pose instead of
        # admitting sweep[0] after every refined candidate missed the cap.
        return _sweep_fallback(sweep)
    return best


def _sweep_fallback(sweep: list[Affine]) -> Affine:
    """Highest-overlap sweep pose that stays within ``MAX_ANISOTROPY``."""
    for affine in sweep:
        if anisotropy(affine) <= MAX_ANISOTROPY:
            return affine
    raise ValueError("no pose within anisotropy cap")


def _coarse_overlap(
    affine: Affine,
    source: list[tuple[float, float]],
    target: list[tuple[float, float]],
) -> float:
    mapped = [affine.apply(x, y) for x, y in source]
    return polygon_iou(mapped, target, grid=36)


def _rotation_sweep_similarities(
    source: list[tuple[float, float]],
    target: list[tuple[float, float]],
) -> list[Affine]:
    """Uniform-scale poses every ``ROTATION_STEP_DEG``, and the mirrored shore."""
    affines: list[Affine] = []
    steps = max(1, int(round(360 / ROTATION_STEP_DEG)))
    for mirrored in (False, True):
        points = _mirror_y(source) if mirrored else source
        for step in range(steps):
            theta = step * (2.0 * math.pi / steps)
            posed = _similarity_ydown(points, target, theta)
            affines.append(_compose_mirror(mirrored, source, posed))
    return affines


def _similarity_ydown(
    source: list[tuple[float, float]],
    target: list[tuple[float, float]],
    theta: float,
) -> Affine:
    """Bbox similarity whose row axis points south at theta=0 (a y-down raster)."""
    sx, sy = _centroid(source)
    tx, ty = _centroid(target)
    source_diag = _bbox_diagonal(source)
    target_diag = _bbox_diagonal(target)
    scale = target_diag / source_diag if source_diag > 0 else 1.0
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    a = scale * cos_t
    b = -scale * sin_t
    c = -scale * sin_t
    d = -scale * cos_t
    return Affine(
        a=a,
        b=b,
        tx=tx - (a * sx + b * sy),
        c=c,
        d=d,
        ty=ty - (c * sx + d * sy),
    )


def _affine_rank(
    affine: Affine,
    source: list[tuple[float, float]],
    target: list[tuple[float, float]],
    hydro_dense: list[tuple[float, float]],
) -> tuple[float, float]:
    """Higher polygon overlap wins. Residual breaks a tie."""
    mapped = [affine.apply(x, y) for x, y in source]
    overlap = polygon_iou(mapped, target, grid=96)
    return (overlap, -_nn_rms(source, hydro_dense, affine))


def _model_shift(
    source: list[tuple[float, float]],
    affine: Affine,
    tps: ThinPlateSpline,
) -> float:
    """RMS distance between the affine image of the shore and the spline image."""
    error = 0.0
    for x, y in source:
        ax, ay = affine.apply(x, y)
        tx, ty = tps.apply(x, y)
        error += (ax - tx) ** 2 + (ay - ty) ** 2
    return math.sqrt(error / len(source))


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
    snap_limit: float | None = None,
    on_frame: list[bool] | None = None,
) -> list[GroundControlPoint]:
    shore = _shore_vertices(pixel_ring, on_frame)
    if on_frame is not None and len(shore) < len(pixel_ring) and len(shore) >= 2:
        samples = resample_open(shore, gcp_count)
    else:
        samples = resample_closed(shore if len(shore) >= 3 else pixel_ring, gcp_count)
    gcps: list[GroundControlPoint] = []
    for col, row in samples:
        easting, northing = affine.apply(col, row)
        nearest = _nearest((easting, northing), hydro_dense)
        if snap_limit is None or math.hypot(nearest[0] - easting, nearest[1] - northing) <= snap_limit:
            easting, northing = nearest
        gcps.append(GroundControlPoint(pixel=col, line=row, easting=easting, northing=northing))
    return gcps


def _shore_vertices(
    ring: list[tuple[float, float]],
    on_frame: list[bool] | None,
) -> list[tuple[float, float]]:
    if on_frame is None or len(on_frame) != len(ring):
        return list(ring)
    shore = [point for point, flag in zip(ring, on_frame, strict=True) if not flag]
    return shore if len(shore) >= 3 else list(ring)


def fit_similarity(
    source: list[tuple[float, float]],
    target: list[tuple[float, float]],
) -> Affine:
    """Least-squares similarity, trying a reflection so a y-down scan can map north-up."""
    if len(source) != len(target) or len(source) < 3:
        raise ValueError("similarity fit needs at least 3 paired points")
    direct = _kabsch(source, target, reflect=False)
    flipped = _kabsch(source, target, reflect=True)
    if _pair_rms(source, target, flipped) + 1e-9 < _pair_rms(source, target, direct):
        return flipped
    return direct


def _kabsch(
    source: list[tuple[float, float]],
    target: list[tuple[float, float]],
    *,
    reflect: bool,
) -> Affine:
    cx, cy = _centroid(source)
    src = [(2.0 * cx - x, y) if reflect else (x, y) for x, y in source]
    sx, sy = _centroid(src)
    tx, ty = _centroid(target)
    align = 0.0
    cross = 0.0
    var = 0.0
    for (x, y), (u, v) in zip(src, target, strict=True):
        dx, dy = x - sx, y - sy
        du, dv = u - tx, v - ty
        align += dx * du + dy * dv
        cross += dx * dv - dy * du
        var += dx * dx + dy * dy
    hyp = math.hypot(align, cross)
    if var < 1e-12 or hyp < 1e-12:
        raise ValueError("singular similarity")
    cos_t = align / hyp
    sin_t = cross / hyp
    scale = hyp / var
    affine = Affine(
        a=scale * cos_t,
        b=-scale * sin_t,
        tx=tx - (scale * cos_t * sx + (-scale * sin_t) * sy),
        c=scale * sin_t,
        d=scale * cos_t,
        ty=ty - (scale * sin_t * sx + scale * cos_t * sy),
    )
    if reflect:
        return Affine(
            a=-affine.a,
            b=affine.b,
            tx=affine.tx + 2.0 * affine.a * cx,
            c=-affine.c,
            d=affine.d,
            ty=affine.ty + 2.0 * affine.c * cx,
        )
    return affine


def _register_partial(
    pixel_ring: list[tuple[float, float]],
    hydro_ring: list[tuple[float, float]],
    on_frame: list[bool],
    *,
    gcp_count: int,
) -> Registration | None:
    """Match the real shore (not the page-edge cut) onto part of the hydro ring."""
    chain = _longest_shore_chain(pixel_ring, on_frame)
    if len(chain) < 3:
        return None
    hydro = resample_closed(ensure_orientation(list(hydro_ring)), _DENSE_COUNT)
    chain_s = resample_open(chain, 24)
    candidates = _arc_slide_affines(chain_s, list(hydro_ring))
    if not candidates:
        return None
    best_affine: Affine | None = None
    best_iou = -1.0
    for affine in candidates:
        try:
            refined = _icp_similarity(chain_s, hydro, affine)
        except ValueError:
            refined = affine
        for posed in (affine, refined):
            mapped = [posed.apply(x, y) for x, y in pixel_ring]
            score = _partial_iou(mapped, on_frame, list(hydro_ring))
            if score > best_iou:
                best_iou = score
                best_affine = posed
    if best_affine is None:
        return None
    return _registration_from_affine(
        pixel_ring,
        hydro_ring,
        hydro,
        best_affine,
        gcp_count=gcp_count,
        on_frame=on_frame,
    )


def _icp_similarity(
    source: list[tuple[float, float]],
    target: list[tuple[float, float]],
    affine: Affine,
    iterations: int = 6,
) -> Affine:
    current = affine
    for _ in range(iterations):
        paired = [_nearest(current.apply(x, y), target) for x, y in source]
        current = fit_similarity(source, paired)
    return current


def _longest_shore_chain(
    ring: list[tuple[float, float]],
    on_frame: list[bool],
) -> list[tuple[float, float]]:
    """Longest run of non-frame vertices. The run may wrap around the ring."""
    count = len(ring)
    if len(on_frame) != count:
        return list(ring)
    if not any(on_frame):
        return list(ring)
    best: list[tuple[float, float]] = []
    current: list[tuple[float, float]] = []
    for index in range(count * 2):
        point = ring[index % count]
        if on_frame[index % count]:
            if len(current) > len(best):
                best = current
            current = []
            if index >= count:
                break
            continue
        current.append(point)
        if index >= count and len(current) >= count:
            break
    if len(current) > len(best):
        best = current
    # Drop the wrapped duplicate of the starting vertex.
    trimmed: list[tuple[float, float]] = []
    for point in best:
        if trimmed and point == trimmed[0] and len(trimmed) > 1:
            break
        trimmed.append(point)
    return trimmed


def resample_open(points: list[tuple[float, float]], count: int) -> list[tuple[float, float]]:
    """Evenly sample an open polyline by arc length. ``count`` >= 2."""
    if count < 2:
        raise ValueError("resample count must be at least 2")
    if len(points) < 2:
        raise ValueError("polyline must have at least 2 points")
    lengths: list[float] = []
    total = 0.0
    for index in range(len(points) - 1):
        x1, y1 = points[index]
        x2, y2 = points[index + 1]
        length = math.hypot(x2 - x1, y2 - y1)
        lengths.append(length)
        total += length
    if total <= 0:
        raise ValueError("polyline has zero length")
    step = total / (count - 1)
    samples: list[tuple[float, float]] = []
    segment = 0
    walked = 0.0
    x1, y1 = points[0]
    x2, y2 = points[1]
    seg_len = lengths[0]
    for sample_index in range(count):
        target = min(total, sample_index * step)
        while walked + seg_len < target and segment < len(lengths) - 1:
            walked += seg_len
            segment += 1
            x1, y1 = points[segment]
            x2, y2 = points[segment + 1]
            seg_len = lengths[segment]
        if seg_len <= 1e-12:
            samples.append((x1, y1))
            continue
        fraction = (target - walked) / seg_len
        samples.append((x1 + fraction * (x2 - x1), y1 + fraction * (y2 - y1)))
    return samples


def _arc_slide_affines(
    chain: list[tuple[float, float]],
    hydro: list[tuple[float, float]],
) -> list[Affine]:
    """Similarities that slide the open shore along the hydro ring.

    Scale is the fraction of the hydro perimeter the visible shore covers.
    Translation follows that arc, so a fragment is not pinned to the lake centroid.
    """
    hydro_s = resample_closed(ensure_orientation(list(hydro)), 120)
    chain_len = _polyline_length(chain)
    hydro_len = _closed_length(hydro_s)
    if chain_len <= 1e-9 or hydro_len <= 1e-9:
        return []
    count_h = len(hydro_s)
    count_c = len(chain)
    ranked: list[tuple[float, Affine]] = []
    fractions = (0.30, 0.40, 0.50, 0.62, 0.75, 0.90, 1.05)
    for mirrored in (False, True):
        source = _mirror_chain_x(chain) if mirrored else chain
        flip_cx = _centroid(chain)[0]
        sx, sy = _centroid(source)
        for step in range(0, 360, 6):
            theta = math.radians(step)
            cos_t = math.cos(theta)
            sin_t = math.sin(theta)
            for fraction in fractions:
                scale = (fraction * hydro_len) / chain_len
                span = fraction * count_h
                if span < count_c - 1:
                    continue
                stride = span / (count_c - 1)
                a = scale * cos_t
                b = -scale * sin_t
                c = -scale * sin_t
                d = -scale * cos_t
                best_err = math.inf
                best_affine: Affine | None = None
                for start in range(0, count_h, 3):
                    window = [_sample_ring(hydro_s, start + index * stride) for index in range(count_c)]
                    wcx = sum(point[0] for point in window) / count_c
                    wcy = sum(point[1] for point in window) / count_c
                    tx = wcx - (a * sx + b * sy)
                    ty = wcy - (c * sx + d * sy)
                    err = 0.0
                    for (x, y), (u, v) in zip(source, window, strict=True):
                        mx = a * x + b * y + tx
                        my = c * x + d * y + ty
                        err += (mx - u) ** 2 + (my - v) ** 2
                    err = math.sqrt(err / count_c) / max(scale, 1e-6)
                    if err < best_err:
                        best_err = err
                        affine = Affine(a=a, b=b, tx=tx, c=c, d=d, ty=ty)
                        if mirrored:
                            affine = _compose_flip_x(flip_cx, affine)
                        best_affine = affine
                if best_affine is not None:
                    ranked.append((best_err, best_affine))
    ranked.sort(key=lambda item: item[0])
    return [affine for _err, affine in ranked[:8]]


def _polyline_length(points: list[tuple[float, float]]) -> float:
    total = 0.0
    for index in range(len(points) - 1):
        x1, y1 = points[index]
        x2, y2 = points[index + 1]
        total += math.hypot(x2 - x1, y2 - y1)
    return total


def _closed_length(points: list[tuple[float, float]]) -> float:
    if len(points) < 2:
        return 0.0
    return _polyline_length(points) + math.hypot(points[0][0] - points[-1][0], points[0][1] - points[-1][1])


def _sample_ring(samples: list[tuple[float, float]], index: float) -> tuple[float, float]:
    count = len(samples)
    index = index % count
    i0 = int(index) % count
    i1 = (i0 + 1) % count
    fraction = index - int(index)
    x0, y0 = samples[i0]
    x1, y1 = samples[i1]
    return (x0 + fraction * (x1 - x0), y0 + fraction * (y1 - y0))


def _mirror_chain_x(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    cx, _cy = _centroid(points)
    return [(2.0 * cx - x, y) for x, y in points]


def _compose_flip_x(cx: float, affine: Affine) -> Affine:
    return Affine(
        a=-affine.a,
        b=affine.b,
        tx=affine.tx + 2.0 * affine.a * cx,
        c=-affine.c,
        d=affine.d,
        ty=affine.ty + 2.0 * affine.c * cx,
    )


def _partial_iou(
    mapped: list[tuple[float, float]],
    on_frame: list[bool],
    hydro: list[tuple[float, float]],
) -> float:
    """Overlap of the visible sheet polygon with hydro inside the sheet footprint.

    The footprint is the half-plane of a straight page-edge cut when the frame
    vertices are colinear, otherwise the expanded box of the visible polygon.
    """
    plane = _frame_halfplane(mapped, on_frame)
    if plane is not None:
        clipped = _clip_ring_halfplane(list(hydro), plane)
        if len(clipped) >= 3:
            return polygon_iou(mapped, clipped)
    return _bbox_partial_iou(mapped, hydro)


def _bbox_partial_iou(
    mapped: list[tuple[float, float]],
    hydro: list[tuple[float, float]],
) -> float:
    xs = [point[0] for point in mapped]
    ys = [point[1] for point in mapped]
    pad_x = max(max(xs) - min(xs), 1.0) * 0.04
    pad_y = max(max(ys) - min(ys), 1.0) * 0.04
    box = [
        (min(xs) - pad_x, min(ys) - pad_y),
        (max(xs) + pad_x, min(ys) - pad_y),
        (max(xs) + pad_x, max(ys) + pad_y),
        (min(xs) - pad_x, max(ys) + pad_y),
    ]
    clipped = _clip_polygon(list(hydro), box)
    if len(clipped) < 3:
        return 0.0
    return polygon_iou(mapped, clipped)


def _frame_halfplane(
    mapped: list[tuple[float, float]],
    on_frame: list[bool],
) -> tuple[float, float, float, float] | None:
    """Return ``(px, py, nx, ny)`` keeping the water side, or None."""
    frame = [point for point, flag in zip(mapped, on_frame, strict=True) if flag]
    shore = [point for point, flag in zip(mapped, on_frame, strict=True) if not flag]
    if len(frame) < 2 or len(shore) < 3:
        return None
    cx, cy = _centroid(frame)
    sxx = syy = sxy = 0.0
    for x, y in frame:
        dx, dy = x - cx, y - cy
        sxx += dx * dx
        syy += dy * dy
        sxy += dx * dy
    angle = 0.5 * math.atan2(2.0 * sxy, sxx - syy)
    direction = (math.cos(angle), math.sin(angle))
    normal = (-direction[1], direction[0])
    residual = 0.0
    for x, y in frame:
        residual += ((x - cx) * normal[0] + (y - cy) * normal[1]) ** 2
    residual = math.sqrt(residual / len(frame))
    span = _bbox_diagonal(frame)
    if span <= 0 or residual > 0.04 * span:
        return None
    water_x, water_y = _centroid(shore)
    if (water_x - cx) * normal[0] + (water_y - cy) * normal[1] < 0:
        normal = (-normal[0], -normal[1])
    return (cx, cy, normal[0], normal[1])


def _clip_ring_halfplane(
    ring: list[tuple[float, float]],
    plane: tuple[float, float, float, float],
) -> list[tuple[float, float]]:
    px, py, nx, ny = plane
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    box = [
        (min(xs) - span, min(ys) - span),
        (max(xs) + span, min(ys) - span),
        (max(xs) + span, max(ys) + span),
        (min(xs) - span, max(ys) + span),
    ]
    subject = _clip_polygon(ring, box)
    return _clip_polygon_edge(subject, (px, py), (px + ny, py - nx))


def _clip_polygon(
    subject: list[tuple[float, float]],
    clipper: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Sutherland–Hodgman. ``clipper`` is convex and counter-clockwise."""
    output = list(subject)
    for index in range(len(clipper)):
        if not output:
            return []
        edge_a = clipper[index]
        edge_b = clipper[(index + 1) % len(clipper)]
        output = _clip_polygon_edge(output, edge_a, edge_b)
    return output


def _clip_polygon_edge(
    subject: list[tuple[float, float]],
    edge_a: tuple[float, float],
    edge_b: tuple[float, float],
) -> list[tuple[float, float]]:
    if not subject:
        return []

    def inside(point: tuple[float, float]) -> bool:
        return (edge_b[0] - edge_a[0]) * (point[1] - edge_a[1]) - (edge_b[1] - edge_a[1]) * (
            point[0] - edge_a[0]
        ) >= -1e-9

    def crossing(start: tuple[float, float], end: tuple[float, float]) -> tuple[float, float]:
        dx, dy = end[0] - start[0], end[1] - start[1]
        ex, ey = edge_b[0] - edge_a[0], edge_b[1] - edge_a[1]
        denom = dx * ey - dy * ex
        if abs(denom) < 1e-12:
            return end
        t = ((edge_a[0] - start[0]) * ey - (edge_a[1] - start[1]) * ex) / denom
        return (start[0] + t * dx, start[1] + t * dy)

    output: list[tuple[float, float]] = []
    previous = subject[-1]
    prev_in = inside(previous)
    for point in subject:
        curr_in = inside(point)
        if curr_in:
            if not prev_in:
                output.append(crossing(previous, point))
            output.append(point)
        elif prev_in:
            output.append(crossing(previous, point))
        previous = point
        prev_in = curr_in
    return output


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
