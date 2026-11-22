# SPDX-License-Identifier: GPL-3.0-only
"""Build the synthetic WI georef fixture from ``fixtures/wi-georef/known-transform.json``.

The raster is drawn in memory (border, PLSS grid, legend, one shoreline) and written
under the work directory. Nothing here is a real DNR extract.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from fishy_pipeline.states.wi.pngutil import RasterImage, write_png
from fishy_pipeline.states.wi.register import Affine

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURE_SPEC = REPO_ROOT / "fixtures" / "wi-georef" / "known-transform.json"


def load_spec(path: Path | None = None) -> dict:
    return json.loads((path or FIXTURE_SPEC).read_text(encoding="utf-8"))


def affine_from_spec(spec: dict) -> Affine:
    """Pixel column/row → EPSG:3071. Row increases south (a y-down raster)."""
    theta = math.radians(float(spec["theta_deg"]))
    scale = float(spec["scale_m_per_px"])
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    return Affine(
        a=scale * cos_t,
        b=-scale * sin_t,
        tx=float(spec["origin_easting"]),
        c=-scale * sin_t,
        d=-scale * cos_t,
        ty=float(spec["origin_northing"]),
    )


def pixel_to_map(spec: dict, col: float, row: float) -> tuple[float, float]:
    return affine_from_spec(spec).apply(col, row)


def render_map(spec: dict) -> RasterImage:
    """White sheet, full-span grid and neatline, a corner legend, and the lake ring."""
    width = int(spec["width"])
    height = int(spec["height"])
    rgb = bytearray([255]) * (width * height * 3)
    inset = int(spec.get("border_inset", 2))
    _stroke_rect(rgb, width, height, inset, inset, width - 1 - inset, height - 1 - inset)
    step = int(spec.get("grid_step", 20))
    for x in range(0, width, step):
        _line(rgb, width, height, x, 0, x, height - 1)
    for y in range(0, height, step):
        _line(rgb, width, height, 0, y, width - 1, y)
    legend = spec.get("legend")
    if legend:
        x, y, box_w, box_h = (int(value) for value in legend)
        _fill_rect(rgb, width, height, x, y, x + box_w - 1, y + box_h - 1)
    ring = [(float(point[0]), float(point[1])) for point in spec["pixel_ring"]]
    for index, start in enumerate(ring):
        end = ring[(index + 1) % len(ring)]
        _line(rgb, width, height, int(round(start[0])), int(round(start[1])), int(round(end[0])), int(round(end[1])))
    return RasterImage(width=width, height=height, rgb=bytes(rgb))


def map_ring(spec: dict, pixel_ring: list) -> list[list[float]]:
    return [[*pixel_to_map(spec, float(point[0]), float(point[1]))] for point in pixel_ring]


def materialize_fixture(dest: Path, spec: dict | None = None) -> tuple[Path, Path]:
    """Write a manifest, EPSG:3071 hydro GeoJSON, and PNGs. Returns those two paths."""
    spec = spec or load_spec()
    dest.mkdir(parents=True, exist_ok=True)
    image = render_map(spec)
    rasters = dest / "rasters"
    written: dict[str, Path] = {}
    for case in spec["cases"]:
        if case.get("role") == "skip":
            continue
        map_key = case["map_key"]
        if map_key in written:
            continue
        path = rasters / f"{map_key}.png"
        write_png(path, image)
        written[map_key] = path

    features = []
    entries = []
    for case in spec["cases"]:
        role = case["role"]
        entry = {
            "map_key": case["map_key"],
            "wbic": case["wbic"],
            "sheet_suffix": case["map_key"][-1],
            "name": case.get("name"),
            "multi_sheet": bool(case.get("multi_sheet", False)),
            "hydro_matched": role != "skip",
        }
        if role == "skip":
            entry["source_media_type"] = case.get("source_media_type", "unknown")
            entry["skip_reason"] = case["skip_reason"]
            entries.append(entry)
            continue
        entry["source_media_type"] = "png"
        entry["raster_path"] = str(written[case["map_key"]])
        entry["hydro"] = {
            "waterbody_name": case["name"],
            "hydroid": None,
            "hydrotype": int(case["hydrotype"]),
        }
        pixel_ring = spec["mismatch_pixel_ring"] if role == "mismatch" else spec["pixel_ring"]
        ring = map_ring(spec, pixel_ring)
        closed = ring + [ring[0]]
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "WATERBODY_WBIC": int(case["wbic"]),
                    "WATERBODY_NAME": case["name"],
                    "HYDROTYPE": int(case["hydrotype"]),
                },
                "geometry": {"type": "Polygon", "coordinates": [closed]},
            }
        )
        entries.append(entry)

    hydro_path = dest / "hydro.geojson"
    hydro_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3071"}},
                "features": features,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path = dest / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "state": "wi",
                "entries": entries,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path, hydro_path


def _set_black(rgb: bytearray, width: int, height: int, x: int, y: int) -> None:
    if 0 <= x < width and 0 <= y < height:
        offset = (y * width + x) * 3
        rgb[offset : offset + 3] = b"\x00\x00\x00"


def _line(rgb: bytearray, width: int, height: int, x0: int, y0: int, x1: int, y1: int) -> None:
    """4-connected Bresenham so a closed ring stays one component."""
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    while True:
        _set_black(rgb, width, height, x0, y0)
        if x0 == x1 and y0 == y1:
            break
        doubled = 2 * err
        if doubled > -dy:
            err -= dy
            x0 += sx
            _set_black(rgb, width, height, x0, y0)
        if doubled < dx:
            err += dx
            y0 += sy
            _set_black(rgb, width, height, x0, y0)


def _stroke_rect(
    rgb: bytearray,
    width: int,
    height: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
) -> None:
    _line(rgb, width, height, x0, y0, x1, y0)
    _line(rgb, width, height, x1, y0, x1, y1)
    _line(rgb, width, height, x1, y1, x0, y1)
    _line(rgb, width, height, x0, y1, x0, y0)


def _fill_rect(
    rgb: bytearray,
    width: int,
    height: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
) -> None:
    for y in range(min(y0, y1), max(y0, y1) + 1):
        for x in range(min(x0, x1), max(x0, x1) + 1):
            _set_black(rgb, width, height, x, y)
