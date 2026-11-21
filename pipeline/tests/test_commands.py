# SPDX-License-Identifier: GPL-3.0-only
import os
import unittest
from pathlib import Path

from fishy_pipeline.commands import (
    contours_geojson_url,
    gdalwarp_to_wgs84_args,
    ogr_reproject_args,
    r2_put_command,
    tippecanoe_args,
)

ROOT = Path(__file__).resolve().parents[2]


class CommandTest(unittest.TestCase):
    def test_dnr_query_url(self):
        url = contours_geojson_url("27013300")
        self.assertIn("MapServer/0/query", url)
        self.assertIn("dowlknum%3D%2727013300%27", url)
        self.assertIn("f=geojson", url)
        with self.assertRaises(ValueError):
            contours_geojson_url("12")

    def test_tool_args_reproject_before_tiling(self):
        ogr = ogr_reproject_args("in.shp", "out.geojson", "EPSG:26915")
        self.assertEqual(ogr[:6], ["ogr2ogr", "-f", "GeoJSON", "-t_srs", "EPSG:4326", "-s_srs"])
        self.assertIn("EPSG:26915", ogr)
        warp = gdalwarp_to_wgs84_args("dem.tif", "wgs.tif", "EPSG:26915")
        self.assertEqual(warp[0], "gdalwarp")
        self.assertIn("EPSG:26915", warp)
        self.assertIn("EPSG:4326", warp)
        tiles = tippecanoe_args("out.pmtiles", [("contours", "contours-4326.geojson")])
        self.assertIn("contours:contours-4326.geojson", tiles)

    def test_r2_command_reads_env(self):
        keys = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID")
        previous = {key: os.environ.get(key) for key in keys}
        try:
            os.environ["CLOUDFLARE_API_TOKEN"] = "token-value-not-in-argv"
            os.environ["CLOUDFLARE_ACCOUNT_ID"] = "account-value-not-in-argv"
            command = r2_put_command("lake.pmtiles", "fishy-tiles", "mn/sample.pmtiles")
            self.assertEqual(
                command,
                [
                    "wrangler",
                    "r2",
                    "object",
                    "put",
                    "fishy-tiles/mn/sample.pmtiles",
                    "--file",
                    "lake.pmtiles",
                    "--remote",
                ],
            )
            joined = " ".join(command)
            self.assertNotIn("token-value-not-in-argv", joined)
            self.assertNotIn("account-value-not-in-argv", joined)
            os.environ.pop("CLOUDFLARE_API_TOKEN")
            os.environ.pop("CLOUDFLARE_ACCOUNT_ID")
            with self.assertRaises(RuntimeError):
                r2_put_command("lake.pmtiles", "fishy-tiles", "mn/sample.pmtiles")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class ArchiveTest(unittest.TestCase):
    def test_committed_samples_are_pmtiles(self):
        samples = ROOT / "apps" / "web" / "public" / "samples"
        for name in (
            "sample-pond.pmtiles",
            "sample-pond-terrain.pmtiles",
            "sample-pond-hillshade.pmtiles",
        ):
            data = (samples / name).read_bytes()
            self.assertTrue(data.startswith(b"PMTiles"), name)
            self.assertLess(len(data), 2_000_000, name)


if __name__ == "__main__":
    unittest.main()
