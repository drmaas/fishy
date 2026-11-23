# Wisconsin bathymetry GCP review

Automatic shoreline registration often misses the 0.85 IoU gate. Human control points fix the warp without lowering the depth-label match requirement.

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