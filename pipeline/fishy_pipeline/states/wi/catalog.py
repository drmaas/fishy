# SPDX-License-Identifier: GPL-3.0-only
"""Parse and optionally harvest the WI DNR lake map catalog HTML."""

from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
from collections import defaultdict
from dataclasses import asdict, dataclass
from http.cookiejar import CookieJar
from pathlib import Path

from fishy_pipeline.states.wi.dnr_fetch import build_dnr_opener, fetch_dnr_document
from fishy_pipeline.states.wi.wbic import (
    is_swims_document_url,
    map_key_from_pdf_url,
    map_key_from_wbic,
    normalize_wbic,
    parse_map_key,
)

CATALOG_FORM_URL = "https://apps.dnr.wi.gov/lakes/Documents/LakeMaps.aspx"
DEFAULT_USER_AGENT = "FishyBathymetry/0.1 (pipeline wi ingest; +https://github.com/drmaas/fishy)"
DEFAULT_RATE_LIMIT_SECONDS = 1.0

ROW_SPLIT_RE = re.compile(r"<tr\b", re.IGNORECASE)
LINK_RE = re.compile(
    r"<a[^>]+href=\"([^\"]+)\"[^>]*>([^<]+)</a>",
    re.IGNORECASE,
)
CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
VIEWSTATE_RE = re.compile(r'name="__VIEWSTATE"[^>]*value="([^"]*)"', re.IGNORECASE)
VIEWSTATE_GENERATOR_RE = re.compile(
    r'name="__VIEWSTATEGENERATOR"[^>]*value="([^"]*)"',
    re.IGNORECASE,
)
EVENTVALIDATION_RE = re.compile(
    r'name="__EVENTVALIDATION"[^>]*value="([^"]*)"',
    re.IGNORECASE,
)
PAGE_STATUS_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.IGNORECASE)
NEXT_POSTBACK_RE = re.compile(
    r"__doPostBack\('([^']*LinkButton3)'\s*,\s*''\)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CatalogRow:
    map_key: str
    wbic: str
    sheet_suffix: str
    name: str
    county: str
    acres: float | None
    max_depth_ft: float | None
    location: str
    pdf_url: str


@dataclass
class CatalogRowDraft:
    pdf_url: str
    wbic: str
    name: str
    county: str
    acres: float | None
    max_depth_ft: float | None
    location: str
    map_key: str | None = None
    sheet_suffix: str | None = None


class CatalogHarvestError(RuntimeError):
    pass


def _strip_tags(fragment: str) -> str:
    text = TAG_RE.sub(" ", fragment)
    text = html.unescape(text)
    return " ".join(text.split())


def _float_cell(text: str) -> float | None:
    cleaned = text.replace(",", "").strip()
    if not cleaned or cleaned == "&nbsp;":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _catalog_link_pairs(block: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for match in LINK_RE.finditer(block):
        href = html.unescape(match.group(1).strip())
        name = _strip_tags(match.group(2))
        lowered = href.lower()
        if "/lakes_maps/" in lowered and lowered.endswith(".pdf"):
            pairs.append((href, name))
            continue
        if "/swims/documents/downloaddocument" in lowered:
            pairs.append((href, name))
    return pairs


def parse_catalog_html(page_html: str) -> list[CatalogRow]:
    """Parse one catalog results page and assign stable map keys."""
    drafts: list[CatalogRowDraft] = []
    parts = ROW_SPLIT_RE.split(page_html)
    for chunk in parts[1:]:
        block = "<tr" + chunk
        cells = [_strip_tags(cell) for cell in CELL_RE.findall(block)]
        if len(cells) < 6:
            continue
        wbic_cell = normalize_wbic(cells[5]) if len(cells) > 5 else None
        if wbic_cell is None:
            continue
        link = None
        for href, name in _catalog_link_pairs(block):
            pdf_url = href if href.startswith("http") else html.unescape(href)
            doclink_key = map_key_from_pdf_url(pdf_url)
            if doclink_key is not None:
                parsed = parse_map_key(doclink_key)
                link = (pdf_url, name, doclink_key, parsed[1] if parsed else "a")
                break
            if is_swims_document_url(pdf_url):
                link = (pdf_url, name, None, None)
                break
        if link is None:
            continue
        pdf_url, name, map_key, suffix = link
        county = cells[1] if len(cells) > 1 else ""
        acres = _float_cell(cells[2]) if len(cells) > 2 else None
        max_depth = _float_cell(cells[3]) if len(cells) > 3 else None
        location = cells[4] if len(cells) > 4 else ""
        drafts.append(
            CatalogRowDraft(
                pdf_url=pdf_url,
                wbic=wbic_cell,
                name=name,
                county=county,
                acres=acres,
                max_depth_ft=max_depth,
                location=location,
                map_key=map_key,
                sheet_suffix=suffix,
            )
        )
    return finalize_catalog_rows(drafts)


def finalize_catalog_rows(drafts: list[CatalogRowDraft]) -> list[CatalogRow]:
    """Assign swims map keys and validate multiple document URLs per WBIC."""
    by_wbic: dict[str, list[CatalogRowDraft]] = defaultdict(list)
    for draft in drafts:
        by_wbic[draft.wbic].append(draft)

    used_map_keys: set[str] = {
        draft.map_key for draft in drafts if draft.map_key is not None
    }
    rows: list[CatalogRow] = []

    for wbic in sorted(by_wbic):
        group = by_wbic[wbic]
        urls = sorted({draft.pdf_url for draft in group})
        if len(urls) > 2:
            raise CatalogHarvestError(
                f"WBIC {wbic} has {len(urls)} distinct document URLs; expected at most 2"
            )

        url_suffix: dict[str, str] = {}
        for draft in group:
            if draft.map_key is not None and draft.sheet_suffix is not None:
                existing = url_suffix.get(draft.pdf_url)
                if existing is not None and existing != draft.sheet_suffix:
                    raise CatalogHarvestError(
                        f"conflicting suffix for {draft.pdf_url} under WBIC {wbic}"
                    )
                url_suffix.setdefault(draft.pdf_url, draft.sheet_suffix)

        used_suffixes = set(url_suffix.values())
        free_suffixes = [suffix for suffix in ("a", "b") if suffix not in used_suffixes]
        for url in urls:
            if url in url_suffix:
                continue
            if not free_suffixes:
                raise CatalogHarvestError(
                    f"WBIC {wbic}: no free a/b suffix for document URL {url}"
                )
            url_suffix[url] = free_suffixes.pop(0)

        for url in urls:
            suffix = url_suffix[url]
            doclink_keys = {
                draft.map_key for draft in group if draft.pdf_url == url and draft.map_key is not None
            }
            if len(doclink_keys) > 1:
                raise CatalogHarvestError(f"conflicting doclink map keys for {url} under WBIC {wbic}")
            if doclink_keys:
                map_key = next(iter(doclink_keys))
            else:
                map_key = map_key_from_wbic(wbic, suffix)
                if map_key in used_map_keys:
                    raise CatalogHarvestError(
                        f"map key {map_key!r} already assigned for WBIC {wbic}"
                    )
                used_map_keys.add(map_key)
            for draft in group:
                if draft.pdf_url != url:
                    continue
                draft.map_key = map_key
                draft.sheet_suffix = suffix

        for draft in group:
            if draft.map_key is None or draft.sheet_suffix is None:
                raise CatalogHarvestError(f"WBIC {wbic}: failed to assign map key")
            rows.append(
                CatalogRow(
                    map_key=draft.map_key,
                    wbic=wbic,
                    sheet_suffix=draft.sheet_suffix,
                    name=draft.name,
                    county=draft.county,
                    acres=draft.acres,
                    max_depth_ft=draft.max_depth_ft,
                    location=draft.location,
                    pdf_url=draft.pdf_url,
                )
            )
    return rows


def _row_dedupe_key(row: CatalogRow) -> tuple:
    return (
        row.wbic,
        row.pdf_url,
        row.name,
        row.county,
        row.location,
        row.acres,
        row.max_depth_ft,
    )


def merge_catalog_pages(pages: list[list[CatalogRow]]) -> list[CatalogRow]:
    seen: set[tuple] = set()
    merged: list[CatalogRow] = []
    for page in pages:
        for row in page:
            key = _row_dedupe_key(row)
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)
    return merged


def catalog_page_status(page_html: str) -> tuple[int, int] | None:
    match = PAGE_STATUS_RE.search(page_html)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def catalog_page_count(page_html: str) -> int | None:
    status = catalog_page_status(page_html)
    return status[1] if status else None


def _postback_field(page_html: str) -> str | None:
    unescaped = html.unescape(page_html)
    match = NEXT_POSTBACK_RE.search(unescaped)
    return match.group(1) if match else None


def _assert_harvest_complete(*, harvested_pages: int, expected_pages: int) -> None:
    if harvested_pages != expected_pages:
        raise CatalogHarvestError(
            f"catalog harvest collected {harvested_pages} pages but pager reports {expected_pages}"
        )


def fetch_catalog_page(
    *,
    cache_dir: Path,
    page_index: int,
    opener,
    previous_html: str | None = None,
    event_target: str | None = None,
    rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> str:
    """Fetch one catalog page, using cache when present."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"catalog-page-{page_index:03d}.html"
    if cache_path.is_file():
        text = cache_path.read_text(encoding="utf-8", errors="replace")
        status = catalog_page_status(text)
        if status and status[0] != page_index:
            raise CatalogHarvestError(
                f"cached {cache_path.name} shows page {status[0]}, expected {page_index}"
            )
        return text

    from fishy_pipeline.states.wi.dnr_fetch import fetch_dnr_bytes

    time.sleep(max(0.0, rate_limit_seconds))
    if page_index == 1 or previous_html is None or event_target is None:
        body, _content_type = fetch_dnr_bytes(
            CATALOG_FORM_URL, opener=opener, user_agent=user_agent
        )
    else:
        viewstate = VIEWSTATE_RE.search(previous_html)
        generator = VIEWSTATE_GENERATOR_RE.search(previous_html)
        eventvalidation = EVENTVALIDATION_RE.search(previous_html)
        if not viewstate or not eventvalidation or not generator:
            raise CatalogHarvestError("catalog postback fields missing on previous page")
        form = {
            "__EVENTTARGET": event_target,
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": viewstate.group(1),
            "__VIEWSTATEGENERATOR": generator.group(1),
            "__EVENTVALIDATION": eventvalidation.group(1),
        }
        encoded = urllib.parse.urlencode(form).encode("utf-8")
        body, _content_type = fetch_dnr_bytes(
            CATALOG_FORM_URL,
            opener=opener,
            data=encoded,
            user_agent=user_agent,
            method="POST",
        )

    text = body.decode("utf-8", errors="replace")
    status = catalog_page_status(text)
    if status is None:
        raise CatalogHarvestError(f"catalog page {page_index} is missing pager status")
    current_page, _total = status
    if current_page != page_index:
        raise CatalogHarvestError(
            f"catalog page did not advance: expected page {page_index}, got {current_page}"
        )
    cache_path.write_text(text, encoding="utf-8")
    return text


def harvest_catalog(
    cache_dir: Path,
    *,
    max_pages: int | None = None,
    rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    cookie_jar: CookieJar | None = None,
) -> list[CatalogRow]:
    """Download catalog pages into ``cache_dir`` and return merged rows."""
    opener = build_dnr_opener(cookie_jar)
    pages_html: list[str] = []
    page_html = fetch_catalog_page(
        cache_dir=cache_dir,
        page_index=1,
        opener=opener,
        rate_limit_seconds=rate_limit_seconds,
        user_agent=user_agent,
    )
    pages_html.append(page_html)
    _status = catalog_page_status(page_html)
    if _status is None:
        raise CatalogHarvestError("catalog page 1 is missing pager status")
    total_pages = _status[1]
    if max_pages is not None:
        total_pages = min(total_pages, max_pages)
    event_target = _postback_field(page_html)
    for page_index in range(2, total_pages + 1):
        if event_target is None:
            raise CatalogHarvestError(
                f"catalog pager Next control missing on page {page_index - 1}; "
                f"expected {total_pages} pages"
            )
        page_html = fetch_catalog_page(
            cache_dir=cache_dir,
            page_index=page_index,
            opener=opener,
            previous_html=page_html,
            event_target=event_target,
            rate_limit_seconds=rate_limit_seconds,
            user_agent=user_agent,
        )
        pages_html.append(page_html)
        event_target = _postback_field(page_html)

    _assert_harvest_complete(harvested_pages=len(pages_html), expected_pages=total_pages)
    parsed = [parse_catalog_html(html) for html in pages_html]
    return merge_catalog_pages(parsed)


def load_cached_catalog_html(cache_dir: Path) -> list[CatalogRow]:
    paths = sorted(cache_dir.glob("catalog-page-*.html"))
    if not paths:
        raise FileNotFoundError(f"no cached catalog HTML under {cache_dir}")
    pages_html = [path.read_text(encoding="utf-8", errors="replace") for path in paths]
    first_status = catalog_page_status(pages_html[0])
    if first_status is not None:
        _assert_harvest_complete(harvested_pages=len(paths), expected_pages=first_status[1])
    parsed = [parse_catalog_html(html) for html in pages_html]
    return merge_catalog_pages(parsed)


def fetch_document(
    url: str,
    dest: Path,
    *,
    rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    opener=None,
):
    if opener is None:
        opener = build_dnr_opener()
    return fetch_dnr_document(
        url,
        dest,
        opener=opener,
        user_agent=user_agent,
        rate_limit_seconds=rate_limit_seconds,
    )


def catalog_rows_to_jsonable(rows: list[CatalogRow]) -> list[dict]:
    return [asdict(row) for row in rows]


def write_catalog_snapshot(path: Path, rows: list[CatalogRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"rows": catalog_rows_to_jsonable(rows)}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")