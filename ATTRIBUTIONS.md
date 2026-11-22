# Attributions

Fishy is GPL-3.0-only. The data it displays are not relicensed by that grant. Each source keeps its own terms.

| Source | Terms | Credit shown in the app |
| --- | --- | --- |
| Minnesota DNR lake bathymetry | Public distribution from the Minnesota Geospatial Commons. Access constraints: none. Use constraints: none, per the DNR metadata for this product. | Bathymetry © Minnesota DNR |
| Wisconsin DNR lake bathymetry PDF maps | Printable lake maps from the Wisconsin DNR lake maps catalog ([apps.dnr.wi.gov/lakes/maps](https://apps.dnr.wi.gov/lakes/maps/)). The catalog pages checked 2026-11-22 carry no separate click-through license on each PDF link; Wisconsin agency geospatial products are published on the [Wisconsin DNR Open Data Portal](https://data-wi-dnr.opendata.arcgis.com/) for public use with source credit. Fishy does not redistribute the PDFs from git. | Bathymetry © Wisconsin DNR (when Wisconsin depth layers are on) |
| Wisconsin DNR 24K Hydro | Hydrography polygons from WI DNR 24K Hydro ([open data](https://data-wi-dnr.opendata.arcgis.com/datasets/wi-dnr::24k-hydro-full-file-geodatabase/about)), native EPSG:3071 (Wisconsin Transverse Mercator). | Credit Wisconsin DNR with hydro joins and published tiles |
| U.S. Geological Survey NHD | Public domain (U.S. government work). | Credit the U.S. Geological Survey when NHD layers are added. |
| NOAA NCEI Great Lakes bathymetry | Public domain (U.S. government work). | Credit NOAA NCEI when those grids are added. |
| OpenStreetMap, via OpenFreeMap or MapTiler | ODbL. The data remain under ODbL. | © OpenStreetMap contributors, linked to https://www.openstreetmap.org/copyright |
| OpenFreeMap | MIT for the tile software and styles. Map data are OpenStreetMap. | OpenFreeMap, next to the OpenStreetMap credit |
| MapTiler (optional) | Their license, only when a key is configured outside the repo. | © MapTiler, plus the OpenStreetMap credit |
| Open-Meteo (not wired yet) | CC BY 4.0 | Credit Open-Meteo, with a link to https://open-meteo.com/ |
| National Weather Service (not wired yet) | Public domain | Credit the National Weather Service |

The map footer keeps the OpenStreetMap credit visible whenever a street basemap is on, and it keeps the Minnesota DNR credit visible with the depth layers. The Credits control lists USGS, NOAA, Open-Meteo, and NWS as well.

Details and the URLs that were checked are in [docs/data-sources.md](docs/data-sources.md).
