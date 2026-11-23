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
# US Letter's long side is 11 in. A page longer than this is a map sheet.
LETTER_LONG_INCHES = 12.0


def clamp_dpi(value: int) -> int:
    if value < MIN_DPI:
        return MIN_DPI
    if value > MAX_DPI:
        return MAX_DPI
    return value


def select_sheet_pages(pages: list[dict]) -> list[dict]:
    """Drop US Letter notes when the PDF also contains a large-format sheet.

    Devils Lake is scanned on letter pages, so letter pages stay when every
    page is letter. A notes page is not chosen over the bathymetry sheet.
    """
    longs = [_page_long_inches(page) for page in pages]
    if any(value is not None and value > LETTER_LONG_INCHES for value in longs):
        return [
            page
            for page, value in zip(pages, longs, strict=True)
            if value is None or value > LETTER_LONG_INCHES
        ]
    return list(pages)


def _page_long_inches(page: dict) -> float | None:
    pixels = page.get("pixels")
    dpi = page.get("dpi")
    if not isinstance(pixels, (list, tuple)) or len(pixels) < 2 or not dpi:
        return None
    return max(float(pixels[0]), float(pixels[1])) / float(dpi)


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
) -> list[dict]:
    """Render every PDF page. The primary ``output_path`` is the largest page.

    Target is ``dpi`` (clamped to 300–400). A page larger than the pixel cap is
    scaled down so the lake is kept. A scan coarser than ``dpi`` is not
    upsampled past its embedded image: extra pixels would only interpolate.
    """
    pages = render_pdf_pages(source_path, output_path.parent, output_path.stem, dpi, media_kind)
    if not pages:
        raise RuntimeError(f"source has no pages: {source_path}")
    primary = max(pages, key=lambda page: page["pixels"][0] * page["pixels"][1])
    if Path(primary["path"]) != output_path:
        output_path.write_bytes(Path(primary["path"]).read_bytes())
        primary = {**primary, "path": str(output_path)}
    return pages


def render_pdf_pages(
    source_path: Path,
    output_dir: Path,
    stem: str,
    dpi: int,
    media_kind: str = "pdf",
) -> list[dict]:
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError("install wi extras: pip install -e 'pipeline/.[wi]'") from error

    dpi = clamp_dpi(dpi)
    max_w, max_h = max_render_pixels(dpi)
    doc = pymupdf.open(source_path)
    rendered: list[dict] = []
    try:
        if doc.page_count < 1:
            raise RuntimeError(f"source has no pages: {source_path}")
        output_dir.mkdir(parents=True, exist_ok=True)
        for index in range(doc.page_count):
            page = doc.load_page(index)
            zoom, actual_dpi, within_cap = _page_zoom(
                doc, page, dpi=dpi, max_w=max_w, max_h=max_h, media_kind=media_kind
            )
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            if doc.page_count == 1:
                path = output_dir / f"{stem}.png"
            else:
                path = output_dir / f"{stem}-p{index}.png"
            pix.save(path)
            rendered.append(
                {
                    "path": str(path),
                    "page": index,
                    "dpi": round(actual_dpi, 2),
                    "pixels": [pix.width, pix.height],
                    "within_cap": within_cap,
                }
            )
    finally:
        doc.close()
    if len(rendered) > 1:
        largest = max(page["pixels"][0] * page["pixels"][1] for page in rendered)
        rendered = [
            page
            for page in rendered
            if page["pixels"][0] * page["pixels"][1] >= 0.08 * largest
        ]
    return rendered


def fit_page_zoom(
    *,
    page_width_pt: float,
    page_height_pt: float,
    dpi: int,
    max_w: int,
    max_h: int,
    native_zoom: float | None,
) -> tuple[float, float, bool]:
    """Zoom that targets ``dpi`` without upsampling a scan or exceeding the pixel cap.

    The third value is false when the page had to be scaled below the request.
    """
    zoom = dpi / 72.0
    if native_zoom is not None:
        zoom = min(zoom, native_zoom)
    width_px = page_width_pt * zoom
    height_px = page_height_pt * zoom
    within_cap = width_px <= max_w and height_px <= max_h
    if not within_cap and width_px > 0 and height_px > 0:
        zoom *= min(max_w / width_px, max_h / height_px)
    return zoom, zoom * 72.0, within_cap


def _page_zoom(doc, page, *, dpi: int, max_w: int, max_h: int, media_kind: str) -> tuple[float, float, bool]:
    """Return zoom, the DPI it represents, and whether the request fit the cap."""
    if media_kind == "jpeg":
        rect = page.rect
        zoom = 1.0
        if rect.width > max_w or rect.height > max_h:
            zoom = min(max_w / rect.width, max_h / rect.height)
        return zoom, dpi * zoom, zoom >= 1.0
    return fit_page_zoom(
        page_width_pt=page.rect.width,
        page_height_pt=page.rect.height,
        dpi=dpi,
        max_w=max_w,
        max_h=max_h,
        native_zoom=_native_zoom(doc, page),
    )


def _native_zoom(doc, page) -> float | None:
    """Pixels per PDF point of the largest embedded image, or None for a vector page."""
    images = page.get_images(full=True)
    if not images:
        return None
    best = 0.0
    for info in images:
        meta = doc.extract_image(info[0])
        if page.rect.width <= 0 or page.rect.height <= 0:
            continue
        best = max(best, meta["width"] / page.rect.width, meta["height"] / page.rect.height)
    return best or None


def render_map_asset(
    source_path: Path,
    output_path: Path,
    dpi: int,
    *,
    media_kind: str,
    render_page: RenderPageFn | None = None,
) -> list[dict]:
    render_page = render_page or render_map_with_pymupdf
    rendered = render_page(source_path, output_path, clamp_dpi(dpi), media_kind)
    if isinstance(rendered, list) and rendered:
        return rendered
    return [{"path": str(output_path), "page": 0, "dpi": clamp_dpi(dpi), "pixels": [0, 0], "within_cap": True}]


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
            pages = render_map_asset(
                Path(source_path),
                raster_path,
                dpi,
                media_kind=media_kind,
                render_page=render_page,
            )
            copy["raster_path"] = str(raster_path)
            copy["raster_pages"] = pages
            copy["raster_dpi"] = pages[0]["dpi"] if pages else clamp_dpi(dpi)
        except Exception as error:  # noqa: BLE001
            copy["raster_skip_reason"] = f"raster_failed: {error}"
            raster_skip_count += 1
        updated.append(copy)
    return updated, raster_skip_count