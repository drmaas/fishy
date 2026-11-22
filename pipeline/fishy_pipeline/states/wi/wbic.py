# SPDX-License-Identifier: GPL-3.0-only
"""Wisconsin WBIC normalization (seven ASCII digits)."""

from __future__ import annotations

import re

WBIC_LEN = 7
MAP_KEY_RE = re.compile(r"^([0-9]{7})([ab])$", re.IGNORECASE)
SWIMS_DOCUMENT_RE = re.compile(
    r"/swims/Documents/DownloadDocument\?id=([0-9]+)",
    re.IGNORECASE,
)


def is_wbic(value: str) -> bool:
    return len(value) == WBIC_LEN and value.isascii() and value.isdigit()


def normalize_wbic(value: str | int | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text.isdigit():
        return None
    padded = text.zfill(WBIC_LEN)
    return padded if is_wbic(padded) else None


def parse_map_key(map_key: str) -> tuple[str, str] | None:
    """Parse a catalog map key such as ``0417300a`` into WBIC and sheet suffix."""
    match = MAP_KEY_RE.match(map_key.strip())
    if not match:
        return None
    wbic = match.group(1)
    suffix = match.group(2).lower()
    if not is_wbic(wbic):
        return None
    return wbic, suffix


def map_key_from_wbic(wbic: str, sheet_suffix: str = "a") -> str:
    """Build a stable cache key ``{wbic}{suffix}`` for swims-hosted PDFs."""
    normalized = normalize_wbic(wbic)
    if normalized is None:
        raise ValueError(f"invalid wbic {wbic!r}")
    suffix = sheet_suffix.lower()
    if suffix not in {"a", "b"}:
        raise ValueError(f"invalid sheet suffix {sheet_suffix!r}")
    return f"{normalized}{suffix}"


def is_swims_document_url(url: str) -> bool:
    return SWIMS_DOCUMENT_RE.search(url) is not None


def map_key_from_pdf_url(url: str) -> str | None:
    """Extract ``0417300a`` from a DNR doclink lakes_maps PDF URL."""
    marker = "/lakes_maps/"
    if marker not in url:
        return None
    tail = url.split(marker, 1)[1]
    name = tail.split("?", 1)[0].split("#", 1)[0]
    if not name.lower().endswith(".pdf"):
        return None
    stem = name[: -len(".pdf")]
    if parse_map_key(stem) is None:
        return None
    return stem.lower()


def resolve_catalog_map_key(href: str, wbic_from_row: str | None) -> tuple[str, str, str] | None:
    """
    Resolve ``(map_key, wbic, sheet_suffix)`` from a catalog link and table WBIC cell.

    Supports doclink ``/lakes_maps/{wbic}{a|b}.pdf`` and swims ``DownloadDocument?id=`` links
    (sheet suffix defaults to ``a`` when only the WBIC column identifies the lake).
    """
    from_doclink = map_key_from_pdf_url(href)
    if from_doclink is not None:
        parsed = parse_map_key(from_doclink)
        if parsed is None:
            return None
        wbic, suffix = parsed
        return from_doclink, wbic, suffix
    if is_swims_document_url(href) and wbic_from_row is not None:
        wbic = normalize_wbic(wbic_from_row)
        if wbic is None:
            return None
        suffix = "a"
        return map_key_from_wbic(wbic, suffix), wbic, suffix
    return None