# SPDX-License-Identifier: GPL-3.0-only
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fishy_pipeline.states.wi.pilot_data import (
    REVIEWED_GCP_DIR,
    depths_match_recorded,
    recorded_depths_ft,
    save_reviewed_gcps,
)
from fishy_pipeline.states.wi.publish import publish_gate
from fishy_pipeline.states.wi.register import (
    GroundControlPoint,
    registration_from_reviewed,
)


class PilotDataTest(unittest.TestCase):
    def test_recorded_depths_feet_and_meters(self):
        body = {
            "unit": "ft",
            "labels": [{"pixel": [1, 2], "value": 5, "unit": "ft", "confidence": 1}],
        }
        self.assertEqual(recorded_depths_ft(body), [5])
        metric = {
            "unit": "m",
            "labels": [{"pixel": [1, 2], "value": 2, "unit": "m", "confidence": 1}],
        }
        self.assertEqual(recorded_depths_ft(metric), [7])

    def test_depths_match_recorded(self):
        body = {"unit": "ft", "labels": [{"pixel": [0, 0], "value": 5, "confidence": 1}]}
        ok, reasons = depths_match_recorded([5], body)
        self.assertTrue(ok)
        self.assertEqual(reasons, [])
        ok, reasons = depths_match_recorded([5, 99], body)
        self.assertFalse(ok)
        self.assertIn("depth_not_in_recorded_labels", reasons)

    def test_save_reviewed_gcps(self):
        with tempfile.TemporaryDirectory() as tmp:
            gcps_dir = Path(tmp) / "gcps"
            with patch("fishy_pipeline.states.wi.pilot_data.REVIEWED_GCP_DIR", gcps_dir):
                path = save_reviewed_gcps(
                    "0417300",
                    {
                        "gcps": [
                            {"pixel": 1, "line": 2, "easting": 3, "northing": 4},
                            {"pixel": 2, "line": 3, "easting": 4, "northing": 5},
                            {"pixel": 3, "line": 4, "easting": 5, "northing": 6},
                            {"pixel": 4, "line": 5, "easting": 6, "northing": 7},
                        ]
                    },
                )
            self.assertTrue(path.is_file())
            self.assertEqual(json.loads(path.read_text())["wbic"], "0417300")


class PublishGateTest(unittest.TestCase):
    def test_publish_requires_iou_and_labels(self):
        with patch(
            "fishy_pipeline.states.wi.publish.load_recorded_labels",
            return_value={"unit": "ft", "labels": [{"value": 5, "pixel": [0, 0], "confidence": 1}]},
        ):
            ok, status, reasons = publish_gate(wbic="0417300", iou=0.95, depths_ft=[5])
            self.assertTrue(ok)
            self.assertEqual(status, "auto")
            self.assertEqual(reasons, [])
        with patch("fishy_pipeline.states.wi.publish.load_recorded_labels", return_value=None):
            ok, status, reasons = publish_gate(wbic="0417300", iou=0.95, depths_ft=[5])
            self.assertFalse(ok)
            self.assertIn("recorded_labels_missing", reasons)


class ReviewedRegistrationTest(unittest.TestCase):
    def test_registration_from_reviewed_gcps(self):
        ring = [(0, 0), (10, 0), (10, 10), (0, 10)]
        hydro = [(100, 100), (110, 100), (110, 110), (100, 110)]
        gcps = [
            GroundControlPoint(0, 0, 100, 100),
            GroundControlPoint(10, 0, 110, 100),
            GroundControlPoint(10, 10, 110, 110),
            GroundControlPoint(0, 10, 100, 110),
        ]
        reg = registration_from_reviewed(ring, hydro, gcps, method="affine")
        self.assertGreater(reg.iou, 0.95)