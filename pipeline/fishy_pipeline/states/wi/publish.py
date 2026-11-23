# SPDX-License-Identifier: GPL-3.0-only
"""When a Wisconsin lake is publishable versus ``needs_review``."""

from __future__ import annotations

from fishy_pipeline.states.wi.georef import DEFAULT_IOU_THRESHOLD
from fishy_pipeline.states.wi.pilot_data import depths_match_recorded, load_recorded_labels

# Reviewed GCPs may drop only this georef queue reason when IoU clears 0.90.
_IOU_GEOREF_REASON = "iou_below_threshold"


def publish_gate(
    *,
    wbic: str,
    iou: float | None,
    depths_ft: list[int],
    georef_qa_status: str | None = None,
    georef_reasons: list[str] | None = None,
    warp_source: str | None = None,
) -> tuple[bool, str, list[str]]:
    """A lake publishes only when georef, IoU, and recorded depths all pass."""
    reasons: list[str] = []
    if georef_qa_status is not None and georef_qa_status != "auto":
        reasons.append("georef_qa_not_auto")

    blocking = list(georef_reasons or [])
    if warp_source == "reviewed" and iou is not None and iou >= DEFAULT_IOU_THRESHOLD:
        blocking = [reason for reason in blocking if reason != _IOU_GEOREF_REASON]

    for reason in blocking:
        if reason not in reasons:
            reasons.append(reason)

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