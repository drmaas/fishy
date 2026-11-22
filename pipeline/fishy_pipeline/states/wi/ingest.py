# SPDX-License-Identifier: GPL-3.0-only
"""
Download Wisconsin DNR bathymetry PDF maps and sidecar metadata into a lake work directory.

Harvester: WI DNR lake map catalog. Join: 24K Hydro waterbodies (EPSG:3071) by WBIC.
Outputs: manifest JSON, cached PDFs, and PNG rasters under a git-ignored work tree.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fishy_pipeline.states.wi import catalog, hydro, manifest, raster
from fishy_pipeline.states.wi.wbic import parse_map_key

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CACHE = REPO_ROOT / "pipeline" / ".cache" / "wi"
DEFAULT_WORK = REPO_ROOT / "pipeline" / "work" / "wi"
FIXTURE_DIR = REPO_ROOT / "fixtures" / "wi-ingest"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fishy_pipeline.states.wi.ingest")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE,
        help=f"download cache (default: {DEFAULT_CACHE})",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=DEFAULT_WORK,
        help=f"manifest and rasters (default: {DEFAULT_WORK})",
    )
    parser.add_argument(
        "--hydro",
        type=Path,
        help="24K Hydro GeoJSON path (local file; not downloaded in this milestone)",
    )
    parser.add_argument(
        "--catalog-html",
        type=Path,
        help="use a single saved catalog page instead of harvesting",
    )
    parser.add_argument(
        "--harvest-catalog",
        action="store_true",
        help="fetch catalog pages from apps.dnr.wi.gov (uses cache)",
    )
    parser.add_argument(
        "--max-catalog-pages",
        type=int,
        help="limit catalog harvest pages (debug)",
    )
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="run against committed fixtures/wi-ingest only",
    )
    parser.add_argument(
        "--map-key",
        metavar="KEY",
        help="limit to one map key such as 0417300a",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=raster.DEFAULT_DPI,
        help=f"raster DPI in [{raster.MIN_DPI}, {raster.MAX_DPI}]",
    )
    parser.add_argument(
        "--skip-pdf",
        action="store_true",
        help="do not download PDFs (use cache or fixtures)",
    )
    parser.add_argument(
        "--skip-raster",
        action="store_true",
        help="build manifest only, no raster step",
    )
    return parser


def _filter_rows(rows: list[catalog.CatalogRow], map_key: str | None) -> list[catalog.CatalogRow]:
    if map_key is None:
        return rows
    key = map_key.lower()
    if parse_map_key(key) is None:
        raise SystemExit(f"invalid --map-key {map_key!r}")
    out = [row for row in rows if row.map_key == key]
    if not out:
        raise SystemExit(f"map key {map_key!r} not present in catalog")
    return out


def run_ingest(args: argparse.Namespace) -> dict:
    cache_dir: Path = args.cache_dir
    work_dir: Path = args.work_dir
    pdf_dir = cache_dir / "pdfs"

    if args.fixtures:
        catalog_path = FIXTURE_DIR / "catalog-snippet.html"
        hydro_path = FIXTURE_DIR / "hydro-snippet.geojson"
        if not catalog_path.is_file() or not hydro_path.is_file():
            raise SystemExit("fixtures/wi-ingest is incomplete")
        rows = catalog.parse_catalog_html(catalog_path.read_text(encoding="utf-8"))
        catalog_source = str(catalog_path)
        hydro_source = str(hydro_path)
        pdf_dir = FIXTURE_DIR
    else:
        if args.catalog_html:
            rows = catalog.parse_catalog_html(args.catalog_html.read_text(encoding="utf-8"))
            catalog_source = str(args.catalog_html)
        elif args.harvest_catalog or (cache_dir / "catalog-page-001.html").is_file():
            if args.harvest_catalog or not list(cache_dir.glob("catalog-page-*.html")):
                rows = catalog.harvest_catalog(
                    cache_dir / "catalog",
                    max_pages=args.max_catalog_pages,
                )
            else:
                rows = catalog.load_cached_catalog_html(cache_dir / "catalog")
            catalog_source = str(cache_dir / "catalog")
        else:
            raise SystemExit("pass --harvest-catalog, --catalog-html, or --fixtures")
        hydro_path = args.hydro
        if hydro_path is None:
            raise SystemExit("--hydro is required unless --fixtures")
        if not hydro_path.is_file():
            raise SystemExit(f"hydro file not found: {hydro_path}")
        hydro_source = str(hydro_path)
    rows = _filter_rows(rows, args.map_key)

    hydro_index = hydro.load_hydro_index(hydro_path)
    entries, unmatched_wbic = hydro.join_catalog_to_hydro(rows, hydro_index)
    entries, multi_sheet_wbics = hydro.tag_multi_sheet_lakes(entries)

    if not args.skip_pdf:
        for row in rows:
            pdf_path = pdf_dir / f"{row.map_key}.pdf"
            if args.fixtures:
                if not pdf_path.is_file():
                    raise SystemExit(f"fixture PDF missing: {pdf_path}")
                continue
            catalog.fetch_pdf(row.pdf_url, pdf_path)

    dpi = raster.clamp_dpi(args.dpi)
    if not args.skip_raster:
        render_source = pdf_dir
        if args.fixtures:

            def _fixture_render(pdf_path: Path, output_path: Path, _dpi: int) -> None:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(raster.TINY_PNG)

            entries = raster.render_manifest_rasters(
                entries,
                pdf_dir=render_source,
                work_dir=work_dir,
                dpi=dpi,
                render_page=_fixture_render,
            )
        else:
            entries = raster.render_manifest_rasters(
                entries,
                pdf_dir=render_source,
                work_dir=work_dir,
                dpi=dpi,
            )

    body = manifest.build_manifest(
        entries,
        unmatched_wbic=unmatched_wbic,
        multi_sheet_wbics=multi_sheet_wbics,
        catalog_source=catalog_source,
        hydro_source=hydro_source,
    )
    manifest_path = work_dir / "manifest.json"
    manifest.write_manifest(manifest_path, body)
    return body


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    run_ingest(args)


if __name__ == "__main__":
    main(sys.argv[1:])