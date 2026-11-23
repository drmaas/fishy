# SPDX-License-Identifier: GPL-3.0-only
"""Re-verify contour publish state from georef + on-disk GeoJSON before R2 package."""

from __future__ import annotations

import json
from pathlib import Path

from fishy_pipeline.common.wi_r2 import refuse_unless_publishable
from fishy_pipeline.states.wi.publish import publish_gate
from fishy_pipeline.states.wi.wbic import normalize_wbic


def load_georef_index(work_dir: Path) -> dict[tuple[str, str], dict]:
    path = work_dir / "georef" / "results.json"
    if not path.is_file():
        raise ValueError(f"georef results not found: {path}")
    body = json.loads(path.read_text(encoding="utf-8"))
    items = body.get("items")
    if not isinstance(items, list):
        raise ValueError("georef results items must be a list")
    index: dict[tuple[str, str], dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        wbic = normalize_wbic(item.get("wbic"))
        map_key = str(item.get("map_key") or "")
        if wbic is None or not map_key:
            continue
        index[(wbic, map_key)] = item
    return index


def _depths_from_geojson(path: Path) -> list[int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    depths: set[int] = set()
    for feature in data.get("features") or []:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        depth = props.get("depth_ft")
        if isinstance(depth, bool) or depth is None:
            continue
        try:
            value = int(depth)
        except (TypeError, ValueError):
            continue
        if value != 0:
            depths.add(value)
    return sorted(depths)


def _reviewed_gcp_present(georef_item: dict, wbic: str) -> bool:
    if georef_item.get("warp_source") != "reviewed":
        return True
    gcp_path = (georef_item.get("paths") or {}).get("gcps")
    if gcp_path and Path(str(gcp_path)).is_file():
        return True
    from fishy_pipeline.states.wi.pilot_data import reviewed_gcp_path

    return reviewed_gcp_path(wbic).is_file()


def expected_contour_publish_state(
    *,
    wbic: str,
    georef_item: dict,
    contour_geojson: Path,
) -> tuple[bool, str, list[str]]:
    """Derive publishable/qa from georef, labels, and the traced GeoJSON on disk."""
    if not contour_geojson.is_file():
        raise ValueError(f"contour GeoJSON missing: {contour_geojson}")
    if not _reviewed_gcp_present(georef_item, wbic):
        raise ValueError(f"reviewed warp missing GCP file for wbic {wbic}")

    depths_ft = _depths_from_geojson(contour_geojson)
    iou = georef_item.get("iou")
    gate_publishable, gate_qa, gate_reasons = publish_gate(
        wbic=wbic,
        iou=iou if isinstance(iou, (int, float)) else None,
        depths_ft=depths_ft,
        georef_qa_status=georef_item.get("qa_status"),
        georef_reasons=list(georef_item.get("reasons") or []),
        warp_source=georef_item.get("warp_source"),
    )
    geo = json.loads(contour_geojson.read_text(encoding="utf-8"))
    file_qa = geo.get("qa_status")
    review_features = any(
        bool((feature.get("properties") or {}).get("needs_review"))
        for feature in geo.get("features") or []
        if isinstance(feature, dict)
    )
    has_lines = bool(
        [
            feature
            for feature in geo.get("features") or []
            if isinstance(feature, dict) and (feature.get("properties") or {}).get("depth_ft") is not None
        ]
    )
    reasons = list(gate_reasons)
    if not has_lines:
        reasons.append("no_contour_lines")
    if review_features or not gate_publishable:
        publishable = False
        qa_status = "needs_review"
    elif file_qa != "auto":
        reasons.append("contour_geojson_qa_mismatch")
        publishable = False
        qa_status = "needs_review"
    else:
        publishable = True
        qa_status = gate_qa
    return publishable, qa_status, reasons


def verify_contour_item(item: dict, georef_item: dict | None) -> tuple[bool, str]:
    """Recompute publish state and refuse when ``contours/results.json`` disagrees."""
    wbic = str(item.get("wbic") or "")
    map_key = str(item.get("map_key") or "")
    if georef_item is None:
        raise ValueError(f"georef row missing for {wbic}/{map_key}")

    path = item.get("path")
    if not path:
        raise ValueError(f"contour path missing for {wbic}/{map_key}")
    expected_pub, expected_qa, reasons = expected_contour_publish_state(
        wbic=wbic,
        georef_item=georef_item,
        contour_geojson=Path(str(path)),
    )

    stored_pub = bool(item.get("publishable"))
    stored_qa = item.get("qa_status")
    geo_qa = json.loads(Path(str(path)).read_text(encoding="utf-8")).get("qa_status")
    if stored_pub != expected_pub or stored_qa != expected_qa:
        raise ValueError(
            f"contour results tampered for {wbic}/{map_key}: "
            f"stored publishable={stored_pub} qa_status={stored_qa!r} "
            f"expected publishable={expected_pub} qa_status={expected_qa!r} ({reasons})"
        )
    if geo_qa != expected_qa:
        raise ValueError(
            f"contour GeoJSON qa_status {geo_qa!r} disagrees with expected {expected_qa!r} for {wbic}/{map_key}"
        )

    checked = {
        **item,
        "publishable": expected_pub,
        "qa_status": expected_qa,
        "reasons": reasons,
    }
    refuse_unless_publishable(checked)
    return expected_pub, expected_qa