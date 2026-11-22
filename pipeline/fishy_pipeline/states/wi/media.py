# SPDX-License-Identifier: GPL-3.0-only
"""Detect DNR lake document bytes by magic number and HTTP Content-Type."""

from __future__ import annotations

import re

PDF_MAGIC = b"%PDF"
JPEG_MAGICS = (b"\xff\xd8\xff",)
# ESRI shapefile main file (.shp) dword at offset 0
SHP_MAGIC = 0x0000270A

MediaKind = str  # "pdf" | "jpeg" | "shapefile" | "unknown"

MEDIA_EXTENSIONS: dict[MediaKind, str] = {
    "pdf": "pdf",
    "jpeg": "jpg",
    "shapefile": "shp",
    "unknown": "bin",
}


def sniff_media_kind(body: bytes, content_type: str | None = None) -> MediaKind:
    if body.startswith(PDF_MAGIC):
        return "pdf"
    if body[:3] in JPEG_MAGICS:
        return "jpeg"
    if len(body) >= 4:
        dword = int.from_bytes(body[0:4], "little", signed=False)
        if dword == SHP_MAGIC:
            return "shapefile"
    if content_type:
        lowered = content_type.split(";", 1)[0].strip().lower()
        if lowered in {"application/pdf", "application/x-pdf"}:
            return "pdf"
        if lowered in {"image/jpeg", "image/jpg"}:
            return "jpeg"
        if "shape" in lowered:
            return "shapefile"
    return "unknown"


def normalize_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower() or None


def is_rasterizable(kind: MediaKind) -> bool:
    return kind in {"pdf", "jpeg"}


def is_doclink_pdf_url(url: str) -> bool:
    return "/lakes_maps/" in url.lower() and url.lower().endswith(".pdf")


_CONTENT_TYPE_RE = re.compile(r"^content-type:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def content_type_from_headers(headers: str) -> str | None:
    match = _CONTENT_TYPE_RE.search(headers)
    if not match:
        return None
    return normalize_content_type(match.group(1).strip())


def cache_extension(kind: MediaKind) -> str:
    return MEDIA_EXTENSIONS.get(kind, "bin")