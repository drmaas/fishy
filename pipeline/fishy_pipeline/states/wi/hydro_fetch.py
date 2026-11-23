# SPDX-License-Identifier: GPL-3.0-only
"""Fetch 24K Hydro polygons for pilot lakes from WI DNR MapServer."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fishy_pipeline.states.wi.hydro import _assert_hydro_crs_3071
from fishy_pipeline.states.wi.wbic import normalize_wbic

HYDRO_LAYER_URL = (
    "https://dnrmaps.wi.gov/arcgis/rest/services/DW_Map_Dynamic/"
    "EN_SurfaceWater_WTM_Ext_Dynamic_L16/MapServer/5"
)
DEFAULT_USER_AGENT = "FishyBathymetry/0.1 (pipeline wi pilot-setup; +https://github.com/drmaas/fishy)"
HYDRO_CRS_BLOCK = {
    "type": "name",
    "properties": {"name": "urn:ogc:def:crs:EPSG::3071"},
}


def _query_url(wbic: str) -> str:
    wbic_norm = normalize_wbic(wbic)
    if wbic_norm is None:
        raise ValueError(f"invalid wbic {wbic!r}")
    where = f"WATERBODY_WBIC={int(wbic_norm)}"
    params = {
        "where": where,
        "outFields": "*",
        "f": "geojson",
        "outSR": "3071",
    }
    return f"{HYDRO_LAYER_URL}/query?{urllib.parse.urlencode(params)}"


def fetch_wbic_feature(wbic: str, *, opener: urllib.request.OpenerDirector | None = None) -> dict | None:
    """Return one GeoJSON feature for a WBIC, or None when the server returns no rows."""
    url = _query_url(wbic)
    request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    client = opener or urllib.request.build_opener()
    try:
        with client.open(request, timeout=120) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as error:
        raise RuntimeError(f"hydro query failed for {wbic}: {error}") from error
    features = body.get("features")
    if not isinstance(features, list) or not features:
        return None
    feature = features[0]
    if not isinstance(feature, dict):
        return None
    return feature


def pilot_hydro_complete(path: Path, wbics: list[str]) -> bool:
    """True when *path* exists and lists every pilot WBIC."""
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        _assert_hydro_crs_3071(data)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    found: set[str] = set()
    for feature in data.get("features") or []:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        wbic = normalize_wbic(props.get("WATERBODY_WBIC") or props.get("waterbody_wbic"))
        if wbic:
            found.add(wbic)
    return all(normalize_wbic(wbic) in found for wbic in wbics)


def fetch_pilot_hydro(path: Path, wbics: list[str], *, rate_limit_seconds: float = 0.5) -> dict:
    """Download hydro features for *wbics* and write *path*. Idempotent when already complete."""
    ordered = sorted({normalize_wbic(w) for w in wbics if normalize_wbic(w)})
    if not ordered:
        raise ValueError("no pilot wbics")
    features: list[dict] = []
    missing: list[str] = []
    opener = urllib.request.build_opener()
    for index, wbic in enumerate(ordered):
        if index:
            time.sleep(rate_limit_seconds)
        feature = fetch_wbic_feature(wbic, opener=opener)
        if feature is None:
            missing.append(wbic)
        else:
            features.append(feature)
    if missing:
        raise RuntimeError(f"24K Hydro returned no feature for: {', '.join(missing)}")
    collection = {
        "type": "FeatureCollection",
        "crs": HYDRO_CRS_BLOCK,
        "features": features,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(collection, indent=2) + "\n", encoding="utf-8")
    return collection