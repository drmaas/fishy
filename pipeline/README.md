# Pipeline

Fishy’s Python tooling turns lake bathymetry into PMTiles for the map. Layout is keyed by **state**:

| State | Runtime path |
| --- | --- |
| Minnesota (`mn`) | **No pipeline.** The Worker queries the [MN DNR MapServer](https://enterprise.gisdata.mn.gov/aghost/rest/services/us_mn_state_dnr/water_lake_bathymetry/MapServer) live. |
| Wisconsin (`wi`) | **Batch pipeline** under `fishy_pipeline/states/wi/` (ingest → georef → contours). Tiles upload to R2. Stubs only for now. |
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

# Wisconsin stages (stubs; need optional deps later)
PYTHONPATH=pipeline python3 -m fishy_pipeline --state wi ingest
PYTHONPATH=pipeline python3 -m fishy_pipeline --state wi georef
PYTHONPATH=pipeline python3 -m fishy_pipeline --state wi contours
```

Legacy module names still work: `python3 -m fishy_pipeline.build` and `python3 -m fishy_pipeline.upload`.

## Optional dependencies

Heavy Wisconsin tooling (GDAL/rasterio, OpenCV, scikit-image, OCR) lives in the **`wi`** extra so default install and CI stay light:

```bash
cd pipeline && pip install -e '.[wi]'
```

CI runs `pnpm run pipeline:test` with stdlib tests only plus system `gdal-bin` for reprojection checks.

See [docs/data-sources.md](../docs/data-sources.md) for Minnesota and Wisconsin source notes.