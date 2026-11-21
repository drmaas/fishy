# Fishy

Fishy is a free lake map for anglers. It is a GPL-3.0-only alternative to a commercial fishing map: a MapLibre map, depth contours, depth shading, and 3D relief from public bathymetry. The first data target is Minnesota DNR lake surveys. Weather, NOAA Great Lakes grids, USGS hydrography, and boat ramps are documented and not built yet.

The site is a Vite + React app on a Cloudflare Worker. A Python pipeline turns contours and a depth grid into PMTiles for Cloudflare R2. Shared lake and depth rules live in `packages/core` and `crates/fishy-core`.

## Status

Milestone zero. The web app frames a synthetic Sample Pond from tiny PMTiles in `apps/web/public/samples/`. It is not a survey, and the DOW id `99999900` is not a real lake. Statewide Minnesota data is not in this repository.

`apps/android` and `apps/ios` are placeholders.

## Run the web app

Node 22 or newer, pnpm 10, and [just](https://github.com/casey/just).

```bash
pnpm install
just dev
```

Open http://localhost:5173. The map starts on Sample Pond. Click the lake for the info panel. The footer carries the OpenStreetMap and Minnesota DNR credits.

OpenFreeMap is the default basemap and needs no key. To use MapTiler instead, copy `apps/web/.dev.vars.example` to `apps/web/.dev.vars` and set `MAPTILER_API_KEY`. Do not commit that file.

Point the depth layers at R2 by changing `FISHY_CONTOURS_URL`, `FISHY_TERRAIN_URL`, and `FISHY_HILLSHADE_URL` in `apps/web/wrangler.jsonc` (public URLs only) or with `wrangler secret` / `.dev.vars` overrides. Those values are tile URLs, not credentials.

## Run the pipeline

Sample Pond is reproducible. You need GDAL (`ogr2ogr`, `gdal_translate`, `gdaldem`), [tippecanoe](https://github.com/felt/tippecanoe), and the [pmtiles](https://github.com/protomaps/go-pmtiles) CLI on `PATH`.

```bash
just pipeline-sample
```

That reads `fixtures/sample-pond` and writes three small archives under `apps/web/public/samples/`. For a real lake, put a GeoJSON, shapefile, or GeoPackage in `/data` (gitignored) and use the same steps. `contours_geojson_url` builds a one-lake query against the DNR MapServer. Source URLs that were checked are in [docs/data-sources.md](docs/data-sources.md).

Upload an archive you already built:

```bash
export CLOUDFLARE_API_TOKEN=... CLOUDFLARE_ACCOUNT_ID=... R2_BUCKET=fishy-tiles
just r2-upload apps/web/public/samples/sample-pond.pmtiles mn/sample-pond.pmtiles
```

The token and account id stay in the environment. Wrangler reads them. They are not stored in the repo.

## Checks

```bash
just check
```

That runs Rust fmt, clippy, and tests; web lint, typecheck, unit tests, and the Worker build; pipeline unit tests; a Worker types check; and gitleaks.
