# SPDX-License-Identifier: GPL-3.0-only
"""Assign positive depth_ft to traced Wisconsin contour lines.

Labels snap to the nearest line. Closed basin rings that have no label take
their depth from the parent ring, starting at the 0 shoreline and stepping by
``interval_ft``, and only while the result stays within ``max_depth_ft``.
A ring that disagrees with itself, breaks that step, wraps an island with no
label, or would pass the max depth is ``needs_review``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from fishy_pipeline.common.depth import round_half_away_from_zero

# Larger intervals win ties, so a sheet of 10, 20, 30 stays 10-foot.
INTERVAL_CANDIDATES = (10, 5, 4, 2)
INTERVAL_SUPPORT = 0.6
SHORT_OPEN_M = 40.0
SECTION_LENGTH_FRACTION = 0.45
SECTION_STRAIGHT_FRACTION = 0.03
SECTION_SHORE_FRACTION = 0.04
# A collar around an island is mostly land. A basin ring that happens to
# contain a small island is mostly water, so it stays on the basin tree.
ISLAND_LAND_FRACTION = 0.35
# A second depth is a conflict when it is at least half the winning count and not a single stray.
LABEL_CONFLICT_RATIO = 0.5
INSIDE_FRACTION = 0.5


@dataclass(frozen=True)
class Line:
    coords: tuple[tuple[float, float], ...]
    closed: bool


@dataclass(frozen=True)
class Label:
    x: float
    y: float
    depth_ft: float
    confidence: float


@dataclass(frozen=True)
class WaterPart:
    exterior: tuple[tuple[float, float], ...]
    holes: tuple[tuple[tuple[float, float], ...], ...] = ()


@dataclass
class TaggedLine:
    coords: tuple[tuple[float, float], ...]
    closed: bool
    depth_ft: float | None
    interval_ft: float | None
    confidence: float
    needs_review: bool
    reason: str | None
    source: str


@dataclass
class Assignment:
    features: list[TaggedLine]
    interval_ft: float | None
    reasons: list[str] = field(default_factory=list)
    unassigned_count: int = 0


def signed_area(ring: tuple[tuple[float, float], ...] | list[tuple[float, float]]) -> float:
    area = 0.0
    count = len(ring)
    for index, (x1, y1) in enumerate(ring):
        x2, y2 = ring[(index + 1) % count]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def point_in_ring(x: float, y: float, ring: tuple[tuple[float, float], ...] | list[tuple[float, float]]) -> bool:
    """Even-odd test. The ring does not need to repeat its first point."""
    inside = False
    count = len(ring)
    if count < 3:
        return False
    for index in range(count):
        x1, y1 = ring[index]
        x2, y2 = ring[(index + 1) % count]
        if (y1 > y) != (y2 > y):
            denom = y2 - y1
            if denom == 0:
                continue
            cross = (x2 - x1) * (y - y1) / denom + x1
            if x < cross:
                inside = not inside
    return inside


def point_in_water(x: float, y: float, parts: list[WaterPart]) -> bool:
    for part in parts:
        if not point_in_ring(x, y, part.exterior):
            continue
        if any(point_in_ring(x, y, hole) for hole in part.holes):
            continue
        return True
    return False


def interior_point(ring: tuple[tuple[float, float], ...]) -> tuple[float, float]:
    """A point that should lie in the ring interior."""
    count = len(ring)
    cx = sum(point[0] for point in ring) / count
    cy = sum(point[1] for point in ring) / count
    if point_in_ring(cx, cy, ring):
        return cx, cy
    direction = 1.0 if signed_area(ring) >= 0 else -1.0
    best: tuple[float, float] | None = None
    best_len = -1.0
    for index in range(count):
        x1, y1 = ring[index]
        x2, y2 = ring[(index + 1) % count]
        dx = x2 - x1
        dy = y2 - y1
        length = math.hypot(dx, dy)
        if length <= best_len or length == 0:
            continue
        # CCW interior is to the left of the edge. Flip when the ring is clockwise.
        nx = -dy * direction
        ny = dx * direction
        norm = math.hypot(nx, ny) or 1.0
        step = min(length * 0.05, max(length * 0.01, 1.0))
        candidate = ((x1 + x2) / 2.0 + nx / norm * step, (y1 + y2) / 2.0 + ny / norm * step)
        best = candidate
        best_len = length
        if point_in_ring(candidate[0], candidate[1], ring):
            return candidate
    if best is not None and point_in_ring(best[0], best[1], ring):
        return best
    return cx, cy


def infer_interval(depths: list[int]) -> int | None:
    """Pick 10, 5, 4, or 2 feet from snapped label depths.

    The larger candidate wins a tie, so a sheet labeled only in tens stays
    a 10-foot interval. None when no candidate explains most of the labels.
    """
    if not depths:
        return None
    best: int | None = None
    best_score = 0
    for interval in INTERVAL_CANDIDATES:
        score = sum(1 for depth in depths if depth > 0 and depth % interval == 0)
        if score > best_score:
            best = interval
            best_score = score
    if best is None or best_score < max(1, math.ceil(INTERVAL_SUPPORT * len(depths) - 1e-9)):
        return None
    return best


def parse_depth_text(text: str) -> int | None:
    """Read a depth token. Years and mixed junk are not depths."""
    parsed = parse_depth_token(text)
    if parsed is None or parsed[1] != "ft":
        return None
    if parsed[0] != int(parsed[0]):
        return None
    return int(parsed[0])


def parse_depth_token(text: str) -> tuple[float, str] | None:
    """Read a depth and its unit. ``10m`` is meters. A bare number is feet.

    A trailing ``m`` is the only letter a meter label may carry. ``10FT`` and
    a year are still rejected.
    """
    stripped = text.strip().rstrip(".")
    if not stripped:
        return None
    unit = "ft"
    body = stripped
    if len(stripped) >= 2 and stripped[-1] in {"m", "M"} and stripped[-2].isdigit():
        unit = "m"
        body = stripped[:-1].strip()
    if any(char.isalpha() for char in body):
        return None
    number = body.replace(".", "", 1)
    if not number.isdigit() or body.count(".") > 1:
        return None
    if len(number) > 3 and unit == "ft":
        return None
    if len(number) > 4:
        return None
    value = float(body)
    if value <= 0 or value > 400:
        return None
    return value, unit


def sheet_depth_unit(texts: list[str]) -> str:
    """``m`` when the sheet says meters, otherwise feet."""
    import re

    meter_word = re.compile(r"\bmeters?\b|\bmetres?\b", re.IGNORECASE)
    if any(meter_word.search(text) for text in texts):
        return "m"
    meter_labels = 0
    for text in texts:
        parsed = parse_depth_token(text)
        if parsed is not None and parsed[1] == "m":
            meter_labels += 1
    if meter_labels >= 2:
        return "m"
    return "ft"


def meters_to_feet(meters: float, digits: int = 0) -> float:
    """International foot. Rounded half away from zero."""
    return round_half_away_from_zero(meters / 0.3048, digits)


def assignment_to_feet(assignment: Assignment) -> Assignment:
    """Convert an assignment that was tagged in meters into integer feet."""
    interval = None if assignment.interval_ft is None else meters_to_feet(assignment.interval_ft, 2)
    features: list[TaggedLine] = []
    for line in assignment.features:
        depth = line.depth_ft
        line_interval = line.interval_ft
        if depth is not None and line.source != "hydro":
            depth = int(meters_to_feet(depth, 0))
        if line_interval is not None:
            line_interval = interval
        features.append(
            TaggedLine(
                coords=line.coords,
                closed=line.closed,
                depth_ft=depth,
                interval_ft=line_interval,
                confidence=line.confidence,
                needs_review=line.needs_review,
                reason=line.reason,
                source=line.source,
            )
        )
    return Assignment(
        features=features,
        interval_ft=interval,
        reasons=list(assignment.reasons),
        unassigned_count=assignment.unassigned_count,
    )


def assign_depths(
    lines: list[Line],
    labels: list[Label],
    parts: list[WaterPart],
    *,
    max_depth_ft: float | None,
    snap_m: float,
) -> Assignment:
    """Tag lines and add the hydro shoreline as depth 0."""
    if snap_m <= 0:
        raise ValueError("snap distance must be positive")
    kept = [line for line in lines if _mostly_inside(line, parts)]
    kept = [line for line in kept if not _is_section_line(line, parts)]
    snapped = _snap_labels(kept, labels, snap_m)
    interval = infer_interval([depth for depths in snapped.values() for depth, _conf in depths])
    if interval is not None:
        snapped = {
            index: [(depth, conf) for depth, conf in depths if depth % interval == 0]
            for index, depths in snapped.items()
        }
        interval = infer_interval([depth for depths in snapped.values() for depth, _conf in depths]) or interval

    indexed: list[tuple[int, Line]] = []
    unassigned = 0
    for index, line in enumerate(kept):
        summary = _label_summary(snapped.get(index, []))
        length = _length(line.coords)
        if not line.closed and summary is None and length < SHORT_OPEN_M:
            continue
        if not line.closed and summary is None:
            unassigned += 1
            continue
        indexed.append((index, line))

    reasons: list[str] = []
    if unassigned:
        reasons.append("unassigned_fragment")

    closed = [(index, line) for index, line in indexed if line.closed]
    basin: list[tuple[int, Line]] = []
    island: list[tuple[int, Line]] = []
    for index, line in closed:
        if _wraps_island(line, parts):
            island.append((index, line))
        else:
            basin.append((index, line))

    areas = {index: abs(signed_area(line.coords)) for index, line in basin}
    parents = _parents(basin, areas)
    features: list[TaggedLine] = []
    tagged: dict[int, TaggedLine] = {}

    for index, line in indexed:
        if line.closed:
            continue
        features.append(
            _tag_open(
                line,
                snapped.get(index, []),
                interval=interval,
                max_depth_ft=max_depth_ft,
            )
        )

    order = sorted(basin, key=lambda item: areas[item[0]], reverse=True)
    for index, line in order:
        parent = parents.get(index)
        parent_tag = None if parent is None else tagged.get(parent)
        parent_depth = 0 if parent is None else parent_tag.depth_ft if parent_tag else None
        parent_review = False if parent is None else bool(parent_tag and parent_tag.needs_review)
        tag = _tag_basin(
            line,
            snapped.get(index, []),
            parent_depth=parent_depth,
            parent_review=parent_review,
            interval=interval,
            max_depth_ft=max_depth_ft,
        )
        tagged[index] = tag
        features.append(tag)

    for index, line in island:
        tag = _tag_island(line, snapped.get(index, []), interval=interval, max_depth_ft=max_depth_ft)
        features.append(tag)
        if tag.reason == "island_unlabeled":
            reasons.append("island_unlabeled")

    if interval is None and any(tag.source != "hydro" for tag in features):
        reasons.append("interval_unknown")
        for tag in features:
            if tag.source == "propagated":
                tag.needs_review = True
                tag.reason = tag.reason or "interval_unknown"
                tag.confidence = min(tag.confidence, 0.4)

    for part in parts:
        features.append(_shore(part.exterior, interval))
        for hole in part.holes:
            features.append(_shore(hole, interval))

    unique_reasons = list(dict.fromkeys(reasons))
    return Assignment(
        features=features,
        interval_ft=interval,
        reasons=unique_reasons,
        unassigned_count=unassigned,
    )


def features_to_geojson(
    tagged: list[TaggedLine],
    *,
    wbic: str,
    interval_ft: int | None,
    max_depth_ft: float | None,
    qa_status: str,
) -> dict:
    """Per-lake contour collection. Depths are non-negative feet."""
    features = []
    for line in tagged:
        if line.depth_ft is None:
            continue
        if line.depth_ft < 0:
            continue
        coords = [[_coord(x), _coord(y)] for x, y in line.coords]
        if line.closed and coords and coords[0] != coords[-1]:
            coords.append(coords[0])
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "wbic": wbic,
                    "depth_ft": line.depth_ft,
                    "interval_ft": line.interval_ft if line.interval_ft is not None else interval_ft,
                    "confidence": line.confidence,
                    "needs_review": line.needs_review,
                    "source": line.source,
                    **({"review_reason": line.reason} if line.reason else {}),
                },
                "geometry": {"type": "LineString", "coordinates": coords},
            }
        )
    return {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3071"}},
        "wbic": wbic,
        "interval_ft": interval_ft,
        "max_depth_ft": max_depth_ft,
        "qa_status": qa_status,
        "features": features,
    }


def cutline_collection(parts: list[WaterPart]) -> dict:
    """Hydro polygon for gdalwarp. Exteriors are counter-clockwise, holes clockwise."""
    features = []
    for part in parts:
        rings = [_closed(_orient(part.exterior, ccw=True))]
        for hole in part.holes:
            rings.append(_closed(_orient(hole, ccw=False)))
        features.append(
            {
                "type": "Feature",
                "properties": {},
                "geometry": {"type": "Polygon", "coordinates": rings},
            }
        )
    return {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3071"}},
        "features": features,
    }


def _orient(ring: tuple[tuple[float, float], ...], *, ccw: bool) -> list[tuple[float, float]]:
    area = signed_area(ring)
    points = list(ring)
    if ccw and area < 0:
        points.reverse()
    elif not ccw and area > 0:
        points.reverse()
    return points


def _closed(ring: list[tuple[float, float]]) -> list[list[float]]:
    coords = [[_coord(x), _coord(y)] for x, y in ring]
    if coords and coords[0] != coords[-1]:
        coords.append(coords[0])
    return coords


def _coord(value: float) -> float:
    return float(round_half_away_from_zero(value, 2))


def _shore(ring: tuple[tuple[float, float], ...], interval: int | None) -> TaggedLine:
    return TaggedLine(
        coords=ring,
        closed=True,
        depth_ft=0,
        interval_ft=interval,
        confidence=1.0,
        needs_review=False,
        reason=None,
        source="hydro",
    )


def _mostly_inside(line: Line, parts: list[WaterPart]) -> bool:
    if not parts or len(line.coords) < 2:
        return False
    hits = sum(1 for x, y in line.coords if point_in_water(x, y, parts))
    return hits / len(line.coords) >= INSIDE_FRACTION


def _is_section_line(line: Line, parts: list[WaterPart]) -> bool:
    if line.closed or len(line.coords) < 2 or not parts:
        return False
    min_x, min_y, max_x, max_y = _bounds(parts)
    diagonal = math.hypot(max_x - min_x, max_y - min_y)
    if diagonal <= 0:
        return False
    length = _length(line.coords)
    if length < SECTION_LENGTH_FRACTION * diagonal:
        return False
    if _max_deviation(line.coords) > SECTION_STRAIGHT_FRACTION * length:
        return False
    shore = SECTION_SHORE_FRACTION * diagonal
    return _shore_distance(line.coords[0], parts) <= shore and _shore_distance(line.coords[-1], parts) <= shore


def _bounds(parts: list[WaterPart]) -> tuple[float, float, float, float]:
    xs = [point[0] for part in parts for point in part.exterior]
    ys = [point[1] for part in parts for point in part.exterior]
    return min(xs), min(ys), max(xs), max(ys)


def _shore_distance(point: tuple[float, float], parts: list[WaterPart]) -> float:
    best = math.inf
    for part in parts:
        best = min(best, _ring_distance(point, part.exterior))
        for hole in part.holes:
            best = min(best, _ring_distance(point, hole))
    return best


def _ring_distance(point: tuple[float, float], ring: tuple[tuple[float, float], ...]) -> float:
    best = math.inf
    count = len(ring)
    for index in range(count):
        best = min(best, _segment_distance(point, ring[index], ring[(index + 1) % count]))
    return best


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
    tee = ((px - x1) * dx + (py - y1) * dy) / length_sq
    tee = max(0.0, min(1.0, tee))
    return math.hypot(px - (x1 + tee * dx), py - (y1 + tee * dy))


def _max_deviation(coords: tuple[tuple[float, float], ...]) -> float:
    start = coords[0]
    end = coords[-1]
    return max(_segment_distance(point, start, end) for point in coords)


def _length(coords: tuple[tuple[float, float], ...]) -> float:
    total = 0.0
    for index in range(len(coords) - 1):
        x1, y1 = coords[index]
        x2, y2 = coords[index + 1]
        total += math.hypot(x2 - x1, y2 - y1)
    return total


def _snap_labels(
    lines: list[Line],
    labels: list[Label],
    snap_m: float,
) -> dict[int, list[tuple[int, float]]]:
    snapped: dict[int, list[tuple[int, float]]] = {}
    for label in labels:
        best_index = None
        best_dist = math.inf
        second = math.inf
        for index, line in enumerate(lines):
            dist = _line_distance((label.x, label.y), line.coords, closed=line.closed)
            if dist < best_dist:
                second = best_dist
                best_dist = dist
                best_index = index
            elif dist < second:
                second = dist
        if best_index is None or best_dist > snap_m:
            continue
        if second <= snap_m and second < best_dist * 1.35:
            continue
        snapped.setdefault(best_index, []).append((label.depth_ft, label.confidence))
    return snapped


def _line_distance(
    point: tuple[float, float],
    coords: tuple[tuple[float, float], ...],
    *,
    closed: bool,
) -> float:
    best = math.inf
    for index in range(len(coords) - 1):
        best = min(best, _segment_distance(point, coords[index], coords[index + 1]))
    if closed and len(coords) > 2:
        best = min(best, _segment_distance(point, coords[-1], coords[0]))
    return best


def _label_summary(pairs: list[tuple[int, float]]) -> tuple[int, float, bool] | None:
    """Return depth, mean confidence, and whether the line's labels conflict."""
    if not pairs:
        return None
    counts: dict[int, list[float]] = {}
    for depth, confidence in pairs:
        counts.setdefault(depth, []).append(confidence)
    ranked = sorted(counts.items(), key=lambda item: len(item[1]), reverse=True)
    depth, confidences = ranked[0]
    conflict = False
    if len(ranked) > 1:
        second_count = len(ranked[1][1])
        if second_count >= 2 and second_count >= LABEL_CONFLICT_RATIO * len(confidences):
            conflict = True
    mean_conf = sum(confidences) / len(confidences)
    return depth, mean_conf, conflict


