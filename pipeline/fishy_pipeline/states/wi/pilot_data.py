# SPDX-License-Identifier: GPL-3.0-only
"""Committed pilot GCPs and depth labels under ``pipeline/data/wi/``."""

from __future__ import annotations

import json
from pathlib import Path

from fishy_pipeline.states.wi.depth_tag import meters_to_feet

REPO_ROOT = Path(__file__).resolve().parents[4]
WI_DATA = REPO_ROOT / "pipeline" / "data" / "wi"
REVIEWED_GCP_DIR = WI_DATA / "gcps"
RECORDED_LABEL_DIR = WI_DATA / "labels"
MIN_REVIEWED_GCPS = 4


def reviewed_gcp_path(wbic: str) -> Path:
    return REVIEWED_GCP_DIR / f"{wbic}.json"


def recorded_label_path(wbic: str) -> Path:
    return RECORDED_LABEL_DIR / f"{wbic}.json"


def load_reviewed_gcps(wbic: str) -> dict | None:
    path = reviewed_gcp_path(wbic)
    if not path.is_file():
        return None
    body = json.loads(path.read_text(encoding="utf-8"))
    gcps = body.get("gcps")
    if not isinstance(gcps, list) or len(gcps) < MIN_REVIEWED_GCPS:
        return None
    return body


def save_reviewed_gcps(wbic: str, body: dict) -> Path:
    gcps = body.get("gcps")
    if not isinstance(gcps, list) or len(gcps) < MIN_REVIEWED_GCPS:
        raise ValueError(f"reviewed GCP file needs at least {MIN_REVIEWED_GCPS} points")
    path = reviewed_gcp_path(wbic)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**body, "version": 1, "wbic": wbic}, indent=2) + "\n", encoding="utf-8")
    return path


def load_recorded_labels(wbic: str) -> dict | None:
    path = recorded_label_path(wbic)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def recorded_depths_ft(body: dict) -> list[int]:
    """Positive integer depths expected from a label file."""
    depths: list[int] = []
    unit = str(body.get("unit") or "ft")
    for item in body.get("labels") or []:
        value = item.get("value")
        item_unit = str(item.get("unit") or unit)
        if value is None:
            continue
        depth = float(value)
        if item_unit == "m":
            depths.append(int(meters_to_feet(depth)))
        else:
            depths.append(int(round(depth)))
    return sorted(set(depths))


def label_file_unit(body: dict) -> str:
    unit = str(body.get("unit") or "ft")
    for item in body.get("labels") or []:
        if str(item.get("unit") or "").startswith("m"):
            return "m"
    if unit.startswith("m"):
        return "m"
    return "ft"


def depths_match_recorded(traced_depths: list[int], body: dict) -> tuple[bool, list[str]]:
    expected = set(recorded_depths_ft(body))
    traced = {int(depth) for depth in traced_depths if int(depth) != 0}
    reasons: list[str] = []
    extra = traced - expected
    if extra:
        reasons.append("depth_not_in_recorded_labels")
    missing = expected - traced
    if missing:
        reasons.append("recorded_depth_missing")
    return (not reasons, reasons)