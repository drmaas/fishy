# SPDX-License-Identifier: GPL-3.0-only
"""Compatibility re-exports; implementations live in ``fishy_pipeline.common.depth``."""

from fishy_pipeline.common.depth import *  # noqa: F403
from fishy_pipeline.common import depth as _depth

__all__ = [name for name in dir(_depth) if not name.startswith("_")]