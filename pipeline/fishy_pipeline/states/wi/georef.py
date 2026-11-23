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
``georef/review_queue.json``. A multi-sheet lake publishes only when every
sheet passes. A flowage is queued even when its hydro polygon is missing. One
registration failure is queued for that lake and the rest of the batch still
runs.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from fishy_pipeline.states.wi import synthetic
from fishy_pipeline.states.wi.constants import DEFAULT_IOU_THRESHOLD  # re-exported for CLI defaults
from fishy_pipeline.states.wi.hydro import FLOWAGE_HYDROTYPE, HydroWaterbody, load_hydro_polygons
from fishy_pipeline.states.wi.manifest import read_manifest
from fishy_pipeline.states.wi.pngutil import read_png
from fishy_pipeline.states.wi.register import DEFAULT_GCP_COUNT, register_shoreline, union_polygon_iou
from fishy_pipeline.states.wi.pilot_data import load_reviewed_gcps
from fishy_pipeline.states.wi.raster import select_sheet_pages
from fishy_pipeline.states.wi.register import GroundControlPoint, registration_from_reviewed
from fishy_pipeline.states.wi.shoreline import MIN_FIGURE_SPAN, extract_shoreline, extract_shoreline_figures
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

    primary = max(sheets, key=lambda sheet: sheet["registration"].iou)
    registration = primary["registration"]
    shoreline = primary["ring"]
    chosen_raster = primary["page"]
    sheet_ious = [sheet["registration"].iou for sheet in sheets]
    # One raster publishes at its own overlap. Several sheets publish only when
    # the weakest sheet still clears the gate. A large page with no shoreline
    # is a missed sheet and fails the lake.
    gate_iou = min(sheet_ious) if len(sheets) > 1 else registration.iou
    if missed_pages:
        gate_iou = 0.0
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
    warp_status, warped_path, warp_error = _warp(
        raster=chosen_raster,
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
    public_sheets = [_public_sheet(sheet) for sheet in sheets]
    for page in missed_pages:
        public_sheets.append(
            {"page": page.name, "iou": 0.0, "rotation_deg": None, "cut_fraction": None, "status": "no_shoreline"}
        )
    sheet_rings = []
    for sheet in sheets:
        warp = sheet["registration"]
        project_sheet = warp.affine.apply if warp.method == "affine" else warp.tps.apply
        sheet_rings.append([project_sheet(col, row) for col, row in sheet["ring"]])
    combined = union_polygon_iou(sheet_rings, list(water.ring)) if len(sheet_rings) > 1 else None
    return {
        **base,
        "qa_status": "needs_review" if reasons else "auto",
        "iou": iou,
        "affine_iou": affine_iou,
        "tps_iou": tps_iou,
        "rotation_deg": round(registration.rotation_deg, 3),
        "sheet_count": len(public_sheets),
        "sheets": public_sheets,
        **({"combined_iou": round(combined, 6)} if combined is not None else {}),
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
        best: dict | None = None
        for figure in extract_shoreline_figures(image):
            if len(figure.ring) < 12:
                continue
            if _span_fraction(figure.ring, image.width, image.height) < max(MIN_FIGURE_SPAN, 0.18):
                continue
            area = abs(_polygon_area(figure.ring))
            if area < 0.03 * image_area or area > 0.72 * image_area:
                continue
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
            candidate = {
                "page": page_raster,
                "registration": registration,
                "ring": list(figure.ring),
                "area": area,
                "cut_fraction": _flag_fraction(figure.on_frame),
            }
            if best is None or _prefer_sheet(candidate, best):
                best = candidate
        if best is not None:
            sheets.append(best)
    return sheets, page_error


def _select_lake_sheets(
    sheets: list[dict],
    page_rasters: list[Path],
) -> tuple[list[dict], list[Path]]:
    """Keep every cut sheet, or the single best closed sheet when one covers the lake.

    A closed shore is a full lake (or a second copy of it). Extra pages are
    notes or another edition and are not required. A cut shore is one piece of
    a multi-sheet lake, so every other large page must score too.
    """
    if not sheets:
        return [], []
    closed = [sheet for sheet in sheets if sheet["cut_fraction"] < _PARTIAL_CUT]
    best_iou = max(sheet["registration"].iou for sheet in sheets)
    if closed and max(sheet["registration"].iou for sheet in closed) >= best_iou - 0.02:
        best = max(closed, key=lambda sheet: sheet["registration"].iou)
        return [best], []
    scored = {sheet["page"] for sheet in sheets}
    missed = [page for page in page_rasters if page not in scored]
    return sheets, missed


def _prefer_sheet(new: dict, old: dict) -> bool:
    """The figure with the higher overlap wins. Area is only a tie break."""
    new_iou = new["registration"].iou
    old_iou = old["registration"].iou
    if abs(new_iou - old_iou) <= 1e-6:
        return new["area"] > old["area"]
    return new_iou > old_iou


def _public_sheet(sheet: dict) -> dict:
    registration = sheet["registration"]
    return {
        "page": sheet["page"].name,
        "iou": round(registration.iou, 6),
        "rotation_deg": round(registration.rotation_deg, 3),
        "cut_fraction": round(sheet["cut_fraction"], 4),
    }


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
    # A multi-sheet lake is queued later, and only when one of its sheets misses
    # the gate. The flag stays on the item so that pass can find the group.
    _ = multi_sheet
    if warp_failed:
        found.append("warp_failed")
    order = {name: index for index, name in enumerate(_REASON_ORDER)}
    return sorted(found, key=lambda name: order[name])


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
) -> tuple[str, Path | None, str | None]:
    if not gdal_available:
        return "unavailable", None, None
    tif_path = out_dir / "warped.tif"
    try:
        if method == "affine" and affine is not None:
            staged = out_dir / "affine.tif"
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
            vrt_path = out_dir / "gcps.vrt"
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
