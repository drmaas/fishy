# SPDX-License-Identifier: GPL-3.0-only
"""
Georeference Wisconsin bathymetry rasters onto 24K Hydro shorelines.

Each manifest row is one ``(wbic, map_key)`` pair. ``map_key`` names the scanned
document and may be shared by many lakes (Chain O' Lakes is one PDF and many
WBICs). The fit for a pair uses that WBIC's own shoreline. Rows ingest skipped
(shapefile, unknown, and any other ``skip_reason``) stay skipped.

Registration sweeps continuous rotation, then affine point registration, then a
thin-plate spline. A sheet that shows only part of the lake is matched to that
portion; page-edge vertices are not treated as shore. When ``gdal_translate``
and ``gdalwarp`` are on ``PATH``, the spline is applied with
``gdalwarp -tps -dstalpha`` in EPSG:3071. Bathymetry ink is black (value 0).
An alpha band marks the valid sheet so a later clip does not treat that ink as
nodata. The IoU score does not require GDAL.

IoU under the default threshold (see ``constants.DEFAULT_IOU_THRESHOLD``), a map
shared by more than one WBIC, a multi-sheet lake whose sheets do not all clear
the gate, or a flowage (HYDROTYPE 707, or "flowage" in the name) is written to
``georef/review_queue.json``. The publish gate for a lake is
``min(per-sheet gate_iou)``; partial sheets use ``min(registration IoU,
full_hydro IoU)``. A stitched ``combined_iou`` may be recorded for diagnostics
but never clears the gate or relaxes ``multi_sheet`` queueing. A multi-sheet lake
publishes only when every sheet passes. A flowage is queued even when its hydro
polygon is missing. One registration failure is queued for that lake and the
rest of the batch still runs.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

from fishy_pipeline.states.wi import synthetic
from fishy_pipeline.states.wi.constants import (  # re-exported for CLI defaults
    DEFAULT_IOU_THRESHOLD,
    NEAR_GATE_IOU,
)
from fishy_pipeline.states.wi.hydro import FLOWAGE_HYDROTYPE, HydroWaterbody, load_hydro_polygons
from fishy_pipeline.states.wi.manifest import read_manifest
from fishy_pipeline.states.wi.pngutil import read_png
from fishy_pipeline.states.wi.register import (
    DEFAULT_GCP_COUNT,
    polygon_iou,
    register_shoreline,
    union_polygon_iou,
)
from fishy_pipeline.states.wi.pilot_data import load_reviewed_gcps
from fishy_pipeline.states.wi.raster import MIN_DPI, select_sheet_pages
from fishy_pipeline.states.wi.register import GroundControlPoint, registration_from_reviewed
from fishy_pipeline.states.wi.shoreline import (
    MAX_ROAD_STRAIGHTNESS,
    MIN_FIGURE_SPAN,
    NEATLINE_FRAME_BOUNDARY,
    NEATLINE_MIN_STRAIGHTNESS,
    NEATLINE_SPAN,
    extract_shoreline,
    extract_shoreline_figures,
    interior_ink_fraction,
    _ring_straightness,
)
from fishy_pipeline.states.wi.wbic import normalize_wbic

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_WORK = REPO_ROOT / "pipeline" / "work" / "wi"
WI_CRS = "EPSG:3071"
_REASON_ORDER = (
    "flowage",
    "shared_sheet",
    "iou_below_threshold",
    "multi_sheet",
    "shoreline_not_found",
    "registration_error",
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


def gdal_translate_affine_args(
    source: str,
    dest_tif: str,
    affine: dict[str, float],
    srs: str = WI_CRS,
) -> list[str]:
    """Place the whole sheet with the fitted affine. No spline resampling.

    Geotransform is ``easting = tx + a*col + b*row``, ``northing = ty + c*col + d*row``.
    """
    return [
        "gdal_translate",
        "-a_srs",
        srs,
        "-a_gt",
        _num(affine["tx"]),
        _num(affine["a"]),
        _num(affine["b"]),
        _num(affine["ty"]),
        _num(affine["c"]),
        _num(affine["d"]),
        source,
        dest_tif,
    ]


def gdalwarp_tps_args(source_vrt: str, dest_tif: str, t_srs: str = WI_CRS) -> list[str]:
    """Warp with a thin-plate spline and an alpha mask.

    ``-dstnodata 0`` would mark the black ink as nodata. ``-dstalpha`` leaves
    the ink at 0 and records validity in the alpha band, so a hydro clip keeps
    the bathymetry lines.
    """
    return [
        "gdalwarp",
        "-tps",
        "-t_srs",
        t_srs,
        "-r",
        "near",
        "-dstalpha",
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
    shared_map_keys = _shared_map_keys(entries)
    tools = gdal_tools_available()
    items: list[dict] = []
    for entry in entries:
        items.append(
            _georeference_entry(
                entry,
                manifest_path=manifest_path,
                polygons=polygons,
                multi_wbics=multi_wbics,
                shared_map_keys=shared_map_keys,
                work_dir=work_dir,
                iou_threshold=iou_threshold,
                gcp_count=gcp_count,
                gdal_available=tools,
            )
        )
    _fail_closed_multi_sheet(items, polygons, iou_threshold)
    for item in items:
        item.pop("_sheet_rings", None)
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


def _is_georef_candidate(entry: dict) -> bool:
    """True for a raster row that is not an ingest skip."""
    if entry.get("skip_reason") or entry.get("raster_skip_reason"):
        return False
    if entry.get("source_media_type") in {"shapefile", "unknown"}:
        return False
    return normalize_wbic(entry.get("wbic")) is not None


def _shared_map_keys(entries: list[dict]) -> set[str]:
    """Map keys used by more than one WBIC.

    One extracted ring, scaled to each lake's own box, can score a similar
    smaller basin as a match. Those rows go to the review queue.
    """
    wbics_by_key: dict[str, set[str]] = {}
    for entry in entries:
        if not _is_georef_candidate(entry):
            continue
        wbic = normalize_wbic(entry.get("wbic"))
        if wbic is None:
            continue
        wbics_by_key.setdefault(str(entry["map_key"]), set()).add(wbic)
    return {map_key for map_key, wbics in wbics_by_key.items() if len(wbics) > 1}


def _multi_sheet_wbics(entries: list[dict]) -> set[str]:
    keys: dict[str, set[str]] = {}
    for entry in entries:
        if not _is_georef_candidate(entry):
            continue
        wbic = normalize_wbic(entry.get("wbic"))
        if wbic is None:
            continue
        keys.setdefault(wbic, set()).add(str(entry["map_key"]))
    return {wbic for wbic, map_keys in keys.items() if len(map_keys) > 1}


def _entry_is_multi(entry: dict, multi_wbics: set[str], wbic: str) -> bool:
    if entry.get("multi_sheet") is True:
        return True
    return wbic in multi_wbics


def _entry_hydrotype(entry: dict, water: HydroWaterbody | None) -> int | None:
    if water is not None and water.hydrotype is not None:
        return water.hydrotype
    hydro = entry.get("hydro") or {}
    value = hydro.get("hydrotype")
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _entry_waterbody_name(entry: dict, water: HydroWaterbody | None) -> str | None:
    if water is not None and water.waterbody_name:
        return water.waterbody_name
    hydro = entry.get("hydro") or {}
    name = hydro.get("waterbody_name") or entry.get("name")
    return str(name) if name else None


def _entry_is_flowage(entry: dict, water: HydroWaterbody | None) -> bool:
    hydro = entry.get("hydro") or {}
    return is_flowage(
        _entry_hydrotype(entry, water),
        None if water is None else water.waterbody_name,
        entry.get("name"),
        hydro.get("waterbody_name"),
    )


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


def _maybe_keep_all_pages(
    sheets: list[dict],
    page_rasters: list[Path],
    hydro_ring: list[tuple[float, float]],
) -> list[dict]:
    """Keep one sheet per page when stitching raises overlap on the hydro polygon."""
    if len(page_rasters) <= 1 or len(sheets) <= 1:
        return sheets
    by_page: dict[Path, dict] = {}
    for sheet in sheets:
        current = by_page.get(sheet["page"])
        if current is None or _prefer_sheet(sheet, current):
            by_page[sheet["page"]] = sheet
    unique = list(by_page.values())
    if len(unique) < 2:
        return sheets
    singles = [sheet["registration"].iou for sheet in unique]
    mapped: list[list[tuple[float, float]]] = []
    for sheet in unique:
        warp = sheet["registration"]
        project = warp.affine.apply if warp.method == "affine" else warp.tps.apply
        mapped.append([project(col, row) for col, row in sheet["ring"]])
    combined = union_polygon_iou(mapped, list(hydro_ring))
    if combined >= max(singles) + 0.02:
        return unique
    return sheets


def _georeference_entry(
    entry: dict,
    *,
    manifest_path: Path,
    polygons: dict[str, HydroWaterbody],
    multi_wbics: set[str],
    shared_map_keys: set[str],
    work_dir: Path,
    iou_threshold: float,
    gcp_count: int,
    gdal_available: bool,
) -> dict:
    raw_wbic = entry.get("wbic")
    wbic = normalize_wbic(raw_wbic)
    map_key = str(entry["map_key"])
    base = {
        "wbic": wbic if wbic is not None else ("" if raw_wbic is None else str(raw_wbic)),
        "map_key": map_key,
        "name": entry.get("name"),
    }
    if wbic is None:
        return {**base, "qa_status": "skipped", "skip_reason": "invalid_wbic", "reasons": [], "iou": None}

    raster = _resolve_raster(entry, manifest_path)
    reason = _skip_reason(entry, raster)
    if reason:
        return {**base, "qa_status": "skipped", "skip_reason": reason, "reasons": [], "iou": None}
    assert raster is not None

    water = polygons.get(wbic)
    shared_sheet = map_key in shared_map_keys
    multi_sheet = _entry_is_multi(entry, multi_wbics, wbic)
    flowage = _entry_is_flowage(entry, water)
    hydrotype = _entry_hydrotype(entry, water)
    waterbody_name = _entry_waterbody_name(entry, water)
    if water is None:
        if flowage:
            return _unscored(
                base,
                reasons=_reasons(
                    flowage=True,
                    shared_sheet=shared_sheet,
                    low_iou=False,
                    multi_sheet=multi_sheet,
                    shoreline_found=None,
                    registration_error=False,
                    warp_failed=False,
                ),
                hydrotype=hydrotype,
                waterbody_name=waterbody_name,
                multi_sheet=multi_sheet,
                flowage=True,
            )
        return {
            **base,
            "qa_status": "skipped",
            "skip_reason": "hydro_geometry_missing",
            "reasons": [],
            "iou": None,
        }

    reviewed = load_reviewed_gcps(wbic) if wbic else None
    if reviewed:
        reviewed_result = _georeference_reviewed(
            base,
            entry=entry,
            reviewed=reviewed,
            water=water,
            work_dir=work_dir,
            manifest_path=manifest_path,
            raster=raster,
            flowage=flowage,
            shared_sheet=shared_sheet,
            multi_sheet=multi_sheet,
            iou_threshold=iou_threshold,
            gdal_available=gdal_available,
            hydrotype=hydrotype,
            waterbody_name=waterbody_name,
        )
        if reviewed_result is not None:
            return reviewed_result

    page_rasters = _page_rasters(entry, raster)
    sheets, page_error = _score_sheet_pages(
        page_rasters,
        hydro_ring=list(water.ring),
        gcp_count=gcp_count,
    )
    sheets = _maybe_keep_all_pages(sheets, page_rasters, list(water.ring))
    sheets, missed_pages = _select_lake_sheets(sheets, page_rasters)
    out_dir = work_dir / "georef" / "lakes" / wbic / map_key
    if not sheets:
        return _unscored(
            base,
            reasons=_reasons(
                flowage=flowage,
                shared_sheet=shared_sheet,
                low_iou=False,
                multi_sheet=multi_sheet,
                shoreline_found=page_error is not None,
                registration_error=page_error is not None,
                warp_failed=False,
            ),
            hydrotype=hydrotype,
            waterbody_name=waterbody_name,
            multi_sheet=multi_sheet,
            flowage=flowage,
            error=page_error,
        )

    for sheet in sheets:
        full_iou = _full_hydro_iou(sheet, list(water.ring))
        sheet["full_iou"] = full_iou
        # A page-edge fragment can score well inside its own footprint and still
        # leave most of the lake uncovered. The gate uses the worse of the two.
        if sheet["cut_fraction"] >= _PARTIAL_CUT:
            sheet["gate_iou"] = min(sheet["registration"].iou, full_iou)
        else:
            sheet["gate_iou"] = sheet["registration"].iou
    sheets = _drop_embed_preview_sheets(sheets, _page_dpi_map(entry))
    primary = max(sheets, key=lambda sheet: sheet["gate_iou"])
    registration = primary["registration"]
    shoreline = primary["ring"]
    chosen_raster = primary["page"]
    # Several sheets publish only when the weakest sheet still clears the gate.
    # A large page with no shoreline is a missed sheet and fails the lake.
    combined_preview = None
    preview_rings: list[list[tuple[float, float]]] = []
    for sheet in sheets:
        warp = sheet["registration"]
        project_sheet = warp.affine.apply if warp.method == "affine" else warp.tps.apply
        preview_rings.append([project_sheet(col, row) for col, row in sheet["ring"]])
    if len(preview_rings) > 1:
        combined_preview = union_polygon_iou(preview_rings, list(water.ring))
    if missed_pages:
        gate_iou = 0.0
    else:
        gate_iou = min(sheet["gate_iou"] for sheet in sheets)
    several_pages_passed = sum(1 for sheet in sheets if sheet["gate_iou"] >= iou_threshold) > 1
    affine_iou = round(registration.affine_iou, 6)
    tps_iou = round(registration.tps_iou, 6)
    iou = round(gate_iou, 6)
    low_iou = gate_iou < iou_threshold
    gcp_dicts = [gcp.as_dict() for gcp in registration.gcps]
    out_dir.mkdir(parents=True, exist_ok=True)
    gcp_path = out_dir / "gcps.json"
    shore_path = out_dir / "shoreline.geojson"
    _write_json(
        gcp_path,
        {
            "crs": WI_CRS,
            "wbic": wbic,
            "map_key": map_key,
            "gcps": gcp_dicts,
            "affine": registration.affine.as_dict(),
            "method": registration.method,
            "source": "auto",
            "rotation_deg": round(registration.rotation_deg, 3),
        },
    )
    project = registration.affine.apply if registration.method == "affine" else registration.tps.apply
    mapped = [project(col, row) for col, row in shoreline]
    _write_json(
        shore_path,
        _shoreline_collection(
            mapped,
            {
                "wbic": wbic,
                "map_key": map_key,
                "iou": round(registration.iou, 6),
            },
        ),
    )
    warp_plan = _warp_plan_for(sheets, primary, iou_threshold)
    warp_rows = []
    warp_status, warped_path, warp_error = "not_run", None, None
    for sheet, tif_name in warp_plan:
        reg = sheet["registration"]
        status, path, error = _warp(
            raster=sheet["page"],
            out_dir=out_dir,
            gcps=[gcp.as_dict() for gcp in reg.gcps],
            gdal_available=gdal_available,
            method=reg.method,
            affine=reg.affine.as_dict(),
            tif_name=tif_name,
        )
        warp_rows.append({"page": sheet["page"].name, "status": status, "path": str(path) if path else None})
        if sheet is primary:
            warp_status, warped_path, warp_error = status, path, error
        elif status == "failed" and warp_status != "failed":
            warp_status, warp_error = status, error
    reasons = _reasons(
        flowage=flowage,
        shared_sheet=shared_sheet,
        low_iou=low_iou,
        multi_sheet=multi_sheet,
        shoreline_found=True,
        registration_error=False,
        warp_failed=warp_status == "failed",
        several_pages_passed=several_pages_passed,
    )
    public_sheets = [_public_sheet(sheet) for sheet in sheets]
    for page in missed_pages:
        public_sheets.append(
            {"page": page.name, "iou": 0.0, "rotation_deg": None, "cut_fraction": None, "status": "no_shoreline"}
        )
    sheet_rings = preview_rings
    combined = round(combined_preview, 6) if combined_preview is not None else None
    return {
        **base,
        "qa_status": "needs_review" if reasons else "auto",
        "iou": iou,
        "affine_iou": affine_iou,
        "tps_iou": tps_iou,
        "rotation_deg": round(registration.rotation_deg, 3),
        "sheet_count": len(public_sheets),
        "sheets": public_sheets,
        **({"combined_iou": combined} if combined is not None else {}),
        "reasons": reasons,
        "affine": registration.affine.as_dict(),
        "gcp_count": len(gcp_dicts),
        "hydrotype": hydrotype,
        "waterbody_name": waterbody_name,
        "multi_sheet": multi_sheet or len(public_sheets) > 1,
        "flowage": flowage,
        "warp_source": "auto",
        "_sheet_rings": sheet_rings,
        "paths": {
            "gcps": str(gcp_path),
            "shoreline": str(shore_path),
            "warped": str(warped_path) if warped_path else None,
            "warps": warp_rows,
        },
        "warp": {
            "tool": "gdal_translate" if registration.method == "affine" else "gdalwarp",
            "status": warp_status,
            "resample": "near",
            "method": registration.method,
            "page": chosen_raster.name,
            "crs": WI_CRS,
            **({"error": warp_error} if warp_error else {}),
        },
    }


def _georeference_reviewed(
    base: dict,
    *,
    entry: dict,
    reviewed: dict,
    water: HydroWaterbody,
    work_dir: Path,
    manifest_path: Path,
    raster: Path,
    flowage: bool,
    shared_sheet: bool,
    multi_sheet: bool,
    iou_threshold: float,
    gdal_available: bool,
    hydrotype: int | None,
    waterbody_name: str | None,
) -> dict | None:
    wbic = base["wbic"]
    map_key = base["map_key"]
    raw_gcps = reviewed.get("gcps")
    if not isinstance(raw_gcps, list) or len(raw_gcps) < 4:
        return None
    page_name = reviewed.get("page")
    chosen: Path | None = None
    for page in _page_rasters(entry, raster):
        if page_name is None or page.name == str(page_name):
            chosen = page
            break
    if chosen is None:
        return None
    shoreline = extract_shoreline(read_png(chosen))
    if shoreline is None:
        return None
    gcps = [
        GroundControlPoint(
            pixel=float(item["pixel"]),
            line=float(item["line"]),
            easting=float(item["easting"]),
            northing=float(item["northing"]),
        )
        for item in raw_gcps
    ]
    method = str(reviewed.get("method") or "tps")
    try:
        registration = registration_from_reviewed(shoreline, list(water.ring), gcps, method=method)
    except Exception as error:
        return _unscored(
            base,
            reasons=_reasons(
                flowage=flowage,
                shared_sheet=shared_sheet,
                low_iou=False,
                multi_sheet=multi_sheet,
                shoreline_found=True,
                registration_error=True,
                warp_failed=False,
            ),
            hydrotype=hydrotype,
            waterbody_name=waterbody_name,
            multi_sheet=multi_sheet,
            flowage=flowage,
            error=str(error)[:500],
        )
    gcp_dicts = [gcp.as_dict() for gcp in registration.gcps]
    iou = round(registration.iou, 6)
    affine_iou = round(registration.affine_iou, 6)
    tps_iou = round(registration.tps_iou, 6)
    low_iou = registration.iou < iou_threshold
    out_dir = work_dir / "georef" / "lakes" / wbic / map_key
    out_dir.mkdir(parents=True, exist_ok=True)
    gcp_path = out_dir / "gcps.json"
    shore_path = out_dir / "shoreline.geojson"
    _write_json(
        gcp_path,
        {
            "crs": WI_CRS,
            "wbic": wbic,
            "map_key": map_key,
            "gcps": gcp_dicts,
            "affine": registration.affine.as_dict(),
            "method": registration.method,
            "source": "reviewed",
        },
    )
    project = registration.affine.apply if registration.method == "affine" else registration.tps.apply
    mapped = [project(col, row) for col, row in shoreline]
    _write_json(
        shore_path,
        _shoreline_collection(mapped, {"wbic": wbic, "map_key": map_key, "iou": iou}),
    )
    warp_status, warped_path, warp_error = _warp(
        raster=chosen,
        out_dir=out_dir,
        gcps=gcp_dicts,
        gdal_available=gdal_available,
        method=registration.method,
        affine=registration.affine.as_dict(),
    )
    reasons = _reasons(
        flowage=flowage,
        shared_sheet=shared_sheet,
        low_iou=low_iou,
        multi_sheet=multi_sheet,
        shoreline_found=True,
        registration_error=False,
        warp_failed=warp_status == "failed",
    )
    return {
        **base,
        "qa_status": "needs_review" if reasons else "auto",
        "iou": iou,
        "affine_iou": affine_iou,
        "tps_iou": tps_iou,
        "reasons": reasons,
        "affine": registration.affine.as_dict(),
        "gcp_count": len(gcp_dicts),
        "hydrotype": hydrotype,
        "waterbody_name": waterbody_name,
        "multi_sheet": multi_sheet,
        "flowage": flowage,
        "warp_source": "reviewed",
        "paths": {
            "gcps": str(gcp_path),
            "shoreline": str(shore_path),
            "warped": str(warped_path) if warped_path else None,
        },
        "warp": {
            "tool": "gdal_translate" if registration.method == "affine" else "gdalwarp",
            "status": warp_status,
            "resample": "near",
            "method": registration.method,
            "page": chosen.name,
            "crs": WI_CRS,
            **({"error": warp_error} if warp_error else {}),
        },
    }


# A shore with at least this much of its boundary on the page edge is a partial sheet.
_PARTIAL_CUT = 0.08
# Figures below this interior ink share are not bathymetry enclosures (roads, neatline boxes).
MIN_BASIN_INK = 0.008
# After registration, mapped footprint span versus hydro span must stay in this band.
MIN_HYDRO_SPAN_RATIO = 0.38
MAX_HYDRO_SPAN_RATIO = 1.85
# Sheet-filling enclosures with modest ink are often roads plus neatline, not the lake.
LARGE_RING_SPAN = 0.72
LARGE_RING_MIN_INK = 0.12


def _score_sheet_pages(
    page_rasters: list[Path],
    *,
    hydro_ring: list[tuple[float, float]],
    gcp_count: int,
) -> tuple[list[dict], str | None]:
    """Best lake-scale figure on each page, or no entry when the page has none."""
    sheets: list[dict] = []
    page_error: str | None = None
    for page_raster in page_rasters:
        image = read_png(page_raster)
        image_area = float(image.width * image.height)
        candidates: list[dict] = []
        for figure in extract_shoreline_figures(image):
            if len(figure.ring) < 12:
                continue
            if _span_fraction(figure.ring, image.width, image.height) < max(MIN_FIGURE_SPAN, 0.18):
                continue
            cut_fraction = _flag_fraction(figure.on_frame)
            if _false_shore_ring(figure.ring, image.width, image.height, cut_fraction):
                continue
            area = abs(_polygon_area(figure.ring))
            if area < 0.03 * image_area or area > 0.72 * image_area:
                continue
            ink = interior_ink_fraction(image, figure.ring)
            try:
                registration = register_shoreline(
                    list(figure.ring),
                    list(hydro_ring),
                    gcp_count,
                    on_frame=list(figure.on_frame),
                )
            except Exception as error:
                page_error = str(error)[:500]
                continue
            if ink < MIN_BASIN_INK and registration.iou < NEAR_GATE_IOU:
                continue
            if not _hydro_footprint_matches(
                registration,
                list(figure.ring),
                hydro_ring,
                cut_fraction=cut_fraction,
            ):
                continue
            span = _span_fraction(list(figure.ring), image.width, image.height)
            straight = _ring_straightness(list(figure.ring))
            if (
                straight >= MAX_ROAD_STRAIGHTNESS
                and ink < 0.02
                and registration.iou < NEAR_GATE_IOU
            ):
                continue
            if (
                span > LARGE_RING_SPAN
                and ink < LARGE_RING_MIN_INK
                and registration.iou < NEAR_GATE_IOU
            ):
                continue
            candidates.append(
                {
                    "page": page_raster,
                    "registration": registration,
                    "ring": list(figure.ring),
                    "area": area,
                    "cut_fraction": cut_fraction,
                    "ink": ink,
                    "image": image,
                }
            )
        picked = _pick_page_basins(candidates, hydro_ring)
        for row in picked:
            sheets.append({key: row[key] for key in row if key not in {"image"}})
    return sheets, page_error


def _pick_page_basins(candidates: list[dict], hydro_ring: list[tuple[float, float]]) -> list[dict]:
    """Keep one or more disjoint basin figures when their union helps overlap."""
    if not candidates:
        return []
    ranked = sorted(candidates, key=lambda row: (-_figure_quality(row), -row["registration"].iou))
    anchor = ranked[0]
    kept = [anchor]
    if len(ranked) < 2:
        return kept

    def mapped_rings(rows: list[dict]) -> list[list[tuple[float, float]]]:
        rings: list[list[tuple[float, float]]] = []
        for row in rows:
            warp = row["registration"]
            project = warp.affine.apply if warp.method == "affine" else warp.tps.apply
            rings.append([project(col, line) for col, line in row["ring"]])
        return rings

    anchor_ring = mapped_rings([anchor])[0]
    span = _centroid_span(anchor_ring)
    anchor_iou = anchor["registration"].iou
    for candidate in ranked[1:5]:
        dist = _centroid_distance(anchor_ring, mapped_rings([candidate])[0])
        if dist < 0.12 * span:
            continue
        candidate_ring = mapped_rings([candidate])[0]
        candidate_hydro = polygon_iou(candidate_ring, list(hydro_ring))
        # A second basin must land on this WBIC's hydro footprint, not only
        # nudge the union score upward.
        if candidate_hydro < 0.30:
            continue
        if candidate["registration"].iou < max(0.35, anchor_iou - 0.25):
            continue
        union = union_polygon_iou(mapped_rings([anchor, candidate]), list(hydro_ring))
        if union < candidate_hydro:
            continue
        gain = union >= anchor_iou + 0.05
        completes = anchor_iou >= NEAR_GATE_IOU and union >= anchor_iou
        if gain or completes:
            kept.append(candidate)
            break
    return kept


def _centroid_span(ring: list[tuple[float, float]]) -> float:
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    return max(max(xs) - min(xs), max(ys) - min(ys), 1.0)


def _centroid_distance(left: list[tuple[float, float]], right: list[tuple[float, float]]) -> float:
    lx = sum(point[0] for point in left) / len(left)
    ly = sum(point[1] for point in left) / len(left)
    rx = sum(point[0] for point in right) / len(right)
    ry = sum(point[1] for point in right) / len(right)
    return math.hypot(lx - rx, ly - ry)


def _select_lake_sheets(
    sheets: list[dict],
    page_rasters: list[Path],
) -> tuple[list[dict], list[Path]]:
    """Keep every page when any sheet is cut. Collapse only an all-closed set.

    A closed shore on every page is one lake drawn more than once. The best of
    those is enough. A cut or partial score on any page means the document does
    not show one complete shore, so every other large page stays in the gate,
    including a page that produced no shoreline.
    """
    if not sheets:
        return [], []
    partial = [sheet for sheet in sheets if sheet["cut_fraction"] >= _PARTIAL_CUT]
    if partial:
        scored = {sheet["page"] for sheet in sheets}
        missed = [page for page in page_rasters if page not in scored]
        return list(sheets), missed
    if len(page_rasters) <= 1 or len(sheets) <= 1:
        best = max(sheets, key=lambda sheet: sheet["registration"].iou)
        return [best], []
    by_page: dict[Path, dict] = {}
    for sheet in sheets:
        current = by_page.get(sheet["page"])
        if current is None or _prefer_sheet(sheet, current):
            by_page[sheet["page"]] = sheet
    unique = list(by_page.values())
    if _pages_complement_sheets(unique):
        missed = [page for page in page_rasters if page not in by_page]
        return unique, missed
    best = max(sheets, key=lambda sheet: sheet["registration"].iou)
    return [best], []


def _pages_complement_sheets(sheets: list[dict]) -> bool:
    """True when separate pages cover different pieces of the same lake."""
    pages = {sheet["page"] for sheet in sheets}
    if len(pages) < 2:
        return False
    mapped: list[list[tuple[float, float]]] = []
    for sheet in sheets:
        warp = sheet["registration"]
        project = warp.affine.apply if warp.method == "affine" else warp.tps.apply
        mapped.append([project(col, row) for col, row in sheet["ring"]])
    if len(mapped) < 2:
        return False
    span = max(_centroid_span(ring) for ring in mapped)
    centroids = [
        (
            sum(point[0] for point in ring) / len(ring),
            sum(point[1] for point in ring) / len(ring),
        )
        for ring in mapped
    ]
    distances = [
        math.hypot(centroids[index][0] - centroids[index + 1][0], centroids[index][1] - centroids[index + 1][1])
        for index in range(len(centroids) - 1)
    ]
    return max(distances) >= 0.18 * span


def _figure_quality(sheet: dict) -> float:
    """Favor real lake ink over long road-like enclosures with weak overlap."""
    iou = sheet["registration"].iou
    ink = float(sheet.get("ink", 0.0))
    return iou * (0.15 + min(ink, 0.40))


def _false_shore_ring(
    ring: tuple[tuple[float, float], ...] | list[tuple[float, float]],
    width: int,
    height: int,
    cut_fraction: float,
) -> bool:
    span = _span_fraction(list(ring), width, height)
    if span < NEATLINE_SPAN or cut_fraction < NEATLINE_FRAME_BOUNDARY:
        return False
    if _ring_straightness(list(ring)) >= NEATLINE_MIN_STRAIGHTNESS:
        return True
    return span >= 0.92 and cut_fraction >= 0.45


def _hydro_footprint_matches(
    registration,
    pixel_ring: list[tuple[float, float]],
    hydro_ring: list[tuple[float, float]],
    *,
    cut_fraction: float = 0.0,
) -> bool:
    if registration.iou >= 0.68:
        return True
    project = registration.affine.apply if registration.method == "affine" else registration.tps.apply
    mapped = [project(col, row) for col, row in pixel_ring]
    hydro_span = _centroid_span(list(hydro_ring))
    mapped_span = _centroid_span(mapped)
    if hydro_span <= 0:
        return True
    ratio = mapped_span / hydro_span
    min_ratio = 0.22 if cut_fraction >= _PARTIAL_CUT else MIN_HYDRO_SPAN_RATIO
    return min_ratio <= ratio <= MAX_HYDRO_SPAN_RATIO


def _prefer_sheet(new: dict, old: dict) -> bool:
    """Overlap counts most. Ink density only reranks sub-threshold candidates."""
    new_iou = new["registration"].iou
    old_iou = old["registration"].iou
    if new_iou >= NEAR_GATE_IOU or old_iou >= NEAR_GATE_IOU:
        if abs(new_iou - old_iou) > 0.01:
            return new_iou > old_iou
        return new.get("ink", 0.0) >= old.get("ink", 0.0)
    return _figure_quality(new) > _figure_quality(old)


def _public_sheet(sheet: dict) -> dict:
    registration = sheet["registration"]
    row = {
        "page": sheet["page"].name,
        "iou": round(sheet.get("gate_iou", registration.iou), 6),
        "footprint_iou": round(registration.iou, 6),
        "rotation_deg": round(registration.rotation_deg, 3),
        "cut_fraction": round(sheet["cut_fraction"], 4),
    }
    if "full_iou" in sheet:
        row["full_iou"] = round(sheet["full_iou"], 6)
    return row


def _full_hydro_iou(sheet: dict, hydro_ring: list[tuple[float, float]]) -> float:
    """Overlap of the mapped ring with the whole hydro polygon, not the footprint."""
    registration = sheet["registration"]
    if sheet["cut_fraction"] < _PARTIAL_CUT:
        return registration.iou
    project = registration.affine.apply if registration.method == "affine" else registration.tps.apply
    mapped = [project(col, row) for col, row in sheet["ring"]]
    return polygon_iou(mapped, list(hydro_ring))


def _warp_plan_for(sheets: list[dict], primary: dict, threshold: float) -> list[tuple[dict, str]]:
    """Warp the primary page and every other page whose gate IoU clears ``threshold``."""
    plan = [(primary, "warped.tif")]
    for sheet in sheets:
        if sheet is primary:
            continue
        if sheet["gate_iou"] >= threshold:
            plan.append((sheet, f"warped-{sheet['page'].stem}.tif"))
    return plan


def _fail_closed_multi_sheet(
    items: list[dict],
    polygons: dict[str, HydroWaterbody],
    threshold: float,
) -> None:
    """Queue a multi-sheet lake unless every scored sheet clears the gate.

    Shared sheets are queued on their own reason and are left alone here.
    """
    groups: dict[str, list[dict]] = {}
    for item in items:
        if item.get("qa_status") == "skipped":
            continue
        groups.setdefault(str(item["wbic"]), []).append(item)
    for wbic, group in groups.items():
        declared = any(bool(item.get("multi_sheet")) for item in group)
        sheet_rows = [row for item in group for row in (item.get("sheets") or [])]
        if not declared and len(sheet_rows) < 2 and len(group) < 2:
            continue
        if not declared and len(group) == 1 and len(sheet_rows) < 2:
            continue
        ious: list[float | None] = []
        for item in group:
            rows = item.get("sheets") or []
            if rows:
                ious.extend(row.get("iou") for row in rows)
            else:
                ious.append(item.get("iou"))
        # Catalog multi-sheet lakes name more than one map key. One scored key
        # is not the whole lake.
        incomplete = declared and len(group) > 1 and any(iou is None for iou in ious)
        weak = any(iou is None or float(iou) < threshold for iou in ious)
        short = declared and len([iou for iou in ious if iou is not None]) < 2 and len(group) < 2
        water = polygons.get(wbic)
        rings = [ring for item in group for ring in (item.get("_sheet_rings") or [])]
        combined = None
        if water is not None and len(rings) >= 2:
            combined = round(union_polygon_iou(rings, list(water.ring)), 6)
            for item in group:
                item["combined_iou"] = combined
        if not (weak or incomplete or short):
            continue
        for item in group:
            reasons = list(item.get("reasons") or [])
            if "multi_sheet" not in reasons:
                reasons.append("multi_sheet")
            item["reasons"] = _sort_reasons(reasons)
            if item.get("qa_status") != "skipped":
                item["qa_status"] = "needs_review"


def _sort_reasons(reasons: list[str]) -> list[str]:
    order = {name: index for index, name in enumerate(_REASON_ORDER)}
    return sorted(reasons, key=lambda name: order.get(name, len(order)))


def _span_fraction(ring: list[tuple[float, float]], width: int, height: int) -> float:
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    long_side = max(width, height, 1)
    return max(max(xs) - min(xs), max(ys) - min(ys)) / long_side


def _polygon_area(ring: list[tuple[float, float]]) -> float:
    area = 0.0
    count = len(ring)
    for index, (x1, y1) in enumerate(ring):
        x2, y2 = ring[(index + 1) % count]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def _flag_fraction(flags: tuple[bool, ...] | list[bool]) -> float:
    if not flags:
        return 0.0
    return sum(1 for flag in flags if flag) / len(flags)


def _unscored(
    base: dict,
    *,
    reasons: list[str],
    hydrotype: int | None,
    waterbody_name: str | None,
    multi_sheet: bool,
    flowage: bool,
    error: str | None = None,
) -> dict:
    item = {
        **base,
        "qa_status": "needs_review",
        "iou": None,
        "reasons": reasons,
        "hydrotype": hydrotype,
        "waterbody_name": waterbody_name,
        "multi_sheet": multi_sheet,
        "flowage": flowage,
        "warp": {"tool": "gdalwarp", "status": "not_run"},
    }
    if error:
        item["error"] = error
    return item


def _reasons(
    *,
    flowage: bool,
    shared_sheet: bool,
    low_iou: bool,
    multi_sheet: bool,
    shoreline_found: bool | None,
    registration_error: bool,
    warp_failed: bool,
    several_pages_passed: bool = False,
) -> list[str]:
    found = []
    if flowage:
        found.append("flowage")
    if shared_sheet:
        found.append("shared_sheet")
    if registration_error:
        found.append("registration_error")
    elif shoreline_found is False:
        found.append("shoreline_not_found")
    elif shoreline_found and low_iou:
        found.append("iou_below_threshold")
    # Catalog multi-sheet groups are queued later, and only when a sheet misses
    # the gate. Several pages of this one document that each cleared the gate
    # are queued here: contours trace a single warped raster.
    if several_pages_passed:
        found.append("multi_sheet")
    _ = multi_sheet
    if warp_failed:
        found.append("warp_failed")
    order = {name: index for index, name in enumerate(_REASON_ORDER)}
    return sorted(found, key=lambda name: order[name])


def _page_dpi_map(entry: dict) -> dict[Path, float]:
    result: dict[Path, float] = {}
    for item in entry.get("raster_pages") or []:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        result[Path(str(item["path"]))] = float(item.get("dpi") or 0.0)
    return result


def _drop_embed_preview_sheets(sheets: list[dict], dpi_map: dict[Path, float]) -> list[dict]:
    """Low-dpi embed previews should not drag down a sharp map page on the same PDF."""
    if len(sheets) < 2:
        return sheets
    sharp_scores = [
        sheet["gate_iou"]
        for sheet in sheets
        if dpi_map.get(sheet["page"], 0.0) >= MIN_DPI
    ]
    if not sharp_scores:
        return sheets
    anchor = max(sharp_scores)
    if anchor < 0.55:
        return sheets
    kept = [
        sheet
        for sheet in sheets
        if dpi_map.get(sheet["page"], 0.0) >= MIN_DPI or sheet["gate_iou"] >= anchor - 0.04
    ]
    return kept if kept else sheets


def _page_rasters(entry: dict, raster: Path) -> list[Path]:
    """Map-sheet pages for this row. A single raster stays a one-item list."""
    raw_pages = entry.get("raster_pages")
    pages: list[Path] = []
    if isinstance(raw_pages, list):
        described = [item for item in raw_pages if isinstance(item, dict)]
        bare = [item for item in raw_pages if not isinstance(item, dict)]
        for item in select_sheet_pages(described):
            path = item.get("path")
            if path and Path(str(path)).is_file():
                pages.append(Path(str(path)))
        for item in bare:
            if item and Path(str(item)).is_file():
                pages.append(Path(str(item)))
    if not pages:
        pages = [raster]
    return pages


def _warp(
    *,
    raster: Path,
    out_dir: Path,
    gcps: list[dict],
    gdal_available: bool,
    method: str = "tps",
    affine: dict[str, float] | None = None,
    tif_name: str = "warped.tif",
) -> tuple[str, Path | None, str | None]:
    if not gdal_available:
        return "unavailable", None, None
    tif_path = out_dir / tif_name
    try:
        if method == "affine" and affine is not None:
            staged = out_dir / f"{Path(tif_name).stem}-affine.tif"
            _run(gdal_translate_affine_args(str(raster), str(staged), affine))
            # -dstalpha matches the spline warp: black ink stays a color, not nodata.
            _run(
                [
                    "gdalwarp",
                    "-t_srs",
                    WI_CRS,
                    "-r",
                    "near",
                    "-dstalpha",
                    "-overwrite",
                    str(staged),
                    str(tif_path),
                ]
            )
        else:
            vrt_path = out_dir / f"{Path(tif_name).stem}.vrt"
            _run(gdal_translate_gcp_args(str(raster), str(vrt_path), gcps))
            _run(gdalwarp_tps_args(str(vrt_path), str(tif_path)))
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
        if item.get("error"):
            review["error"] = item["error"]
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
