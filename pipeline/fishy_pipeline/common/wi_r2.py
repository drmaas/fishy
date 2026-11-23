# SPDX-License-Identifier: GPL-3.0-only
"""Versioned Wisconsin R2 object keys and publish guards for static lake JSON."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fishy_pipeline.states.wi.wbic import is_wbic

WI_R2_VERSION = "v1"
WI_R2_PREFIX = f"wi/{WI_R2_VERSION}"

WI_LAKE_DETAIL_KEY_RE = re.compile(rf"^{WI_R2_PREFIX}/lakes/([0-9]{{7}})\.json$")
WI_LAKES_INDEX_KEY = f"{WI_R2_PREFIX}/lakes_index.json"

PUBLISHED_QA_STATUS = "verified"


def lake_detail_object_key(wbic: str) -> str:
    if not is_wbic(wbic):
        raise ValueError(f"invalid wbic {wbic!r}")
    return f"{WI_R2_PREFIX}/lakes/{wbic}.json"


def lakes_index_object_key() -> str:
    return WI_LAKES_INDEX_KEY


def published_qa_status(*, publishable: bool) -> str:
    return PUBLISHED_QA_STATUS if publishable else "needs_review"


def refuse_unless_publishable(contour_item: dict) -> None:
    """Raise when a traced sheet is active but not cleared for R2 publish."""
    if contour_item.get("qa_status") in {"skipped", "failed"}:
        return
    if contour_item.get("publishable"):
        return
    wbic = contour_item.get("wbic", "?")
    reasons = contour_item.get("reasons") or []
    raise ValueError(f"lake {wbic} is not publishable: {reasons}")


def wbic_ready_for_publish(items: list[dict]) -> bool:
    """True when every non-skipped contour row for the lake is publishable."""
    active = [item for item in items if item.get("qa_status") not in {"skipped", "failed"}]
    if not active:
        return False
    return all(bool(item.get("publishable")) for item in active)


def assert_lake_detail_uploadable(file_path: Path, object_key: str) -> None:
    match = WI_LAKE_DETAIL_KEY_RE.match(object_key)
    if not match:
        return
    body = json.loads(file_path.read_text(encoding="utf-8"))
    wbic = str(body.get("id") or body.get("dowlknum") or match.group(1))
    qa = body.get("qa_status")
    if qa != PUBLISHED_QA_STATUS:
        raise SystemExit(f"refusing upload for {object_key}: lake {wbic} qa_status is {qa!r}")


def assert_lakes_index_uploadable(file_path: Path, object_key: str) -> None:
    if object_key != WI_LAKES_INDEX_KEY:
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
    assert_lake_detail_uploadable(file_path, object_key)
    assert_lakes_index_uploadable(file_path, object_key)