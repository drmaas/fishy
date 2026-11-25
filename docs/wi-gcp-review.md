# Wisconsin bathymetry GCP review

Automatic shoreline registration is the default. Human control points stay the fallback when a sheet still misses the publish IoU gate (`DEFAULT_IOU_THRESHOLD`, currently 0.75).

## How auto-registration works

Sheets are often rotated, and rotation is not limited to quarter turns. Initialization tries a similarity every 6 degrees, plus a mirror, and keeps the poses with the best polygon overlap. Affine ICP refines those poses. A fit whose axes differ by more than 1.40 is dropped, so a stretched inner contour cannot impersonate the shore. North arrows and scale bars are not used: arrow styles vary, and the hydro perimeter supplies scale.

The shoreline is the water inside the heavy outer line. Small gaps in that line are closed. When the line runs off the page, the page edge (and a detected neatline, when there is one) closes the polygon. Vertices on that edge are marked as frame, not shore.

A low-resolution scan stays in the lake minimum. DPI does not remove a page, and a sharp letter page is not added back to outrank that scan. A letter-size page with almost no ink and no lake-scale figure is a citation and is left out. A large sheet stays even when the ink is light, and so does the only large sheet on the document. A legend or north arrow is a small near-rectangle with almost no depth ink. An oval shore and a basin closed by a straight dam stay eligible. A long straight run is still removed when it is a section line. When deleting that run would open a basin, including a line that spans the page, only the pixels that seal that basin are kept.

A sheet that shows only part of the lake is matched on its own. The page-edge cut is left out of the correspondence, and the footprint score is recorded. The publish gate also requires that mapped ring to cover the full hydro polygon, so a fragment of one basin cannot clear the gate on footprint overlap alone. Each sheet is reported with its own IoU and rotation. The lake-level gate is `min(per-sheet gate_iou)`; partial sheets use the worse of footprint IoU and full-hydro coverage. The combined IoU is the union of the placed sheets against the hydro polygon and is diagnostic only—it does not clear the gate or relax multi-sheet queueing. Catalog multi-sheet lakes (different map keys) still fail closed unless every scored sheet clears `DEFAULT_IOU_THRESHOLD`. When more than one page of the same document clears the gate, each passing page is warped. The row publishes when every kept sheet clears 0.75 and each sheet's published warp places its ring on the hydro polygon at that threshold. The published score stays the minimum. A missed page, a sheet under 0.75, or a placed ring that misses the hydro polygon keeps the `multi_sheet` queue. Contour tracing reads every kept warp. Recorded labels apply only when the label file's page is that warp's source raster, using that warp's control points. The interval from that page steps depths on the other kept warps. The publish gate runs once on the merged depths. A warp stays in review when its own rings cannot be assigned with confidence. A document shared by several WBICs is still queued as `shared_sheet`. After the affine, Wisconsin DNR DRG road centerlines are inverse-projected into the sheet and a narrow casing pair beside a centerline is cleared. Every enclosed basin keeps its seal. The shore is scored again. A gain is kept only when that clear did not remove a basin seal. A tie, or a trace that loses overlap without hitting the outline, keeps the first registration. A raster that is not in the kept set is a missed page and scores the lake at 0.

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

The preview IoU updates after four points and shows whether it meets the publish gate (`DEFAULT_IOU_THRESHOLD`). Re-run georef after saving:

```bash
PYTHONPATH=pipeline .venv/bin/python -m fishy_pipeline --state wi georef \
  --work-dir pipeline/work/wi \
  --hydro pipeline/work/wi/pilot-hydro.geojson
```

When a committed GCP file exists, georef uses it instead of the automatic shoreline fit (`warp_source: reviewed` in `georef/results.json`).

## Depth labels

Contour depths use committed label lists under `pipeline/data/wi/labels/<wbic>.json` (sheet pixel, value, unit, confidence). EasyOCR is optional (`--ocr-fallback` on contours only).

A lake is publishable only when georef is `auto` with no blocking queue reasons, IoU ≥ `DEFAULT_IOU_THRESHOLD`, and traced depths match the recorded label set. Everything else stays `needs_review`.