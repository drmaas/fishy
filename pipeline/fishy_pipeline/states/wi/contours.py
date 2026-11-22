# SPDX-License-Identifier: GPL-3.0-only
"""
Vectorize Wisconsin depth contours from georeferenced rasters.

Planned steps: threshold and trace depth lines (scikit-image / OpenCV), attach depth_ft in feet
positive like Minnesota, merge lake polygons, then call ``fishy_pipeline.common.build`` to emit
vector, terrain, and hillshade PMTiles for upload to R2.

Not implemented in this milestone; ``python -m fishy_pipeline --state wi contours`` is a stub.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    raise SystemExit("wi contours is not implemented yet (install with: pip install '.[wi]')")


if __name__ == "__main__":
    main()