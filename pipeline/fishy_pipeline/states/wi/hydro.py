# SPDX-License-Identifier: GPL-3.0-only
"""Join catalog rows to WI DNR 24K Hydro waterbodies by WBIC."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from fishy_pipeline.states.wi.wbic import normalize_wbic

HYDRO_CRS_3071_RE = re.compile(r"EPSG::3071|\b3071\b")


@dataclass(frozen=True)
class HydroMatch:
    wbic: str
    waterbody_name: str | None
    hydroid: int | None
    hydrotype: int | None


def _assert_hydro_crs_3071(data: dict) -> None:
    crs = data.get("crs")
    if crs is None:
        raise ValueError("hydro GeoJSON must declare CRS EPSG:3071")
    blob = json.dumps(crs)
    if not HYDRO_CRS_3071_RE.search(blob):
        raise ValueError("hydro GeoJSON CRS must be EPSG:3071")


def load_hydro_index(path: Path) -> dict[str, HydroMatch]:
    """Load a GeoJSON FeatureCollection and index lake polygons by WBIC."""
    data = json.loads(path.read_text(encoding="utf-8"))
    _assert_hydro_crs_3071(data)
    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError("hydro GeoJSON must contain a features array")
    index: dict[str, HydroMatch] = {}
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        wbic = normalize_wbic(props.get("WATERBODY_WBIC") or props.get("waterbody_wbic"))
        if wbic is None:
            continue
        name = props.get("WATERBODY_NAME") or props.get("WATERBODY_ROW_NAME") or props.get("waterbody_name")
        hydroid = _int_or_none(props.get("HYDROID") or props.get("hydroid"))
        hydrotype = _int_or_none(props.get("HYDROTYPE") or props.get("hydrotype"))
        index[wbic] = HydroMatch(
            wbic=wbic,
            waterbody_name=str(name) if name else None,
            hydroid=hydroid,
            hydrotype=hydrotype,
        )
    return index


def _int_or_none(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def join_catalog_to_hydro(
    catalog_rows: list,
    hydro_index: dict[str, HydroMatch],
) -> tuple[list[dict], list[str]]:
    """Return manifest entries and padded WBICs from catalog rows with no hydro match."""
    joined: list[dict] = []
    unmatched_wbic: list[str] = []
    for row in catalog_rows:
        hydro = hydro_index.get(row.wbic)
        entry = {
            "map_key": row.map_key,
            "wbic": row.wbic,
            "sheet_suffix": row.sheet_suffix,
            "name": row.name,
            "county": row.county,
            "acres": row.acres,
            "max_depth_ft": row.max_depth_ft,
            "location": row.location,
            "pdf_url": row.pdf_url,
            "hydro_matched": hydro is not None,
        }
        if hydro is not None:
            entry["hydro"] = {
                "waterbody_name": hydro.waterbody_name,
                "hydroid": hydro.hydroid,
                "hydrotype": hydro.hydrotype,
            }
        else:
            unmatched_wbic.append(row.wbic)
        joined.append(entry)
    return joined, sorted(set(unmatched_wbic))


# 24K Hydro HYDROTYPE domain: 706 Lake/Pond, 707 Reservoir/Flowage.
FLOWAGE_HYDROTYPE = 707


@dataclass(frozen=True)
class HydroWaterbody:
    wbic: str
    ring: tuple[tuple[float, float], ...]
    waterbody_name: str | None
    hydroid: int | None
    hydrotype: int | None


@dataclass(frozen=True)
class HydroPart:
    """One polygon part. ``holes`` are island rings inside ``exterior``."""

    exterior: tuple[tuple[float, float], ...]
    holes: tuple[tuple[tuple[float, float], ...], ...] = ()


@dataclass(frozen=True)
class HydroGeometry:
    """All parts of the largest 24K Hydro feature for one WBIC, holes kept.

    Georef matches the largest exterior and ignores islands. Contour clipping
    uses that same feature, including island rings, and does not pull in a
    second feature that shares the WBIC.
    """

    wbic: str
    parts: tuple[HydroPart, ...]
    waterbody_name: str | None
    hydroid: int | None
    hydrotype: int | None


def _ring_area(ring: list[tuple[float, float]]) -> float:
    area = 0.0
    for index, (x1, y1) in enumerate(ring):
        x2, y2 = ring[(index + 1) % len(ring)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _clean_ring(coordinates: list) -> list[tuple[float, float]] | None:
    ring = [(float(point[0]), float(point[1])) for point in coordinates]
    if len(ring) >= 2:
        dx = ring[0][0] - ring[-1][0]
        dy = ring[0][1] - ring[-1][1]
        if dx * dx + dy * dy < 1e-8:
            ring = ring[:-1]
    if len(ring) < 3:
        return None
    return ring


def _largest_exterior(geometry: dict) -> list[tuple[float, float]] | None:
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind == "Polygon" and isinstance(coordinates, list) and coordinates:
        return _clean_ring(coordinates[0])
    if kind == "MultiPolygon" and isinstance(coordinates, list):
        best: list[tuple[float, float]] | None = None
        best_area = -1.0
        for polygon in coordinates:
            if not polygon:
                continue
            ring = _clean_ring(polygon[0])
            if ring is None:
                continue
            area = _ring_area(ring)
            if area > best_area:
                best = ring
                best_area = area
        return best
    return None


def _parts_from_geometry(geometry: dict) -> list[tuple[list[tuple[float, float]], list[list[tuple[float, float]]]]]:
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind == "Polygon" and isinstance(coordinates, list):
        polygons = [coordinates]
    elif kind == "MultiPolygon" and isinstance(coordinates, list):
        polygons = [polygon for polygon in coordinates if isinstance(polygon, list)]
    else:
        return []
    parts: list[tuple[list[tuple[float, float]], list[list[tuple[float, float]]]]] = []
    for polygon in polygons:
        if not polygon:
            continue
        exterior = _clean_ring(polygon[0])
        if exterior is None:
            continue
        holes: list[list[tuple[float, float]]] = []
        for hole in polygon[1:]:
            cleaned = _clean_ring(hole)
            if cleaned is not None:
                holes.append(cleaned)
        parts.append((exterior, holes))
    return parts


def _feature_area(parts: list[tuple[list[tuple[float, float]], list]]) -> float:
    return sum(_ring_area(exterior) for exterior, _holes in parts)


def load_hydro_geometries(path: Path) -> dict[str, HydroGeometry]:
    """Index 24K Hydro polygons by WBIC, keeping island rings.

    Same CRS rule as ``load_hydro_polygons``. When several features share a
    WBIC, the feature with the largest exterior area is kept (every part of
    that feature, including holes).
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    _assert_hydro_crs_3071(data)
    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError("hydro GeoJSON must contain a features array")
    index: dict[str, HydroGeometry] = {}
    areas: dict[str, float] = {}
    for feature in features:
        if not isinstance(feature, dict):
            continue
        parts = _parts_from_geometry(feature.get("geometry") or {})
        if not parts:
            continue
        props = feature.get("properties") or {}
        wbic = normalize_wbic(props.get("WATERBODY_WBIC") or props.get("waterbody_wbic"))
        if wbic is None:
            continue
        area = _feature_area(parts)
        if wbic in areas and area <= areas[wbic]:
            continue
        name = props.get("WATERBODY_NAME") or props.get("WATERBODY_ROW_NAME") or props.get("waterbody_name")
        index[wbic] = HydroGeometry(
            wbic=wbic,
            parts=tuple(HydroPart(exterior=tuple(exterior), holes=tuple(tuple(hole) for hole in holes)) for exterior, holes in parts),
            waterbody_name=str(name) if name else None,
            hydroid=_int_or_none(props.get("HYDROID") or props.get("hydroid")),
            hydrotype=_int_or_none(props.get("HYDROTYPE") or props.get("hydrotype")),
        )
        areas[wbic] = area
    return index


