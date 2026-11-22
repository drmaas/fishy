# Architecture

Fishy is a monorepo. The web app and the bathymetry pipeline do not share a process. Shared display rules live in `packages/core` (TypeScript, used by the site and by future native clients). `fixtures/contract.json` is the check that those rules and the Python pipeline helpers still agree.

## Web

`apps/web` is a Vite + React app on a Cloudflare Worker (`wrangler.jsonc`, `@cloudflare/vite-plugin`).

- The browser loads MapLibre. The default style is OpenFreeMap Liberty, which needs no key. The map opens on Minnesota. A state control lists Minnesota only for now.
- Depth contours, depth-zone polygons, and the lake polygon can come from a vector PMTiles archive (`pmtiles` protocol). Sample Pond still ships that way for local demos.
- Surveyed Minnesota lakes are fetched through Worker routes under `/api/lakes`. The Worker queries the DNR MapServer, rewrites properties to positive `depth_ft`, and stores successful responses in the Cache API for one day. Search matches a lake name or an 8-digit DOW id. Zoomed-in views load nearby shore outlines. Selecting a lake loads its outline and generalized contours as GeoJSON.
- A terrarium raster-dem PMTiles archive drives `map.setTerrain` (3D relief). A hillshade PMTiles archive is a raster overlay.
- Clicking a surveyed outline or a PMTiles lake polygon opens a panel: name, max depth, area, DOW lake id, and a LakeFinder link.
- `/api/config` returns public URLs only. `MAPTILER_API_KEY` is optional. When it is set, `/api/basemap/style.json` fetches the outdoor MapTiler style on the Worker. `/api/basemap/upstream/*` forwards only that style, its sprite, TileJSON, tiles, and glyphs, adds the key server-side, rewrites JSON bodies back through the proxy, and refuses redirects. The style body must not contain the key. Do not log upstream URLs.

Non-API requests that reach the Worker return 404. Static files, including `public/samples/*.pmtiles`, are assets. PMTiles reads use HTTP range requests.

## Pipeline

`pipeline/fishy_pipeline` normalizes DNR field names (`depth` / `abs_depth` to `depth_ft`) and builds `ogr2ogr`, `tippecanoe`, GDAL, and `pmtiles convert` commands. `pnpm pipeline:sample` runs that chain on `fixtures/sample-pond`, a synthetic lake with the DNR schema. Real extracts stay in `/data` and are gitignored.

`pnpm pipeline:r2-upload` runs `wrangler r2 object put`. `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, and `R2_BUCKET` come from the environment. They are not command arguments and they are not files in git.

## Native placeholders

`apps/android` and `apps/ios` are empty on purpose. Shared rules for those apps should come from `packages/core` when a client is added.