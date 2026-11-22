# SPDX-License-Identifier: GPL-3.0-only
"""Backward-compatible module entry for ``python3 -m fishy_pipeline.upload``."""

from fishy_pipeline.common.upload import main

if __name__ == "__main__":
    main()