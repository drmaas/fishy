# SPDX-License-Identifier: GPL-3.0-only
"""Versioned Wisconsin R2 object keys and publish guards for static lake JSON."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from fishy_pipeline.states.wi.wbic import is_wbic

WI_R2_VERSION = "v1"
WI_R2_PREFIX = f"wi/{WI_R2_VERSION}"

PACKAGE_MANIFEST_NAME = "package-manifest.json"
CURRENT_POINTER_KEY = f"{WI_R2_PREFIX}/current.json"

WI_GEN_LAKE_DETAIL_RE = re.compile(rf"^{WI_R2_PREFIX}/([^/]+)/lakes/([0-9]{{7}})\.json$")
WI_GEN_LAKES_INDEX_RE = re.compile(rf"^{WI_R2_PREFIX}/([^/]+)/lakes_index\.json$")

PUBLISHED_QA_STATUS = "verified"


def new_build_id() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).strftime("%Y%m%dT%H%M%SZ")


def generation_prefix(build_id: str) -> str:
    return f"{WI_R2_PREFIX}/{build_id}"


def generation_lake_detail_key(build_id: str, wbic: str) -> str:
    if not is_wbic(wbic):
        raise ValueError(f"invalid wbic {wbic!r}")
    return f"{generation_prefix(build_id)}/lakes/{wbic}.json"


def generation_lakes_index_key(build_id: str) -> str:
    return f"{generation_prefix(build_id)}/lakes_index.json"


def published_qa_status(*, publishable: bool, qa_status: str | None = None) -> str:
    if not publishable or qa_status == "needs_review":
        return "needs_review"
    return PUBLISHED_QA_STATUS


def contour_item_cleared_for_publish(contour_item: dict) -> bool:
    if contour_item.get("qa_status") in {"skipped", "failed"}:
        return True
    if contour_item.get("qa_status") == "needs_review":
        return False
    return bool(contour_item.get("publishable"))


def refuse_unless_publishable(contour_item: dict) -> None:
    """Raise when a traced sheet is active but not cleared for R2 publish."""
    if contour_item.get("qa_status") in {"skipped", "failed"}:
        return
    wbic = contour_item.get("wbic", "?")
    reasons = list(contour_item.get("reasons") or [])
    if contour_item.get("qa_status") == "needs_review":
        raise ValueError(f"lake {wbic} is not publishable: qa_status needs_review")
    if not contour_item.get("publishable"):
        if not reasons:
            reasons = ["not_publishable"]
        raise ValueError(f"lake {wbic} is not publishable: {reasons}")


def wbic_ready_for_publish(items: list[dict]) -> bool:
    """True when every non-skipped contour row for the lake is publishable and not needs_review."""
    active = [item for item in items if item.get("qa_status") not in {"skipped", "failed"}]
    if not active:
        return False
    return all(contour_item_cleared_for_publish(item) for item in active)


def build_current_pointer(build_id: str, *, lake_detail_keys: list[str], lakes_index_key: str) -> dict:
    return {
        "version": 1,
        "generation": build_id,
        "prefix": generation_prefix(build_id),
        "lakes_index_key": lakes_index_key,
        "lake_detail_keys": lake_detail_keys,
    }


def assert_lake_detail_uploadable(file_path: Path, object_key: str) -> None:
    match = WI_GEN_LAKE_DETAIL_RE.match(object_key)
    if not match:
        return
    body = json.loads(file_path.read_text(encoding="utf-8"))
    wbic = str(body.get("id") or body.get("dowlknum") or match.group(2))
    qa = body.get("qa_status")
    if qa != PUBLISHED_QA_STATUS:
        raise SystemExit(f"refusing upload for {object_key}: lake {wbic} qa_status is {qa!r}")


def assert_lakes_index_uploadable(file_path: Path, object_key: str) -> None:
    if not WI_GEN_LAKES_INDEX_RE.match(object_key):
        return
    body = json.loads(file_path.read_text(encoding="utf-8"))
    rows = body if isinstance(body, list) else body.get("lakes")
    if not isinstance(rows, list):
        raise SystemExit(f"refusing upload for {object_key}: expected a JSON array")
    for row in rows:
        if not isinstance(row, dict):
            raise SystemExit(f"refusing upload for {object_key}: invalid index row")
        qa = row.get("qa_status")
        if qa != PUBLISHED_QA_STATUS:
            wbic = row.get("id", "?")
            raise SystemExit(f"refusing upload for {object_key}: lake {wbic} qa_status is {qa!r}")


def assert_wi_object_uploadable(file_path: Path, object_key: str) -> None:
    if object_key == CURRENT_POINTER_KEY:
        return
    assert_lake_detail_uploadable(file_path, object_key)
    assert_lakes_index_uploadable(file_path, object_key)