# SPDX-License-Identifier: GPL-3.0-only
"""Project committed sheet-pixel labels through the georef warp."""

from __future__ import annotations

import json
from pathlib import Path

from fishy_pipeline.states.wi.depth_tag import meters_to_feet
from fishy_pipeline.states.wi.pilot_data import label_file_unit, load_recorded_labels
from fishy_pipeline.states.wi.register import Affine, GroundControlPoint, ThinPlateSpline, fit_affine, fit_tps


def load_gcps_from_work(work_gcp_path: Path) -> tuple[list[GroundControlPoint], str, dict[str, float] | None]:
    body = json.loads(work_gcp_path.read_text(encoding="utf-8"))
    raw = body.get("gcps") or []
    gcps = [
        GroundControlPoint(
            pixel=float(item["pixel"]),
            line=float(item["line"]),
            easting=float(item["easting"]),
            northing=float(item["northing"]),
        )
        for item in raw
    ]
    method = str(body.get("method") or ("tps" if len(gcps) >= 3 else "affine"))
    affine_dict = body.get("affine")
    affine = None
    if isinstance(affine_dict, dict):
        affine = Affine(
            a=float(affine_dict["a"]),
            b=float(affine_dict["b"]),
            tx=float(affine_dict["tx"]),
            c=float(affine_dict["c"]),
            d=float(affine_dict["d"]),
            ty=float(affine_dict["ty"]),
        )
    elif len(gcps) >= 3:
        sources = [(gcp.pixel, gcp.line) for gcp in gcps]
        targets = [(gcp.easting, gcp.northing) for gcp in gcps]
        affine = fit_affine(sources, targets)
    return gcps, method, affine.as_dict() if affine else None


def project_pixel(
    col: float,
    row: float,
    *,
    gcps: list[GroundControlPoint],
    method: str,
    affine: Affine | None,
    tps: ThinPlateSpline | None,
) -> tuple[float, float]:
    if method == "affine" and affine is not None:
        return affine.apply(col, row)
    if tps is not None:
        return tps.apply(col, row)
    if len(gcps) >= 3:
        tps = fit_tps(gcps)
        return tps.apply(col, row)
    if affine is not None:
        return affine.apply(col, row)
    raise ValueError("cannot project sheet pixel without a warp model")


def labels_for_trace(
    wbic: str,
    *,
    gcps: list[GroundControlPoint],
    method: str,
    affine: Affine | None,
    geotransform: tuple[float, ...],
    clipped_width: int,
    clipped_height: int,
) -> tuple[list[tuple[float, float, float, float]], list[tuple[int, int, int, int]], list[str], str]:
    """Return label centers in clipped pixels, inpaint boxes, and sheet unit."""
    body = load_recorded_labels(wbic)
    if body is None:
        return [], [], [], "ft"
    unit = label_file_unit(body)
    tps = fit_tps(gcps) if method == "tps" and len(gcps) >= 3 else None
    labels_px: list[tuple[float, float, float, float]] = []
    boxes: list[tuple[int, int, int, int]] = []
    texts: list[str] = []
    for item in body.get("labels") or []:
        pixel = item.get("pixel")
        if not isinstance(pixel, (list, tuple)) or len(pixel) < 2:
            continue
        col, row = float(pixel[0]), float(pixel[1])
        value = float(item["value"])
        item_unit = str(item.get("unit") or unit)
        confidence = float(item.get("confidence", 1.0))
        easting, northing = project_pixel(col, row, gcps=gcps, method=method, affine=affine, tps=tps)
        depth = value if item_unit.startswith("m") else float(value)
        cx, cy = _map_to_clipped_pixel(easting, northing, geotransform)
        if cx < -5 or cy < -5 or cx > clipped_width + 5 or cy > clipped_height + 5:
            continue
        labels_px.append((cx, cy, depth, confidence))
        half_w, half_h = 14, 8
        x0 = max(0, int(cx - half_w))
        y0 = max(0, int(cy - half_h))
        x1 = min(clipped_width, int(cx + half_w))
        y1 = min(clipped_height, int(cy + half_h))
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        boxes.append((x0, y0, x1, y1))
        texts.append(f"{int(value) if value == int(value) else value}{'m' if item_unit.startswith('m') else ''}")
    return labels_px, boxes, texts, unit


def _map_to_clipped_pixel(easting: float, northing: float, transform: tuple[float, ...]) -> tuple[float, float]:
    origin_x, pixel_w, rot1, origin_y, rot2, pixel_h = transform
    if abs(rot1) > 1e-9 or abs(rot2) > 1e-9:
        # Clipped warps from this pipeline are north-up affines.
        pass
    if abs(pixel_w) < 1e-12 or abs(pixel_h) < 1e-12:
        return origin_x, origin_y
    col = (easting - origin_x) / pixel_w
    row = (northing - origin_y) / pixel_h
    return col, row