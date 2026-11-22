# SPDX-License-Identifier: GPL-3.0-only
"""
Georeference ingested Wisconsin bathymetry page images.

Planned steps: detect map frame and graticule or tic marks, solve an affine or polynomial
transform from pixel space to Wisconsin Transverse Mercator or WGS84, and write GeoTIFF depth
grids plus control-point sidecars for QA. Uses GDAL/rasterio and OpenCV from the ``wi`` extra.

Not implemented in this milestone; ``python -m fishy_pipeline --state wi georef`` is a stub.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    raise SystemExit("wi georef is not implemented yet (install with: pip install '.[wi]')")


if __name__ == "__main__":
    main()