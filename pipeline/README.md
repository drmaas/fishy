# Pipeline

Turns one lake's contours and a depth grid into PMTiles.

Inputs are local files: GeoJSON today, and anything `ogr2ogr` can read when you point the same commands at a shapefile or GeoPackage. DNR contours use `depth` (negative feet) and `abs_depth`. The archives store `depth_ft` as a positive number, plus `dowlknum` and `lake_name`. Lake polygons also carry `max_depth_ft` and `area_acres`.

Outputs, for the sample:

- `sample-pond.pmtiles` — vector layers `lakes`, `contours`, `zones`
- `sample-pond-terrain.pmtiles` — terrarium raster-dem (elevation meters, depth below the shore is negative)
- `sample-pond-hillshade.pmtiles` — hillshade raster

```bash
just pipeline-sample
just r2-upload path/to/file.pmtiles some/key.pmtiles
```

See [docs/data-sources.md](../docs/data-sources.md) for the Minnesota URLs the commands target.
