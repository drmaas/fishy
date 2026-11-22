# SPDX-License-Identifier: GPL-3.0-only
"""Render ingested PDF or JPEG map assets to DPI-tagged PNG rasters."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

RenderPageFn = Callable[[Path, Path, int, str], None]

MIN_DPI = 300
MAX_DPI = 400
DEFAULT_DPI = 350
MAX_PAGE_INCHES = (39.0, 35.0)


def clamp_dpi(value: int) -> int:
    if value < MIN_DPI:
        return MIN_DPI
    if value > MAX_DPI:
        return MAX_DPI
    return value


def max_render_pixels(dpi: int) -> tuple[int, int]:
    dpi = clamp_dpi(dpi)
    return (int(MAX_PAGE_INCHES[0] * dpi), int(MAX_PAGE_INCHES[1] * dpi))


TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000"
    "907753de0000000a49444154789c6360000000020001e221bc3300000000"
    "49454e44ae426082"
)


def raster_path_for_map(work_dir: Path, map_key: str) -> Path:
    return work_dir / "rasters" / f"{map_key}.png"


def render_map_with_pymupdf(
    source_path: Path,
    output_path: Path,
    dpi: int,
    media_kind: str,
) -> None:
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError("install wi extras: pip install -e 'pipeline/.[wi]'") from error

    dpi = clamp_dpi(dpi)
    max_w, max_h = max_render_pixels(dpi)
    doc = pymupdf.open(source_path)
    try:
        if doc.page_count < 1:
            raise RuntimeError(f"source has no pages: {source_path}")
        page = doc.load_page(0)
        if media_kind == "jpeg":
            rect = page.rect
            width_px = rect.width
            height_px = rect.height
            if width_px > max_w or height_px > max_h:
                scale = min(max_w / width_px, max_h / height_px)
                width_px *= scale
                height_px *= scale
            matrix = pymupdf.Matrix(width_px / rect.width, height_px / rect.height)
        else:
            zoom = dpi / 72.0
            width_px = page.rect.width * zoom
            height_px = page.rect.height * zoom
            if width_px > max_w or height_px > max_h:
                raise RuntimeError(
                    f"page exceeds render cap ({max_w}x{max_h}px at {dpi} DPI): {source_path}"
                )
            matrix = pymupdf.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pix.save(output_path)
    finally:
        doc.close()


def render_map_asset(
    source_path: Path,
    output_path: Path,
    dpi: int,
    *,
    media_kind: str,
    render_page: RenderPageFn | None = None,
) -> None:
    render_page = render_page or render_map_with_pymupdf
    render_page(source_path, output_path, clamp_dpi(dpi), media_kind)


def render_manifest_rasters(
    entries: list[dict],
    *,
    sources_dir: Path,
    work_dir: Path,
    dpi: int,
    render_page: RenderPageFn | None = None,
    skip_reason_for=None,
) -> tuple[list[dict], int]:
    """Rasterize entries when possible; record per-row raster skip reasons."""
    from fishy_pipeline.states.wi.documents import find_cached_source, skip_reason_for_raster

    skip_reason_for = skip_reason_for or skip_reason_for_raster
    updated: list[dict] = []
    raster_skip_count = 0
    for entry in entries:
        copy = dict(entry)
        reason = skip_reason_for(entry)
        if reason:
            copy["raster_skip_reason"] = reason
            raster_skip_count += 1
            updated.append(copy)
            continue
        source_path = entry.get("source_path")
        if source_path is None:
            found = find_cached_source(sources_dir, entry["map_key"])
            source_path = str(found) if found else None
        if source_path is None or not Path(source_path).is_file():
            copy["raster_skip_reason"] = "source_missing"
            raster_skip_count += 1
            updated.append(copy)
            continue
        media_kind = str(entry.get("source_media_type") or "pdf")
        raster_path = raster_path_for_map(work_dir, entry["map_key"])
        try:
            render_map_asset(
                Path(source_path),
                raster_path,
                dpi,
                media_kind=media_kind,
                render_page=render_page,
            )
            copy["raster_path"] = str(raster_path)
            copy["raster_dpi"] = clamp_dpi(dpi)
        except Exception as error:  # noqa: BLE001
            copy["raster_skip_reason"] = f"raster_failed: {error}"
            raster_skip_count += 1
        updated.append(copy)
    return updated, raster_skip_count