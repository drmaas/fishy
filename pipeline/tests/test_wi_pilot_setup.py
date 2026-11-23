# SPDX-License-Identifier: GPL-3.0-only
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fishy_pipeline.states.wi import gcp_review
from fishy_pipeline.states.wi.hydro_fetch import pilot_hydro_complete
from fishy_pipeline.states.wi.pilot_data import pilot_wbics
from fishy_pipeline.states.wi.pilot_setup import (
    PILOT_SETUP_CLI,
    arch_install_hint,
    check_system_deps,
    pilot_catalog_rows,
)


class PilotSetupTest(unittest.TestCase):
    def test_pilot_wbic_count(self):
        self.assertEqual(len(pilot_wbics()), 10)

    def test_pilot_catalog_rows_match_labels(self):
        rows = pilot_catalog_rows()
        self.assertEqual(len(rows), 10)
        self.assertEqual({row.wbic for row in rows}, set(pilot_wbics()))

    def test_gcp_review_refuses_missing_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            with self.assertRaises(SystemExit) as ctx:
                gcp_review.main(["--work-dir", str(work)])
            self.assertIn("pilot-setup", str(ctx.exception))
            self.assertIn(PILOT_SETUP_CLI, str(ctx.exception))

    def test_gcp_review_refuses_empty_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            manifest = work / "manifest.json"
            hydro = work / "pilot-hydro.geojson"
            manifest.write_text(json.dumps({"entries": []}), encoding="utf-8")
            hydro.write_text(
                json.dumps(
                    {
                        "type": "FeatureCollection",
                        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3071"}},
                        "features": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit) as ctx:
                gcp_review.main(["--work-dir", str(work)])
            self.assertIn("no lakes", str(ctx.exception).lower())

    def test_pilot_hydro_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pilot-hydro.geojson"
            wbics = pilot_wbics()[:2]
            features = [
                {
                    "type": "Feature",
                    "properties": {"WATERBODY_WBIC": int(wbic)},
                    "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
                }
                for wbic in wbics
            ]
            path.write_text(
                json.dumps(
                    {
                        "type": "FeatureCollection",
                        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3071"}},
                        "features": features,
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(pilot_hydro_complete(path, wbics))
            self.assertFalse(pilot_hydro_complete(path, pilot_wbics()))

    def test_arch_install_hint_documents_uv_venv(self):
        hint = arch_install_hint()
        self.assertIn("uv venv", hint)
        self.assertIn("--system-site-packages", hint)
        self.assertIn("uv pip install", hint)

    def test_pilot_setup_cli_uses_venv_python(self):
        self.assertIn(".venv/bin/python", PILOT_SETUP_CLI)
        self.assertIn("PYTHONPATH=pipeline", PILOT_SETUP_CLI)

    def test_check_system_deps_with_gdal(self):
        with patch(
            "fishy_pipeline.states.wi.pilot_setup.shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}" if name.startswith("gdal") else None,
        ):
            self.assertEqual(check_system_deps(), [])