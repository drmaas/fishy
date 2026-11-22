# SPDX-License-Identifier: GPL-3.0-only
"""Backward-compatible module entry for ``python3 -m fishy_pipeline.build``."""

from fishy_pipeline.common.build import *  # noqa: F403
from fishy_pipeline.common.build import main

if __name__ == "__main__":
    main()