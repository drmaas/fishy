# SPDX-License-Identifier: GPL-3.0-only
"""Subprocess argument builders. Credentials stay in the environment."""

from __future__ import annotations

import os
import shutil
import urllib.parse

from fishy_pipeline.common.depth import is_dowlknum

CONTOURS_LAYER = (
    "https://enterprise.gisdata.mn.gov/aghost/rest/services/"
    "us_mn_state_dnr/water_lake_bathymetry/MapServer/0"
)
OUTLINES_LAYER = (
    "https://enterprise.gisdata.mn.gov/aghost/rest/services/"
    "us_mn_state_dnr/water_lake_bathymetry/MapServer/1"
)
DEM_LAYER = (
    "https://enterprise.gisdata.mn.gov/aghost/rest/services/"
    "us_mn_state_dnr/water_lake_bathymetry/MapServer/3"
)
SHADED_RELIEF_IMAGE = (
    "https://enterprise.gisdata.mn.gov/aghost/rest/services/"
    "us_mn_state_dnr/water_lake_bathy_shaded_relief/ImageServer"
)


def contours_geojson_url(dowlknum: str) -> str:
    if not is_dowlknum(dowlknum):
        raise ValueError("dowlknum must be 8 digits")
    query = urllib.parse.urlencode(
        {
            "where": f"dowlknum='{dowlknum}'",
            "outFields": "*",
            "f": "geojson",
        }
    )
    return f"{CONTOURS_LAYER}/query?{query}"


def read_source_srs(directory: str) -> str:
    path = os.path.join(directory, "source.srs")
    if not os.path.isfile(path):
        return "EPSG:4326"
    with open(path, encoding="utf-8") as handle:
        value = handle.read().strip()
    code = value[5:]
    if not value.startswith("EPSG:") or not code or any(char < "0" or char > "9" for char in code):
        raise ValueError("source.srs must be an EPSG code such as EPSG:26915")
    return value


def ogr_reproject_args(source: str, dest: str, source_srs: str = "EPSG:4326") -> list[str]:
    return [
        "ogr2ogr",
        "-f",
        "GeoJSON",
        "-t_srs",
        "EPSG:4326",
        "-s_srs",
        source_srs,
        "-overwrite",
        dest,
        source,
    ]


def tippecanoe_args(output: str, layers: list[tuple[str, str]]) -> list[str]:
    command = [
        "tippecanoe",
        "-o",
        output,
        "--force",
        "-Z",
        "8",
        "-z",
        "14",
        "--no-feature-limit",
        "--no-tile-size-limit",
        "--drop-densest-as-needed",
    ]
    for name, path in layers:
        command.extend(["-L", f"{name}:{path}"])
    return command


def gdal_translate_asc_args(source: str, dest: str) -> list[str]:
    return ["gdal_translate", source, dest]


def gdalwarp_to_wgs84_args(source: str, dest: str, source_srs: str) -> list[str]:
    return [
        "gdalwarp",
        "-s_srs",
        source_srs,
        "-t_srs",
        "EPSG:4326",
        "-r",
        "near",
        "-overwrite",
        source,
        dest,
    ]


def gdaldem_hillshade_args(source: str, dest: str) -> list[str]:
    return ["gdaldem", "hillshade", source, dest, "-s", "111120", "-z", "8"]


def gdal_mbtiles_args(source: str, dest: str) -> list[str]:
    return [
        "gdal_translate",
        "-of",
        "MBTiles",
        source,
        dest,
        "-co",
        "TILE_FORMAT=PNG",
        "-co",
        "RESAMPLING=NEAREST",
    ]


def pmtiles_convert_args(source: str, dest: str) -> list[str]:
    return ["pmtiles", "convert", source, dest]


def find_tool(name: str, env_key: str) -> str | None:
    override = os.environ.get(env_key)
    if override:
        return override
    return shutil.which(name)


def r2_put_command(file: str, bucket: str, key: str, wrangler: str = "wrangler") -> list[str]:
    """Build `wrangler r2 object put`. The API token is read by wrangler from the environment."""
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    if not token or not account:
        raise RuntimeError("CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID must be set in the environment")
    if not bucket or any(char in bucket for char in " /\n\t"):
        raise RuntimeError("R2_BUCKET must be a single bucket name")
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise RuntimeError("object key must be a relative path")
    command = [wrangler, "r2", "object", "put", f"{bucket}/{key}", "--file", file, "--remote"]
    for part in command:
        if token in part or account in part:
            raise RuntimeError("refusing to put credentials on the command line")
    return command