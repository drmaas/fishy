# Pipeline

Turns one lake's contours and a depth grid into PMTiles.

`build_lake` reads four files from one directory: `lakes.geojson`, `contours.geojson`, `zones.geojson`, and `dem.asc`. Optional `source.srs` names the input CRS (`EPSG:4326` when the file is absent). Every vector layer is reprojected to EPSG:4326 before tippecanoe, and the depth grid is warped with `gdalwarp` before the terrain and hillshade archives are built. Outputs are always `sample-pond.pmtiles`, `sample-pond-terrain.pmtiles`, and `sample-pond-hillshade.pmtiles`.

DNR contours use `depth` (negative feet) and `abs_depth`. The archives store `depth_ft` as a positive number, plus `dowlknum` and `lake_name`. Lake polygons also carry `max_depth_ft` and `area_acres`. Minnesota DNR files are EPSG:26915; export them to those four names (or convert a shapefile or GeoPackage to GeoJSON with `ogr2ogr` first) and write `EPSG:26915` into `source.srs`.

Outputs, for the sample:

- `sample-pond.pmtiles` — vector layers `lakes`, `contours`, `zones`
- `sample-pond-terrain.pmtiles` — terrarium raster-dem (elevation meters, depth below the shore is negative)
- `sample-pond-hillshade.pmtiles` — hillshade raster

```bash
just pipeline-sample
just r2-upload path/to/file.pmtiles some/key.pmtiles
```

See [docs/data-sources.md](../docs/data-sources.md) for the Minnesota URLs the commands target.
