# SPDX-License-Identifier: GPL-3.0-only
"""Shared Wisconsin batch constants."""

from __future__ import annotations

# Georef queue, publish gate, and gcp-review preview messaging.
DEFAULT_IOU_THRESHOLD = 0.75

# Ink / basin reranking band only — not a publish or queue gate.
NEAR_GATE_IOU = DEFAULT_IOU_THRESHOLD - 0.01