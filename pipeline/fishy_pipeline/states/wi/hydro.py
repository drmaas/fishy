# SPDX-License-Identifier: GPL-3.0-only
"""Join catalog rows to WI DNR 24K Hydro waterbodies by WBIC."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from fishy_pipeline.states.wi.wbic import normalize_wbic


@dataclass(frozen=True)
class HydroMatch:
    wbic: str
    waterbody_name: str | None
    hydroid: int | None
    hydrotype: int | None


def load_hydro_index(path: Path) -> dict[str, HydroMatch]:
    """Load a GeoJSON FeatureCollection and index lake polygons by WBIC."""
    data = json.loads(path.read_text(encoding="utf-8"))
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
    """Return manifest entries plus WBICs present in hydro but not in catalog (unused here)."""
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


def tag_multi_sheet_lakes(entries: list[dict]) -> tuple[list[dict], list[str]]:
    """Mark entries that share a WBIC with more than one sheet suffix."""
    suffixes_by_wbic: dict[str, set[str]] = {}
    for entry in entries:
        suffixes_by_wbic.setdefault(entry["wbic"], set()).add(entry["sheet_suffix"])
    multi = sorted(wbic for wbic, suffixes in suffixes_by_wbic.items() if len(suffixes) > 1)
    tagged: list[dict] = []
    for entry in entries:
        copy = dict(entry)
        copy["multi_sheet"] = entry["wbic"] in multi
        tagged.append(copy)
    return tagged, multi