# Sample Pond

Synthetic stand-in for one Minnesota lake. The rings use the same field names as the DNR bathymetry contours (`depth` in negative feet, `abs_depth`, `dowlknum`, `lake_name`). The DOW id `99999900` is not a real lake. `dem.asc` is a 10 by 8 depth grid in feet, negative downward, on EPSG:4326. There is no `source.srs` file, so the builder treats the coordinates as EPSG:4326 and still warps the grid.

`just pipeline-sample` turns this directory into the PMTiles under `apps/web/public/samples/`. Do not replace these files with a statewide extract.
