# SPDX-License-Identifier: GPL-3.0-only
"""
Trace Wisconsin depth contours and tag them with positive depth_ft.

Reads ``georef/results.json`` and the 24K Hydro polygon (island rings included).
Every kept warp is clipped to that polygon. Depth labels come from committed
``pipeline/data/wi/labels/<wbic>.json`` only when that file's page is the raster
being traced. The interval from that page is then used to step depths on the
other kept warps. The publish gate runs once on the merged depths. EasyOCR runs
only with ``--ocr-fallback``. Label ink is inpainted, strokes are skeletonized
and traced.
Depths snap to labels and step inward from the 0 shoreline by ``interval_ft``,
staying inside ``max_depth_ft``. A lake publishes only when shoreline IoU is at
least ``constants.DEFAULT_IOU_THRESHOLD`` and traced depths match the recorded
labels. Everything else is ``needs_review`` and is not published.

Per-lake GeoJSON is written under ``contours/lakes/<wbic>/<map_key>/contours.geojson``.
OpenCV, scikit-image, EasyOCR, and the GDAL bindings are imported only when a
real sheet is traced.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from fishy_pipeline.states.wi.depth_tag import (
    JOIN_GAP_MIN_M,
    Label,
    Line,
    WaterPart,
    assign_depths,
    assignment_to_feet,
    cutline_collection,
    features_to_geojson,
    join_gap_m_for_pixel,
)
from fishy_pipeline.states.wi.hydro import HydroGeometry, HydroPart, load_hydro_geometries
from fishy_pipeline.states.wi.manifest import read_manifest
from fishy_pipeline.states.wi.pilot_data import load_recorded_labels
from fishy_pipeline.states.wi.recorded_labels import label_page_matches
from fishy_pipeline.states.wi.publish import publish_gate
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
    parser.add_argument(
        "--ocr-fallback",
        action="store_true",
        help="use EasyOCR when recorded labels are absent or projected off the clip",
    )
    parser.add_argument(
        "--compare-ocr",
        action="store_true",
        help="also trace with EasyOCR-only labels and report depths_ft_ocr",
    )
    return parser


def run_contours(
    *,
    manifest_path: Path,
    hydro_path: Path,
    work_dir: Path,
    wbic: str | None = None,
    map_key: str | None = None,
    ocr_fallback: bool = False,
    compare_ocr: bool = False,
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
                ocr_fallback=ocr_fallback,
                compare_ocr=compare_ocr,
            )
        )
    body = {
        "version": 1,
        "state": "wi",
        "crs": WI_CRS,
        "label_source": "recorded",
        "items": traced,
    }
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
        join_gap_m=0,
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
        "label_source": "fixture",
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


def _trace_item(
    item: dict,
    *,
    entry: dict | None,
    geometry: HydroGeometry | None,
    work_dir: Path,
    ocr_fallback: bool,
    compare_ocr: bool,
) -> dict:
    wbic = normalize_wbic(item.get("wbic")) or str(item.get("wbic") or "")
    map_key = str(item.get("map_key") or "")
    base = {
        "wbic": wbic,
        "map_key": map_key,
        "name": item.get("name") or (None if entry is None else entry.get("name")),
        "iou": item.get("iou"),
        "georef_qa_status": item.get("qa_status"),
        "georef_reasons": list(item.get("reasons") or []),
        "warp_source": item.get("warp_source"),
    }
    if item.get("qa_status") == "skipped":
        return {**base, "qa_status": "skipped", "skip_reason": item.get("skip_reason"), "reasons": []}
    jobs = _kept_warps(item)
    if not jobs:
        return {**base, "qa_status": "skipped", "skip_reason": "warp_missing", "reasons": []}
    if geometry is None or not geometry.parts:
        return {**base, "qa_status": "skipped", "skip_reason": "hydro_geometry_missing", "reasons": []}
    out_dir = work_dir / "contours" / "lakes" / wbic / map_key
    try:
        pieces = []
        for index, job in enumerate(jobs):
            page = str(job.get("page") or "")
            sheet_dir = out_dir if len(jobs) == 1 else out_dir / "sheets" / (_sheet_dir_name(page) or f"sheet-{index}")
            pieces.append(
                _trace_sheet(
                    base,
                    entry=entry,
                    geometry=geometry,
                    warped=Path(str(job["path"])),
                    out_dir=sheet_dir,
                    work_dir=work_dir,
                    gcp_path=job.get("gcps"),
                    page=page or None,
                    ocr_fallback=ocr_fallback,
                    compare_ocr=compare_ocr and index == 0,
                    publish=len(jobs) == 1,
                )
            )
        if len(pieces) == 1:
            _drop_trace_scratch(pieces[0])
            return pieces[0]
        _carry_label_interval(pieces)
        return _merge_sheet_traces(base, pieces, out_dir=out_dir, entry=entry)
    except Exception as error:
        return {
            **base,
            "qa_status": "failed",
            "reasons": ["trace_error"],
            "error": f"{error.__class__.__name__}: {error}"[:500],
            "trace": traceback.format_exc(limit=8)[-1500:],
        }


def _kept_warps(item: dict) -> list[dict]:
    """Successful warps to trace. The headline file is the fallback."""
    paths = item.get("paths") or {}
    rows: list[dict] = []
    for row in paths.get("warps") or []:
        if not isinstance(row, dict):
            continue
        if row.get("status") not in (None, "ok"):
            continue
        path = row.get("path")
        if not path or not Path(str(path)).is_file():
            continue
        rows.append(row)
    if rows:
        return rows
    warped = paths.get("warped")
    if not warped or not Path(str(warped)).is_file():
        return []
    headline = (item.get("warp") or {}).get("page")
    return [
        {
            "page": headline,
            "path": warped,
            "gcps": paths.get("gcps"),
            "status": "ok",
        }
    ]


def _sheet_dir_name(page: str) -> str:
    stem = Path(page).stem
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in stem)


# A page that is not the label file's page has no recorded depths of its own.
# Those two reasons belong to the lake gate, which runs once on the merge.
_LABEL_REJECT_REASONS = frozenset({"recorded_depth_missing", "no_contour_lines"})


def _merge_sheet_traces(base: dict, pieces: list[dict], *, out_dir: Path, entry: dict | None) -> dict:
    """One lake collection from every kept warp. A weak sheet keeps the lake in review."""
    features = []
    hydro_kept = False
    for piece in pieces:
        for feature in piece.pop("_features", []):
            if getattr(feature, "source", None) == "hydro":
                if hydro_kept:
                    continue
                hydro_kept = True
            features.append(feature)
        _drop_trace_scratch(piece)
    reasons: list[str] = []
    for piece in pieces:
        for reason in _sheet_reasons_for_lake(piece):
            if reason not in reasons:
                reasons.append(reason)
    review = any(_sheet_trace_failed(piece) for piece in pieces)
    review = review or any(getattr(feature, "needs_review", False) for feature in features if feature.depth_ft is not None)
    depths = sorted({int(feature.depth_ft) for feature in features if feature.depth_ft is not None})
    emitted = [feature for feature in features if feature.depth_ft is not None and feature.source != "hydro"]
    iou = base.get("iou")
    publishable, qa_status, gate_reasons = publish_gate(
        wbic=str(base["wbic"]),
        iou=iou if isinstance(iou, (int, float)) else None,
        depths_ft=[depth for depth in depths if depth != 0],
        georef_qa_status=base.get("georef_qa_status"),
        georef_reasons=base.get("georef_reasons"),
        warp_source=base.get("warp_source"),
    )
    for reason in gate_reasons:
        if reason not in reasons:
            reasons.append(reason)
    if not emitted:
        if "no_contour_lines" not in reasons:
            reasons.append("no_contour_lines")
    if review or not publishable:
        qa_status = "needs_review"
        publishable = False
    interval = next((piece.get("interval_ft") for piece in pieces if piece.get("interval_ft") is not None), None)
    max_depth = _max_depth(entry)
    collection = features_to_geojson(
        features,
        wbic=str(base["wbic"]),
        interval_ft=interval,
        max_depth_ft=max_depth,
        qa_status=qa_status,
    )
    geojson_path = out_dir / "contours.geojson"
    _write_json(geojson_path, collection)
    recorded = any(piece.get("recorded_labels") for piece in pieces)
    applied = any(piece.get("labels_applied") for piece in pieces)
    return {
        **base,
        "qa_status": qa_status,
        "publishable": publishable,
        "interval_ft": interval,
        "max_depth_ft": max_depth,
        "depths_ft": depths,
        "recorded_labels": recorded,
        "label_source": "recorded" if applied else ("ocr" if any(piece.get("label_source") == "ocr" for piece in pieces) else "none"),
        "contour_count": len(emitted),
        "label_count": sum(int(piece.get("label_count") or 0) for piece in pieces),
        "sheet_count": len(pieces),
        "sheets": [
            {
                "page": piece.get("page"),
                "qa_status": piece.get("qa_status"),
                "labels_applied": bool(piece.get("labels_applied")),
                "path": piece.get("path"),
            }
            for piece in pieces
        ],
        "needs_review_count": sum(1 for line in emitted if line.needs_review),
        "unassigned_count": sum(int(piece.get("unassigned_count") or 0) for piece in pieces),
        "reasons": reasons,
        "path": str(geojson_path),
    }


def _trace_sheet(
    base: dict,
    *,
    entry: dict | None,
    geometry: HydroGeometry,
    warped: Path,
    out_dir: Path,
    work_dir: Path,
    gcp_path: str | None,
    ocr_fallback: bool,
    compare_ocr: bool,
    page: str | None = None,
    publish: bool = True,
) -> dict:
    parts = [_water_part(part) for part in geometry.parts]
    out_dir.mkdir(parents=True, exist_ok=True)
    cutline_path = out_dir / "cutline.geojson"
    clipped_path = out_dir / "clipped.tif"
    _write_json(cutline_path, cutline_collection(parts))
    clip_raster(warped, clipped_path, cutline_path)
    sheet_gcp = Path(str(gcp_path)) if gcp_path else None
    if sheet_gcp is not None and not sheet_gcp.is_file():
        sheet_gcp = None
    wbic = str(base["wbic"])
    traced = trace_sheet(
        clipped_path,
        out_dir,
        wbic=wbic,
        sheet_gcp_path=sheet_gcp,
        ocr_fallback=ocr_fallback,
        page=page,
    )
    max_depth = _max_depth(entry)
    assign_max = max_depth * 0.3048 if traced.unit == "m" and max_depth is not None else max_depth
    join_gap_m = join_gap_m_for_pixel(traced.pixel_size_m)
    depths_ocr: list[int] | None = None
    if compare_ocr and ocr_fallback:
        traced_ocr = trace_sheet(
            clipped_path,
            out_dir / "ocr-compare",
            wbic=None,
            sheet_gcp_path=None,
            ocr_fallback=True,
        )
        ocr_max = max_depth * 0.3048 if traced_ocr.unit == "m" and max_depth is not None else max_depth
        assignment_ocr = assign_depths(
            [Line(coords=line.coords, closed=line.closed) for line in traced_ocr.lines],
            [
                Label(x=label.x, y=label.y, depth_ft=label.depth_ft, confidence=label.confidence)
                for label in traced_ocr.labels
            ],
            parts,
            max_depth_ft=ocr_max,
            snap_m=traced_ocr.snap_m,
            join_gap_m=join_gap_m_for_pixel(traced_ocr.pixel_size_m),
        )
        if traced_ocr.unit == "m":
            assignment_ocr = assignment_to_feet(assignment_ocr)
        depths_ocr = sorted(
            {
                int(feature.depth_ft)
                for feature in assignment_ocr.features
                if feature.depth_ft is not None and feature.source != "hydro"
            }
        )
    traced_lines = [Line(coords=line.coords, closed=line.closed) for line in traced.lines]
    traced_labels = [
        Label(x=label.x, y=label.y, depth_ft=label.depth_ft, confidence=label.confidence) for label in traced.labels
    ]
    assignment = assign_depths(
        traced_lines,
        traced_labels,
        parts,
        max_depth_ft=assign_max,
        snap_m=traced.snap_m,
        join_gap_m=join_gap_m,
    )
    if traced.unit == "m":
        assignment = assignment_to_feet(assignment)
    emitted = [line for line in assignment.features if line.depth_ft is not None and line.source != "hydro"]
    depths = sorted({int(feature.depth_ft) for feature in assignment.features if feature.depth_ft is not None})
    iou = base.get("iou")
    labels_applied = _recorded_labels_applied(wbic, page, traced.labels)
    recorded_body = load_recorded_labels(wbic)
    label_page_rejected = recorded_body is not None and not label_page_matches(recorded_body, page)
    review = any(line.needs_review for line in assignment.features if line.depth_ft is not None)
    reasons = list(assignment.reasons)
    publishable = False
    if publish:
        publishable, qa_status, gate_reasons = publish_gate(
            wbic=wbic,
            iou=iou if isinstance(iou, (int, float)) else None,
            depths_ft=[depth for depth in depths if depth != 0],
            georef_qa_status=base.get("georef_qa_status"),
            georef_reasons=base.get("georef_reasons"),
            warp_source=base.get("warp_source"),
        )
        reasons = list(dict.fromkeys(reasons + gate_reasons))
        if not emitted:
            reasons.append("no_contour_lines")
        if reasons or review or not publishable:
            qa_status = "needs_review"
            publishable = False
    else:
        # The lake gate runs once, on the merged depths. A page that is not the
        # label page does not invent recorded_depth_missing or no_contour_lines.
        if not emitted and not label_page_rejected:
            reasons.append("no_contour_lines")
        qa_status = "needs_review" if reasons or review else "auto"
    collection = features_to_geojson(
        assignment.features,
        wbic=str(base["wbic"]),
        interval_ft=assignment.interval_ft,
        max_depth_ft=max_depth,
        qa_status=qa_status,
    )
    geojson_path = out_dir / "contours.geojson"
    _write_json(geojson_path, collection)
    summary = {
        **base,
        "qa_status": qa_status,
        "publishable": publishable,
        "interval_ft": assignment.interval_ft,
        "max_depth_ft": max_depth,
        "depths_ft": depths,
        "depths_ft_ocr": depths_ocr,
        "page": page,
        "labels_applied": labels_applied,
        "label_page_rejected": label_page_rejected,
        "recorded_labels": recorded_body is not None,
        "label_source": (
            "recorded"
            if labels_applied
            else ("ocr" if ocr_fallback and traced.labels else "none")
        ),
        "_features": assignment.features,
        "_lines": traced_lines,
        "_assign_labels": traced_labels,
        "_parts": parts,
        "_assign_max": assign_max,
        "_snap_m": traced.snap_m,
        "_join_gap_m": join_gap_m,
        "contour_count": len(emitted),
        "label_count": len(traced.labels),
        "needs_review_count": sum(1 for line in emitted if line.needs_review),
        "unassigned_count": assignment.unassigned_count,
        "reasons": reasons,
        "pixel_size_m": round(traced.pixel_size_m, 4),
        "snap_m": round(traced.snap_m, 2),
        "path": str(geojson_path),
    }
    print(
        f"{base['wbic']} {base['map_key']} {qa_status} publishable={publishable} iou={iou} interval={assignment.interval_ft} depths={depths}",
        flush=True,
    )
    return summary


def _drop_trace_scratch(piece: dict) -> None:
    for key in ("_lines", "_assign_labels", "_parts", "_assign_max", "_snap_m", "_join_gap_m"):
        piece.pop(key, None)


def _sheet_reasons_for_lake(piece: dict) -> list[str]:
    reasons = list(piece.get("reasons") or [])
    if piece.get("label_page_rejected"):
        return [reason for reason in reasons if reason not in _LABEL_REJECT_REASONS]
    return reasons


def _sheet_trace_failed(piece: dict) -> bool:
    """True when this warp's own tracing cannot be published.

    A page that is not the label page is not failed merely because it has no
    recorded labels. Ambiguous rings and a missing interval still fail it.
    """
    if _sheet_reasons_for_lake(piece):
        return True
    if piece.get("label_page_rejected"):
        return False
    return piece.get("qa_status") not in (None, "auto")


def _carry_label_interval(pieces: list[dict]) -> None:
    """Step unlabeled warps with the interval read from the label page.

    Nothing is carried when that page did not produce an interval. The
    unlabeled warp then stays in review. A carried assignment that still marks
    a ring ``needs_review`` stays in review too.
    """
    donors = [
        piece
        for piece in pieces
        if piece.get("labels_applied") and isinstance(piece.get("interval_ft"), int) and piece["interval_ft"] > 0
    ]
    if len(donors) != 1:
        return
    interval = int(donors[0]["interval_ft"])
    for piece in pieces:
        if not piece.get("label_page_rejected") or piece.get("labels_applied"):
            continue
        if piece.get("interval_ft") is not None:
            continue
        lines = piece.get("_lines")
        parts = piece.get("_parts")
        if not lines or not parts:
            continue
        stored_gap = piece.get("_join_gap_m")
        join_gap_m = float(stored_gap) if isinstance(stored_gap, (int, float)) else JOIN_GAP_MIN_M
        assignment = assign_depths(
            lines,
            list(piece.get("_assign_labels") or []),
            parts,
            max_depth_ft=piece.get("_assign_max"),
            snap_m=float(piece.get("_snap_m") or piece.get("snap_m") or 1.0),
            interval_ft=interval,
            join_gap_m=join_gap_m,
        )
        _replace_assignment(piece, assignment)


def _replace_assignment(piece: dict, assignment) -> None:
    """Replace a rejected page's tags after the label-page interval is known."""
    emitted = [line for line in assignment.features if line.depth_ft is not None and line.source != "hydro"]
    depths = sorted({int(feature.depth_ft) for feature in assignment.features if feature.depth_ft is not None})
    review = any(line.needs_review for line in assignment.features if line.depth_ft is not None)
    reasons = [reason for reason in assignment.reasons if reason not in _LABEL_REJECT_REASONS]
    qa_status = "needs_review" if reasons or review else "auto"
    piece["qa_status"] = qa_status
    piece["interval_ft"] = assignment.interval_ft
    piece["depths_ft"] = depths
    piece["_features"] = assignment.features
    piece["contour_count"] = len(emitted)
    piece["needs_review_count"] = sum(1 for line in emitted if line.needs_review)
    piece["unassigned_count"] = assignment.unassigned_count
    piece["reasons"] = reasons
    path = piece.get("path")
    if path:
        collection = features_to_geojson(
            assignment.features,
            wbic=str(piece.get("wbic") or ""),
            interval_ft=assignment.interval_ft,
            max_depth_ft=piece.get("max_depth_ft"),
            qa_status=qa_status,
        )
        _write_json(Path(str(path)), collection)


def _recorded_labels_applied(wbic: str, page: str | None, labels) -> bool:
    body = load_recorded_labels(wbic)
    return bool(labels) and body is not None and label_page_matches(body, page)


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
            ocr_fallback=args.ocr_fallback,
            compare_ocr=args.compare_ocr,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main(sys.argv[1:])
