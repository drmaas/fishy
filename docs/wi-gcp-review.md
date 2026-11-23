# Wisconsin bathymetry GCP review

Automatic shoreline registration is the default. Human control points stay the fallback when a sheet still misses the 0.85 IoU gate. The gate itself is unchanged.

## How auto-registration works

Sheets are often rotated, and rotation is not limited to quarter turns. Initialization tries a similarity every 6 degrees, plus a mirror, and keeps the poses with the best polygon overlap. Affine ICP refines those poses. A fit whose axes differ by more than 1.40 is dropped, so a stretched inner contour cannot impersonate the shore. North arrows and scale bars are not used: arrow styles vary, and the hydro perimeter supplies scale.

The shoreline is the water inside the heavy outer line. Small gaps in that line are closed. When the line runs off the page, the page edge (and a detected neatline, when there is one) closes the polygon. Vertices on that edge are marked as frame, not shore.

A sheet that shows only part of the lake is matched on its own. The page-edge cut is left out of the correspondence, and the footprint score is recorded. The publish gate also requires that mapped ring to cover the full hydro polygon, so a fragment of one basin cannot clear 0.85 on footprint overlap alone. Each sheet is reported with its own IoU and rotation. The combined IoU is the union of the placed sheets against the hydro polygon. The lake publishes only when every sheet clears 0.85. One miss queues the whole lake with reason `multi_sheet`. When more than one page of the same document clears the gate, each passing page is warped and the row stays in review, because contour tracing reads one warped raster. A PDF whose pages are all closed shores keeps the best page. A document shared by several WBICs is still queued as `shared_sheet`.

`georef/results.json` records `rotation_deg`, `sheets` (page, IoU, rotation, cut fraction), and `combined_iou` when more than one sheet was placed. Review the per-lake IoUs before changing the gate.

## Fresh clone bootstrap

The review UI reads a **git-ignored** work tree (`pipeline/work/wi/`): manifest, rendered sheet PNGs, and a 24K Hydro extract. Nothing in that directory ships with the repo.

From the repository root:

```bash
# System tools (Arch / CachyOS example)
sudo pacman -S gdal python-gdal uv

# Editable pipeline + wi extra (Arch marks system Python externally managed;
# uv venv with --system-site-packages keeps pacman's python-gdal visible)
uv venv --python /usr/bin/python --system-site-packages .venv
uv pip install -e 'pipeline[wi]'

# One command: hydro extract, 10 pilot PDFs, rasters, manifest, auto-georef
PYTHONPATH=pipeline .venv/bin/python -m fishy_pipeline --state wi pilot-setup \
  --work-dir pipeline/work/wi
```

`pilot-setup` is idempotent: re-run it to refresh missing outputs. Pass `--force` to re-download hydro and re-ingest. Pass `--skip-georef` to stop after rasters.

Required on `PATH` for full setup: `gdal_translate`, `gdalwarp`. PyMuPDF (`pipeline[wi]`) is required for PDF→PNG rendering.

## Run the review UI

```bash
PYTHONPATH=pipeline .venv/bin/python -m fishy_pipeline --state wi gcp-review \
  --work-dir pipeline/work/wi
```

`--hydro` defaults to `pipeline/work/wi/pilot-hydro.geojson`. If the manifest or hydro file is missing, or the manifest has no pilot lakes, the command exits with the `pilot-setup` command above — it never serves an empty page.

Open `http://127.0.0.1:8765/` in a local browser. The server binds to localhost only (use `--allow-remote` only if you intentionally bind a non-loopback `--host`). It is not part of the production web app.

Check the API after setup:

```bash
curl -s http://127.0.0.1:8765/api/lakes | python -m json.tool
```

## Place control points

1. Choose a lake from the drop-down.
2. Click a recognizable point on the scanned sheet (a pier, inlet, or grid intersection).
3. Click the matching location on the hydro map (EPSG:3071).
4. Repeat until you have four to six pairs.
5. Save. The file is written to `pipeline/data/wi/gcps/<wbic>.json` and is meant to be committed.

The preview IoU updates after four points and shows whether it meets the 0.85 publish gate. Re-run georef after saving:

```bash
PYTHONPATH=pipeline .venv/bin/python -m fishy_pipeline --state wi georef \
  --work-dir pipeline/work/wi \
  --hydro pipeline/work/wi/pilot-hydro.geojson
```

When a committed GCP file exists, georef uses it instead of the automatic shoreline fit (`warp_source: reviewed` in `georef/results.json`).

## Depth labels

Contour depths use committed label lists under `pipeline/data/wi/labels/<wbic>.json` (sheet pixel, value, unit, confidence). EasyOCR is optional (`--ocr-fallback` on contours only).

A lake is publishable only when georef is `auto` with no blocking queue reasons, IoU ≥ 0.85, and traced depths match the recorded label set. Everything else stays `needs_review`.