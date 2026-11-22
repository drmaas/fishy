# SPDX-License-Identifier: GPL-3.0-only
import json
import tempfile
import unittest
from pathlib import Path

from fishy_pipeline.states.wi import catalog, hydro, ingest, raster
from fishy_pipeline.states.wi.dnr_fetch import assert_dnr_apps_url
from fishy_pipeline.states.wi.wbic import (
    is_wbic,
    is_swims_document_url,
    normalize_wbic,
    parse_map_key,
    resolve_catalog_map_key,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "wi-ingest"
CATALOG_HTML = (FIXTURES / "catalog-snippet.html").read_text(encoding="utf-8")
PAGER_HTML = (FIXTURES / "catalog-pager-encoded.html").read_text(encoding="utf-8")
HYDRO_PATH = FIXTURES / "hydro-snippet.geojson"


class WbicTest(unittest.TestCase):
    def test_normalize_and_map_key(self):
        self.assertTrue(is_wbic("0417300"))
        self.assertFalse(is_wbic("417300"))
        self.assertEqual(normalize_wbic(417300), "0417300")
        self.assertEqual(parse_map_key("0417300a"), ("0417300", "a"))

    def test_swims_link_uses_padded_wbic(self):
        href = "https://apps.dnr.wi.gov/swims/Documents/DownloadDocument?id=84291351"
        self.assertTrue(is_swims_document_url(href))
        resolved = resolve_catalog_map_key(href, "2128100")
        self.assertEqual(resolved, ("2128100a", "2128100", "a"))


class CatalogTest(unittest.TestCase):
    def test_parse_fixture_rows(self):
        rows = catalog.parse_catalog_html(CATALOG_HTML)
        self.assertEqual(len(rows), 3)
        ada = rows[0]
        self.assertEqual(ada.map_key, "0417300a")
        self.assertEqual(ada.wbic, "0417300")
        self.assertEqual(ada.name, "Ada Lake")
        self.assertEqual(ada.acres, 75)
        self.assertEqual(ada.max_depth_ft, 60)
        self.assertIn("0417300a.pdf", ada.pdf_url)
        altoona = next(row for row in rows if row.name.startswith("Altoona"))
        self.assertEqual(altoona.map_key, "2128100a")
        self.assertEqual(altoona.wbic, "2128100")
        self.assertIn("DownloadDocument?id=", altoona.pdf_url)

    def test_entity_encoded_pager_postback(self):
        target = catalog._postback_field(PAGER_HTML)
        self.assertEqual(
            target,
            "ctl00$LeftPageContent$gvDocumentTable$ctl24$LinkButton3",
        )
        self.assertEqual(catalog.catalog_page_status(PAGER_HTML), (1, 3))

    def test_harvest_count_mismatch_raises(self):
        with self.assertRaises(catalog.CatalogHarvestError):
            catalog._assert_harvest_complete(harvested_pages=1, expected_pages=138)


class HydroJoinTest(unittest.TestCase):
    def test_join_and_multi_sheet_tags(self):
        rows = catalog.parse_catalog_html(CATALOG_HTML)
        index = hydro.load_hydro_index(HYDRO_PATH)
        entries, unmatched = hydro.join_catalog_to_hydro(rows, index)
        self.assertEqual(unmatched, ["2128100"])
        ada_entries = [e for e in entries if e["wbic"] == "0417300"]
        self.assertEqual(len(ada_entries), 2)
        self.assertTrue(all(entry["hydro_matched"] for entry in ada_entries))
        self.assertFalse(entries[2]["hydro_matched"])
        tagged, multi = hydro.tag_multi_sheet_lakes(entries)
        self.assertEqual(multi, ["0417300"])
        self.assertTrue(tagged[0]["multi_sheet"])

    def test_rejects_non_3071_crs(self):
        with tempfile.NamedTemporaryFile("w", suffix=".geojson", delete=False) as handle:
            handle.write(
                '{"type":"FeatureCollection","crs":{"type":"name","properties":{"name":"EPSG:4326"}},'
                '"features":[]}'
            )
            path = Path(handle.name)
        try:
            with self.assertRaises(ValueError):
                hydro.load_hydro_index(path)
        finally:
            path.unlink()


class FetchGuardTest(unittest.TestCase):
    def test_dnr_host_allowlist(self):
        assert_dnr_apps_url("https://apps.dnr.wi.gov/doclink/lakes_maps/0417300a.pdf")
        with self.assertRaises(ValueError):
            assert_dnr_apps_url("https://evil.example.com/lakes_maps/0417300a.pdf")


class RasterTest(unittest.TestCase):
    def test_clamp_dpi_and_render_hook(self):
        self.assertEqual(raster.clamp_dpi(250), 300)
        self.assertEqual(raster.clamp_dpi(500), 400)
        self.assertEqual(raster.max_render_pixels(400), (15600, 14000))
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
            self.assertEqual(body["entry_count"], 3)
            self.assertEqual(body["multi_sheet_wbics"], ["0417300"])
            self.assertEqual(body["unmatched_wbic"], ["2128100"])
            manifest_path = work / "manifest.json"
            self.assertTrue(manifest_path.is_file())
            on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
            for entry in on_disk["entries"]:
                if entry["hydro_matched"]:
                    raster_path = Path(entry["raster_path"])
                    self.assertTrue(raster_path.is_file(), entry["map_key"])
                    self.assertEqual(entry["raster_dpi"], 350)


if __name__ == "__main__":
    unittest.main()