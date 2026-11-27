# SPDX-License-Identifier: GPL-3.0-only
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fishy_pipeline.states.wi import catalog, hydro, ingest, raster
from fishy_pipeline.states.wi import documents
from fishy_pipeline.states.wi import media
from fishy_pipeline.states.wi.dnr_fetch import assert_dnr_apps_url, DnrDocument
from fishy_pipeline.states.wi.wbic import (
    is_wbic,
    is_swims_document_url,
    normalize_wbic,
    parse_map_key,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "wi-ingest"
CATALOG_HTML = (FIXTURES / "catalog-snippet.html").read_text(encoding="utf-8")
MAP_KEYS_HTML = (FIXTURES / "catalog-map-keys.html").read_text(encoding="utf-8")
PAGER_HTML = (FIXTURES / "catalog-pager-encoded.html").read_text(encoding="utf-8")
HYDRO_PATH = FIXTURES / "hydro-snippet.geojson"
PDF_BYTES = (FIXTURES / "0417300a.pdf").read_bytes()
JPEG_BYTES = (FIXTURES / "2128100a.jpg").read_bytes()
SHP_BYTES = (FIXTURES / "0850800b.shp").read_bytes()


class WbicTest(unittest.TestCase):
    def test_normalize_and_map_key(self):
        self.assertTrue(is_wbic("0417300"))
        self.assertFalse(is_wbic("417300"))
        self.assertEqual(normalize_wbic(417300), "0417300")
        self.assertEqual(parse_map_key("0417300a"), ("0417300", "a"))

    def test_swims_detected(self):
        href = "https://apps.dnr.wi.gov/swims/Documents/DownloadDocument?id=84291351"
        self.assertTrue(is_swims_document_url(href))


class MediaTest(unittest.TestCase):
    def test_sniff_pdf_jpeg_shapefile(self):
        self.assertEqual(media.sniff_media_kind(PDF_BYTES), "pdf")
        self.assertEqual(media.sniff_media_kind(JPEG_BYTES), "jpeg")
        self.assertEqual(media.sniff_media_kind(SHP_BYTES), "shapefile")


class CatalogTest(unittest.TestCase):
    def test_parse_fixture_rows(self):
        rows = catalog.parse_catalog_html(CATALOG_HTML)
        self.assertEqual(len(rows), 3)
        altoona = next(row for row in rows if row.name.startswith("Altoona"))
        self.assertEqual(altoona.map_key, "2128100a")
        self.assertEqual(altoona.wbic, "2128100")

    def test_hartman_keeps_doclink_map_key(self):
        rows = catalog.parse_catalog_html(MAP_KEYS_HTML)
        map_keys = {row.name: row.map_key for row in rows}
        self.assertEqual(map_keys["Hartman Lake"], "0263900a")

    def test_chain_o_lakes_keeps_all_county_rows(self):
        rows = catalog.parse_catalog_html(MAP_KEYS_HTML)
        chain = [row for row in rows if row.name.startswith("Chain O")]
        self.assertEqual(len(chain), 3)
        self.assertTrue(all(row.map_key == "0261100a" for row in chain))
        counties = {row.county for row in chain}
        self.assertEqual(counties, {"Vilas", "Oneida", "Forest"})

    def test_clear_lake_two_map_keys(self):
        rows = catalog.parse_catalog_html(MAP_KEYS_HTML)
        clear = [row for row in rows if "Clear Lake" in row.name]
        self.assertEqual({row.map_key for row in clear}, {"1841200a", "1841200b"})

    def test_amber_and_plover_row_counts(self):
        rows = catalog.parse_catalog_html(MAP_KEYS_HTML)
        amber = [row for row in rows if row.name.startswith("Amber Lake")]
        plover = [row for row in rows if row.name.startswith("Plover River")]
        self.assertEqual(len(amber), 2)
        self.assertEqual(len(plover), 3)

    def test_entity_encoded_pager_postback(self):
        target = catalog._postback_field(PAGER_HTML)
        self.assertEqual(
            target,
            "ctl00$LeftPageContent$gvDocumentTable$ctl24$LinkButton3",
        )

    def test_fetch_catalog_page_unpacks_byte_tuple(self):
        html = "<html>Page\n1\nof\n2\n</html>"
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "fishy_pipeline.states.wi.dnr_fetch.fetch_dnr_bytes",
                return_value=(html.encode("utf-8"), "text/html"),
            ) as fetch:
                text = catalog.fetch_catalog_page(
                    cache_dir=Path(tmp),
                    page_index=1,
                    opener=object(),
                    rate_limit_seconds=0,
                )
            fetch.assert_called_once()
            self.assertEqual(text, html)
            self.assertEqual((Path(tmp) / "catalog-page-001.html").read_text(encoding="utf-8"), html)

    def test_third_distinct_url_raises(self):
        drafts = [
            catalog.CatalogRowDraft("http://a", "1111111", "A", "X", None, None, "loc"),
            catalog.CatalogRowDraft("http://b", "1111111", "B", "X", None, None, "loc"),
            catalog.CatalogRowDraft("http://c", "1111111", "C", "X", None, None, "loc"),
        ]
        with self.assertRaises(catalog.CatalogHarvestError):
            catalog.finalize_catalog_rows(drafts)

    def test_merge_keeps_same_pdf_different_counties(self):
        page_a = catalog.parse_catalog_html(MAP_KEYS_HTML)
        page_b = [row for row in page_a if row.name.startswith("Chain O")][0:1]
        merged = catalog.merge_catalog_pages([page_a, page_b])
        chain = [row for row in merged if row.name.startswith("Chain O")]
        self.assertEqual(len(chain), 3)


