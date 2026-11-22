# WI georef fixture

Synthetic sheet used by `pipeline/tests/test_wi_georef.py` and `python3 -m fishy_pipeline --state wi georef --fixtures`. No DNR raster or hydro extract is stored here.

`known-transform.json` is the whole fixture. The test draws a 160×160 map (neatline, full-span grid, corner legend, one shoreline) and a matching EPSG:3071 hydro ring from:

```text
theta = radians(theta_deg)
scale = scale_m_per_px
easting  = scale * cos(theta) * col - scale * sin(theta) * row + origin_easting
northing = -scale * sin(theta) * col - scale * cos(theta) * row + origin_northing
```

Row increases south, the same way a PNG does. Cases in the file:

| WBIC | Map key | What it checks |
| --- | --- | --- |
| `0417300` | `0417300a` | Known transform. IoU at least 0.90, not queued. |
| `0261100` | `0417300a` | Same document, different shoreline (shared map key). Low IoU, queued. |
| `1403200` | `1403200a` | HYDROTYPE 707 flowage. Queued even when the fit is good. |
| `2271600` | `2271600a`, `2271600b` | Multi-sheet. Both queued. |
| `0850800` | `0850800b` | Shapefile skip reason kept. |
| `3000001` | `3000001a` | Unknown skip reason kept. |

`map_key` is the document name. `0417300` and `0261100` share `0417300a` and are fitted separately.