def _wraps_island(line: Line, parts: list[WaterPart]) -> bool:
    holes = [hole for part in parts for hole in part.holes]
    if not holes:
        return False
    ring = line.coords
    area = abs(signed_area(ring))
    if area <= 0:
        return False
    land = 0.0
    for hole in holes:
        hole_area = abs(signed_area(hole))
        if hole_area <= 0 or hole_area >= area:
            continue
        hx, hy = interior_point(hole)
        if point_in_ring(hx, hy, ring):
            land += hole_area
    return land / area >= ISLAND_LAND_FRACTION


def _parents(basin: list[tuple[int, Line]], areas: dict[int, float]) -> dict[int, int]:
    parents: dict[int, int] = {}
    for index, line in basin:
        px, py = interior_point(line.coords)
        best = None
        best_area = math.inf
        for other, other_line in basin:
            if other == index or areas[other] <= areas[index] * 1.01:
                continue
            if point_in_ring(px, py, other_line.coords) and areas[other] < best_area:
                best = other
                best_area = areas[other]
        if best is not None:
            parents[index] = best
    return parents


def _tag_open(
    line: Line,
    pairs: list[tuple[int, float]],
    *,
    interval: int | None,
    max_depth_ft: float | None,
) -> TaggedLine:
    summary = _label_summary(pairs)
    if summary is None:
        return TaggedLine(
            coords=line.coords,
            closed=False,
            depth_ft=None,
            interval_ft=interval,
            confidence=0.3,
            needs_review=True,
            reason="unassigned_fragment",
            source="unassigned",
        )
    depth, confidence, conflict = summary
    reason = None
    needs = False
    if conflict:
        needs = True
        reason = "label_conflict"
    elif _above_max(depth, max_depth_ft):
        needs = True
        reason = "above_max_depth"
    elif interval is not None and depth % interval != 0:
        needs = True
        reason = "label_breaks_propagation"
    return TaggedLine(
        coords=line.coords,
        closed=False,
        depth_ft=depth,
        interval_ft=interval,
        confidence=_confidence("label", confidence, needs_review=needs, steps=1),
        needs_review=needs,
        reason=reason,
        source="label",
    )


