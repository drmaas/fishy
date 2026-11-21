# SPDX-License-Identifier: GPL-3.0-only
import json
import math
import unittest
from pathlib import Path

from fishy_pipeline.depth import (
    SAMPLE_VIEW,
    depth_feet_from_dnr,
    feet_to_meters,
    is_dowlknum,
    lake_finder_url,
    normalize_properties,
    pmtiles_protocol_url,
    round_half_away_from_zero,
    square_meters_to_acres,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "fixtures" / "contract.json").read_text())


class ContractTest(unittest.TestCase):
    def test_round(self):
        for row in CONTRACT["round"]:
            value = math.nan if row["value"] is None else row["value"]
            self.assertEqual(round_half_away_from_zero(value, row["digits"]), row["out"])

    def test_units_and_ids(self):
        for row in CONTRACT["feetToMeters"]:
            self.assertAlmostEqual(feet_to_meters(row["feet"]), row["meters"])
        for row in CONTRACT["dnrDepthFeet"]:
            self.assertEqual(depth_feet_from_dnr(row["depth"], row["absDepth"]), row["feet"])
        for row in CONTRACT["acres"]:
            self.assertAlmostEqual(square_meters_to_acres(row["squareMeters"]), row["acres"])
        for row in CONTRACT["dowlknum"]:
            self.assertEqual(is_dowlknum(row["id"]), row["valid"])
            self.assertEqual(lake_finder_url(row["id"]), row.get("lakeFinder") if row["valid"] else None)
        for row in CONTRACT["pmtiles"]:
            self.assertEqual(pmtiles_protocol_url(row["input"]), row["url"])
        self.assertEqual(SAMPLE_VIEW, CONTRACT["sampleView"])
        arabic = "".join(chr(0x0660 + int(digit)) for digit in "27013300")
        self.assertFalse(is_dowlknum(arabic))

    def test_normalize_sample_contour(self):
        raw = json.loads((ROOT / "fixtures" / "sample-pond" / "contours.geojson").read_text())
        props = normalize_properties(raw["features"][1]["properties"])
        self.assertEqual(props["depth_ft"], 15)
        self.assertEqual(props["dowlknum"], "99999900")
        self.assertEqual(props["lake_name"], "Sample Pond")


if __name__ == "__main__":
    unittest.main()
