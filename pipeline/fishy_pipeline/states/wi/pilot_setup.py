# SPDX-License-Identifier: GPL-3.0-only
"""One-command bootstrap for the 10-lake WI contour pilot (gcp-review work dir)."""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from pathlib import Path

from fishy_pipeline.states.wi import documents, hydro, manifest, raster
from fishy_pipeline.states.wi import catalog
from fishy_pipeline.states.wi.catalog import CatalogRow
from fishy_pipeline.states.wi.dnr_fetch import build_dnr_opener
from fishy_pipeline.states.wi.georef import run_georef
from fishy_pipeline.states.wi.hydro_fetch import fetch_pilot_hydro, pilot_hydro_complete
from fishy_pipeline.states.wi.pilot_data import (
    RECORDED_LABEL_DIR,
    load_recorded_labels,
    pilot_wbics,
)
from fishy_pipeline.states.wi.wbic import normalize_wbic, parse_map_key

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CACHE = REPO_ROOT / "pipeline" / ".cache" / "wi"
DEFAULT_WORK = REPO_ROOT / "pipeline" / "work" / "wi"
PILOT_HYDRO_NAME = "pilot-hydro.geojson"
DOC_LINK_BASE = "https://apps.dnr.wi.gov/doclink/lakes_maps"

PILOT_SETUP_CLI = (
    "python -m fishy_pipeline --state wi pilot-setup --work-dir pipeline/work/wi"
)

SYSTEM_DEPS = (
    ("gdal_translate", "gdal", "Translates rasters for georef warps."),
    ("gdalwarp", "gdal", "Applies TPS/affine warps during georef."),
)


def arch_install_hint() -> str:
    return (
        "Arch / CachyOS (pacman):\n"
        "  sudo pacman -S gdal python-gdal\n"
        "Python venv (repo root; --system-site-packages keeps pacman's python-gdal):\n"
        "  python -m venv --system-site-packages .venv\n"
        "  source .venv/bin/activate    # bash / zsh\n"
        "  # source .venv/bin/activate.fish   # fish\n"
        "  pip install -e 'pipeline[wi]'   # PyMuPDF for ingest raster step\n"
    )


def check_system_deps() -> list[str]:
    """Return missing CLI tools."""
    missing: list[str] = []
    for binary, _package, _why in SYSTEM_DEPS:
        if shutil.which(binary) is None:
            missing.append(binary)
    return missing


def pymupdf_available() -> bool:
    return importlib.util.find_spec("fitz") is not None or importlib.util.find_spec("pymupdf") is not None


def pilot_catalog_rows() -> list[CatalogRow]:
    """Build catalog rows from committed label fixtures (no full catalog harvest)."""
    rows: list[CatalogRow] = []
    for wbic in pilot_wbics():
        body = load_recorded_labels(wbic)
        if body is None:
            raise SystemExit(f"missing label fixture for pilot wbic {wbic}")
        map_key = str(body.get("map_key") or "")
        parsed = parse_map_key(map_key)
        if parsed is None:
            raise SystemExit(f"label file for {wbic} has invalid map_key {map_key!r}")
        wbic_norm, suffix = parsed
        pdf_url = f"{DOC_LINK_BASE}/{map_key}.pdf"
        rows.append(
            CatalogRow(
                map_key=map_key,
                wbic=wbic_norm,
                sheet_suffix=suffix,
                name=wbic_norm,
                county="",
                acres=None,
                max_depth_ft=None,
                location="",
                pdf_url=pdf_url,
            )
        )
    return rows


def manifest_covers_pilot(manifest_path: Path) -> bool:
    if not manifest_path.is_file():
        return False
    body = manifest.read_manifest(manifest_path)
    found = {
        normalize_wbic(entry.get("wbic"))
        for entry in body.get("entries") or []
        if normalize_wbic(entry.get("wbic"))
    }
    return all(wbic in found for wbic in pilot_wbics())


