# Data sources

Checked on 2026-11-21. A URL below was requested and returned HTTP 200 unless the note says otherwise. Fishy does not vendor these datasets. Download them outside the repository and keep the files out of git.

Weather is not implemented. The clients, when they land, will call Open-Meteo and api.weather.gov with no API key.

## Minnesota DNR lake bathymetry (live)

Minnesota is served **live** in production: the Fishy Worker queries the DNR MapServer per lake. There is **no Minnesota batch pipeline** in this repository. The shared `pipeline/fishy_pipeline/common/` build exists only for the synthetic `fixtures/sample-pond` demo and local PMTiles checks.

The product is **Lake Bathymetric Outlines, Contours, and DEM**, published by the DNR Division of Fish and Wildlife, Section of Fisheries, on the Minnesota Geospatial Commons.

| What | URL |
| --- | --- |
| Dataset page | https://gis.data.mn.gov/datasets/lake-bathymetric-outlines-contours-and-dem-2 |
| Item | https://www.arcgis.com/sharing/rest/content/items/c4bbf4b8e45a48afba2d73f24089c187?f=json |
| MapServer | https://enterprise.gisdata.mn.gov/aghost/rest/services/us_mn_state_dnr/water_lake_bathymetry/MapServer |
| Contours (layer 0) | https://enterprise.gisdata.mn.gov/aghost/rest/services/us_mn_state_dnr/water_lake_bathymetry/MapServer/0 |
| Outline (layer 1) | https://enterprise.gisdata.mn.gov/aghost/rest/services/us_mn_state_dnr/water_lake_bathymetry/MapServer/1 |
| Survey metadata (layer 2) | https://enterprise.gisdata.mn.gov/aghost/rest/services/us_mn_state_dnr/water_lake_bathymetry/MapServer/2 |
| Bathymetric DEM (layer 3) | https://enterprise.gisdata.mn.gov/aghost/rest/services/us_mn_state_dnr/water_lake_bathymetry/MapServer/3 |
| Shaded relief ImageServer | https://enterprise.gisdata.mn.gov/aghost/rest/services/us_mn_state_dnr/water_lake_bathy_shaded_relief/ImageServer |

The older GDRS file path `https://resources.gisdata.mn.gov/pub/gdrs/data/pub/us_mn_state_dnr/water_lake_bathymetry/` returned 404 on this date, including `gpkg_water_lake_bathymetry.zip` and `metadata/metadata.html`. Use the MapServer, or a GeoPackage / shapefile / file geodatabase exported from the dataset page.

Contour fields used by the pipeline:

- `depth` is negative feet. Zero is the shore.
- `abs_depth` is the positive depth in feet.
- `dowlknum` is the 8-digit Division of Waters lake id.
- `lake_name` is the DNR name.

The outline and DEM are NAD83 UTM zone 15 (EPSG:26915). The Worker and clients use WGS84; the sample build in `common/` reprojects vectors with `ogr2ogr` and warps rasters with `gdalwarp` when generating offline PMTiles.

A one-lake GeoJSON extract (verified with `dowlknum=27013300`, Lake Minnetonka; the service returned features):

```text
https://enterprise.gisdata.mn.gov/aghost/rest/services/us_mn_state_dnr/water_lake_bathymetry/MapServer/0/query?where=dowlknum%3D%2727013300%27&outFields=*&f=geojson
```

`contours_geojson_url` in `pipeline/fishy_pipeline/common/commands.py` (re-exported from `fishy_pipeline.commands`) builds that URL. Do not commit the response. Statewide contours are large.

Terms: the MapServer item reports access `public` and `licenseInfo` of `None`. The retired metadata page stated access constraints none and use constraints none. Credit the Minnesota Department of Natural Resources on the map. LakeFinder links use `https://www.dnr.state.mn.us/lakefind/showreport.html?downum=<DOWLKNUM>` (verified for `27013300`).

## Wisconsin DNR lake bathymetry (batch)

Wisconsin is **pre-built** from DNR PDF bathymetry maps, tiled to PMTiles, and served from **R2**. Implementation lives under `pipeline/fishy_pipeline/states/wi/` (`ingest`, `georef`, `contours`).

| What | URL |
| --- | --- |
| Lake map catalog (HTML) | https://apps.dnr.wi.gov/lakes/maps/ |
| Catalog form POST target | https://apps.dnr.wi.gov/lakes/Documents/LakeMaps.aspx |
| Example PDF (Ada Lake, doclink) | https://apps.dnr.wi.gov/doclink/lakes_maps/0417300a.pdf |
| Example map download (Altoona Lake, SWIMS — often JPEG) | https://apps.dnr.wi.gov/swims/Documents/DownloadDocument?id=84291351 |
| 24K Hydro open data | https://data-wi-dnr.opendata.arcgis.com/datasets/wi-dnr::24k-hydro-full-file-geodatabase/about |
| 24K Waterbody MapServer layer | https://dnrmaps.wi.gov/arcgis/rest/services/DW_Map_Dynamic/EN_SurfaceWater_WTM_Ext_Dynamic_L16/MapServer/5 |

`ingest` (WI 3/8) harvests the catalog into a manifest (WBIC, sheet suffix `a`/`b`, county, acres, max depth, PDF URL), joins rows to **24K Hydro** waterbodies on padded 7-digit WBIC in EPSG:3071, and renders PDF pages to **300–400 DPI** PNG rasters under `pipeline/work/` (git-ignored). Downloads cache under `pipeline/.cache/wi/` (git-ignored). Committed fixtures for CI live in `fixtures/wi-ingest/`.

