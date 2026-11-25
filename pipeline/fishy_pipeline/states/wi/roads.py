# SPDX-License-Identifier: GPL-3.0-only
"""Project Wisconsin road centerlines into a survey sheet after the affine.

The centerlines come from the Wisconsin DNR DRG basemap Roads layer, which is
already in EPSG:3071. Georef inverse-projects those lines with the sheet affine
and clears cased ink beside them. The hydro polygon is not used to choose the
trace. A coordinate box outside Wisconsin skips the request, so synthetic
fixtures stay offline. A failed request clears nothing.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request

from fishy_pipeline.states.wi.register import Affine

# Queried 2026-11-25. Polyline layer, spatial reference 3071, max 2000 records.
ROADS_QUERY_URL = (
    "https://dnrmaps.wi.gov/ArcGIS/rest/services/"
    "DW_Map_Cached/EN_DRG_Basemap_WTM_Ext/MapServer/8/query"
)
_ROAD_PAD_M = 1200.0
_PAGE_SIZE = 2000
_MAX_PAGES = 4
_USER_AGENT = "fishy-pipeline/wi-georef"


def roads_in_wisconsin(ring: list[tuple[float, float]]) -> bool:
    """True when ``ring`` sits in the EPSG:3071 extent of Wisconsin."""
    if len(ring) < 3:
        return False
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    return (
        min(xs) > 200_000
        and max(xs) < 900_000
        and min(ys) > 100_000
        and max(ys) < 850_000
    )


def fetch_road_centerlines(
    ring: list[tuple[float, float]],
    *,
    timeout: float = 25.0,
) -> list[list[tuple[float, float]]]:
    """Road polylines in EPSG:3071 that meet the hydro bbox, or an empty list."""
    if not roads_in_wisconsin(ring):
        return []
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    envelope = (
        min(xs) - _ROAD_PAD_M,
        min(ys) - _ROAD_PAD_M,
        max(xs) + _ROAD_PAD_M,
        max(ys) + _ROAD_PAD_M,
    )
    lines: list[list[tuple[float, float]]] = []
    offset = 0
    for _page in range(_MAX_PAGES):
        try:
            payload = _query_roads(envelope, offset, timeout)
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
            return lines
        lines.extend(_polylines_from_esri(payload))
        if not payload.get("exceededTransferLimit"):
            break
        offset += _PAGE_SIZE
    return lines


def pixel_from_map(affine: Affine, easting: float, northing: float) -> tuple[float, float]:
    """Inverse of ``Affine.apply``. Raises ``ValueError`` when the affine is singular."""
    det = affine.a * affine.d - affine.b * affine.c
    if abs(det) < 1e-12:
        raise ValueError("singular affine")
    east = easting - affine.tx
    north = northing - affine.ty
    column = (affine.d * east - affine.b * north) / det
    row = (-affine.c * east + affine.a * north) / det
    return column, row


def project_roads_to_pixels(
    roads: list[list[tuple[float, float]]],
    affine: Affine,
    width: int,
    height: int,
    *,
    margin: float = 80.0,
) -> list[list[tuple[float, float]]]:
    """Inverse-project roads that pass near the sheet. A singular affine yields nothing."""
    projected: list[list[tuple[float, float]]] = []
    for road in roads:
        if len(road) < 2:
            continue
        try:
            pixels = [pixel_from_map(affine, east, north) for east, north in road]
        except ValueError:
            return []
        if any(-margin <= x <= width + margin and -margin <= y <= height + margin for x, y in pixels):
            projected.append(pixels)
    return projected


def search_pixels_for_affine(affine: Affine) -> float:
    """Corridor half-width, in source pixels, for about a 120 m road search."""
    meters = max(math.hypot(affine.a, affine.c), math.hypot(affine.b, affine.d))
    if meters < 0.05 or meters > 30:
        return 48.0
    # Half-width of the search. The projected line can sit off the ink when the
    # first affine still includes the road, so the corridor is about 120 m.
    return min(220.0, max(18.0, 120.0 / meters))


def _query_roads(
    envelope: tuple[float, float, float, float],
    offset: int,
    timeout: float,
) -> dict:
    xmin, ymin, xmax, ymax = envelope
    params = urllib.parse.urlencode(
        {
            "where": "1=1",
            "geometry": f"{xmin},{ymin},{xmax},{ymax}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "3071",
            "outSR": "3071",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "HWY_NUM",
            "returnGeometry": "true",
            "resultOffset": str(offset),
            "resultRecordCount": str(_PAGE_SIZE),
            "f": "json",
        }
    )
    request = urllib.request.Request(
        f"{ROADS_QUERY_URL}?{params}",
        headers={"User-Agent": _USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("road response was not an object")
    if payload.get("error"):
        raise ValueError("road query failed")
    return payload


def _polylines_from_esri(payload: dict) -> list[list[tuple[float, float]]]:
    lines: list[list[tuple[float, float]]] = []
    features = payload.get("features") or []
    if not isinstance(features, list):
        return lines
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry") or {}
        paths = geometry.get("paths") if isinstance(geometry, dict) else None
        if not isinstance(paths, list):
            continue
        for path in paths:
            if not isinstance(path, list) or len(path) < 2:
                continue
            line: list[tuple[float, float]] = []
            for point in path:
                if not isinstance(point, (list, tuple)) or len(point) < 2:
                    continue
                line.append((float(point[0]), float(point[1])))
            if len(line) >= 2:
                lines.append(line)
    return lines
