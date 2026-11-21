# Fishy

Fishy is a free lake map for anglers. It is a GPL-3.0-only alternative to a commercial fishing map: a MapLibre map, depth contours, depth shading, and 3D relief from public bathymetry. The first data target is Minnesota DNR lake surveys. Weather, NOAA Great Lakes grids, USGS hydrography, and boat ramps are documented and not built yet.

The site is a Vite + React app on a Cloudflare Worker. A Python pipeline turns contours and a depth grid into PMTiles for Cloudflare R2. Shared lake and depth rules live in `packages/core`.

## Status

Milestone zero. The web app frames a synthetic Sample Pond from tiny PMTiles in `apps/web/public/samples/`. It is not a survey, and the DOW id `99999900` is not a real lake. Statewide Minnesota data is not in this repository.

`apps/android` and `apps/ios` are placeholders.

## Toolchain

Node 26 LTS and pnpm 12.21.0 run the web app, checks, and CI. `.nvmrc` is `26`. `nvm install` installs the latest Node 26 LTS.

```bash
pnpm install
pnpm dev
```

Open http://localhost:5173. The map starts on Sample Pond. Click the lake for the info panel. The footer carries the OpenStreetMap and Minnesota DNR credits.

OpenFreeMap is the default basemap and needs no key. To use MapTiler instead, copy `apps/web/.dev.vars.example` to `apps/web/.dev.vars` and set `MAPTILER_API_KEY`. Do not commit that file.

Point the depth layers at R2 by changing `FISHY_CONTOURS_URL`, `FISHY_TERRAIN_URL`, and `FISHY_HILLSHADE_URL` in `apps/web/wrangler.jsonc` (public URLs only) or with `wrangler secret` / `.dev.vars` overrides. Those values are tile URLs, not credentials.

## Run the pipeline

`pnpm pipeline:sample` reads `fixtures/sample-pond` (`lakes.geojson`, `contours.geojson`, `zones.geojson`, and `dem.asc`) and writes `sample-pond.pmtiles`, `sample-pond-terrain.pmtiles`, and `sample-pond-hillshade.pmtiles` under `apps/web/public/samples/`. Those are the only input names the builder reads and the only output names it writes.

```bash
pnpm pipeline:sample
```

The same command works on another directory with those four files:

```bash
PYTHONPATH=pipeline python3 -m fishy_pipeline.build /path/to/lake apps/web/public/samples
```

It still writes the `sample-pond*.pmtiles` names. If the coordinates are not already EPSG:4326, put the source CRS in `source.srs` in that directory. Minnesota DNR bathymetry is `EPSG:26915`. The builder reprojects every vector layer with `ogr2ogr -t_srs EPSG:4326` and warps the depth grid with `gdalwarp` before tiling. `contours_geojson_url` only builds a MapServer query string. It does not download a lake. Checked source URLs are in [docs/data-sources.md](docs/data-sources.md).

You need GDAL (`ogr2ogr`, `gdal_translate`, `gdalwarp`, `gdaldem`), [tippecanoe](https://github.com/felt/tippecanoe), and the [pmtiles](https://github.com/protomaps/go-pmtiles) CLI on `PATH`.

Upload an archive you already built:

```bash
export CLOUDFLARE_API_TOKEN=... CLOUDFLARE_ACCOUNT_ID=... R2_BUCKET=fishy-tiles
pnpm pipeline:r2-upload -- apps/web/public/samples/sample-pond.pmtiles mn/sample-pond.pmtiles
```

The token and account id stay in the environment. Wrangler reads them. They are not stored in the repo.

## Checks

```bash
pnpm check
```

That runs web lint, typecheck, workspace unit tests, pipeline unit tests, the Worker build, a Worker types check, and gitleaks. Install [gitleaks](https://github.com/gitleaks/gitleaks) locally before running `pnpm check` or `pnpm secrets`.