# Architecture

Fishy is a monorepo. The web app and the bathymetry pipeline do not share a process. Shared display rules live in `packages/core` (TypeScript, used by the site) and `crates/fishy-core` (Rust, for a future native client). `fixtures/contract.json` is the check that those copies, and the Python pipeline helpers, still agree.

## Web

`apps/web` is a Vite + React app on a Cloudflare Worker (`wrangler.jsonc`, `@cloudflare/vite-plugin`).

- The browser loads MapLibre. The default style is OpenFreeMap Liberty, which needs no key.
- Depth contours, depth-zone polygons, and the lake polygon come from a vector PMTiles archive (`pmtiles` protocol).
- A terrarium raster-dem PMTiles archive drives `map.setTerrain` (3D relief). A hillshade PMTiles archive is a raster overlay.
- Clicking the lake polygon opens a panel: name, max depth, area, DOW lake id, and a LakeFinder link.
- `/api/config` returns public URLs only. `MAPTILER_API_KEY` is optional. When it is set, `/api/basemap/style.json` fetches a MapTiler style on the Worker and rewrites tile hosts to `/api/basemap/upstream/*`, which adds the key server-side. The style body must not contain the key. Do not log upstream URLs.

Non-API requests that reach the Worker return 404. Static files, including `public/samples/*.pmtiles`, are assets. PMTiles reads use HTTP range requests.

## Pipeline

`pipeline/fishy_pipeline` normalizes DNR field names (`depth` / `abs_depth` to `depth_ft`) and builds `ogr2ogr`, `tippecanoe`, GDAL, and `pmtiles convert` commands. `just pipeline-sample` runs that chain on `fixtures/sample-pond`, a synthetic lake with the DNR schema. Real extracts stay in `/data` and are gitignored.

`just r2-upload` runs `wrangler r2 object put`. `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, and `R2_BUCKET` come from the environment. They are not command arguments and they are not files in git.

## Native placeholders

`apps/android` and `apps/ios` are empty on purpose. `crates/fishy-core` is the Rust home for rules those apps should share.