def _tag_basin(
    line: Line,
    pairs: list[tuple[int, float]],
    *,
    parent_depth: int | None,
    parent_review: bool,
    interval: int | None,
    max_depth_ft: float | None,
) -> TaggedLine:
    summary = _label_summary(pairs)
    if summary is None:
        return _propagated(line, parent_depth, parent_review, interval, max_depth_ft)
    depth, confidence, conflict = summary
    reason = None
    needs = False
    agrees = _agrees(depth, parent_depth, interval)
    if conflict:
        needs = True
        reason = "label_conflict"
    elif _above_max(depth, max_depth_ft):
        needs = True
        reason = "above_max_depth"
    elif parent_depth is not None and interval is not None and not agrees:
        needs = True
        reason = "label_breaks_propagation"
    elif parent_review and not agrees:
        needs = True
        reason = "propagated_from_ambiguous"
    source_conf = _confidence("label", confidence, needs_review=needs, steps=_steps(depth, parent_depth, interval))
    return TaggedLine(
        coords=line.coords,
        closed=True,
        depth_ft=depth,
        interval_ft=interval,
        confidence=source_conf,
        needs_review=needs,
        reason=reason,
        source="label",
    )


def _tag_island(
    line: Line,
    pairs: list[tuple[int, float]],
    *,
    interval: int | None,
    max_depth_ft: float | None,
) -> TaggedLine:
    summary = _label_summary(pairs)
    if summary is None:
        return TaggedLine(
            coords=line.coords,
            closed=True,
            depth_ft=None,
            interval_ft=interval,
            confidence=0.3,
            needs_review=True,
            reason="island_unlabeled",
            source="unassigned",
        )
    depth, confidence, conflict = summary
    reason = None
    needs = False
    if conflict:
        needs = True
        reason = "label_conflict"
    elif _above_max(depth, max_depth_ft):
        needs = True
        reason = "above_max_depth"
    elif interval is not None and depth % interval != 0:
        needs = True
        reason = "label_breaks_propagation"
    return TaggedLine(
        coords=line.coords,
        closed=True,
        depth_ft=depth,
        interval_ft=interval,
        confidence=_confidence("label", confidence, needs_review=needs, steps=1),
        needs_review=needs,
        reason=reason,
        source="label",
    )


