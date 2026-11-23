# SPDX-License-Identifier: GPL-3.0-only
"""When a Wisconsin lake is publishable versus ``needs_review``."""

from __future__ import annotations

from fishy_pipeline.states.wi.georef import DEFAULT_IOU_THRESHOLD
from fishy_pipeline.states.wi.pilot_data import depths_match_recorded, load_recorded_labels


def publish_gate(
    *,
    wbic: str,
    iou: float | None,
    depths_ft: list[int],
) -> tuple[bool, str, list[str]]:
    """A lake publishes only at IoU >= 0.90 and with depths matching recorded labels."""
    reasons: list[str] = []
    if iou is None:
        reasons.append("georef_unscored")
    elif iou < DEFAULT_IOU_THRESHOLD:
        reasons.append("georef_iou_below_threshold")
    labels = load_recorded_labels(wbic)
    if labels is None:
        reasons.append("recorded_labels_missing")
    else:
        ok, label_reasons = depths_match_recorded(depths_ft, labels)
        if not ok:
            reasons.extend(label_reasons)
    publishable = not reasons
    qa_status = "auto" if publishable else "needs_review"
    return publishable, qa_status, reasons