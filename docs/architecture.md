# Architecture

Fishy is a monorepo. The web app and the bathymetry pipeline do not share a process. Shared display rules live in `packages/core` (TypeScript, used by the site and by future native clients). `fixtures/contract.json` is the check that those rules and the Python pipeline helpers still agree.

## Web

`apps/web` is a Vite + React app on a Cloudflare Worker (`wrangler.jsonc`, `@cloudflare/vite-plugin`).

- The browser loads MapLibre. The default style is OpenFreeMap Liberty, which needs no key. The map opens on Minnesota. A state control lists Minnesota only for now. The locate control is opt-in; the GPS coordinate never leaves the browser.
- Depth contours, depth-zone polygons, and the lake polygon can come from a vector PMTiles archive (`pmtiles` protocol). Sample Pond still ships that way for local demos.
- Surveyed Minnesota lakes are fetched through Worker routes under `/api/lakes`. The Worker queries the DNR MapServer, rewrites properties to positive `depth_ft`, and stores successful responses in the Cache API for one day. Search matches a lake name or an 8-digit DOW id. Zoomed-in views load nearby shore outlines. Selecting a lake loads its outline and generalized contours as GeoJSON.
- A terrarium raster-dem PMTiles archive drives `map.setTerrain` (3D relief). A hillshade PMTiles archive is a raster overlay.
- Clicking a surveyed outline or a PMTiles lake polygon opens a panel: name, max depth, area, DOW lake id, and a LakeFinder link.
- `/api/config` returns public URLs only. `MAPTILER_API_KEY` is optional. When it is set, `/api/basemap/style.json` fetches the outdoor MapTiler style on the Worker. `/api/basemap/upstream/*` forwards only that style, its sprite, TileJSON, tiles, and glyphs, adds the key server-side, rewrites JSON bodies back through the proxy, and refuses redirects. The style body must not contain the key. Do not log upstream URLs.

Non-API requests that reach the Worker return 404. Static files, including `public/samples/*.pmtiles`, are assets. PMTiles reads use HTTP range requests.

## Pipeline

`pipeline/fishy_pipeline` normalizes DNR field names (`depth` / `abs_depth` to `depth_ft`) and builds `ogr2ogr`, `tippecanoe`, GDAL, and `pmtiles convert` commands. `pnpm pipeline:sample` runs that chain on `fixtures/sample-pond`, a synthetic lake with the DNR schema. Real extracts stay in `/data` and are gitignored.

Wisconsin adds a batch path under `fishy_pipeline/states/wi/`: ingest, georeference, contour tracing, then `package`. `contours` clips each warped sheet to the 24K Hydro polygon (island rings included), reads depth labels, and writes a per-lake GeoJSON in EPSG:3071. `package` builds per-lake JSON (MN-shaped detail plus `source_pdf_url` and `qa_status`) and `lakes_index.json`, staging each run under a new generation prefix `wi/v1/<build-id>/…`. Upload puts only the keys listed in `package-manifest.json`, then writes `wi/v1/current.json` last so clients never read a half-published generation. A future Worker step will read `wi/v1/current.json`, follow `lakes_index_key` and `lake_detail_keys`, and serve WI lakes the same way MN uses MapServer today (`SUPPORTED_STATES` is still `{ mn }` until that wiring lands). Only lakes that pass the publish gate are included.

Wisconsin is a separate batch under `fishy_pipeline/states/wi/`. `ingest` writes a manifest and rasters. `georef` registers each `(wbic, map_key)` pair to that WBIC's 24K Hydro shoreline in EPSG:3071 and writes `georef/review_queue.json` for a low IoU, a shared sheet, a multi-sheet lake, or a flowage. Optional human GCPs in `pipeline/data/wi/gcps/` override the automatic warp. `contours` uses committed depth labels in `pipeline/data/wi/labels/`; EasyOCR is optional. A lake publishes only when IoU ≥ 0.85 and traced depths match the recorded labels (see [wi-gcp-review.md](wi-gcp-review.md)). R2 upload and the web app are later stages. Minnesota stays on the live MapServer.

`pnpm pipeline:r2-upload` runs `wrangler r2 object put`. `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, and `R2_BUCKET` come from the environment. They are not command arguments and they are not files in git.

## Native placeholders

`apps/android` and `apps/ios` are empty on purpose. Shared rules for those apps should come from `packages/core` when a client is added.