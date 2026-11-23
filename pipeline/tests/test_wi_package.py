# SPDX-License-Identifier: GPL-3.0-only
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fishy_pipeline.common.build import build_wi_lake_artifacts, refuse_unless_publishable
from fishy_pipeline.common.upload import upload_file, upload_wi_artifacts
from fishy_pipeline.common.wi_r2 import (
    CURRENT_POINTER_KEY,
    PACKAGE_MANIFEST_NAME,
    generation_lake_detail_key,
    generation_lakes_index_key,
)
from fishy_pipeline.depth import is_lake_id_for_state, is_wbic
from fishy_pipeline.states.wi.package import run_fixture, run_package

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "fixtures" / "contract.json").read_text(encoding="utf-8"))


def _detail(wbic: str) -> dict:
    return {
        **CONTRACT["wiLakeDetail"],
        "id": wbic,
        "dowlknum": wbic,
        "qa_status": "verified",
        "outline": {"type": "FeatureCollection", "features": []},
        "contours": {"type": "FeatureCollection", "features": []},
    }


class WiPackageTest(unittest.TestCase):
    def test_fixture_build_matches_contract_scalars(self):
        with tempfile.TemporaryDirectory() as raw:
            dest = Path(raw)
            body = run_fixture(dest)
            build_id = body["build_id"]
            self.assertEqual(body["lakes"], CONTRACT["wiLakeIndex"])
            detail_path = dest / generation_lake_detail_key(build_id, CONTRACT["wiLakeDetail"]["id"])
            detail = json.loads(detail_path.read_text(encoding="utf-8"))
            for key in ("id", "name", "maxDepthFt", "areaAcres", "extent", "source_pdf_url", "qa_status", "survey_date"):
                self.assertEqual(detail[key], CONTRACT["wiLakeDetail"][key])
            index_path = dest / generation_lakes_index_key(build_id)
            index_rows = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(index_rows, CONTRACT["wiLakeIndex"])
            manifest = json.loads((dest / PACKAGE_MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["uploads"]), 2)

    def test_shrinking_index_drops_stale_lake_from_upload_set(self):
        with tempfile.TemporaryDirectory() as raw:
            dest = Path(raw)
            build_wi_lake_artifacts(dest, build_id="genone", details=[_detail("0417300"), _detail("0980900")])
            stale = dest / generation_lake_detail_key("genone", "0980900")
            self.assertTrue(stale.is_file())
            build_wi_lake_artifacts(dest, build_id="gentwo", details=[_detail("0417300")])
            self.assertFalse(stale.is_file())
            self.assertFalse((dest / generation_lake_detail_key("genone", "0417300")).is_file())
            manifest = json.loads((dest / PACKAGE_MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual(manifest["build_id"], "gentwo")
            keys = [entry["key"] for entry in manifest["uploads"]]
            self.assertEqual(keys, [generation_lake_detail_key("gentwo", "0417300"), generation_lakes_index_key("gentwo")])

    def test_partial_upload_failure_skips_pointer(self):
        with tempfile.TemporaryDirectory() as raw:
            dest = Path(raw)
            build_wi_lake_artifacts(dest, build_id="genupload", details=[_detail("0417300")])
            calls: list[str] = []

            def flaky_put(file_path: Path, key: str) -> None:
                calls.append(key)
                if len(calls) > 1:
                    raise RuntimeError("upload failed")

            with self.assertRaises(RuntimeError):
                upload_wi_artifacts(dest, put_object=flaky_put)
            self.assertFalse((dest / CURRENT_POINTER_KEY).is_file())
            self.assertNotIn(CURRENT_POINTER_KEY, calls)

    def test_upload_uses_manifest_only(self):
        with tempfile.TemporaryDirectory() as raw:
            dest = Path(raw)
            build_wi_lake_artifacts(dest, build_id="gendry", details=[_detail("0417300")])
            stray = dest / "wi/v1/gendry/lakes/9999999.json"
            stray.parent.mkdir(parents=True, exist_ok=True)
            stray.write_text("{}", encoding="utf-8")
            keys: list[str] = []

            def record_put(_file_path: Path, key: str) -> None:
                keys.append(key)

            upload_wi_artifacts(dest, put_object=record_put)
            self.assertNotIn("wi/v1/gendry/lakes/9999999.json", keys)
            self.assertIn(CURRENT_POINTER_KEY, keys)

    @patch("fishy_pipeline.states.wi.lake_json.reproject_geojson_in_memory", side_effect=lambda data, *_args: data)
    @patch("fishy_pipeline.states.wi.package.find_tool", return_value="ogr2ogr")
    @patch(
        "fishy_pipeline.states.wi.package_verify.publish_gate",
        side_effect=lambda **kwargs: (
            (False, "needs_review", ["bad_depth"])
            if 99 in kwargs.get("depths_ft", [])
            else (True, "auto", [])
        ),
    )
    def test_failed_sheet_excluded_from_emitted_contours(self, _gate, _ogr, _reproject):
        with tempfile.TemporaryDirectory() as raw:
            work = Path(raw) / "work"
            dest = Path(raw) / "dest"
            good_dir = work / "contours" / "lakes" / "0417300" / "0417300a"
            bad_dir = work / "contours" / "lakes" / "0417300" / "0417300b"
            good_dir.mkdir(parents=True)
            bad_dir.mkdir(parents=True)

            def write_geo(path: Path, depth: int) -> None:
                path.write_text(
                    json.dumps(
                        {
                            "type": "FeatureCollection",
                            "qa_status": "auto",
                            "features": [
                                {
                                    "type": "Feature",
                                    "properties": {"depth_ft": depth, "needs_review": False},
                                    "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            good_path = good_dir / "contours.geojson"
            bad_path = bad_dir / "contours.geojson"
            write_geo(good_path, 10)
            write_geo(bad_path, 99)

            (work / "georef").mkdir(parents=True)
            (work / "georef" / "results.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "wbic": "0417300",
                                "map_key": "0417300a",
                                "qa_status": "auto",
                                "iou": 0.95,
                                "reasons": [],
                                "warp_source": "auto",
                            },
                            {
                                "wbic": "0417300",
                                "map_key": "0417300b",
                                "qa_status": "auto",
                                "iou": 0.95,
                                "reasons": [],
                                "warp_source": "auto",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (work / "contours" / "results.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "wbic": "0417300",
                                "map_key": "0417300a",
                                "publishable": True,
                                "qa_status": "auto",
                                "depths_ft": [10],
                                "path": str(good_path),
                            },
                            {
                                "wbic": "0417300",
                                "map_key": "0417300b",
                                "qa_status": "failed",
                                "publishable": False,
                                "depths_ft": [99],
                                "path": str(bad_path),
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            manifest = work / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "wbic": "0417300",
                                "map_key": "0417300a",
                                "name": "Lake Wissota",
                                "county": "Chippewa",
                                "acres": 6044,
                                "max_depth_ft": 72,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            hydro = work / "hydro.geojson"
            hydro.write_text(
                json.dumps(
                    {
                        "type": "FeatureCollection",
                        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3071"}},
                        "features": [
                            {
                                "type": "Feature",
                                "properties": {"WATERBODY_WBIC": 417300, "WATERBODY_NAME": "Lake Wissota"},
                                "geometry": {
                                    "type": "Polygon",
                                    "coordinates": [[[0, 0], [100, 0], [100, 100], [0, 0]]],
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            body = run_package(work_dir=work, dest=dest, manifest_path=manifest, hydro_path=hydro)
            self.assertEqual(len(body["lakes"]), 1)
            build_id = body["build_id"]
            detail = json.loads(
                (dest / generation_lake_detail_key(build_id, "0417300")).read_text(encoding="utf-8")
            )
            depths = {
                int(feature["properties"]["depth_ft"])
                for feature in detail["contours"]["features"]
            }
            self.assertEqual(depths, {10})
            self.assertNotIn(99, depths)

    def test_tampered_contours_results_json_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            work = Path(raw) / "work"
            dest = Path(raw) / "dest"
            lake_dir = work / "contours" / "lakes" / "0417300" / "0417300a"
            lake_dir.mkdir(parents=True)
            geo = {
                "type": "FeatureCollection",
                "qa_status": "auto",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"depth_ft": 5, "needs_review": False},
                        "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                    }
                ],
            }
            geo_path = lake_dir / "contours.geojson"
            geo_path.write_text(json.dumps(geo), encoding="utf-8")
            (work / "georef").mkdir(parents=True)
            (work / "georef" / "results.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "wbic": "0417300",
                                "map_key": "0417300a",
                                "qa_status": "auto",
                                "iou": 0.55,
                                "reasons": ["iou_below_threshold"],
                                "warp_source": "auto",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (work / "contours").mkdir(parents=True, exist_ok=True)
            (work / "contours" / "results.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "wbic": "0417300",
                                "map_key": "0417300a",
                                "publishable": True,
                                "qa_status": "auto",
                                "depths_ft": [5],
                                "path": str(geo_path),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            manifest = work / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "wbic": "0417300",
                                "map_key": "0417300a",
                                "name": "Fake",
                                "county": "Test",
                                "acres": 100,
                                "max_depth_ft": 20,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            hydro = work / "hydro.geojson"
            hydro.write_text(
                json.dumps(
                    {
                        "type": "FeatureCollection",
                        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3071"}},
                        "features": [
                            {
                                "type": "Feature",
                                "properties": {"WATERBODY_WBIC": 417300, "WATERBODY_NAME": "Fake"},
                                "geometry": {
                                    "type": "Polygon",
                                    "coordinates": [[[0, 0], [100, 0], [100, 100], [0, 0]]],
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                run_package(work_dir=work, dest=dest, manifest_path=manifest, hydro_path=hydro)
            self.assertIn("tampered", str(ctx.exception).lower())

    def test_hand_edited_publishable_true_needs_review_refused(self):
        with self.assertRaises(ValueError):
            refuse_unless_publishable(
                {
                    "wbic": "0417300",
                    "qa_status": "needs_review",
                    "publishable": True,
                    "reasons": [],
                }
            )

    def test_wbic_contract_rows(self):
        for row in CONTRACT["wbic"]:
            self.assertEqual(is_wbic(row["id"]), row["valid"])
        for row in CONTRACT["lakeRef"]:
            self.assertEqual(is_lake_id_for_state(row["state"], row["id"]), row["valid"])

    def test_needs_review_lake_cannot_be_built(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(ValueError):
                build_wi_lake_artifacts(Path(raw), build_id="bad", details=[_detail("0417300") | {"qa_status": "needs_review"}])

    def test_needs_review_detail_cannot_upload(self):
        with tempfile.TemporaryDirectory() as raw:
            detail_path = Path(raw) / "bad.json"
            detail_path.write_text(
                json.dumps({**CONTRACT["wiLakeDetail"], "qa_status": "needs_review"}),
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                upload_file(detail_path, generation_lake_detail_key("genx", CONTRACT["wiLakeDetail"]["id"]), dry_run=True)


if __name__ == "__main__":
    unittest.main()