`georef` registers each raster to that lake's shoreline. `contours` clips the warped sheet to the hydro polygon, including island rings, and writes per-lake contour GeoJSON under `pipeline/work/wi/contours/`. Those GeoJSON files are generated output, not committed. The next stage publishes them to R2.

`georef` (WI 4/8) registers each raster to that WBIC's 24K Hydro shoreline in EPSG:3071. `wbic` is the lake id (normalized to seven digits). `map_key` names the document: one sheet can be shared by many lakes, and each `(wbic, map_key)` pair is fitted to that waterbody's own ring. A shared `map_key` is queued (`shared_sheet`) so one dominant ring cannot auto-pass a similar smaller basin. The stage ignores the neatline, PLSS grid, and legend, sweeps rotation every few degrees, and refines with point registration. A sheet that shows only part of the lake records footprint overlap, and the gate also requires the mapped ring to cover the full hydro polygon. The warp uses `gdalwarp -tps -dstalpha` when GDAL is on `PATH` and the spline stays near the affine. The alpha band keeps black ink valid for a later shoreline clip. GDAL is MIT/X licensed; CI installs `gdal-bin` and `python3-gdal`. IoU below 0.85, a shared sheet, a multi-sheet lake that does not clear 0.85 on every sheet, or a flowage (HYDROTYPE 707, or "flowage" in the waterbody name, even when the hydro polygon is missing) is written to `georef/review_queue.json`. A registration failure queues that lake and the batch continues. Shapefile and unknown rows stay skipped with their ingest reason. The synthetic check is `fixtures/wi-georef/known-transform.json`. No DNR raster is committed.

Most lakes link straight to `doclink/lakes_maps/{wbic}{a|b}.pdf`. Some rows (for example Altoona Lake) use a SWIMS `DownloadDocument?id=` URL that may return a JPEG, shapefile, or other format; ingest keeps those rows under the padded WBIC, assigns `{wbic}a` / `{wbic}b` when needed, and records detected media types in the manifest. Doclink PDFs are preferred when both are present.

One-lake hydro query (verified against Ada Lake `WATERBODY_WBIC=417300`):

```text
https://dnrmaps.wi.gov/arcgis/rest/services/DW_Map_Dynamic/EN_SurfaceWater_WTM_Ext_Dynamic_L16/MapServer/5/query?where=WATERBODY_WBIC%3D417300&outFields=*&f=geojson&outSR=3071
```

Terms: lake map catalog pages checked 2026-11-22 did not show a per-file license on the PDF links. Wisconsin DNR hydrography is published on the state open data portal for public use with credit to the Wisconsin Department of Natural Resources. Do not commit statewide hydro GeoPackages, PDFs, or rendered rasters. Credit Wisconsin DNR on the map when Wisconsin layers ship (see [ATTRIBUTIONS.md](../ATTRIBUTIONS.md)).

## NOAA NCEI Great Lakes bathymetry

Not loaded yet. Product page (200): https://www.ncei.noaa.gov/products/great-lakes-bathymetry

Grid extract (200): https://www.ncei.noaa.gov/maps/grid-extract/

An old NGDC zip, `https://www.ngdc.noaa.gov/mgg/greatlakes/erie/data/bathymetry/erie_lld.zip`, returned 404. Pull grids from the product page or Grid Extract. The data are United States government work, public domain. Credit NOAA National Centers for Environmental Information.

## USGS National Hydrography Dataset

Not loaded yet.

- Product page (200): https://www.usgs.gov/national-hydrography/national-hydrography-dataset
- Access page (200): https://www.usgs.gov/national-hydrography/access-national-hydrography-products
- Staged state GeoPackages (200): https://prd-tnm.s3.amazonaws.com/index.html?prefix=StagedProducts/Hydrography/NHD/State/GPKG/

USGS data are public domain. Credit the U.S. Geological Survey.

## OpenStreetMap boat ramps

Not loaded yet. Ramps are features tagged `leisure=slipway`. Tag page (200): https://wiki.openstreetmap.org/wiki/Tag:leisure%3Dslipway

Copyright (200): https://www.openstreetmap.org/copyright

The data stay under the Open Database License. The visible credit is “© OpenStreetMap contributors”, linked to that copyright page. A future extract can use the Overpass API at `https://overpass-api.de/api/interpreter`. A query from this environment timed out, so that interpreter call is not marked verified.

## Basemap

Default style, no key (200): https://tiles.openfreemap.org/styles/liberty

Dark style, no key (200): https://tiles.openfreemap.org/styles/dark

OpenFreeMap serves OpenStreetMap data. Keep the OpenStreetMap credit visible. An optional MapTiler outdoor style is proxied by the Worker only when `MAPTILER_API_KEY` is set in the environment. The key is not written into the repository or into the style document sent to the browser.

## Weather (later)

- Open-Meteo forecast (200): https://api.open-meteo.com/v1/forecast
- License (200): https://open-meteo.com/en/licence (CC BY 4.0; credit Open-Meteo)
- NWS API root (200): https://api.weather.gov/
- NWS API docs (200): https://www.weather.gov/documentation/services-web-api

NWS data are public domain. Credit the National Weather Service. Send a descriptive User-Agent on NWS requests.