def load_hydro_polygons(path: Path) -> dict[str, HydroWaterbody]:
    """Index 24K Hydro exteriors by WBIC. Same CRS rule as ``load_hydro_index``.

    When several features share a WBIC, the largest exterior ring is kept.
    Island holes are ignored; georef matches the outer shoreline.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    _assert_hydro_crs_3071(data)
    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError("hydro GeoJSON must contain a features array")
    index: dict[str, HydroWaterbody] = {}
    areas: dict[str, float] = {}
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry") or {}
        ring = _largest_exterior(geometry)
        if ring is None:
            continue
        props = feature.get("properties") or {}
        wbic = normalize_wbic(props.get("WATERBODY_WBIC") or props.get("waterbody_wbic"))
        if wbic is None:
            continue
        area = _ring_area(ring)
        if wbic in areas and area <= areas[wbic]:
            continue
        name = props.get("WATERBODY_NAME") or props.get("WATERBODY_ROW_NAME") or props.get("waterbody_name")
        index[wbic] = HydroWaterbody(
            wbic=wbic,
            ring=tuple(ring),
            waterbody_name=str(name) if name else None,
            hydroid=_int_or_none(props.get("HYDROID") or props.get("hydroid")),
            hydrotype=_int_or_none(props.get("HYDROTYPE") or props.get("hydrotype")),
        )
        areas[wbic] = area
    return index


def tag_multi_sheet_lakes(entries: list[dict]) -> tuple[list[dict], list[str]]:
    """Mark entries that share a WBIC with more than one map document."""
    suffixes_by_wbic: dict[str, set[str]] = {}
    map_keys_by_wbic: dict[str, set[str]] = {}
    for entry in entries:
        suffixes_by_wbic.setdefault(entry["wbic"], set()).add(entry["sheet_suffix"])
        map_keys_by_wbic.setdefault(entry["wbic"], set()).add(entry["map_key"])
    multi = sorted(
        wbic
        for wbic in suffixes_by_wbic
        if len(suffixes_by_wbic[wbic]) > 1 or len(map_keys_by_wbic.get(wbic, set())) > 1
    )
    tagged: list[dict] = []
    for entry in entries:
        copy = dict(entry)
        copy["multi_sheet"] = entry["wbic"] in multi
        tagged.append(copy)
    return tagged, multi