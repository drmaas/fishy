# SPDX-License-Identifier: GPL-3.0-only
"""Turn a small lake directory into vector, terrain, and hillshade PMTiles."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from fishy_pipeline.common.commands import (
    find_tool,
    gdal_mbtiles_args,
    gdal_translate_asc_args,
    gdalwarp_to_wgs84_args,
    gdaldem_hillshade_args,
    ogr_reproject_args,
    pmtiles_convert_args,
    read_source_srs,
    tippecanoe_args,
)
from fishy_pipeline.common.depth import normalize_properties
from fishy_pipeline.common.wi_r2 import (
    PUBLISHED_QA_STATUS,
    WI_R2_PREFIX,
    lake_detail_object_key,
    lakes_index_object_key,
    refuse_unless_publishable,
)
from fishy_pipeline.states.wi.lake_json import build_lakes_index


def load_normalized(path: Path) -> dict:
    data = json.loads(path.read_text())
    for feature in data["features"]:
        feature["properties"] = normalize_properties(feature["properties"])
    return data


def attach_max_depth(lakes: dict, contours: dict) -> dict:
    deepest: dict[str, float] = {}
    for feature in contours["features"]:
        props = feature["properties"]
        dowlknum = props["dowlknum"]
        deepest[dowlknum] = max(deepest.get(dowlknum, 0.0), float(props["depth_ft"]))
    for feature in lakes["features"]:
        props = feature["properties"]
        props.setdefault("max_depth_ft", deepest.get(props["dowlknum"], 0.0))
    return lakes


def write_geojson(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr)
        sys.stderr.write(completed.stdout)
        raise SystemExit(f"command failed ({completed.returncode}): {command[0]}")


def write_elevation_and_terrarium(dem_asc: Path, work: Path, source_srs: str) -> tuple[Path, Path]:
    from osgeo import gdal

    gdal.UseExceptions()
    raw_tif = work / "depth_raw.tif"
    depth_tif = work / "depth_ft.tif"
    _run(gdal_translate_asc_args(str(dem_asc), str(raw_tif)))
    _run(gdalwarp_to_wgs84_args(str(raw_tif), str(depth_tif), source_srs))
    depth_ds = gdal.Open(str(depth_tif))
    band = depth_ds.GetRasterBand(1)
    depths = band.ReadAsArray().astype("float64")
    nodata = band.GetNoDataValue()
    elev = depths * 0.3048
    if nodata is not None:
        elev[depths == nodata] = 0.0
    elev_tif = work / "elev_m.tif"
    driver = gdal.GetDriverByName("GTiff")
    elev_ds = driver.Create(str(elev_tif), depth_ds.RasterXSize, depth_ds.RasterYSize, 1, gdal.GDT_Float32)
    elev_ds.SetGeoTransform(depth_ds.GetGeoTransform())
    elev_ds.SetProjection(depth_ds.GetProjection())
    elev_ds.GetRasterBand(1).WriteArray(elev.astype("float32"))
    elev_ds.FlushCache()
    elev_ds = None

    shifted = elev + 32768.0
    red = (shifted // 256).astype("uint8")
    green = (shifted % 256).astype("uint8")
    blue = ((shifted * 256) % 256).astype("uint8")
    terrain_tif = work / "terrarium.tif"
    terrain = driver.Create(str(terrain_tif), depth_ds.RasterXSize, depth_ds.RasterYSize, 3, gdal.GDT_Byte)
    terrain.SetGeoTransform(depth_ds.GetGeoTransform())
    terrain.SetProjection(depth_ds.GetProjection())
    terrain.GetRasterBand(1).WriteArray(red)
    terrain.GetRasterBand(2).WriteArray(green)
    terrain.GetRasterBand(3).WriteArray(blue)
    terrain.FlushCache()
    terrain = None
    depth_ds = None
    return elev_tif, terrain_tif


def raster_pmtiles(source: Path, dest: Path, work: Path, label: str) -> None:
    pmtiles = find_tool("pmtiles", "PMTILES")
    if not pmtiles:
        raise SystemExit("pmtiles CLI is not on PATH")
    mbtiles = work / f"{label}.mbtiles"
    _run(gdal_mbtiles_args(str(source), str(mbtiles)))
    _run([pmtiles, *pmtiles_convert_args(str(mbtiles), str(dest))[1:]])


def reproject_vectors(ogr: str, staged: dict[str, Path], work: Path, source_srs: str) -> dict[str, Path]:
    reprojected: dict[str, Path] = {}
    for name, path in staged.items():
        dest = work / f"{name}-4326.geojson"
        _run([ogr, *ogr_reproject_args(str(path), str(dest), source_srs)[1:]])
        if not dest.exists():
            raise SystemExit(f"ogr2ogr did not write {name}")
        reprojected[name] = dest
    return reprojected


def build_lake(source: Path, dest: Path) -> None:
    tippecanoe = find_tool("tippecanoe", "TIPPECANOE")
    ogr = find_tool("ogr2ogr", "OGR2OGR")
    if not tippecanoe or not ogr:
        raise SystemExit("tippecanoe and ogr2ogr must be on PATH")
    source_srs = read_source_srs(str(source))
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fishy-") as raw:
        work = Path(raw)
        lakes = attach_max_depth(
            load_normalized(source / "lakes.geojson"),
            load_normalized(source / "contours.geojson"),
        )
        zones = load_normalized(source / "zones.geojson")
        contours = load_normalized(source / "contours.geojson")
        staged = {
            "lakes": work / "lakes.geojson",
            "contours": work / "contours.geojson",
            "zones": work / "zones.geojson",
        }
        write_geojson(staged["lakes"], lakes)
        write_geojson(staged["contours"], contours)
        write_geojson(staged["zones"], zones)
        reprojected = reproject_vectors(ogr, staged, work, source_srs)
        vector = dest / "sample-pond.pmtiles"
        _run(
            [
                tippecanoe,
                *tippecanoe_args(
                    str(vector),
                    [
                        ("lakes", str(reprojected["lakes"])),
                        ("contours", str(reprojected["contours"])),
                        ("zones", str(reprojected["zones"])),
                    ],
                )[1:],
            ]
        )
        elev_tif, terrain_tif = write_elevation_and_terrarium(source / "dem.asc", work, source_srs)
        hillshade = work / "hillshade.tif"
        _run(gdaldem_hillshade_args(str(elev_tif), str(hillshade)))
        raster_pmtiles(terrain_tif, dest / "sample-pond-terrain.pmtiles", work, "terrain")
        raster_pmtiles(hillshade, dest / "sample-pond-hillshade.pmtiles", work, "hillshade")


def build_wi_lake_artifacts(
    dest: Path,
    *,
    fixtures: bool = False,
    details: list[dict] | None = None,
    skipped: list[dict] | None = None,
) -> dict:
    """Write ``wi/v1/lakes/*.json`` and ``wi/v1/lakes_index.json`` locally."""
    dest.mkdir(parents=True, exist_ok=True)
    if fixtures:
        root = Path(__file__).resolve().parents[3]
        contract = json.loads((root / "fixtures" / "contract.json").read_text(encoding="utf-8"))
        scalar = contract["wiLakeDetail"]
        outline = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "lake_name": scalar["name"],
                        "dowlknum": scalar["id"],
                        "island": "N",
                        "area_acres": scalar["areaAcres"],
                        "cty_name": contract["wiLakeIndex"][0]["county"],
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [scalar["extent"]["west"], scalar["extent"]["south"]],
                                [scalar["extent"]["east"], scalar["extent"]["south"]],
                                [scalar["extent"]["east"], scalar["extent"]["north"]],
                                [scalar["extent"]["west"], scalar["extent"]["north"]],
                                [scalar["extent"]["west"], scalar["extent"]["south"]],
                            ]
                        ],
                    },
                }
            ],
        }
        contours = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "lake_name": scalar["name"],
                        "dowlknum": scalar["id"],
                        "depth_ft": 20,
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [scalar["extent"]["west"] + 0.01, scalar["extent"]["south"] + 0.01],
                            [scalar["extent"]["east"] - 0.01, scalar["extent"]["north"] - 0.01],
                        ],
                    },
                }
            ],
        }
        detail = {
            **scalar,
            "dowlknum": scalar["id"],
            "extent": dict(scalar["extent"]),
            "outline": outline,
            "contours": contours,
            "county": contract["wiLakeIndex"][0]["county"],
            "qa_status": PUBLISHED_QA_STATUS,
        }
        details = [detail]
    if details is None:
        raise ValueError("details are required unless fixtures=True")
    for detail in details:
        wbic = str(detail.get("id") or detail.get("dowlknum") or "")
        if detail.get("qa_status") != PUBLISHED_QA_STATUS:
            raise ValueError(f"lake {wbic} qa_status is not {PUBLISHED_QA_STATUS!r}")
        lake_path = dest / lake_detail_object_key(wbic)
        lake_path.parent.mkdir(parents=True, exist_ok=True)
        lake_path.write_text(json.dumps(detail, indent=2) + "\n", encoding="utf-8")
    index_rows = build_lakes_index(details)
    index_path = dest / lakes_index_object_key()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index_rows, indent=2) + "\n", encoding="utf-8")
    return {
        "version": 1,
        "state": "wi",
        "prefix": WI_R2_PREFIX,
        "lakes": index_rows,
        "skipped": skipped or [],
        "files": [lake_detail_object_key(str(row["id"])) for row in index_rows] + [lakes_index_object_key()],
    }


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        raise SystemExit("usage: python3 -m fishy_pipeline build SOURCE_DIR DEST_DIR")
    build_lake(Path(args[0]), Path(args[1]))