# Pipeline

Fishy’s Python tooling turns lake bathymetry into PMTiles for the map. Layout is keyed by **state**:

| State | Runtime path |
| --- | --- |
| Minnesota (`mn`) | **No pipeline.** The Worker queries the [MN DNR MapServer](https://enterprise.gisdata.mn.gov/aghost/rest/services/us_mn_state_dnr/water_lake_bathymetry/MapServer) live. |
| Wisconsin (`wi`) | **Batch pipeline** under `fishy_pipeline/states/wi/` (ingest → georef → contours). Tiles upload to R2. `ingest`, `georef`, and `contours` are implemented. |
| Sample / dev | Shared **`fishy_pipeline/common/`** build on `fixtures/sample-pond` (synthetic DNR-shaped data). |

## Shared build (`common/`)

`build_lake` reads four files from one directory: `lakes.geojson`, `contours.geojson`, `zones.geojson`, and `dem.asc`. Optional `source.srs` names the input CRS (`EPSG:4326` when the file is absent). Every vector layer is reprojected to EPSG:4326 before tippecanoe, and the depth grid is warped with `gdalwarp` before the terrain and hillshade archives are built. Outputs are always `sample-pond.pmtiles`, `sample-pond-terrain.pmtiles`, and `sample-pond-hillshade.pmtiles`.

DNR-shaped contours use `depth` (negative feet) and `abs_depth`. The archives store `depth_ft` as a positive number, plus `dowlknum` and `lake_name`. Lake polygons also carry `max_depth_ft` and `area_acres`. Minnesota DNR exports are EPSG:26915; convert to those four names and write `EPSG:26915` into `source.srs` when reusing this builder on real extracts.

Outputs, for the sample:

- `sample-pond.pmtiles` — vector layers `lakes`, `contours`, `zones`
- `sample-pond-terrain.pmtiles` — terrarium raster-dem (elevation meters, depth below the shore is negative)
- `sample-pond-hillshade.pmtiles` — hillshade raster

## CLI

```bash
# Sample build (same as before)
pnpm pipeline:sample
# or
PYTHONPATH=pipeline python3 -m fishy_pipeline build fixtures/sample-pond apps/web/public/samples

# R2 upload
pnpm pipeline:r2-upload -- path/to/file.pmtiles some/key.pmtiles

# Wisconsin ingest
PYTHONPATH=pipeline python3 -m fishy_pipeline --state wi ingest --fixtures
PYTHONPATH=pipeline python3 -m fishy_pipeline --state wi ingest --harvest-catalog --hydro /path/to/24k-hydro-snippet.geojson

# Wisconsin georef (synthetic fixture, or a work dir produced by ingest)
PYTHONPATH=pipeline python3 -m fishy_pipeline --state wi georef --fixtures --work-dir pipeline/work/wi
PYTHONPATH=pipeline python3 -m fishy_pipeline --state wi georef --hydro /path/to/24k-hydro.geojson

# Wisconsin contours (synthetic fixture, or a work dir produced by georef)
PYTHONPATH=pipeline python3 -m fishy_pipeline --state wi contours --fixtures --work-dir pipeline/work/wi
PYTHONPATH=pipeline python3 -m fishy_pipeline --state wi contours --hydro /path/to/24k-hydro.geojson
```

## Wisconsin georef

`georef` reads the ingest manifest and a 24K Hydro GeoJSON in EPSG:3071. Each row is one `(wbic, map_key)` pair. `map_key` is the document name and can be shared (the Chain O' Lakes sheet is one file and many WBICs). The fit uses that WBIC's own shoreline.

The stage drops the neatline, full-span PLSS grid, and legend, then keeps the dark component that encloses the most water. It initializes from the bounding box and four orientations, refines with affine point registration, and places a thin-plate spline through a small set of control points. The published IoU is the lower of the affine overlap and the spline overlap, so a rubber-sheet cannot hide a mismatched waterbody. The threshold starts at 0.90.

A `map_key` shared by more than one WBIC is queued with reason `shared_sheet`. The sheet can hold several shorelines, and scaling one dominant ring to each lake's box can auto-pass a similar but smaller basin. Manifest WBICs are normalized to seven digits before the hydro lookup. A registration failure is queued as `registration_error` for that lake and the batch continues.

`gdal_translate -gcp` and `gdalwarp -tps -dstalpha -t_srs EPSG:3071` write `georef/lakes/<wbic>/<map_key>/warped.tif` when GDAL is on `PATH`. The alpha band is the mask: bathymetry ink is black (0), so a nodata value of 0 would erase those lines when a later warp clips to the shoreline. GDAL is MIT/X licensed and is already installed in CI as `gdal-bin` (the ink-survival test also uses the `python3-gdal` bindings). The IoU itself does not need GDAL. Lakes under the threshold, shared sheets, multi-sheet lakes, and flowages (HYDROTYPE 707, or "flowage" in the name, including a flowage with no hydro polygon) are listed in `georef/review_queue.json`, with a path to that pair's control points when a fit was written. Shapefile and unknown rows stay skipped with the ingest reason.

`fixtures/wi-georef/known-transform.json` is a synthetic sheet. The test draws the raster in memory. No DNR raster is committed.

## Wisconsin contours

`contours` reads `georef/results.json`, the ingest manifest, and the same 24K Hydro GeoJSON. It clips `warped.tif` to that lake's hydro polygon, including island rings, with `gdalwarp -cutline -dstalpha`. Depth labels are read with EasyOCR (the one OCR engine for this stage). Label boxes are inpainted, the remaining ink is skeletonized, and the strokes become LineStrings in EPSG:3071.

Depths snap to those labels. Closed basin rings with no label take the next interval inward from the parent, starting at the hydro shoreline as depth 0. A ring stops inside `max_depth_ft` from the manifest. Conflicting labels, a step that breaks the interval, an unlabeled island collar, or a depth past the max are `needs_review`. The GeoJSON properties are positive `depth_ft`, `wbic`, `interval_ft`, and `confidence`.

Output lands in `contours/lakes/<wbic>/<map_key>/contours.geojson` and `contours/results.json` under the work directory. Rasters and GeoJSON stay out of git. `--fixtures` writes a synthetic pond and does not load EasyOCR.

EasyOCR, OpenCV, and scikit-image are in the `wi` extra. The depth rules and the clip command are covered by `pnpm run pipeline:test` without that extra. GDAL is MIT/X. EasyOCR's detector weights download on first use and are not committed.

Legacy module names still work: `python3 -m fishy_pipeline.build` and `python3 -m fishy_pipeline.upload`.

## Optional dependencies

Heavy Wisconsin tooling (PyMuPDF for PDF rasterization, plus rasterio, OpenCV, scikit-image, EasyOCR) lives in the **`wi`** extra so default install and CI stay light. PyMuPDF is **AGPL-3.0** (or a commercial license from Artifex) — review before distributing combined binaries. Georeferencing does not import that extra. Contour tracing imports OpenCV, scikit-image, and EasyOCR only when a real sheet is traced. The spline warp and the hydro clip use the GDAL command-line tools (**MIT/X**), which CI installs as `gdal-bin`.

```bash
cd pipeline && pip install -e '.[wi]'
```

CI runs `pnpm run pipeline:test` with stdlib tests only plus system `gdal-bin` for reprojection checks.

See [docs/data-sources.md](../docs/data-sources.md) for Minnesota and Wisconsin source notes.