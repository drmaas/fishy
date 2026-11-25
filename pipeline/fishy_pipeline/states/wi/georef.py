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
the gate, several pages whose stitch could not be validated, or a flowage
(HYDROTYPE 707, or "flowage" in the name) is written to
``georef/review_queue.json``. The publish gate for a lake is
``min(per-sheet gate_iou)``; partial sheets use ``min(registration IoU,
full_hydro IoU)``. A stitched ``combined_iou`` is recorded for diagnostics.
It does not clear the gate and it does not publish a sheet that missed the
threshold. A multi-page document publishes when every kept sheet clears the
threshold and each sheet's published warp places its ring on the hydro polygon
at that threshold. The published score stays the minimum per-sheet gate IoU.
A missed page, a sheet under the threshold, or a placed ring that misses the
hydro polygon keeps the ``multi_sheet`` queue. A flowage is queued even when
its hydro polygon is missing. One registration failure is queued for that lake
and the rest of the batch still runs.
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
    GroundControlPoint,
    polygon_iou,
    prepare_control_points,
    register_shoreline,
    union_polygon_iou,
)
from fishy_pipeline.states.wi.pilot_data import load_reviewed_gcps
from fishy_pipeline.states.wi.raster import LETTER_LONG_INCHES, select_sheet_pages
from fishy_pipeline.states.wi.register import registration_from_reviewed
from fishy_pipeline.states.wi.roads import (
    fetch_road_centerlines,
    project_roads_to_pixels,
    search_pixels_for_affine,
)
from fishy_pipeline.states.wi.shoreline import (
    MAX_ROAD_STRAIGHTNESS,
    MIN_FIGURE_SPAN,
    NEATLINE_FRAME_BOUNDARY,
    NEATLINE_MIN_STRAIGHTNESS,
    NEATLINE_SPAN,
    cleanup_shore_ring,
    clear_cartographic_lines,
    clear_projected_casings,
    cleared_hits_basin_seal,
    extract_shoreline,
    extract_shoreline_figures,
    interior_ink_fraction,
    _clear_cased_roads,
    _dark_mask,
    _ring_boxiness,
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
    "degenerate_gcps",
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
    fetch_roads: bool = False,
) -> dict:
    """Register every manifest row and write results plus the review queue.

    ``fetch_roads`` loads Wisconsin DNR road centerlines and clears cased ink
    after the affine. The synthetic fixture leaves it off so tests stay offline.
    """
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
                fetch_roads=fetch_roads,
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
    fetch_roads: bool = False,
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
    hydro_ring = list(water.ring)
    road_lines: list[list[tuple[float, float]]] = []
    if fetch_roads:
        try:
            road_lines = fetch_road_centerlines(hydro_ring)
        except (OSError, TimeoutError, ValueError):
            road_lines = []
    sheets, page_error = _score_sheet_pages(
        page_rasters,
        hydro_ring=hydro_ring,
        gcp_count=gcp_count,
        roads=road_lines,
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
        full_iou = _full_hydro_iou(sheet, hydro_ring)
        sheet["full_iou"] = full_iou
        # A page-edge fragment can score well inside its own footprint and still
        # leave most of the lake uncovered. The gate uses the worse of the two.
        if sheet["cut_fraction"] >= _PARTIAL_CUT:
            sheet["gate_iou"] = min(sheet["registration"].iou, full_iou)
        else:
            sheet["gate_iou"] = sheet["registration"].iou
    # A low-resolution page stays in the minimum. DPI is not a duplicate test,
    # and dropping the scan is what used to lift a letter page across the gate.
    # The headline fit is the sheet that set that minimum.
    primary = _headline_sheet(sheets)
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
        combined_preview = union_polygon_iou(preview_rings, hydro_ring)
    # combined_preview stays diagnostic. The gate below does not read it.
    iou, queue_unvalidated_stitch = _stitch_gate(sheets, hydro_ring, missed_pages, iou_threshold)
    affine_iou = round(registration.affine_iou, 6)
    tps_iou = round(registration.tps_iou, 6)
    low_iou = iou < iou_threshold
    gcp_dicts = [gcp.as_dict() for gcp in registration.gcps]
    out_dir.mkdir(parents=True, exist_ok=True)
    gcp_path = out_dir / "gcps.json"
    shore_path = out_dir / "shoreline.geojson"
    _write_json(gcp_path, _gcp_body(wbic, map_key, registration, page=chosen_raster.name))
    project = registration.affine.apply if registration.method == "affine" else registration.tps.apply
    mapped = [project(col, row) for col, row in shoreline]
    _write_json(
        shore_path,
        _stitched_shoreline(
            sheets,
            {
                "wbic": wbic,
                "map_key": map_key,
                "iou": iou,
            },
            mapped,
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
        sheet_gcp = gcp_path if sheet is primary else out_dir / f"gcps-{sheet['page'].stem}.json"
        if sheet is not primary:
            _write_json(sheet_gcp, _gcp_body(wbic, map_key, reg, page=sheet["page"].name))
        warp_rows.append(
            {
                "page": sheet["page"].name,
                "status": status,
                "path": str(path) if path else None,
                "gcps": str(sheet_gcp),
            }
        )
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
        warp_failed=warp_status == "failed" and not _is_degenerate_gcp_error(warp_error),
        several_pages_passed=queue_unvalidated_stitch,
        degenerate_gcps=_is_degenerate_gcp_error(warp_error),
    )
    public_sheets = [_public_sheet(sheet) for sheet in sheets]
    for page in missed_pages:
        public_sheets.append(
            {"page": page.name, "iou": 0.0, "rotation_deg": None, "cut_fraction": None, "status": "no_shoreline"}
        )
    sheet_rings = preview_rings
    combined = round(combined_preview, 6) if combined_preview is not None else None
    road_mask = _road_mask_summary(sheets)
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
        **({"road_mask": road_mask} if road_mask is not None else {}),
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
        message = str(error)[:500]
        degenerate = _is_degenerate_gcp_error(message)
        return _unscored(
            base,
            reasons=_reasons(
                flowage=flowage,
                shared_sheet=shared_sheet,
                low_iou=False,
                multi_sheet=multi_sheet,
                shoreline_found=True,
                registration_error=not degenerate,
                warp_failed=False,
                degenerate_gcps=degenerate,
            ),
            hydrotype=hydrotype,
            waterbody_name=waterbody_name,
            multi_sheet=multi_sheet,
            flowage=flowage,
            error=message,
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
        warp_failed=warp_status == "failed" and not _is_degenerate_gcp_error(warp_error),
        degenerate_gcps=_is_degenerate_gcp_error(warp_error),
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
# Below the near-gate band, ink breaks ties this close. A wider gap is a
# different ring (shore versus road enclosure), so overlap wins.
_ANCHOR_IOU_BAND = 0.10
# A contoured basin is several times a blank section cell. The cell must not win.
_BASIN_AREA_MULTIPLE = 3.0
# Doclink PDFs append a letter citation page. Rendered at the request DPI it
# looks sharp, but it is a few lines of type, not a map sheet. A large sheet
# can be this light and still be the map.
_CITATION_DARK_FRACTION = 0.012
# A legend, scale, or north arrow is a small near-rectangle with almost no
# depth ink. An oval and a dammed basin are less rectangular, and a basin that
# covers more of the sheet is not a legend even when one side is a straight dam.
_LEGEND_MAX_INK = 0.03
_LEGEND_MAX_AREA = 0.08
_LEGEND_MIN_BOXINESS = 0.90
# A ring under this share of the page still takes the hydro span check.
# Nine percent of the page, and six percent with ink just over the legend cap,
# used to skip that check once overlap reached 0.68.
_SMALL_RING_AREA = 0.12
# Wiping a sheet grid re-fits the same shore by a few hundredths. On the
# synthetic fixture that lifts a deliberate mismatch from 0.719 to 0.758 and
# across the publish cutoff. 0.05 is a different shore, so a smaller move
# keeps the first registration.
_CARTO_MIN_GAIN = 0.05


def _score_sheet_pages(
    page_rasters: list[Path],
    *,
    hydro_ring: list[tuple[float, float]],
    gcp_count: int,
    roads: list[list[tuple[float, float]]] | None = None,
) -> tuple[list[dict], str | None]:
    """Best lake-scale figure on each page, or no entry when the page has none."""
    sheets: list[dict] = []
    page_error: str | None = None
    road_lines = roads or []
    for page_raster in page_rasters:
        image = read_png(page_raster)
        candidates, error = _collect_page_candidates(
            image,
            page_raster,
            hydro_ring,
            gcp_count,
        )
        if error is not None:
            page_error = error
        candidates.extend(
            _spur_cleaned_candidates(
                candidates,
                image=image,
                hydro_ring=hydro_ring,
                gcp_count=gcp_count,
                page=page_raster,
            )
        )
        candidates = _drop_margin_enclosures(candidates)
        picked = _pick_page_basins(candidates, hydro_ring)
        if road_lines and picked:
            picked = _apply_vector_road_mask(
                image,
                page_raster,
                hydro_ring,
                gcp_count,
                road_lines,
                picked,
            )
        if picked:
            picked = _apply_cartographic_mask(
                image,
                page_raster,
                hydro_ring,
                gcp_count,
                picked,
            )
        for row in picked:
            sheets.append({key: row[key] for key in row if key not in {"image"}})
    return sheets, page_error


def _collect_page_candidates(
    image,
    page: Path,
    hydro_ring: list[tuple[float, float]],
    gcp_count: int,
    mask: bytearray | None = None,
) -> tuple[list[dict], str | None]:
    """Score every figure. A lone blank oval stays; it does not beat an inked basin."""
    figures = extract_shoreline_figures(image, mask)
    page_error: str | None = None

    def consider(keep_margin: bool) -> list[dict]:
        nonlocal page_error
        found: list[dict] = []
        for figure in figures:
            try:
                row = _score_ring(
                    image,
                    list(figure.ring),
                    list(figure.on_frame),
                    hydro_ring=hydro_ring,
                    gcp_count=gcp_count,
                    page=page,
                    _keep_margin=keep_margin,
                )
            except Exception as error:
                page_error = str(error)[:500]
                continue
            if row is not None:
                found.append(row)
        return found

    candidates = consider(False)
    if not candidates:
        candidates = consider(True)
    return candidates, page_error


def _stitch_gate(
    sheets: list[dict],
    hydro_ring: list[tuple[float, float]],
    missed_pages: list,
    iou_threshold: float,
) -> tuple[float, bool]:
    """Published IoU and whether an unvalidated stitch queues the lake.

    ``combined_iou`` is not an argument. A missed page scores 0 and queues.
    A kept sheet under ``iou_threshold`` does not publish. Two rings that sit
    on the hydro at or above the threshold publish at the minimum sheet score.
    """
    if missed_pages or not sheets:
        gate_iou = 0.0
        stitch_score = None
    else:
        gate_iou = min(float(sheet["gate_iou"]) for sheet in sheets)
        stitch_score = _validated_stitch_score(sheets, hydro_ring, iou_threshold) if len(sheets) > 1 else None
    several_pages_passed = sum(1 for sheet in sheets if float(sheet.get("gate_iou", 0.0)) >= iou_threshold) > 1
    stitch_publishes = stitch_score is not None and stitch_score >= iou_threshold and not missed_pages
    queue_unvalidated_stitch = bool(missed_pages) or (several_pages_passed and not stitch_publishes)
    return round(gate_iou, 6), queue_unvalidated_stitch


def _gcp_body(wbic: str, map_key: str, registration, *, page: str) -> dict:
    return {
        "crs": WI_CRS,
        "wbic": wbic,
        "map_key": map_key,
        "page": page,
        "gcps": [gcp.as_dict() for gcp in registration.gcps],
        "affine": registration.affine.as_dict(),
        "method": registration.method,
        "source": "auto",
        "rotation_deg": round(registration.rotation_deg, 3),
    }


def _keep_road_trace(
    old_iou: float,
    new_iou: float,
    outline_hit: int,
    *,
    cleared_seal: bool = False,
) -> bool:
    """True when the road-cleared trace should replace the first registration.

    A gain of 0.002 is a different shore only when the clear did not remove
    the first ring's seal. Opening that seal can raise the overlap without
    removing a road. A drop no larger than 0.08 is kept when the clear hit
    the traced outline. A tie keeps the first fit.
    """
    if new_iou >= old_iou + 0.002 and not cleared_seal:
        return True
    if outline_hit >= 12 and new_iou + 0.08 >= old_iou and new_iou <= old_iou - 0.002:
        return True
    return False


def _apply_vector_road_mask(
    image,
    page: Path,
    hydro_ring: list[tuple[float, float]],
    gcp_count: int,
    roads: list[list[tuple[float, float]]],
    picked: list[dict],
) -> list[dict]:
    """Clear cased ink along roads placed by the affine, then re-score.

    The first trace supplies the affine. Roads are inverse-projected into the
    sheet and only a narrow ink pair beside a centerline is removed. The new
    trace replaces the page when its overlap moves by enough to be a different
    shore. An unchanged overlap keeps the first registration: a second fit with
    the same score can still stack two control points on one map location and
    fail the warp. A collapsed trace is recorded and not published.
    """
    anchor = max(picked, key=lambda row: float(row["registration"].iou))
    old_iou = float(anchor["registration"].iou)
    affine = anchor["registration"].affine
    try:
        polylines = project_roads_to_pixels(roads, affine, image.width, image.height)
        if not polylines:
            return picked
        mask = _dark_mask(image)
        _clear_cased_roads(mask, image.width, image.height)
        before = bytearray(mask)
        cleared = clear_projected_casings(
            mask,
            image.width,
            image.height,
            polylines,
            search=search_pixels_for_affine(affine),
        )
    except (OSError, ValueError, ZeroDivisionError):
        return picked
    if cleared <= 0:
        return picked
    outline_hit = _ring_pixels_cleared(before, mask, image.width, image.height, anchor.get("ring") or [])
    cleared_seal = cleared_hits_basin_seal(before, mask, image.width, image.height)
    candidates, _error = _collect_page_candidates(
        image,
        page,
        hydro_ring,
        gcp_count,
        mask,
    )
    candidates.extend(
        _spur_cleaned_candidates(
            candidates,
            image=image,
            hydro_ring=hydro_ring,
            gcp_count=gcp_count,
            page=page,
        )
    )
    candidates = _drop_margin_enclosures(candidates)
    new_picked = _pick_page_basins(candidates, hydro_ring)
    new_iou = max((float(row["registration"].iou) for row in new_picked), default=None)
    accept = (
        new_iou is not None
        and bool(new_picked)
        and _keep_road_trace(old_iou, new_iou, outline_hit, cleared_seal=cleared_seal)
    )
    if not accept or new_iou is None:
        for row in picked:
            row["road_pixels_cleared"] = cleared
            row["road_mask_accepted"] = False
            row["road_iou_before"] = old_iou
            if new_iou is not None:
                row["road_iou_after"] = new_iou
        return picked
    for row in new_picked:
        row["road_pixels_cleared"] = cleared
        row["road_mask_accepted"] = True
        row["road_iou_before"] = old_iou
        row["road_iou_after"] = new_iou
    return new_picked


def _apply_cartographic_mask(
    image,
    page: Path,
    hydro_ring: list[tuple[float, float]],
    gcp_count: int,
    picked: list[dict],
) -> list[dict]:
    """Clear grid lines, then keep the new shore only when overlap rises.

    The first registration stays when the clear opens an irregular basin or
    the weakest kept ring rises by less than ``_CARTO_MIN_GAIN``. A second
    ring that scores well must not hide one that falls. A grid wipe can nudge
    the same shore by a few hundredths; that is not a new shore.
    """
    old_iou = min(float(row["registration"].iou) for row in picked)
    try:
        mask = _dark_mask(image)
        _clear_cased_roads(mask, image.width, image.height)
        cleared, opened = clear_cartographic_lines(mask, image.width, image.height)
    except (OSError, ValueError, ZeroDivisionError):
        return picked
    if cleared <= 0 or opened:
        for row in picked:
            row["cartographic_pixels_cleared"] = cleared
            row["cartographic_mask_accepted"] = False
            row["cartographic_basin_opened"] = opened
        return picked
    candidates, _error = _collect_page_candidates(
        image,
        page,
        hydro_ring,
        gcp_count,
        mask,
    )
    candidates.extend(
        _spur_cleaned_candidates(
            candidates,
            image=image,
            hydro_ring=hydro_ring,
            gcp_count=gcp_count,
            page=page,
        )
    )
    candidates = _drop_margin_enclosures(candidates)
    new_picked = _pick_page_basins(candidates, hydro_ring)
    new_iou = min((float(row["registration"].iou) for row in new_picked), default=None)
    accept = new_iou is not None and bool(new_picked) and new_iou >= old_iou + _CARTO_MIN_GAIN
    if not accept or new_iou is None:
        for row in picked:
            row["cartographic_pixels_cleared"] = cleared
            row["cartographic_mask_accepted"] = False
            row["cartographic_basin_opened"] = False
            if new_iou is not None:
                row["cartographic_iou_after"] = new_iou
        return picked
    for row in new_picked:
        row["cartographic_pixels_cleared"] = cleared
        row["cartographic_mask_accepted"] = True
        row["cartographic_basin_opened"] = False
        row["cartographic_iou_before"] = old_iou
        row["cartographic_iou_after"] = new_iou
    return new_picked


def _ring_pixels_cleared(
    before: bytearray,
    after: bytearray,
    width: int,
    height: int,
    ring: list[tuple[float, float]],
) -> int:
    """Samples of ``ring`` that were ink and are white after the road pass."""
    hits = 0
    seen: set[tuple[int, int]] = set()
    for x, y in ring:
        ix = int(round(x))
        iy = int(round(y))
        for dy in range(-2, 3):
            yy = iy + dy
            if yy < 0 or yy >= height:
                continue
            for dx in range(-2, 3):
                xx = ix + dx
                if xx < 0 or xx >= width or (xx, yy) in seen:
                    continue
                seen.add((xx, yy))
                index = yy * width + xx
                if before[index] and not after[index]:
                    hits += 1
    return hits


def _road_mask_summary(sheets: list[dict]) -> dict | None:
    cleared = [sheet for sheet in sheets if int(sheet.get("road_pixels_cleared") or 0) > 0]
    if not cleared:
        return None
    pages = []
    for sheet in cleared:
        page = sheet.get("page")
        entry = {
            "page": page.name if isinstance(page, Path) else str(page),
            "pixels_cleared": int(sheet["road_pixels_cleared"]),
            "accepted": bool(sheet.get("road_mask_accepted")),
        }
        if "road_iou_before" in sheet:
            entry["iou_before"] = round(float(sheet["road_iou_before"]), 6)
        if "road_iou_after" in sheet:
            entry["iou_after"] = round(float(sheet["road_iou_after"]), 6)
        pages.append(entry)
    return {
        "pixels_cleared": sum(item["pixels_cleared"] for item in pages),
        "accepted": any(item["accepted"] for item in pages),
        "pages": pages,
    }


def _validated_stitch_score(
    sheets: list[dict],
    hydro_ring: list[tuple[float, float]],
    threshold: float,
) -> float | None:
    """Minimum placed-ring overlap when the stitch holds, else None.

    Each sheet must already clear ``threshold`` on its gate score, and the
    ring placed by that sheet's published warp must land on the full hydro
    polygon within one raster cell of the threshold. The union of those rings
    must cover at least as much of the lake as the weakest sheet, allowing
    the same cell of slack. ``combined_iou`` is not this value. None keeps
    the multi-sheet queue.
    """
    if len(sheets) < 2:
        return None
    # One cell on the 160-grid IoU raster. This absorbs a second rasterization
    # of a score that already cleared the gate. It does not lower the gate.
    slack = 1.0 / 160.0
    overlaps: list[float] = []
    mapped: list[list[tuple[float, float]]] = []
    for sheet in sheets:
        if float(sheet.get("gate_iou", 0.0)) < threshold:
            return None
        ring = sheet.get("ring")
        if not ring or sheet.get("registration") is None or len(ring) < 3:
            return None
        placed = _mapped_ring(sheet)
        overlap = polygon_iou(placed, list(hydro_ring))
        if overlap + slack < threshold:
            return None
        overlaps.append(overlap)
        mapped.append(placed)
    weakest = min(float(sheet["gate_iou"]) for sheet in sheets)
    union = union_polygon_iou(mapped, list(hydro_ring))
    if union + slack < weakest:
        return None
    return weakest


def _margin_enclosure(area_fraction: float, ink: float) -> bool:
    """A small outline with almost no depth ink. Overlap does not make it the shore."""
    return area_fraction < _LEGEND_MAX_AREA and ink < MIN_BASIN_INK


def _rejects_low_ink(
    area_fraction: float,
    ink: float,
    iou: float | None,
    *,
    keep_margin: bool = False,
) -> bool:
    """True when low ink removes the ring.

    Rings smaller than ``_LEGEND_MAX_AREA`` keep that rejection no matter how
    well they overlap. A larger ring still gets the near-gate waiver. Passing
    ``keep_margin`` retains a blank outline only when the page has no other figure.
    """
    if _margin_enclosure(area_fraction, ink):
        return not keep_margin
    if iou is None:
        return False
    return ink < MIN_BASIN_INK and iou < NEAR_GATE_IOU


def _row_area_fraction(row: dict) -> float | None:
    image = row.get("image")
    area = row.get("area")
    if image is None or area is None:
        return None
    denom = float(image.width * image.height)
    if denom <= 0:
        return None
    return float(area) / denom


def _drop_margin_enclosures(candidates: list[dict]) -> list[dict]:
    """Drop a small low-ink outline when a much larger inked basin is on the page.

    A blank cell is dropped even when its overlap is higher. A small ring whose
    overlap reaches the near-gate band stays when the larger basin does not:
    that ring is the lake. Overlap below the band does not save the outline.
    """
    basins: list[dict] = []
    fractions: list[float | None] = []
    for row in candidates:
        fraction = _row_area_fraction(row)
        fractions.append(fraction)
        ink = float(row.get("ink") or 0.0)
        if fraction is not None and fraction >= _LEGEND_MAX_AREA and ink >= MIN_BASIN_INK:
            basins.append(row)
    if not basins:
        return list(candidates)
    basin_fractions = [float(_row_area_fraction(row) or 0.0) for row in basins]
    basin_overlap = max(float(row["registration"].iou) for row in basins)
    kept: list[dict] = []
    for row, fraction in zip(candidates, fractions, strict=True):
        ink = float(row.get("ink") or 0.0)
        if fraction is None:
            kept.append(row)
            continue
        larger = any(
            basin >= max(_LEGEND_MAX_AREA, fraction * _BASIN_AREA_MULTIPLE) for basin in basin_fractions
        )
        if not larger:
            kept.append(row)
            continue
        if _margin_enclosure(fraction, ink):
            continue
        # Trout's north-arrow enclosure is 3.5% of the page with ink 0.021.
        # It is above the blank-cell floor and still not the basin.
        small_low_ink = fraction < _LEGEND_MAX_AREA and ink < _LEGEND_MAX_INK
        if small_low_ink:
            overlap = float(row["registration"].iou)
            if overlap >= NEAR_GATE_IOU and basin_overlap < NEAR_GATE_IOU:
                kept.append(row)
                continue
            continue
        kept.append(row)
    return kept or list(candidates)


def _legend_box(
    ring: list[tuple[float, float]],
    ink: float,
    straight: float,
    *,
    area_fraction: float,
) -> bool:
    """A map legend or north arrow, not a shoreline.

    Every branch requires a small footprint and a near-rectangular outline.
    An oval pond and a basin closed by one straight dam stay eligible. An
    affine fit can still stretch a real legend onto the hydro polygon, so a
    high IoU does not make the box the shore.
    """
    if area_fraction >= _LEGEND_MAX_AREA or ink >= _LEGEND_MAX_INK:
        return False
    if _ring_boxiness(ring) < _LEGEND_MIN_BOXINESS:
        return False
    if straight >= 0.80 and ink < 0.012:
        return True
    if straight >= 0.40 and ink < _LEGEND_MAX_INK:
        return True
    return ink < 0.012


def _score_ring(
    image,
    ring: list[tuple[float, float]],
    on_frame: list[bool] | tuple[bool, ...] | None,
    *,
    hydro_ring: list[tuple[float, float]],
    gcp_count: int,
    page: Path,
    _allow_spur_cleanup: bool = True,
    _keep_margin: bool = False,
) -> dict | None:
    """One figure that is allowed to compete, or None when it is not a shore."""
    if len(ring) < 12:
        return None
    flags = list(on_frame) if on_frame is not None else [False] * len(ring)
    if len(flags) != len(ring):
        flags = [False] * len(ring)
    if _span_fraction(ring, image.width, image.height) < max(MIN_FIGURE_SPAN, 0.18):
        return None
    cut_fraction = _flag_fraction(flags)
    if _false_shore_ring(ring, image.width, image.height, cut_fraction):
        return None
    area = abs(_polygon_area(ring))
    image_area = float(image.width * image.height)
    if area < 0.03 * image_area or area > 0.72 * image_area:
        return None
    ink = interior_ink_fraction(image, ring)
    straight = _ring_straightness(ring)
    area_fraction = area / image_area if image_area else 0.0
    if _legend_box(ring, ink, straight, area_fraction=area_fraction):
        return None
    # A high overlap used to waive this for every ring. A blank section cell
    # then beat the contoured basin. Small rings keep the ink rejection.
    # ``_keep_margin`` is only for a page whose sole figure is that outline.
    if _rejects_low_ink(area_fraction, ink, iou=None, keep_margin=_keep_margin):
        return None
    registration = register_shoreline(list(ring), list(hydro_ring), gcp_count, on_frame=flags)
    if _rejects_low_ink(area_fraction, ink, registration.iou, keep_margin=_keep_margin):
        return None
    if not _hydro_footprint_matches(
        registration,
        list(ring),
        hydro_ring,
        cut_fraction=cut_fraction,
        area_fraction=area_fraction,
        ink=ink,
    ):
        return None
    span = _span_fraction(list(ring), image.width, image.height)
    # Spur cleanup runs before this rejection. Otherwise a shore with an
    # out-and-back road is dropped, and the cleanup never sees it.
    road_rejection = (
        straight >= MAX_ROAD_STRAIGHTNESS and ink < 0.02 and registration.iou < NEAR_GATE_IOU
    )
    if road_rejection and _allow_spur_cleanup and cut_fraction < _PARTIAL_CUT:
        best: dict | None = None
        for cleaned in cleanup_shore_ring(list(ring)):
            try:
                alt = _score_ring(
                    image,
                    cleaned,
                    None,
                    hydro_ring=hydro_ring,
                    gcp_count=gcp_count,
                    page=page,
                    _allow_spur_cleanup=False,
                )
            except Exception:
                continue
            if alt is None:
                continue
            if best is None or alt["registration"].iou > best["registration"].iou:
                best = alt
        return best
    if road_rejection:
        return None
    if span > LARGE_RING_SPAN and ink < LARGE_RING_MIN_INK and registration.iou < NEAR_GATE_IOU:
        return None
    return {
        "page": page,
        "registration": registration,
        "ring": list(ring),
        "area": area,
        "cut_fraction": cut_fraction,
        "ink": ink,
        "image": image,
    }


def _spur_cleaned_candidates(
    candidates: list[dict],
    *,
    image,
    hydro_ring: list[tuple[float, float]],
    gcp_count: int,
    page: Path,
) -> list[dict]:
    """Re-score every closed ring after thin road spurs are opened off.

    Cleanup is not reserved for the current overlap winner. A spurred shore
    that lost to another ring still gets the spur removed. A cleaned ring is
    kept only when its overlap rises by at least 0.02, so a same-score box is
    not substituted.
    """
    closed = [row for row in candidates if row["cut_fraction"] < _PARTIAL_CUT]
    cleaned: list[dict] = []
    for seed in closed:
        base_iou = seed["registration"].iou
        for ring in cleanup_shore_ring(seed["ring"]):
            try:
                row = _score_ring(
                    image,
                    ring,
                    None,
                    hydro_ring=hydro_ring,
                    gcp_count=gcp_count,
                    page=page,
                )
            except Exception:
                continue
            if row is None or row["registration"].iou < base_iou + 0.02:
                continue
            cleaned.append(row)
    return cleaned


def _publish_gate(row: dict, hydro_ring: list[tuple[float, float]]) -> float:
    """Score a candidate the way the lake gate will: partials use the full hydro."""
    iou = float(row["registration"].iou)
    if float(row.get("cut_fraction") or 0.0) < _PARTIAL_CUT:
        return iou
    cached = row.get("_full_iou")
    if cached is None:
        registration = row["registration"]
        project = registration.affine.apply if registration.method == "affine" else registration.tps.apply
        mapped = [project(col, line) for col, line in row["ring"]]
        cached = polygon_iou(mapped, list(hydro_ring))
        row["_full_iou"] = cached
    return min(iou, float(cached))


def _anchor_ink_area(row: dict) -> tuple[float, float]:
    """Near-gate rank: interior ink, then how much of the page the ring covers."""
    ink = float(row.get("ink") or 0.0)
    fraction = _row_area_fraction(row)
    if fraction is None:
        return ink, 0.0
    return ink, float(fraction)


def _choose_anchor(candidates: list[dict], hydro_ring: list[tuple[float, float]]) -> dict:
    """Above 0.74, ink and area decide only inside a tenth of overlap.

    A wider gap keeps the higher score. Big Muskellunge's 0.902 spline stays
    ahead of a 0.779 affine ring. Allequash's 0.022 gap still picks the inkier
    ring. Below the band, a wide overlap gap still wins.
    """
    candidates = _drop_margin_enclosures(candidates)
    scored = [(_publish_gate(row, hydro_ring), row) for row in candidates]
    above = [pair for pair in scored if pair[0] >= NEAR_GATE_IOU]
    if above:
        best_gate = max(pair[0] for pair in above)
        close = [pair for pair in above if best_gate - pair[0] <= _ANCHOR_IOU_BAND]
        if len(close) > 1:
            return max(close, key=lambda pair: _anchor_ink_area(pair[1]))[1]
        return max(above, key=lambda pair: pair[0])[1]
    best_gate = max(pair[0] for pair in scored)
    band = [pair for pair in scored if best_gate - pair[0] < _ANCHOR_IOU_BAND]
    return max(band, key=lambda pair: (_figure_quality(pair[1]), pair[0]))[1]


def _pick_page_basins(candidates: list[dict], hydro_ring: list[tuple[float, float]]) -> list[dict]:
    """Keep one or more disjoint basin figures when their union helps overlap."""
    if not candidates:
        return []
    anchor = _choose_anchor(candidates, hydro_ring)
    ranked = [anchor] + sorted(
        (row for row in candidates if row is not anchor),
        key=lambda row: -row["registration"].iou,
    )
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


def _affine_overlap(sheet: dict) -> float:
    return float(sheet["registration"].affine_iou)


def _published_overlap(sheet: dict) -> float:
    """The score the lake gate reads: ``Registration.iou``."""
    return float(sheet["registration"].iou)


def _closed_sheets_to_keep(unique: list[dict]) -> list[dict]:
    """Rank closed sibling sheets by affine overlap.

    Keep every sheet whose affine overlap clears the publish cutoff, and keep
    a sibling whose published score also clears. That sibling stays when two
    or more sheets already cleared on the affine. Dropping it used to mark the
    page missing and force the lake score to 0. When no affine clears, keep
    every sheet whose published score clears. The highest affine is the
    fallback only when no published score clears.
    """
    cleared = [sheet for sheet in unique if _affine_overlap(sheet) >= DEFAULT_IOU_THRESHOLD]
    cleared_ids = {id(sheet) for sheet in cleared}
    siblings = [
        sheet
        for sheet in unique
        if id(sheet) not in cleared_ids and _published_overlap(sheet) >= DEFAULT_IOU_THRESHOLD
    ]
    if cleared:
        return [*cleared, *siblings]
    published = [sheet for sheet in unique if _published_overlap(sheet) >= DEFAULT_IOU_THRESHOLD]
    if published:
        return published
    return [max(unique, key=_affine_overlap)]


def _select_lake_sheets(
    sheets: list[dict],
    page_rasters: list[Path],
) -> tuple[list[dict], list[Path]]:
    """Keep every page when any sheet is cut. Closed siblings rank by affine.

    A cut or partial score on any page means the document does not show one
    complete shore, so every other large page stays in the gate, including a
    page that produced no shoreline. Closed sheets are ranked by affine overlap.
    Every closed sheet whose affine overlap clears 0.75 stays, and a sibling
    whose published score also clears stays with it, including when two or
    more affines already cleared. When no affine clears, every published score
    at or above 0.75 stays. A raster that is not in the kept set is missed.
    """
    if not sheets:
        return [], list(page_rasters)
    partial = [sheet for sheet in sheets if sheet["cut_fraction"] >= _PARTIAL_CUT]
    if partial:
        return list(sheets), _pages_not_kept(sheets, page_rasters)
    if len(page_rasters) <= 1 or len(sheets) <= 1:
        best = max(sheets, key=lambda sheet: sheet["registration"].iou)
        kept = [best]
        return kept, _pages_not_kept(kept, page_rasters)
    by_page: dict[Path, dict] = {}
    for sheet in sheets:
        current = by_page.get(sheet["page"])
        if current is None or _prefer_sheet(sheet, current):
            by_page[sheet["page"]] = sheet
    unique = list(by_page.values())
    if _pages_complement_sheets(unique):
        return unique, _pages_not_kept(unique, page_rasters)
    kept = _closed_sheets_to_keep(unique)
    return kept, _pages_not_kept(kept, page_rasters)


def _pages_not_kept(kept: list[dict], page_rasters: list[Path]) -> list[Path]:
    """Rasters that are not in the kept sheet set."""
    kept_pages = {sheet["page"] for sheet in kept}
    return [page for page in page_rasters if page not in kept_pages]


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
    area_fraction: float | None = None,
    ink: float | None = None,
) -> bool:
    # A blank cell, a 9% ring, or a 6% ring with ink just over the legend cap
    # can be stretched onto the lake and clear 0.68. That overlap does not skip
    # the span check. Callers that omit the area keep the old shortcut.
    margin = (
        area_fraction is not None
        and ink is not None
        and _margin_enclosure(area_fraction, ink)
    )
    small = area_fraction is not None and area_fraction < _SMALL_RING_AREA
    if registration.iou >= 0.68 and not margin and not small:
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


def _headline_sheet(sheets: list[dict]) -> dict:
    """The sheet whose gate is the lake minimum. Its fit is the headline."""
    return min(sheets, key=lambda sheet: float(sheet["gate_iou"]))


def _public_sheet(sheet: dict) -> dict:
    registration = sheet["registration"]
    row = {
        "page": sheet["page"].name,
        "iou": round(sheet.get("gate_iou", registration.iou), 6),
        "footprint_iou": round(registration.iou, 6),
        "affine_iou": round(registration.affine_iou, 6),
        "tps_iou": round(registration.tps_iou, 6),
        "method": registration.method,
        "rotation_deg": round(registration.rotation_deg, 3),
        "cut_fraction": round(sheet["cut_fraction"], 4),
    }
    if "full_iou" in sheet:
        row["full_iou"] = round(sheet["full_iou"], 6)
    if sheet.get("road_pixels_cleared"):
        row["road_pixels_cleared"] = int(sheet["road_pixels_cleared"])
        row["road_mask_accepted"] = bool(sheet.get("road_mask_accepted"))
    if sheet.get("cartographic_pixels_cleared"):
        row["cartographic_pixels_cleared"] = int(sheet["cartographic_pixels_cleared"])
        row["cartographic_mask_accepted"] = bool(sheet.get("cartographic_mask_accepted"))
        if "cartographic_basin_opened" in sheet:
            row["cartographic_basin_opened"] = bool(sheet["cartographic_basin_opened"])
        if sheet.get("cartographic_iou_after") is not None:
            row["cartographic_iou_after"] = round(float(sheet["cartographic_iou_after"]), 6)
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
    degenerate_gcps: bool = False,
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
    # the gate. ``several_pages_passed`` here means several pages cleared and
    # the stitch was not validated. A validated stitch does not set the flag.
    if several_pages_passed:
        found.append("multi_sheet")
    _ = multi_sheet
    if warp_failed:
        found.append("warp_failed")
    if degenerate_gcps:
        found.append("degenerate_gcps")
    order = {name: index for index, name in enumerate(_REASON_ORDER)}
    return sorted(found, key=lambda name: order[name])


def _sampled_dark_fraction(image) -> float:
    total = image.width * image.height
    if total <= 0:
        return 1.0
    rgb = image.rgb
    dark = 0
    seen = 0
    for index in range(0, total, 7):
        offset = index * 3
        if (rgb[offset] + rgb[offset + 1] + rgb[offset + 2]) // 3 < 128:
            dark += 1
        seen += 1
    if seen == 0:
        return 1.0
    return dark / seen


def _is_citation_raster(path: Path, *, long_inches: float | None = None) -> bool:
    """True when a page is a doclink citation, not a bathymetry sheet.

    A citation is letter-size type. A large sheet stays even when the ink is
    light, and a light page that still holds a lake-scale figure stays too.
    Low ink by itself is not enough: Sparkling's map is a light sheet, and a
    sparser sheet in the rest of the catalog can fall under the darkness cutoff.
    """
    try:
        image = read_png(path)
    except (OSError, ValueError):
        return False
    if image.width * image.height < 64:
        return False
    if _sampled_dark_fraction(image) >= _CITATION_DARK_FRACTION:
        return False
    if long_inches is not None and long_inches > LETTER_LONG_INCHES:
        return False
    if extract_shoreline_figures(image):
        return False
    if long_inches is None:
        return True
    return long_inches <= LETTER_LONG_INCHES


def _page_long_inches(page: dict) -> float | None:
    pixels = page.get("pixels")
    dpi = page.get("dpi")
    if not isinstance(pixels, (list, tuple)) or len(pixels) < 2 or not dpi:
        return None
    try:
        dpi_value = float(dpi)
    except (TypeError, ValueError):
        return None
    if dpi_value <= 0:
        return None
    return max(float(pixels[0]), float(pixels[1])) / dpi_value


def _page_rasters(entry: dict, raster: Path) -> list[Path]:
    """Map-sheet pages for this row. A single raster stays a one-item list."""
    raw_pages = entry.get("raster_pages")
    described: list[tuple[Path, float | None]] = []
    if isinstance(raw_pages, list):
        items = [item for item in raw_pages if isinstance(item, dict)]
        bare = [item for item in raw_pages if not isinstance(item, dict)]
        for item in select_sheet_pages(items):
            path = item.get("path")
            if path and Path(str(path)).is_file():
                described.append((Path(str(path)), _page_long_inches(item)))
        for item in bare:
            if item and Path(str(item)).is_file():
                described.append((Path(str(item)), None))
    if not described:
        described = [(raster, None)]
    large = [path for path, long_inches in described if long_inches is not None and long_inches > LETTER_LONG_INCHES]
    maps: list[Path] = []
    for path, long_inches in described:
        # The only large sheet is the map, even when its ink is sparse.
        if len(large) == 1 and path in large:
            maps.append(path)
            continue
        if _is_citation_raster(path, long_inches=long_inches):
            continue
        maps.append(path)
    return maps or [path for path, _long_inches in described]


def _is_degenerate_gcp_error(error: str | None) -> bool:
    return bool(error) and error.startswith("degenerate control points")


def _affine_is_usable(affine: dict[str, float] | None) -> bool:
    if not affine:
        return False
    try:
        values = [float(affine[key]) for key in ("a", "b", "tx", "c", "d", "ty")]
    except (KeyError, TypeError, ValueError):
        return False
    if not all(math.isfinite(value) for value in values):
        return False
    determinant = values[0] * values[4] - values[1] * values[3]
    return abs(determinant) >= 1e-12


def _prepared_gcp_dicts(gcps: list[dict]) -> tuple[list[dict] | None, str | None]:
    points: list[GroundControlPoint] = []
    for item in gcps:
        try:
            points.append(
                GroundControlPoint(
                    pixel=float(item["pixel"]),
                    line=float(item["line"]),
                    easting=float(item["easting"]),
                    northing=float(item["northing"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    prepared = prepare_control_points(points)
    if not prepared.usable:
        return None, prepared.reason or "degenerate control points"
    return [point.as_dict() for point in prepared.points], None


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
    use_method = method
    use_gcps = gcps
    if method != "affine":
        cleaned, reject = _prepared_gcp_dicts(gcps)
        if cleaned is None:
            # The shore affine still places the sheet. A spline through a
            # duplicate or collinear set does not.
            if _affine_is_usable(affine):
                use_method = "affine"
            else:
                return "failed", None, reject
        else:
            use_gcps = cleaned
    tif_path = out_dir / tif_name
    try:
        if use_method == "affine" and affine is not None:
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
            _run(gdal_translate_gcp_args(str(raster), str(vrt_path), use_gcps))
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


def _close_ring(ring: list[tuple[float, float]]) -> list[list[float]]:
    closed = list(ring)
    if closed[0] != closed[-1]:
        closed.append(closed[0])
    return [[x, y] for x, y in closed]


def _shoreline_collection(ring: list[tuple[float, float]], properties: dict) -> dict:
    return {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3071"}},
        "features": [
            {
                "type": "Feature",
                "properties": properties,
                "geometry": {"type": "Polygon", "coordinates": [_close_ring(ring)]},
            }
        ],
    }


def _mapped_ring(sheet: dict) -> list[tuple[float, float]]:
    warp = sheet["registration"]
    project = warp.affine.apply if warp.method == "affine" else warp.tps.apply
    return [project(col, row) for col, row in sheet["ring"]]


def _stitched_shoreline(
    sheets: list[dict],
    properties: dict,
    primary_ring: list[tuple[float, float]],
) -> dict:
    """One feature per sheet, plus their stitch. The properties iou is the gate.

    The stitch is the sheets placed in map coordinates. It is not a new score:
    ``properties["iou"]`` stays the minimum sheet gate, and a higher union
    overlap does not replace it.
    """
    collection = _shoreline_collection(primary_ring, {**properties, "role": "primary"})
    if len(sheets) < 2:
        return collection
    mapped = [_mapped_ring(sheet) for sheet in sheets]
    for sheet, ring in zip(sheets, mapped, strict=True):
        registration = sheet["registration"]
        collection["features"].append(
            {
                "type": "Feature",
                "properties": {
                    "role": "sheet",
                    "page": sheet["page"].name,
                    "iou": round(float(sheet.get("gate_iou", registration.iou)), 6),
                },
                "geometry": {"type": "Polygon", "coordinates": [_close_ring(ring)]},
            }
        )
    collection["features"].append(
        {
            "type": "Feature",
            "properties": {"role": "stitched", "iou": properties.get("iou")},
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [[_close_ring(ring)] for ring in mapped],
            },
        }
    )
    return collection


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
            fetch_roads=not args.fixtures,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main(sys.argv[1:])
