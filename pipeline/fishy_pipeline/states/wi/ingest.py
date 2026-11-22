# SPDX-License-Identifier: GPL-3.0-only
"""
Download Wisconsin DNR bathymetry PDF maps and sidecar metadata into a lake work directory.

Planned inputs: DNR lake pages and printable contour maps (not the Minnesota MapServer).
Outputs per lake: raw PDF bytes, extracted page images, and a manifest JSON under the work tree.
Requires the ``wi`` optional dependencies (rasterio/GDAL, OpenCV, scikit-image, OCR).

Not implemented in this milestone; ``python -m fishy_pipeline --state wi ingest`` is a stub.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    raise SystemExit("wi ingest is not implemented yet (install with: pip install '.[wi]')")


if __name__ == "__main__":
    main()