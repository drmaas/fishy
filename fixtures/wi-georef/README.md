# WI georef fixture

Synthetic sheet used by `pipeline/tests/test_wi_georef.py` and `python3 -m fishy_pipeline --state wi georef --fixtures`. No DNR raster or hydro extract is stored here.

`known-transform.json` is the whole fixture. The test draws a map (neatline, full-span grid, corner legend, and the shorelines for that sheet) and a matching EPSG:3071 hydro ring from:

```text
theta = radians(theta_deg)
scale = scale_m_per_px
easting  = scale * cos(theta) * col - scale * sin(theta) * row + origin_easting
northing = -scale * sin(theta) * col - scale * cos(theta) * row + origin_northing
```

Row increases south, the same way a PNG does. Cases in the file:

| WBIC | Map key | What it checks |
| --- | --- | --- |
| `0417300` | `0417300a` | Known transform. IoU at least `DEFAULT_IOU_THRESHOLD`, not queued. |
| `0261100` | `0261100a` | Different shoreline on its own sheet. Low IoU, queued. |
| `1403200` | `1403200a` | HYDROTYPE 707 flowage. Queued even when the fit is good. |
| `1403299` | `1403299a` | Flowage whose hydro polygon is missing. Still queued. |
| `2271600` | `2271600a`, `2271600b` | Multi-sheet. Both queued. |
| `1500000`, `1500001` | `1500000a` | Two similar shorelines on one sheet. The smaller lake would score above the gate if the dominant ring were scaled to its box. Both are queued with `shared_sheet`. |
| `0850800` | `0850800b` | Shapefile skip reason kept. |
| `3000001` | `3000001a` | Unknown skip reason kept. |

`map_key` is the document name. `1500000` and `1500001` share `1500000a` and are fitted separately, then queued because the document is shared.
