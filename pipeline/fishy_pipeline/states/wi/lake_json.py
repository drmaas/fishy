# SPDX-License-Identifier: GPL-3.0-only
"""Assemble Wisconsin lake detail and index JSON for versioned R2 publish."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Iterable

from fishy_pipeline.common.wi_r2 import PUBLISHED_QA_STATUS, published_qa_status
from fishy_pipeline.states.wi.hydro import HydroGeometry, load_hydro_geometries
from fishy_pipeline.states.wi.manifest import read_manifest
from fishy_pipeline.states.wi.wbic import normalize_wbic

WI_SOURCE_CRS = "EPSG:3071"
WGS84 = "EPSG:4326"


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def bbox_from_geojson(data: dict) -> dict[str, float] | None:
    coords: list[tuple[float, float]] = []

    def walk(node) -> None:
        if isinstance(node, (list, tuple)):
            if len(node) >= 2 and isinstance(node[0], (int, float)) and isinstance(node[1], (int, float)):
                coords.append((float(node[0]), float(node[1])))
                return
            for child in node:
                walk(child)

    for feature in data.get("features") or []:
        walk((feature or {}).get("geometry", {}).get("coordinates"))
    if not coords:
        return None
    xs = [point[0] for point in coords]
    ys = [point[1] for point in coords]
    return {
        "west": min(xs),
        "south": min(ys),
        "east": max(xs),
        "north": max(ys),
    }


def outline_from_hydro(geometry: HydroGeometry, *, wbic: str, name: str, county: str | None, acres: float | None) -> dict:
    from fishy_pipeline.states.wi.depth_tag import WaterPart, cutline_collection

    parts = [
        WaterPart(exterior=part.exterior, holes=part.holes)
        for part in geometry.parts
    ]
    cutline = cutline_collection(parts)
    props = {
        "wbic": wbic,
        "lake_name": name,
        "dowlknum": wbic,
        "island": "N",
    }
    if county:
        props["cty_name"] = county
    if acres is not None:
        props["area_acres"] = acres
    features = []
    for feature in cutline.get("features") or []:
        features.append(
            {
                "type": "Feature",
                "properties": dict(props),
                "geometry": feature.get("geometry"),
            }
        )
    return {
        "type": "FeatureCollection",
        "crs": cutline.get("crs"),
        "features": features,
    }


def _contour_features_for_response(collection: dict, *, wbic: str, lake_name: str) -> list[dict]:
    out: list[dict] = []
    for feature in collection.get("features") or []:
        if not isinstance(feature, dict):
            continue
        props = dict(feature.get("properties") or {})
        depth_ft = _num(props.get("depth_ft"))
        if depth_ft is None:
            continue
        out.append(
            {
                "type": "Feature",
                "properties": {
                    "lake_name": lake_name,
                    "dowlknum": wbic,
                    "depth_ft": depth_ft,
                },
                "geometry": feature.get("geometry"),
            }
        )
    return out


def merge_contour_collections(paths: Iterable[Path], *, wbic: str, lake_name: str) -> dict:
  features: list[dict] = []
  for path in paths:
    if not path.is_file():
      continue
    data = json.loads(path.read_text(encoding="utf-8"))
    features.extend(_contour_features_for_response(data, wbic=wbic, lake_name=lake_name))
  return {"type": "FeatureCollection", "features": features}


def index_row_from_detail(detail: dict) -> dict:
  extent = detail["extent"]
  return {
    "id": detail["id"],
    "name": detail["name"],
    "county": detail.get("county") or "",
    "acres": detail["areaAcres"],
    "bbox": {
      "west": extent["west"],
      "south": extent["south"],
      "east": extent["east"],
      "north": extent["north"],
    },
    "qa_status": detail["qa_status"],
  }


def build_lake_detail_payload(
  *,
  wbic: str,
  name: str,
  county: str | None,
  area_acres: float,
  max_depth_ft: float,
  source_pdf_url: str | None,
  survey_date: str | None,
  publishable: bool,
  outline: dict,
  contours: dict,
) -> dict:
  qa_status = published_qa_status(publishable=publishable)
  extent = bbox_from_geojson(outline) or bbox_from_geojson(contours)
  if extent is None:
    raise ValueError(f"could not derive extent for wbic {wbic}")
  payload: dict = {
    "id": wbic,
    "dowlknum": wbic,
    "name": name,
    "maxDepthFt": max_depth_ft,
    "areaAcres": area_acres,
    "extent": extent,
    "outline": outline,
    "contours": contours,
    "qa_status": qa_status,
  }
  if county:
    payload["county"] = county
  if source_pdf_url:
    payload["source_pdf_url"] = source_pdf_url
  if survey_date:
    payload["survey_date"] = survey_date
  return payload


def manifest_entry_for_wbic(manifest: dict, wbic: str) -> dict | None:
  for entry in manifest.get("entries") or []:
    if normalize_wbic(entry.get("wbic")) == wbic:
      return entry
  return None


def contour_paths_for_items(items: list[dict]) -> list[Path]:
  paths: list[Path] = []
  for item in items:
    path = item.get("path")
    if not path:
      continue
    paths.append(Path(str(path)))
  return paths


def load_contour_results(work_dir: Path) -> dict:
  path = work_dir / "contours" / "results.json"
  if not path.is_file():
    raise ValueError(f"contour results not found: {path}")
  return json.loads(path.read_text(encoding="utf-8"))


def group_contour_items(results: dict) -> dict[str, list[dict]]:
  grouped: dict[str, list[dict]] = {}
  for item in results.get("items") or []:
    if not isinstance(item, dict):
      continue
    wbic = normalize_wbic(item.get("wbic"))
    if wbic is None:
      continue
    grouped.setdefault(wbic, []).append(item)
  return grouped


def reproject_geojson_in_memory(data: dict, ogr: str, work: Path, source_srs: str = WI_SOURCE_CRS) -> dict:
  from fishy_pipeline.common.commands import ogr_reproject_args

  src = work / "src.geojson"
  dest = work / "dest.geojson"
  src.write_text(json.dumps(data), encoding="utf-8")
  command = [ogr, *ogr_reproject_args(str(src), str(dest), source_srs)[1:]]
  import subprocess
  import sys

  completed = subprocess.run(command, check=False, text=True, capture_output=True)
  if completed.returncode != 0:
    sys.stderr.write(completed.stderr)
    sys.stderr.write(completed.stdout)
    raise SystemExit(f"command failed ({completed.returncode}): {command[0]}")
  return json.loads(dest.read_text(encoding="utf-8"))


def build_publishable_lake_detail(
  *,
  wbic: str,
  items: list[dict],
  manifest_path: Path,
  hydro_path: Path,
  ogr: str,
  work: Path,
) -> dict:
  manifest = read_manifest(manifest_path)
  entry = manifest_entry_for_wbic(manifest, wbic)
  if entry is None:
    raise ValueError(f"manifest entry missing for wbic {wbic}")
  geometries = load_hydro_geometries(hydro_path)
  geometry = geometries.get(wbic)
  if geometry is None:
    raise ValueError(f"hydro geometry missing for wbic {wbic}")
  name = str(entry.get("name") or geometry.waterbody_name or "Unnamed lake")
  county = str(entry.get("county") or "") or None
  acres = _num(entry.get("acres"))
  if acres is None:
    raise ValueError(f"acres missing for wbic {wbic}")
  max_depth = _num(entry.get("max_depth_ft"))
  if max_depth is None:
    for item in items:
      max_depth = _num(item.get("max_depth_ft"))
      if max_depth is not None:
        break
  if max_depth is None:
    raise ValueError(f"max_depth_ft missing for wbic {wbic}")
  outline_native = outline_from_hydro(
    geometry,
    wbic=wbic,
    name=name,
    county=county,
    acres=acres,
  )
  outline = reproject_geojson_in_memory(outline_native, ogr, work)
  contours_native = merge_contour_collections(contour_paths_for_items(items), wbic=wbic, lake_name=name)
  contours = reproject_geojson_in_memory(contours_native, ogr, work)
  return build_lake_detail_payload(
    wbic=wbic,
    name=name,
    county=county,
    area_acres=acres,
    max_depth_ft=max_depth,
    source_pdf_url=str(entry.get("pdf_url") or "") or None,
    survey_date=None,
    publishable=True,
    outline=outline,
    contours=contours,
  )


def build_lakes_index(details: list[dict]) -> list[dict]:
    return [index_row_from_detail(detail) for detail in details]