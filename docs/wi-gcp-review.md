# Wisconsin bathymetry GCP review

Automatic shoreline registration often misses the 0.90 IoU gate. Human control points fix the warp without lowering the threshold.

## Run the review UI

From the repository root, with the pilot manifest and hydro extract in the work directory:

```bash
PYTHONPATH=pipeline python3 -m fishy_pipeline --state wi gcp-review \
  --work-dir pipeline/work/wi \
  --manifest pipeline/work/wi/manifest.json \
  --hydro pipeline/work/wi/pilot-hydro.geojson
```

Open `http://127.0.0.1:8765/` in a local browser. The server binds to localhost only. It is not part of the production web app.

## Place control points

1. Choose a lake from the drop-down.
2. Click a recognizable point on the scanned sheet (a pier, inlet, or grid intersection).
3. Click the matching location on the hydro map (EPSG:3071).
4. Repeat until you have four to six pairs.
5. Save. The file is written to `pipeline/data/wi/gcps/<wbic>.json` and is meant to be committed.

The preview IoU updates after four points. Re-run georef after saving:

```bash
PYTHONPATH=pipeline python3 -m fishy_pipeline --state wi georef \
  --work-dir pipeline/work/wi \
  --manifest pipeline/work/wi/manifest.json \
  --hydro pipeline/work/wi/pilot-hydro.geojson
```

When a committed GCP file exists, georef uses it instead of the automatic shoreline fit (`warp_source: reviewed` in `georef/results.json`).

## Depth labels

Contour depths use committed label lists under `pipeline/data/wi/labels/<wbic>.json` (sheet pixel, value, unit, confidence). EasyOCR is optional (`--ocr-fallback` on contours only).

A lake is publishable only when IoU ≥ 0.90 and traced depths match the recorded label set. Everything else stays `needs_review`.