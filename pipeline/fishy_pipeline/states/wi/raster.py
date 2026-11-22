# SPDX-License-Identifier: GPL-3.0-only
"""Render ingested PDF maps to DPI-tagged PNG rasters."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

RenderPageFn = Callable[[Path, Path, int], None]

MIN_DPI = 300
MAX_DPI = 400
DEFAULT_DPI = 350

# 1×1 PNG (stdlib tests can assert output without optional deps).
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000"
    "907753de0000000a49444154789c6360000000020001e221bc3300000000"
    "49454e44ae426082"
)


def clamp_dpi(value: int) -> int:
    if value < MIN_DPI:
        return MIN_DPI
    if value > MAX_DPI:
        return MAX_DPI
    return value


def raster_path_for_map(work_dir: Path, map_key: str) -> Path:
    return work_dir / "rasters" / f"{map_key}.png"


def render_pdf_with_pymupdf(pdf_path: Path, output_path: Path, dpi: int) -> None:
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError("install wi extras: pip install -e 'pipeline/.[wi]'") from error

    dpi = clamp_dpi(dpi)
    doc = pymupdf.open(pdf_path)
    try:
        if doc.page_count < 1:
            raise RuntimeError(f"PDF has no pages: {pdf_path}")
        page = doc.load_page(0)
        zoom = dpi / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pix.save(output_path)
    finally:
        doc.close()


def render_pdf_pages(
    pdf_path: Path,
    output_path: Path,
    dpi: int,
    *,
    render_page: RenderPageFn | None = None,
) -> None:
    """Render the first PDF page to ``output_path``."""
    render_page = render_page or render_pdf_with_pymupdf
    render_page(pdf_path, output_path, clamp_dpi(dpi))


def render_manifest_rasters(
    entries: list[dict],
    *,
    pdf_dir: Path,
    work_dir: Path,
    dpi: int,
    render_page: RenderPageFn | None = None,
) -> list[dict]:
    """Render each manifest entry whose PDF exists; attach ``raster_path``."""
    updated: list[dict] = []
    for entry in entries:
        copy = dict(entry)
        pdf_path = pdf_dir / f"{entry['map_key']}.pdf"
        raster_path = raster_path_for_map(work_dir, entry["map_key"])
        if pdf_path.is_file():
            render_pdf_pages(pdf_path, raster_path, dpi, render_page=render_page)
            copy["raster_path"] = str(raster_path)
            copy["raster_dpi"] = clamp_dpi(dpi)
        updated.append(copy)
    return updated