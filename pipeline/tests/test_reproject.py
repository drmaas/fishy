# SPDX-License-Identifier: GPL-3.0-only
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fishy_pipeline.commands import gdalwarp_to_wgs84_args, ogr_reproject_args, read_source_srs

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "utm-26915"


class ReprojectTest(unittest.TestCase):
    def test_fixture_declares_utm(self):
        self.assertEqual(read_source_srs(str(FIXTURE)), "EPSG:26915")

    @unittest.skipUnless(shutil.which("ogr2ogr"), "ogr2ogr")
    def test_utm_point_becomes_lon_lat(self):
        with tempfile.TemporaryDirectory() as raw:
            dest = Path(raw) / "point.geojson"
            command = ogr_reproject_args(str(FIXTURE / "point.geojson"), str(dest), "EPSG:26915")
            subprocess.run(command, check=True, capture_output=True, text=True)
            lon, lat = json.loads(dest.read_text())["features"][0]["geometry"]["coordinates"]
            self.assertAlmostEqual(lon, -93.27, places=2)
            self.assertAlmostEqual(lat, 44.98, places=2)

    @unittest.skipUnless(shutil.which("gdalwarp") and shutil.which("gdal_translate"), "gdal")
    def test_utm_grid_warps_to_wgs84(self):
        with tempfile.TemporaryDirectory() as raw:
            work = Path(raw)
            raw_tif = work / "raw.tif"
            warped = work / "wgs.tif"
            subprocess.run(
                ["gdal_translate", str(FIXTURE / "dem.asc"), str(raw_tif)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                gdalwarp_to_wgs84_args(str(raw_tif), str(warped), "EPSG:26915"),
                check=True,
                capture_output=True,
                text=True,
            )
            info = json.loads(
                subprocess.check_output(["gdalinfo", "-json", str(warped)], text=True)
            )
            upper_left = info["cornerCoordinates"]["upperLeft"]
            self.assertAlmostEqual(upper_left[0], -93.27, places=2)
            self.assertAlmostEqual(upper_left[1], 44.98, places=2)


if __name__ == "__main__":
    unittest.main()
