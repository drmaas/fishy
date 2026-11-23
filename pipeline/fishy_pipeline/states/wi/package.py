# SPDX-License-Identifier: GPL-3.0-only
"""Build Wisconsin per-lake JSON and lakes_index.json under versioned ``wi/v1/`` keys."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from fishy_pipeline.common.build import build_wi_lake_artifacts
from fishy_pipeline.common.commands import find_tool
from fishy_pipeline.common.upload import upload_wi_artifacts
from fishy_pipeline.common.wi_r2 import WI_R2_PREFIX
from fishy_pipeline.states.wi.lake_json import (
    build_publishable_lake_detail,
    group_contour_items,
    load_contour_results,
)
from fishy_pipeline.states.wi.package_verify import load_georef_index, verify_contour_item

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_WORK = REPO_ROOT / "pipeline" / "work" / "wi"
DEFAULT_DEST = REPO_ROOT / "pipeline" / "work" / "wi" / "publish"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fishy_pipeline.states.wi.package")
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="local staging directory for wi/v1 artifacts")
    parser.add_argument("--manifest", type=Path, help="ingest manifest JSON")
    parser.add_argument("--hydro", type=Path, help="24K Hydro GeoJSON in EPSG:3071")
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="emit contract-shaped JSON without GDAL or a populated work tree",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="upload staged artifacts via wrangler (requires Cloudflare env); use --dry-run in tests",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list upload targets without calling wrangler",
    )
    return parser


def run_fixture(dest: Path) -> dict:
    return build_wi_lake_artifacts(dest, fixtures=True)


def run_package(
    *,
    work_dir: Path,
    dest: Path,
    manifest_path: Path,
    hydro_path: Path,
) -> dict:
    results = load_contour_results(work_dir)
    georef_index = load_georef_index(work_dir)
    grouped = group_contour_items(results)
    details: list[dict] = []
    skipped: list[dict] = []
    publish_items: list[tuple[str, list[dict]]] = []
    for wbic, items in sorted(grouped.items()):
            active = [
                item
                for item in items
                if item.get("qa_status") not in {"skipped", "failed"}
            ]
            if not active:
                continue
            try:
                for item in active:
                    map_key = str(item.get("map_key") or "")
                    publishable, _qa = verify_contour_item(item, georef_index.get((wbic, map_key)))
                    if not publishable:
                        raise ValueError(f"lake {wbic} is not publishable")
            except ValueError as error:
                message = str(error)
                if "tampered" in message or "disagrees" in message:
                    raise
                skipped.append({"wbic": wbic, "error": message})
                continue
            publish_items.append((wbic, items))

    ogr = find_tool("ogr2ogr", "OGR2OGR")
    if not ogr and publish_items:
        raise SystemExit("ogr2ogr must be on PATH")
    with tempfile.TemporaryDirectory(prefix="fishy-wi-json-") as raw:
        scratch = Path(raw)
        for wbic, items in publish_items:
            detail = build_publishable_lake_detail(
                wbic=wbic,
                items=items,
                manifest_path=manifest_path,
                hydro_path=hydro_path,
                ogr=ogr,
                work=scratch,
            )
            details.append(detail)
    return build_wi_lake_artifacts(dest, details=details, skipped=skipped)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    dest: Path = args.dest
    try:
        if args.fixtures:
            if args.manifest or args.hydro or args.upload:
                raise ValueError("--fixtures does not take --manifest, --hydro, or --upload")
            run_fixture(dest)
            print(f"fixture artifacts written under {dest / WI_R2_PREFIX}", flush=True)
            return
        manifest_path = args.manifest or (args.work_dir / "manifest.json")
        hydro_path = args.hydro
        if hydro_path is None:
            raise ValueError("--hydro is required unless --fixtures")
        if not manifest_path.is_file():
            raise ValueError(f"manifest not found: {manifest_path}")
        if not hydro_path.is_file():
            raise ValueError(f"hydro file not found: {hydro_path}")
        body = run_package(
            work_dir=args.work_dir,
            dest=dest,
            manifest_path=manifest_path,
            hydro_path=hydro_path,
        )
        lake_count = len(body.get("lakes") or [])
        print(f"built {lake_count} publishable lake(s) under {dest / WI_R2_PREFIX}", flush=True)
        if args.upload or args.dry_run:
            upload_wi_artifacts(dest, dry_run=args.dry_run or not args.upload)
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main(sys.argv[1:])