class HydroJoinTest(unittest.TestCase):
    def test_join_and_multi_sheet_tags(self):
        rows = catalog.parse_catalog_html(CATALOG_HTML)
        index = hydro.load_hydro_index(HYDRO_PATH)
        entries, unmatched = hydro.join_catalog_to_hydro(rows, index)
        self.assertEqual(unmatched, ["2128100"])
        tagged, multi = hydro.tag_multi_sheet_lakes(entries)
        self.assertEqual(multi, ["0417300"])

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


class DocumentFetchTest(unittest.TestCase):
    def test_download_continues_after_bad_row(self):
        rows = catalog.parse_catalog_html(MAP_KEYS_HTML)
        north_rows = [row for row in rows if row.name.startswith("North Lake")]
        with tempfile.TemporaryDirectory() as tmp:
            sources = Path(tmp)
            with mock.patch("fishy_pipeline.states.wi.catalog.fetch_dnr_document") as fetch:
                def side_effect(url, dest, **kwargs):
                    if "55500002" in url:
                        final = dest.parent / f"{dest.name}.shp"
                        final.write_bytes(SHP_BYTES)
                        return DnrDocument(body=SHP_BYTES, content_type=None, media_kind="shapefile")
                    final = dest.parent / f"{dest.name}.pdf"
                    final.write_bytes(PDF_BYTES)
                    return DnrDocument(body=PDF_BYTES, content_type="application/pdf", media_kind="pdf")

                fetch.side_effect = side_effect
                assets = documents.download_sources(
                    north_rows,
                    sources,
                    user_agent=catalog.DEFAULT_USER_AGENT,
                    rate_limit_seconds=0,
                )
            entries, _ = hydro.join_catalog_to_hydro(north_rows, {})
            entries, skip_count = documents.annotate_entries_with_sources(entries, assets)
            self.assertEqual(len(entries), 2)
            self.assertEqual(skip_count, 1)
            shp = next(e for e in entries if e["map_key"] == "0850800b")
            self.assertEqual(shp["skip_reason"], "unsupported_media_type: shapefile")
            pdf = next(e for e in entries if e["map_key"] == "0850800a")
            self.assertEqual(pdf["source_media_type"], "pdf")
            self.assertTrue(pdf["preferred_document"])


class FetchGuardTest(unittest.TestCase):
    def test_dnr_host_allowlist(self):
        assert_dnr_apps_url("https://apps.dnr.wi.gov/doclink/lakes_maps/0417300a.pdf")
        with self.assertRaises(ValueError):
            assert_dnr_apps_url("https://evil.example.com/lakes_maps/0417300a.pdf")


class RasterTest(unittest.TestCase):
    def test_clamp_dpi_and_render_hook(self):
        self.assertEqual(raster.max_render_pixels(400), (15600, 14000))
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            pdf = FIXTURES / "0417300a.pdf"

            def fake_render(_src: Path, output: Path, _dpi: int, _kind: str) -> None:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(raster.TINY_PNG)

            out = work / "rasters" / "0417300a.png"
            raster.render_map_asset(pdf, out, 350, media_kind="pdf", render_page=fake_render)
            self.assertTrue(out.is_file())

    def test_raster_skips_shapefile_entry(self):
        entries = [
            {
                "map_key": "0850800b",
                "source_media_type": "shapefile",
                "skip_reason": "unsupported_media_type: shapefile",
            }
        ]
        updated, raster_skip = raster.render_manifest_rasters(
            entries,
            sources_dir=FIXTURES,
            work_dir=FIXTURES,
            dpi=350,
            render_page=lambda *_args: None,
        )
        self.assertEqual(raster_skip, 1)
        self.assertIn("unsupported_media_type", updated[0]["raster_skip_reason"])

    def test_letter_notes_page_is_not_the_map_sheet(self):
        pages = [
            {"path": "map", "page": 0, "pixels": [14000, 11000], "dpi": 350},
            {"path": "notes", "page": 1, "pixels": [2550, 3300], "dpi": 300},
        ]
        chosen = raster.select_sheet_pages(pages)
        self.assertEqual([page["path"] for page in chosen], ["map"])

    def test_low_dpi_large_sheet_does_not_pull_a_sharp_letter_page_back(self):
        pages = [
            {"path": "preview", "page": 0, "pixels": [2784, 2464], "dpi": 72},
            {"path": "map", "page": 1, "pixels": [2550, 3300], "dpi": 350},
        ]
        chosen = raster.select_sheet_pages(pages)
        self.assertEqual([page["path"] for page in chosen], ["preview"])

    def test_letter_only_scan_keeps_every_page(self):
        pages = [
            {"path": "a", "pixels": [3300, 2550], "dpi": 300},
            {"path": "b", "pixels": [2550, 3300], "dpi": 300},
        ]
        self.assertEqual(raster.select_sheet_pages(pages), pages)


class IngestFixtureTest(unittest.TestCase):
    def test_fixture_pipeline_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            args = ingest.build_parser().parse_args(["--fixtures", "--work-dir", str(work)])
            body = ingest.run_ingest(args)
            self.assertEqual(body["entry_count"], 3)
            self.assertIn("skip_count", body)
            self.assertEqual(body["unmatched_wbic"], ["2128100"])
            jpeg = next(e for e in body["entries"] if e["map_key"] == "2128100a")
            self.assertEqual(jpeg["source_media_type"], "jpeg")


if __name__ == "__main__":
    unittest.main()