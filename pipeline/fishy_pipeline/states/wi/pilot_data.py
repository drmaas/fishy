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


def validate_recorded_label_file(body: dict, path: Path | None = None) -> list[str]:
    """Return human-readable schema errors, or [] when the file is valid."""
    errors: list[str] = []
    prefix = f"{path}: " if path else ""
    if not isinstance(body, dict):
        return [f"{prefix}root must be an object"]
    for key in ("version", "wbic", "map_key", "page", "unit", "labels"):
        if key not in body:
            errors.append(f"{prefix}missing {key}")
    wbic = body.get("wbic")
    if wbic is not None and (not isinstance(wbic, str) or not wbic.isdigit() or len(wbic) != 7):
        errors.append(f"{prefix}wbic must be seven ASCII digits")
    labels = body.get("labels")
    if labels is not None:
        if not isinstance(labels, list):
            errors.append(f"{prefix}labels must be a list")
        else:
            for index, label in enumerate(labels):
                if not isinstance(label, dict):
                    errors.append(f"{prefix}labels[{index}] must be an object")
                    continue
                if not isinstance(label.get("pixel"), list) or len(label.get("pixel", [])) != 2:
                    errors.append(f"{prefix}labels[{index}].pixel must be [x, y]")
                if not isinstance(label.get("value"), (int, float)):
                    errors.append(f"{prefix}labels[{index}].value must be a number")
                if label.get("confidence") is not None and not isinstance(label.get("confidence"), (int, float)):
                    errors.append(f"{prefix}labels[{index}].confidence must be a number")
    return errors


def validate_committed_label_files() -> list[str]:
    errors: list[str] = []
    if not RECORDED_LABEL_DIR.is_dir():
        return errors
    for path in sorted(RECORDED_LABEL_DIR.glob("*.json")):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            errors.append(f"{path}: invalid JSON ({error})")
            continue
        errors.extend(validate_recorded_label_file(body, path))
        wbic = str(body.get("wbic", ""))
        if path.stem != wbic:
            errors.append(f"{path}: filename does not match wbic {wbic}")
    return errors


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