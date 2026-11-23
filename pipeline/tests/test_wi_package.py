# SPDX-License-Identifier: GPL-3.0-only
import json
import tempfile
import unittest
from pathlib import Path

from fishy_pipeline.common.build import build_wi_lake_artifacts, refuse_unless_publishable
from fishy_pipeline.common.upload import upload_file
from fishy_pipeline.common.wi_r2 import lake_detail_object_key, lakes_index_object_key
from fishy_pipeline.depth import is_lake_id_for_state, is_wbic
from fishy_pipeline.states.wi.package import run_fixture

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "fixtures" / "contract.json").read_text(encoding="utf-8"))


class WiPackageTest(unittest.TestCase):
    def test_fixture_build_matches_contract_scalars(self):
        with tempfile.TemporaryDirectory() as raw:
            dest = Path(raw)
            body = run_fixture(dest)
            self.assertEqual(body["lakes"], CONTRACT["wiLakeIndex"])
            detail_path = dest / lake_detail_object_key(CONTRACT["wiLakeDetail"]["id"])
            detail = json.loads(detail_path.read_text(encoding="utf-8"))
            for key in ("id", "name", "maxDepthFt", "areaAcres", "extent", "source_pdf_url", "qa_status", "survey_date"):
                self.assertEqual(detail[key], CONTRACT["wiLakeDetail"][key])
            self.assertEqual(detail["dowlknum"], CONTRACT["wiLakeDetail"]["id"])
            self.assertIn("outline", detail)
            self.assertIn("contours", detail)
            index_path = dest / lakes_index_object_key()
            index_rows = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(index_rows, CONTRACT["wiLakeIndex"])

    def test_wbic_contract_rows(self):
        for row in CONTRACT["wbic"]:
            self.assertEqual(is_wbic(row["id"]), row["valid"])
        for row in CONTRACT["lakeRef"]:
            self.assertEqual(is_lake_id_for_state(row["state"], row["id"]), row["valid"])

    def test_needs_review_lake_cannot_be_built(self):
        detail = {
            **CONTRACT["wiLakeDetail"],
            "qa_status": "needs_review",
            "outline": {"type": "FeatureCollection", "features": []},
            "contours": {"type": "FeatureCollection", "features": []},
        }
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(ValueError):
                build_wi_lake_artifacts(Path(raw), details=[detail])

    def test_needs_review_contour_item_cannot_be_built(self):
        with self.assertRaises(ValueError):
            refuse_unless_publishable(
                {
                    "wbic": "0417300",
                    "qa_status": "needs_review",
                    "publishable": False,
                    "reasons": ["georef_iou_below_threshold"],
                }
            )

    def test_needs_review_detail_cannot_upload(self):
        with tempfile.TemporaryDirectory() as raw:
            dest = Path(raw)
            detail_path = dest / "bad.json"
            detail_path.write_text(
                json.dumps({**CONTRACT["wiLakeDetail"], "qa_status": "needs_review"}),
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                upload_file(detail_path, lake_detail_object_key(CONTRACT["wiLakeDetail"]["id"]), dry_run=True)


if __name__ == "__main__":
    unittest.main()