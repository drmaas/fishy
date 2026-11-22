# SPDX-License-Identifier: GPL-3.0-only
"""Assemble the Wisconsin ingest manifest JSON."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


def build_manifest(
    entries: list[dict],
    *,
    unmatched_wbic: list[str],
    multi_sheet_wbics: list[str],
    catalog_source: str,
    hydro_source: str,
    skip_count: int = 0,
    raster_skip_count: int = 0,
) -> dict:
    return {
        "version": 1,
        "state": "wi",
        "generated_at": datetime.now(tz=UTC).replace(microsecond=0).isoformat(),
        "catalog_source": catalog_source,
        "hydro_source": hydro_source,
        "entry_count": len(entries),
        "skip_count": skip_count,
        "raster_skip_count": raster_skip_count,
        "entries": entries,
        "unmatched_wbic": unmatched_wbic,
        "multi_sheet_wbics": multi_sheet_wbics,
    }


def write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def read_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))