def _propagated(
    line: Line,
    parent_depth: int | None,
    parent_review: bool,
    interval: int | None,
    max_depth_ft: float | None,
) -> TaggedLine:
    if parent_depth is None or interval is None:
        return TaggedLine(
            coords=line.coords,
            closed=True,
            depth_ft=None,
            interval_ft=interval,
            confidence=0.3,
            needs_review=True,
            reason="interval_unknown",
            source="unassigned",
        )
    depth = parent_depth + interval
    reason = None
    needs = False
    if parent_review:
        needs = True
        reason = "propagated_from_ambiguous"
    if _above_max(depth, max_depth_ft):
        needs = True
        reason = "above_max_depth"
    return TaggedLine(
        coords=line.coords,
        closed=True,
        depth_ft=depth,
        interval_ft=interval,
        confidence=_confidence("propagated", 0.0, needs_review=needs, steps=1),
        needs_review=needs,
        reason=reason,
        source="propagated",
    )


def _agrees(depth: int, parent_depth: int | None, interval: int | None) -> bool:
    if parent_depth is None or interval is None or interval <= 0:
        return True
    delta = depth - parent_depth
    return delta > 0 and delta % interval == 0


def _steps(depth: int, parent_depth: int | None, interval: int | None) -> int:
    if parent_depth is None or interval is None or interval <= 0:
        return 1
    delta = depth - parent_depth
    if delta <= 0 or delta % interval != 0:
        return 0
    return delta // interval


def _above_max(depth: int, max_depth_ft: float | None) -> bool:
    if max_depth_ft is None:
        return False
    return depth > max_depth_ft + 0.5


def _confidence(source: str, ocr_confidence: float, *, needs_review: bool, steps: int) -> float:
    ocr = min(1.0, max(0.0, ocr_confidence))
    if source == "label" and not needs_review and steps == 1:
        value = 0.7 + 0.25 * ocr
    elif source == "label" and not needs_review and steps > 1:
        value = 0.55 + 0.15 * ocr
    elif source == "label":
        value = 0.4 + 0.2 * ocr
    elif source == "propagated" and not needs_review:
        value = 0.55
    else:
        value = 0.35
    if needs_review:
        value = min(value, 0.4)
    return float(round_half_away_from_zero(value, 2))
