# SPDX-License-Identifier: GPL-3.0-only
"""
Trace Wisconsin depth contours and tag them with positive depth_ft.

Reads ``georef/results.json`` and the 24K Hydro polygon (island rings included).
Each warped sheet is clipped to that polygon, depth labels are read with
EasyOCR, the label ink is inpainted, and the remaining strokes are skeletonized
and traced. Depths snap to those labels and then step inward from the 0
shoreline by ``interval_ft``, staying inside ``max_depth_ft``. Ambiguous rings
are marked ``needs_review``.

Per-lake GeoJSON is written under ``contours/lakes/<wbic>/<map_key>/contours.geojson``.
Rasters stay in the work directory and are not part of the git tree. The OCR
engine is EasyOCR. OpenCV, scikit-image, EasyOCR, and the GDAL bindings are
imported only when a real sheet is traced.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from fishy_pipeline.states.wi.depth_tag import (
    Label,
    Line,
    WaterPart,
    assign_depths,
    assignment_to_feet,
    cutline_collection,
    features_to_geojson,
)
from fishy_pipeline.states.wi.georef import DEFAULT_IOU_THRESHOLD
from fishy_pipeline.states.wi.hydro import HydroGeometry, HydroPart, load_hydro_geometries
from fishy_pipeline.states.wi.manifest import read_manifest
from fishy_pipeline.states.wi.trace import clip_raster, trace_sheet
from fishy_pipeline.states.wi.wbic import normalize_wbic

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_WORK = REPO_ROOT / "pipeline" / "work" / "wi"
WI_CRS = "EPSG:3071"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fishy_pipeline.states.wi.contours")
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--manifest", type=Path, help="ingest manifest JSON")
    parser.add_argument("--hydro", type=Path, help="24K Hydro GeoJSON in EPSG:3071")
    parser.add_argument("--wbic", help="trace only this WBIC")
    parser.add_argument("--map-key", help="trace only this map key")
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="write a synthetic pond GeoJSON (no raster, no OCR, no DNR extract)",
    )
    return parser


def run_contours(
    *,
    manifest_path: Path,
    hydro_path: Path,
    work_dir: Path,
    wbic: str | None = None,
    map_key: str | None = None,
) -> dict:
    """Trace every georeferenced sheet that has a warped raster."""
    manifest = read_manifest(manifest_path)
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ValueError("manifest entries must be a list")
    georef_path = work_dir / "georef" / "results.json"
    if not georef_path.is_file():
        raise ValueError(f"georef results not found: {georef_path}")
    georef_results = json.loads(georef_path.read_text(encoding="utf-8"))
    items = georef_results.get("items")
    if not isinstance(items, list):
        raise ValueError("georef results items must be a list")
    geometries = load_hydro_geometries(hydro_path)
    by_entry = {}
    for entry in entries:
        key = (normalize_wbic(entry.get("wbic")), str(entry.get("map_key")))
        by_entry[key] = entry
    wanted_wbic = normalize_wbic(wbic) if wbic else None
    traced: list[dict] = []
    for item in items:
        item_wbic = normalize_wbic(item.get("wbic")) or str(item.get("wbic") or "")
        item_key = str(item.get("map_key") or "")
        if wanted_wbic is not None and item_wbic != wanted_wbic:
            continue
        if map_key is not None and item_key != map_key:
            continue
        traced.append(
            _trace_item(
                item,
                entry=by_entry.get((item_wbic, item_key)),
                geometry=geometries.get(item_wbic),
                work_dir=work_dir,
            )
        )
    body = {"version": 1, "state": "wi", "crs": WI_CRS, "ocr": "easyocr", "items": traced}
    _write_json(work_dir / "contours" / "results.json", body)
    return body


def run_fixture(work_dir: Path) -> dict:
    """Synthetic nested pond. Proves the GeoJSON contract without a raster."""
    shore = _box(0, 0, 100, 100)
    outer = _box(10, 10, 90, 90)
    inner = _box(30, 30, 70, 70)
    assignment = assign_depths(
        [
            Line(coords=outer, closed=True),
            Line(coords=inner, closed=True),
        ],
        [
            Label(x=10, y=50, depth_ft=5, confidence=0.9),
            Label(x=30, y=50, depth_ft=10, confidence=0.92),
        ],
        [WaterPart(exterior=shore)],
        max_depth_ft=20,
        snap_m=5,
    )
    qa_status = "needs_review" if assignment.reasons or any(line.needs_review for line in assignment.features) else "auto"
    collection = features_to_geojson(
        assignment.features,
        wbic="0000101",
        interval_ft=assignment.interval_ft,
        max_depth_ft=20,
        qa_status=qa_status,
    )
    out = work_dir / "contours" / "lakes" / "0000101" / "0000101a.geojson"
    _write_json(out, collection)
    depths = sorted({feature["properties"]["depth_ft"] for feature in collection["features"]})
    body = {
        "version": 1,
        "state": "wi",
        "crs": WI_CRS,
        "ocr": "easyocr",
        "items": [
            {
                "wbic": "0000101",
                "map_key": "0000101a",
                "name": "Fixture Pond",
                "qa_status": qa_status,
                "iou": None,
                "interval_ft": assignment.interval_ft,
                "max_depth_ft": 20,
                "depths_ft": depths,
                "reasons": assignment.reasons,
                "path": str(out),
            }
        ],
    }
    _write_json(work_dir / "contours" / "results.json", body)
    return body


def _trace_item(item: dict, *, entry: dict | None, geometry: HydroGeometry | None, work_dir: Path) -> dict:
    wbic = normalize_wbic(item.get("wbic")) or str(item.get("wbic") or "")
    map_key = str(item.get("map_key") or "")
    base = {
        "wbic": wbic,
        "map_key": map_key,
        "name": item.get("name") or (None if entry is None else entry.get("name")),
        "iou": item.get("iou"),
        "georef_qa_status": item.get("qa_status"),
        "georef_reasons": list(item.get("reasons") or []),
    }
    if item.get("qa_status") == "skipped":
        return {**base, "qa_status": "skipped", "skip_reason": item.get("skip_reason"), "reasons": []}
    warped = (item.get("paths") or {}).get("warped")
    if not warped or not Path(str(warped)).is_file():
        return {**base, "qa_status": "skipped", "skip_reason": "warp_missing", "reasons": []}
    if geometry is None or not geometry.parts:
        return {**base, "qa_status": "skipped", "skip_reason": "hydro_geometry_missing", "reasons": []}
    out_dir = work_dir / "contours" / "lakes" / wbic / map_key
    try:
        return _trace_sheet(base, entry=entry, geometry=geometry, warped=Path(str(warped)), out_dir=out_dir)
    except Exception as error:
        return {
            **base,
            "qa_status": "failed",
            "reasons": ["trace_error"],
            "error": f"{error.__class__.__name__}: {error}"[:500],
            "trace": traceback.format_exc(limit=8)[-1500:],
        }


def _trace_sheet(base: dict, *, entry: dict | None, geometry: HydroGeometry, warped: Path, out_dir: Path) -> dict:
    parts = [_water_part(part) for part in geometry.parts]
    out_dir.mkdir(parents=True, exist_ok=True)
    cutline_path = out_dir / "cutline.geojson"
    clipped_path = out_dir / "clipped.tif"
    _write_json(cutline_path, cutline_collection(parts))
    clip_raster(warped, clipped_path, cutline_path)
    traced = trace_sheet(clipped_path, out_dir)
    max_depth = _max_depth(entry)
    assign_max = max_depth * 0.3048 if traced.unit == "m" and max_depth is not None else max_depth
    assignment = assign_depths(
        [Line(coords=line.coords, closed=line.closed) for line in traced.lines],
        [Label(x=label.x, y=label.y, depth_ft=label.depth_ft, confidence=label.confidence) for label in traced.labels],
        parts,
        max_depth_ft=assign_max,
        snap_m=traced.snap_m,
    )
    if traced.unit == "m":
        assignment = assignment_to_feet(assignment)
    reasons = list(assignment.reasons)
    iou = base.get("iou")
    if isinstance(iou, (int, float)) and iou < DEFAULT_IOU_THRESHOLD:
        reasons.append("georef_iou_below_threshold")
    elif iou is None:
        reasons.append("georef_unscored")
    emitted = [line for line in assignment.features if line.depth_ft is not None and line.source != "hydro"]
    if not emitted:
        reasons.append("no_contour_lines")
    review = any(line.needs_review for line in assignment.features if line.depth_ft is not None)
    qa_status = "needs_review" if review or reasons else "auto"
    collection = features_to_geojson(
        assignment.features,
        wbic=str(base["wbic"]),
        interval_ft=assignment.interval_ft,
        max_depth_ft=max_depth,
        qa_status=qa_status,
    )
    geojson_path = out_dir / "contours.geojson"
    _write_json(geojson_path, collection)
    depths = sorted({int(feature["properties"]["depth_ft"]) for feature in collection["features"]})
    summary = {
        **base,
        "qa_status": qa_status,
        "interval_ft": assignment.interval_ft,
        "max_depth_ft": max_depth,
        "depths_ft": depths,
        "contour_count": len(emitted),
        "label_count": len(traced.labels),
        "needs_review_count": sum(1 for line in emitted if line.needs_review),
        "unassigned_count": assignment.unassigned_count,
        "reasons": list(dict.fromkeys(reasons)),
        "pixel_size_m": round(traced.pixel_size_m, 4),
        "snap_m": round(traced.snap_m, 2),
        "path": str(geojson_path),
    }
    print(
        f"{base['wbic']} {base['map_key']} {qa_status} iou={iou} interval={assignment.interval_ft} depths={depths}",
        flush=True,
    )
    return summary


def _water_part(part: HydroPart) -> WaterPart:
    return WaterPart(exterior=part.exterior, holes=part.holes)


def _max_depth(entry: dict | None) -> float | None:
    if not entry:
        return None
    value = entry.get("max_depth_ft")
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        depth = float(value)
    except (TypeError, ValueError):
        return None
    if depth != depth:  # NaN
        return None
    return depth


def _box(x0: float, y0: float, x1: float, y1: float) -> tuple[tuple[float, float], ...]:
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def _write_json(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    work_dir: Path = args.work_dir
    try:
        if args.fixtures:
            if args.manifest or args.hydro or args.wbic or args.map_key:
                raise ValueError("--fixtures does not take --manifest, --hydro, --wbic, or --map-key")
            run_fixture(work_dir)
            return
        manifest_path = args.manifest or (work_dir / "manifest.json")
        hydro_path = args.hydro
        if hydro_path is None:
            raise ValueError("--hydro is required unless --fixtures")
        if not manifest_path.is_file():
            raise ValueError(f"manifest not found: {manifest_path}")
        if not hydro_path.is_file():
            raise ValueError(f"hydro file not found: {hydro_path}")
        run_contours(
            manifest_path=manifest_path,
            hydro_path=hydro_path,
            work_dir=work_dir,
            wbic=args.wbic,
            map_key=args.map_key,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main(sys.argv[1:])
