# SPDX-License-Identifier: GPL-3.0-only
"""
Georeference Wisconsin bathymetry rasters onto 24K Hydro shorelines.

Each manifest row is one ``(wbic, map_key)`` pair. ``map_key`` names the scanned
document and may be shared by many lakes (Chain O' Lakes is one PDF and many
WBICs). The fit for a pair uses that WBIC's own shoreline. Rows ingest skipped
(shapefile, unknown, and any other ``skip_reason``) stay skipped.

Registration is bbox/orientation, then affine point registration, then a thin-plate
spline. When ``gdal_translate`` and ``gdalwarp`` are on ``PATH``, the spline is
applied with ``gdalwarp -tps`` in EPSG:3071. The IoU score does not require GDAL.

IoU under 0.90, a multi-sheet lake, or a flowage (HYDROTYPE 707, or "flowage" in
the name) is written to ``georef/review_queue.json``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from fishy_pipeline.states.wi import synthetic
from fishy_pipeline.states.wi.hydro import FLOWAGE_HYDROTYPE, HydroWaterbody, load_hydro_polygons
from fishy_pipeline.states.wi.manifest import read_manifest
from fishy_pipeline.states.wi.pngutil import read_png
from fishy_pipeline.states.wi.register import DEFAULT_GCP_COUNT, register_shoreline
from fishy_pipeline.states.wi.shoreline import extract_shoreline

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_WORK = REPO_ROOT / "pipeline" / "work" / "wi"
WI_CRS = "EPSG:3071"
DEFAULT_IOU_THRESHOLD = 0.90
_REASON_ORDER = (
    "flowage",
    "iou_below_threshold",
    "multi_sheet",
    "shoreline_not_found",
    "warp_failed",
)


def is_flowage(hydrotype: int | None, *names: str | None) -> bool:
    """Reservoir/flowage waterbodies always need a human check."""
    if hydrotype == FLOWAGE_HYDROTYPE:
        return True
    for name in names:
        if name and "flowage" in name.casefold():
            return True
    return False


def gdal_translate_gcp_args(
    source: str,
    dest_vrt: str,
    gcps: list[dict],
    srs: str = WI_CRS,
) -> list[str]:
    command = ["gdal_translate", "-of", "VRT", "-a_srs", srs]
    for gcp in gcps:
        command.extend(
            [
                "-gcp",
                _num(gcp["pixel"]),
                _num(gcp["line"]),
                _num(gcp["easting"]),
                _num(gcp["northing"]),
            ]
        )
    command.extend([source, dest_vrt])
    return command


def gdalwarp_tps_args(source_vrt: str, dest_tif: str, t_srs: str = WI_CRS) -> list[str]:
    return [
        "gdalwarp",
        "-tps",
        "-t_srs",
        t_srs,
        "-r",
        "near",
        "-dstnodata",
        "0",
        "-overwrite",
        source_vrt,
        dest_tif,
    ]


def gdal_tools_available() -> bool:
    return shutil.which("gdal_translate") is not None and shutil.which("gdalwarp") is not None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fishy_pipeline.states.wi.georef")
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=DEFAULT_WORK,
        help=f"georef outputs (default: {DEFAULT_WORK})",
    )
    parser.add_argument("--manifest", type=Path, help="ingest manifest JSON")
    parser.add_argument("--hydro", type=Path, help="24K Hydro GeoJSON in EPSG:3071")
    parser.add_argument(
        "--iou-threshold",
        type=float,
        default=DEFAULT_IOU_THRESHOLD,
        help=f"shorelines below this IoU are queued (default: {DEFAULT_IOU_THRESHOLD})",
    )
    parser.add_argument(
        "--gcp-count",
        type=int,
        default=DEFAULT_GCP_COUNT,
        help="thin-plate spline control points per lake",
    )
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="georeference the synthetic fixtures/wi-georef sheet (no network, no DNR extract)",
    )
    return parser


def run_georef(
    *,
    manifest_path: Path,
    hydro_path: Path,
    work_dir: Path,
    iou_threshold: float = DEFAULT_IOU_THRESHOLD,
    gcp_count: int = DEFAULT_GCP_COUNT,
) -> dict:
    """Register every manifest row and write results plus the review queue."""
    if not 0 < iou_threshold <= 1:
        raise ValueError("iou threshold must be in (0, 1]")
    if gcp_count < 4:
        raise ValueError("gcp_count must be at least 4")
    manifest = read_manifest(manifest_path)
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ValueError("manifest entries must be a list")
    polygons = load_hydro_polygons(hydro_path)
    multi_wbics = _multi_sheet_wbics(entries)
    tools = gdal_tools_available()
    items: list[dict] = []
    for entry in entries:
        items.append(
            _georeference_entry(
                entry,
                manifest_path=manifest_path,
                polygons=polygons,
                multi_wbics=multi_wbics,
                work_dir=work_dir,
                iou_threshold=iou_threshold,
                gcp_count=gcp_count,
                gdal_available=tools,
            )
        )
    results = {
        "version": 1,
        "state": "wi",
        "crs": WI_CRS,
        "iou_threshold": iou_threshold,
        "items": items,
    }
    queue = {
        "version": 1,
        "state": "wi",
        "crs": WI_CRS,
        "iou_threshold": iou_threshold,
        "items": _review_items(items),
    }
    georef_dir = work_dir / "georef"
    _write_json(georef_dir / "results.json", results)
    _write_json(georef_dir / "review_queue.json", queue)
    return results


def _multi_sheet_wbics(entries: list[dict]) -> set[str]:
    keys: dict[str, set[str]] = {}
    for entry in entries:
        keys.setdefault(str(entry["wbic"]), set()).add(str(entry["map_key"]))
    return {wbic for wbic, map_keys in keys.items() if len(map_keys) > 1}


def _entry_is_multi(entry: dict, multi_wbics: set[str]) -> bool:
    if entry.get("multi_sheet") is True:
        return True
    return str(entry["wbic"]) in multi_wbics


def _resolve_raster(entry: dict, manifest_path: Path) -> Path | None:
    raw = entry.get("raster_path")
    if not raw:
        return None
    path = Path(str(raw))
    if path.is_file():
        return path
    sibling = manifest_path.parent / path
    if sibling.is_file():
        return sibling
    return None


def _skip_reason(entry: dict, raster: Path | None) -> str | None:
    if entry.get("skip_reason"):
        return str(entry["skip_reason"])
    if entry.get("raster_skip_reason"):
        return str(entry["raster_skip_reason"])
    media = entry.get("source_media_type")
    if media in {"shapefile", "unknown"}:
        return f"unsupported_media_type: {media}"
    if entry.get("hydro_matched") is False:
        return "hydro_unmatched"
    if raster is None:
        return "raster_missing"
    return None


def _georeference_entry(
    entry: dict,
    *,
    manifest_path: Path,
    polygons: dict[str, HydroWaterbody],
    multi_wbics: set[str],
    work_dir: Path,
    iou_threshold: float,
    gcp_count: int,
    gdal_available: bool,
) -> dict:
    wbic = str(entry["wbic"])
    map_key = str(entry["map_key"])
    base = {
        "wbic": wbic,
        "map_key": map_key,
        "name": entry.get("name"),
    }
    raster = _resolve_raster(entry, manifest_path)
    reason = _skip_reason(entry, raster)
    if reason:
        return {**base, "qa_status": "skipped", "skip_reason": reason, "reasons": [], "iou": None}

    water = polygons.get(wbic)
    if water is None:
        return {
            **base,
            "qa_status": "skipped",
            "skip_reason": "hydro_geometry_missing",
            "reasons": [],
            "iou": None,
        }

    multi_sheet = _entry_is_multi(entry, multi_wbics)
    flowage = is_flowage(
        water.hydrotype,
        water.waterbody_name,
        entry.get("name"),
        (entry.get("hydro") or {}).get("waterbody_name"),
    )
    shoreline = extract_shoreline(read_png(raster))
    out_dir = work_dir / "georef" / "lakes" / wbic / map_key
    if shoreline is None:
        reasons = _reasons(
            flowage=flowage,
            low_iou=False,
            multi_sheet=multi_sheet,
            shoreline_found=False,
            warp_failed=False,
        )
        return {
            **base,
            "qa_status": "needs_review",
            "iou": None,
            "reasons": reasons,
            "hydrotype": water.hydrotype,
            "waterbody_name": water.waterbody_name,
            "multi_sheet": multi_sheet,
            "flowage": flowage,
            "warp": {"tool": "gdalwarp", "status": "not_run"},
        }

    registration = register_shoreline(shoreline, list(water.ring), gcp_count=gcp_count)
    iou = registration.iou
    low_iou = iou < iou_threshold
    gcp_dicts = [gcp.as_dict() for gcp in registration.gcps]
    out_dir.mkdir(parents=True, exist_ok=True)
    gcp_path = out_dir / "gcps.json"
    shore_path = out_dir / "shoreline.geojson"
    _write_json(
        gcp_path,
        {"crs": WI_CRS, "wbic": wbic, "map_key": map_key, "gcps": gcp_dicts},
    )
    mapped = [registration.tps.apply(col, row) for col, row in shoreline]
    _write_json(
        shore_path,
        _shoreline_collection(
            mapped,
            {
                "wbic": wbic,
                "map_key": map_key,
                "iou": round(iou, 6),
            },
        ),
    )
    warp_status, warped_path, warp_error = _warp(
        raster=raster,
        out_dir=out_dir,
        gcps=gcp_dicts,
        gdal_available=gdal_available,
    )
    reasons = _reasons(
        flowage=flowage,
        low_iou=low_iou,
        multi_sheet=multi_sheet,
        shoreline_found=True,
        warp_failed=warp_status == "failed",
    )
    return {
        **base,
        "qa_status": "needs_review" if reasons else "auto",
        "iou": round(iou, 6),
        "affine_iou": round(registration.affine_iou, 6),
        "tps_iou": round(registration.tps_iou, 6),
        "reasons": reasons,
        "affine": registration.affine.as_dict(),
        "gcp_count": len(gcp_dicts),
        "hydrotype": water.hydrotype,
        "waterbody_name": water.waterbody_name,
        "multi_sheet": multi_sheet,
        "flowage": flowage,
        "paths": {
            "gcps": str(gcp_path),
            "shoreline": str(shore_path),
            "warped": str(warped_path) if warped_path else None,
        },
        "warp": {
            "tool": "gdalwarp",
            "status": warp_status,
            "resample": "near",
            "method": "tps",
            "crs": WI_CRS,
            **({"error": warp_error} if warp_error else {}),
        },
    }


def _reasons(
    *,
    flowage: bool,
    low_iou: bool,
    multi_sheet: bool,
    shoreline_found: bool,
    warp_failed: bool,
) -> list[str]:
    found = []
    if flowage:
        found.append("flowage")
    if not shoreline_found:
        found.append("shoreline_not_found")
    elif low_iou:
        found.append("iou_below_threshold")
    if multi_sheet:
        found.append("multi_sheet")
    if warp_failed:
        found.append("warp_failed")
    order = {name: index for index, name in enumerate(_REASON_ORDER)}
    return sorted(found, key=lambda name: order[name])


def _warp(
    *,
    raster: Path,
    out_dir: Path,
    gcps: list[dict],
    gdal_available: bool,
) -> tuple[str, Path | None, str | None]:
    if not gdal_available:
        return "unavailable", None, None
    vrt_path = out_dir / "gcps.vrt"
    tif_path = out_dir / "warped.tif"
    translate = gdal_translate_gcp_args(str(raster), str(vrt_path), gcps)
    warp = gdalwarp_tps_args(str(vrt_path), str(tif_path))
    try:
        _run(translate)
        _run(warp)
    except RuntimeError as error:
        return "failed", None, str(error)[:500]
    return "ok", tif_path, None


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"{command[0]} failed: {detail}")


def _review_items(items: list[dict]) -> list[dict]:
    queued = []
    for item in items:
        if item.get("qa_status") == "skipped" or not item.get("reasons"):
            continue
        review = {
            "wbic": item["wbic"],
            "map_key": item["map_key"],
            "name": item.get("name"),
            "reasons": item["reasons"],
            "iou": item.get("iou"),
            "hydrotype": item.get("hydrotype"),
            "waterbody_name": item.get("waterbody_name"),
        }
        gcp_path = (item.get("paths") or {}).get("gcps")
        if gcp_path:
            review["gcp_path"] = gcp_path
        queued.append(review)
    queued.sort(key=lambda item: (item["wbic"], item["map_key"]))
    return queued


def _shoreline_collection(ring: list[tuple[float, float]], properties: dict) -> dict:
    closed = list(ring)
    if closed[0] != closed[-1]:
        closed.append(closed[0])
    return {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3071"}},
        "features": [
            {
                "type": "Feature",
                "properties": properties,
                "geometry": {"type": "Polygon", "coordinates": [[[x, y] for x, y in closed]]},
            }
        ],
    }


def _write_json(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


def _num(value: float) -> str:
    return format(float(value), ".8f")


def _prepare_fixture_inputs(work_dir: Path) -> tuple[Path, Path]:
    return synthetic.materialize_fixture(work_dir / "fixture-input")


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    work_dir: Path = args.work_dir
    if args.fixtures:
        if args.manifest or args.hydro:
            raise SystemExit("--fixtures does not take --manifest or --hydro")
        manifest_path, hydro_path = _prepare_fixture_inputs(work_dir)
    else:
        manifest_path = args.manifest or (work_dir / "manifest.json")
        hydro_path = args.hydro
        if hydro_path is None:
            raise SystemExit("--hydro is required unless --fixtures")
        if not manifest_path.is_file():
            raise SystemExit(f"manifest not found: {manifest_path}")
        if not hydro_path.is_file():
            raise SystemExit(f"hydro file not found: {hydro_path}")
    try:
        run_georef(
            manifest_path=manifest_path,
            hydro_path=hydro_path,
            work_dir=work_dir,
            iou_threshold=args.iou_threshold,
            gcp_count=args.gcp_count,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main(sys.argv[1:])
