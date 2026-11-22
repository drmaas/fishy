# SPDX-License-Identifier: GPL-3.0-only
"""Download, classify, and annotate DNR catalog documents."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fishy_pipeline.states.wi import catalog
from fishy_pipeline.states.wi.media import (
    cache_extension,
    is_doclink_pdf_url,
    is_rasterizable,
    sniff_media_kind,
)


@dataclass
class SourceAsset:
    map_key: str
    path: Path | None
    media_kind: str
    content_type: str | None
    fetch_error: str | None = None


def source_cache_path(sources_dir: Path, map_key: str, media_kind: str) -> Path:
    return sources_dir / f"{map_key}.{cache_extension(media_kind)}"


def find_cached_source(sources_dir: Path, map_key: str) -> Path | None:
    for ext in ("pdf", "jpg", "shp", "bin"):
        candidate = sources_dir / f"{map_key}.{ext}"
        if candidate.is_file():
            return candidate
    return None


def load_fixture_asset(fixture_dir: Path, map_key: str) -> SourceAsset:
    path = find_cached_source(fixture_dir, map_key)
    if path is None:
        return SourceAsset(
            map_key=map_key,
            path=None,
            media_kind="unknown",
            content_type=None,
            fetch_error=f"fixture source missing for {map_key}",
        )
    data = path.read_bytes()
    return SourceAsset(
        map_key=map_key,
        path=path,
        media_kind=sniff_media_kind(data),
        content_type=None,
    )


def download_sources(
    rows: list[catalog.CatalogRow],
    sources_dir: Path,
    *,
    user_agent: str,
    rate_limit_seconds: float,
    opener=None,
) -> dict[str, SourceAsset]:
    """Fetch each unique ``map_key`` once; never abort the whole batch on one failure."""
    sources_dir.mkdir(parents=True, exist_ok=True)
    urls_by_map_key: dict[str, str] = {}
    for row in rows:
        urls_by_map_key.setdefault(row.map_key, row.pdf_url)

    assets: dict[str, SourceAsset] = {}
    for map_key, url in sorted(urls_by_map_key.items()):
        dest = sources_dir / map_key
        cached = find_cached_source(sources_dir, map_key)
        if cached is not None:
            data = cached.read_bytes()
            assets[map_key] = SourceAsset(
                map_key=map_key,
                path=cached,
                media_kind=sniff_media_kind(data),
                content_type=None,
            )
            continue
        try:
            document = catalog.fetch_document(
                url,
                dest,
                opener=opener,
                user_agent=user_agent,
                rate_limit_seconds=rate_limit_seconds,
            )
            path = source_cache_path(sources_dir, map_key, document.media_kind)
            assets[map_key] = SourceAsset(
                map_key=map_key,
                path=path,
                media_kind=document.media_kind,
                content_type=document.content_type,
            )
        except Exception as error:  # noqa: BLE001 - continue ingest for other lakes
            assets[map_key] = SourceAsset(
                map_key=map_key,
                path=None,
                media_kind="unknown",
                content_type=None,
                fetch_error=str(error),
            )
    return assets


def mark_preferred_documents(entries: list[dict]) -> None:
    """Prefer doclink PDFs when a WBIC has multiple downloaded source types."""
    by_wbic: dict[str, list[dict]] = {}
    for entry in entries:
        by_wbic.setdefault(entry["wbic"], []).append(entry)
    for group in by_wbic.values():
        doclink_pdf = [
            entry
            for entry in group
            if entry.get("source_media_type") == "pdf" and is_doclink_pdf_url(entry["pdf_url"])
        ]
        prefer_pdf = len(doclink_pdf) > 0
        for entry in group:
            entry["preferred_document"] = (
                entry.get("source_media_type") == "pdf" and is_doclink_pdf_url(entry["pdf_url"])
                if prefer_pdf
                else entry.get("source_media_type") in {"pdf", "jpeg"}
            )


def annotate_entries_with_sources(
    entries: list[dict],
    assets: dict[str, SourceAsset],
) -> tuple[list[dict], int]:
    """Attach source metadata and compute how many map keys were skipped."""
    skip_count = 0
    for entry in entries:
        asset = assets.get(entry["map_key"])
        if asset is None:
            entry["source_media_type"] = "unknown"
            entry["skip_reason"] = "source_not_fetched"
            skip_count += 1
            continue
        entry["source_media_type"] = asset.media_kind
        if asset.content_type:
            entry["content_type"] = asset.content_type
        if asset.path is not None:
            entry["source_path"] = str(asset.path)
        if asset.fetch_error:
            entry["skip_reason"] = f"fetch_failed: {asset.fetch_error}"
            skip_count += 1
        elif asset.media_kind == "shapefile":
            entry["skip_reason"] = "unsupported_media_type: shapefile"
            skip_count += 1
        elif asset.media_kind == "unknown":
            entry["skip_reason"] = "unsupported_media_type: unknown"
            skip_count += 1
    mark_preferred_documents(entries)
    return entries, skip_count


def skip_reason_for_raster(entry: dict) -> str | None:
    if entry.get("skip_reason"):
        return entry["skip_reason"]
    media = entry.get("source_media_type")
    if media == "shapefile":
        return "unsupported_media_type: shapefile"
    if media == "unknown":
        return "unsupported_media_type: unknown"
    if not is_rasterizable(str(media)):
        return f"unsupported_media_type: {media}"
    return None