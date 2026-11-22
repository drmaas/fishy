# SPDX-License-Identifier: GPL-3.0-only
import json
import tempfile
import unittest
from pathlib import Path

from fishy_pipeline.states.wi import catalog, hydro, ingest, raster
from fishy_pipeline.states.wi.wbic import is_wbic, normalize_wbic, parse_map_key

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "wi-ingest"
CATALOG_HTML = (FIXTURES / "catalog-snippet.html").read_text(encoding="utf-8")
HYDRO_PATH = FIXTURES / "hydro-snippet.geojson"


class WbicTest(unittest.TestCase):
    def test_normalize_and_map_key(self):
        self.assertTrue(is_wbic("0417300"))
        self.assertFalse(is_wbic("417300"))
        self.assertEqual(normalize_wbic(417300), "0417300")
        self.assertEqual(parse_map_key("0417300a"), ("0417300", "a"))


class CatalogTest(unittest.TestCase):
    def test_parse_fixture_rows(self):
        rows = catalog.parse_catalog_html(CATALOG_HTML)
        self.assertEqual(len(rows), 2)
        ada = rows[0]
        self.assertEqual(ada.map_key, "0417300a")
        self.assertEqual(ada.wbic, "0417300")
        self.assertEqual(ada.name, "Ada Lake")
        self.assertEqual(ada.acres, 75)
        self.assertEqual(ada.max_depth_ft, 60)
        self.assertIn("0417300a.pdf", ada.pdf_url)
        self.assertFalse(any("Altoona" in row.name for row in rows))


class HydroJoinTest(unittest.TestCase):
    def test_join_and_multi_sheet_tags(self):
        rows = catalog.parse_catalog_html(CATALOG_HTML)
        index = hydro.load_hydro_index(HYDRO_PATH)
        entries, unmatched = hydro.join_catalog_to_hydro(rows, index)
        self.assertEqual(unmatched, [])
        self.assertTrue(all(entry["hydro_matched"] for entry in entries))
        self.assertEqual(entries[0]["hydro"]["waterbody_name"], "Ada Lake")
        tagged, multi = hydro.tag_multi_sheet_lakes(entries)
        self.assertEqual(multi, ["0417300"])
        self.assertTrue(tagged[0]["multi_sheet"])
        self.assertTrue(tagged[1]["multi_sheet"])


class RasterTest(unittest.TestCase):
    def test_clamp_dpi_and_render_hook(self):
        self.assertEqual(raster.clamp_dpi(250), 300)
        self.assertEqual(raster.clamp_dpi(500), 400)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            pdf = FIXTURES / "0417300a.pdf"

            def fake_render(_pdf: Path, output: Path, dpi: int) -> None:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(raster.TINY_PNG)

            out = work / "rasters" / "0417300a.png"
            raster.render_pdf_pages(pdf, out, 350, render_page=fake_render)
            self.assertTrue(out.is_file())
            self.assertGreater(out.stat().st_size, 0)


class IngestFixtureTest(unittest.TestCase):
    def test_fixture_pipeline_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            args = ingest.build_parser().parse_args(["--fixtures", "--work-dir", str(work)])
            body = ingest.run_ingest(args)
            self.assertEqual(body["entry_count"], 2)
            self.assertEqual(body["multi_sheet_wbics"], ["0417300"])
            manifest_path = work / "manifest.json"
            self.assertTrue(manifest_path.is_file())
            on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["unmatched_wbic"], [])
            for entry in on_disk["entries"]:
                raster_path = Path(entry["raster_path"])
                self.assertTrue(raster_path.is_file(), entry["map_key"])
                self.assertEqual(entry["raster_dpi"], 350)


if __name__ == "__main__":
    unittest.main()