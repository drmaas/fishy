# SPDX-License-Identifier: GPL-3.0-only
"""Parse and optionally harvest the WI DNR lake map catalog HTML."""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from fishy_pipeline.states.wi.wbic import map_key_from_pdf_url, normalize_wbic, parse_map_key

CATALOG_BASE_URL = "https://apps.dnr.wi.gov/lakes/maps/"
DEFAULT_USER_AGENT = "FishyBathymetry/0.1 (pipeline wi ingest; +https://github.com/drmaas/fishy)"
DEFAULT_RATE_LIMIT_SECONDS = 1.0

ROW_SPLIT_RE = re.compile(r"<tr\b", re.IGNORECASE)
PDF_LINK_RE = re.compile(
    r"<a[^>]+href=\"([^\"]+)\"[^>]*>([^<]+)</a>",
    re.IGNORECASE,
)
CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
VIEWSTATE_RE = re.compile(r'name="__VIEWSTATE"[^>]*value="([^"]*)"', re.IGNORECASE)
EVENTVALIDATION_RE = re.compile(
    r'name="__EVENTVALIDATION"[^>]*value="([^"]*)"',
    re.IGNORECASE,
)
PAGE_COUNT_RE = re.compile(r"Page\s+\d+\s+of\s+(\d+)", re.IGNORECASE)
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


def parse_catalog_html(page_html: str) -> list[CatalogRow]:
    """Parse one catalog results page into rows (stdlib HTML only)."""
    rows: list[CatalogRow] = []
    parts = ROW_SPLIT_RE.split(page_html)
    for chunk in parts[1:]:
        block = "<tr" + chunk
        link = None
        for candidate in PDF_LINK_RE.finditer(block):
            href = html.unescape(candidate.group(1).strip())
            if map_key_from_pdf_url(href) is None:
                continue
            link = (href, _strip_tags(candidate.group(2)))
            break
        if link is None:
            continue
        href, name = link
        map_key = map_key_from_pdf_url(href)
        if map_key is None:
            continue
        parsed = parse_map_key(map_key)
        if parsed is None:
            continue
        wbic, suffix = parsed
        cells = [_strip_tags(cell) for cell in CELL_RE.findall(block)]
        if len(cells) < 6:
            continue
        county = cells[1] if len(cells) > 1 else ""
        acres = _float_cell(cells[2]) if len(cells) > 2 else None
        max_depth = _float_cell(cells[3]) if len(cells) > 3 else None
        location = cells[4] if len(cells) > 4 else ""
        wbic_cell = normalize_wbic(cells[5]) if len(cells) > 5 else None
        if wbic_cell and wbic_cell != wbic:
            wbic = wbic_cell
        pdf_url = href if href.startswith("http") else urllib.parse.urljoin(CATALOG_BASE_URL, href)
        rows.append(
            CatalogRow(
                map_key=map_key,
                wbic=wbic,
                sheet_suffix=suffix,
                name=name,
                county=county,
                acres=acres,
                max_depth_ft=max_depth,
                location=location,
                pdf_url=pdf_url,
            )
        )
    return rows


def merge_catalog_pages(pages: list[list[CatalogRow]]) -> list[CatalogRow]:
    by_key: dict[str, CatalogRow] = {}
    for page in pages:
        for row in page:
            by_key[row.map_key] = row
    return [by_key[key] for key in sorted(by_key)]


def catalog_page_count(page_html: str) -> int | None:
    match = PAGE_COUNT_RE.search(page_html)
    if not match:
        return None
    return int(match.group(1))


def _postback_field(page_html: str) -> str | None:
    match = NEXT_POSTBACK_RE.search(page_html)
    return match.group(1) if match else None


def _fetch_url(url: str, data: bytes | None = None, *, user_agent: str) -> bytes:
    request = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": user_agent},
        method="POST" if data is not None else "GET",
    )
    if data is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def fetch_catalog_page(
    *,
    cache_dir: Path,
    page_index: int,
    previous_html: str | None = None,
    event_target: str | None = None,
    rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> str:
    """Fetch one catalog page, using cache when present."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"catalog-page-{page_index:03d}.html"
    if cache_path.is_file():
        return cache_path.read_text(encoding="utf-8", errors="replace")

    time.sleep(max(0.0, rate_limit_seconds))
    if page_index == 1 or previous_html is None or event_target is None:
        body = _fetch_url(CATALOG_BASE_URL, user_agent=user_agent)
    else:
        viewstate = VIEWSTATE_RE.search(previous_html)
        eventvalidation = EVENTVALIDATION_RE.search(previous_html)
        if not viewstate or not eventvalidation:
            raise RuntimeError("catalog postback fields missing on previous page")
        form = {
            "__EVENTTARGET": event_target,
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": viewstate.group(1),
            "__EVENTVALIDATION": eventvalidation.group(1),
        }
        encoded = urllib.parse.urlencode(form).encode("utf-8")
        body = _fetch_url(CATALOG_BASE_URL, encoded, user_agent=user_agent)

    text = body.decode("utf-8", errors="replace")
    cache_path.write_text(text, encoding="utf-8")
    return text


def harvest_catalog(
    cache_dir: Path,
    *,
    max_pages: int | None = None,
    rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[CatalogRow]:
    """Download catalog pages into ``cache_dir`` and return merged rows."""
    pages_html: list[str] = []
    page_html = fetch_catalog_page(
        cache_dir=cache_dir,
        page_index=1,
        rate_limit_seconds=rate_limit_seconds,
        user_agent=user_agent,
    )
    pages_html.append(page_html)
    total_pages = catalog_page_count(page_html) or 1
    if max_pages is not None:
        total_pages = min(total_pages, max_pages)
    event_target = _postback_field(page_html)
    for page_index in range(2, total_pages + 1):
        if event_target is None:
            break
        page_html = fetch_catalog_page(
            cache_dir=cache_dir,
            page_index=page_index,
            previous_html=page_html,
            event_target=event_target,
            rate_limit_seconds=rate_limit_seconds,
            user_agent=user_agent,
        )
        pages_html.append(page_html)
        event_target = _postback_field(page_html)
    parsed = [parse_catalog_html(html) for html in pages_html]
    return merge_catalog_pages(parsed)


def load_cached_catalog_html(cache_dir: Path) -> list[CatalogRow]:
    paths = sorted(cache_dir.glob("catalog-page-*.html"))
    if not paths:
        raise FileNotFoundError(f"no cached catalog HTML under {cache_dir}")
    pages = [parse_catalog_html(path.read_text(encoding="utf-8", errors="replace")) for path in paths]
    return merge_catalog_pages(pages)


def fetch_pdf(
    url: str,
    dest: Path,
    *,
    rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        return
    time.sleep(max(0.0, rate_limit_seconds))
    try:
        body = _fetch_url(url, user_agent=user_agent)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"PDF download failed ({error.code}): {url}") from error
    dest.write_bytes(body)


def catalog_rows_to_jsonable(rows: list[CatalogRow]) -> list[dict]:
    return [asdict(row) for row in rows]


def write_catalog_snapshot(path: Path, rows: list[CatalogRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"rows": catalog_rows_to_jsonable(rows)}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")