def run_pilot_ingest(
    *,
    work_dir: Path,
    cache_dir: Path,
    hydro_path: Path,
    dpi: int,
    force: bool,
) -> Path:
    """Download pilot PDFs, render rasters, and write manifest.json (pilot lakes only)."""
    manifest_path = work_dir / "manifest.json"
    if manifest_covers_pilot(manifest_path) and not force:
        return manifest_path

    if not pymupdf_available():
        raise SystemExit(
            "PyMuPDF is required to render pilot PDFs.\n"
            f"{arch_install_hint()}\n"
            "Then re-run:\n"
            f"  {PILOT_SETUP_CLI}"
        )

    sources_dir = cache_dir / "sources"
    rows = pilot_catalog_rows()
    hydro_index = hydro.load_hydro_index(hydro_path)
    entries, unmatched_wbic = hydro.join_catalog_to_hydro(rows, hydro_index)
    entries, multi_sheet_wbics = hydro.tag_multi_sheet_lakes(entries)

    for entry in entries:
        hydro_match = (entry.get("hydro") or {}).get("waterbody_name")
        if hydro_match:
            entry["name"] = hydro_match

    opener = build_dnr_opener()
    assets = documents.download_sources(
        rows,
        sources_dir,
        user_agent=catalog.DEFAULT_USER_AGENT,
        rate_limit_seconds=catalog.DEFAULT_RATE_LIMIT_SECONDS,
        opener=opener,
    )
    entries, skip_count = documents.annotate_entries_with_sources(entries, assets)

    dpi = raster.clamp_dpi(dpi)
    entries, raster_skip_count = raster.render_manifest_rasters(
        entries,
        sources_dir=sources_dir,
        work_dir=work_dir,
        dpi=dpi,
    )

    body = manifest.build_manifest(
        entries,
        unmatched_wbic=unmatched_wbic,
        multi_sheet_wbics=multi_sheet_wbics,
        catalog_source=str(RECORDED_LABEL_DIR),
        hydro_source=str(hydro_path),
        skip_count=skip_count,
        raster_skip_count=raster_skip_count,
    )
    manifest.write_manifest(manifest_path, body)
    return manifest_path


def run_pilot_setup(args: argparse.Namespace) -> None:
    missing = check_system_deps()
    if missing:
        lines = [
            "Missing system tools: " + ", ".join(missing),
            "",
            arch_install_hint(),
            f"Then re-run:\n  {PILOT_SETUP_CLI}",
        ]
        raise SystemExit("\n".join(lines))

    work_dir: Path = args.work_dir
    cache_dir: Path = args.cache_dir
    work_dir.mkdir(parents=True, exist_ok=True)

    wbics = pilot_wbics()
    hydro_path = args.hydro or (work_dir / PILOT_HYDRO_NAME)
    if pilot_hydro_complete(hydro_path, wbics) and not args.force:
        print(f"hydro OK: {hydro_path}", flush=True)
    else:
        print(f"fetching 24K Hydro for {len(wbics)} pilot lakes …", flush=True)
        fetch_pilot_hydro(hydro_path, wbics)
        print(f"wrote {hydro_path}", flush=True)

    if not args.skip_ingest:
        print("ingest: PDFs and rasters …", flush=True)
        manifest_path = run_pilot_ingest(
            work_dir=work_dir,
            cache_dir=cache_dir,
            hydro_path=hydro_path,
            dpi=args.dpi,
            force=args.force,
        )
        print(f"manifest: {manifest_path}", flush=True)
    else:
        manifest_path = work_dir / "manifest.json"

    if args.skip_georef:
        return

    if not manifest_path.is_file():
        raise SystemExit(f"manifest missing after ingest: {manifest_path}")

    print("georef: auto registration …", flush=True)
    run_georef(
        manifest_path=manifest_path,
        hydro_path=hydro_path,
        work_dir=work_dir,
    )
    print("pilot setup complete", flush=True)
    print(
        "Start GCP review:\n"
        "  python -m fishy_pipeline --state wi gcp-review \\\n"
        f"    --work-dir {work_dir} \\\n"
        f"    --hydro {hydro_path}",
        flush=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fishy_pipeline.states.wi.pilot_setup")
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--hydro",
        type=Path,
        help=f"24K Hydro extract path (default: <work-dir>/{PILOT_HYDRO_NAME})",
    )
    parser.add_argument("--dpi", type=int, default=raster.DEFAULT_DPI)
    parser.add_argument("--force", action="store_true", help="re-download hydro and re-ingest")
    parser.add_argument("--skip-ingest", action="store_true")
    parser.add_argument("--skip-georef", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        run_pilot_setup(args)
    except RuntimeError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main(sys.argv[1:])