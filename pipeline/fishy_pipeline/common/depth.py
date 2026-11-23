# SPDX-License-Identifier: GPL-3.0-only
"""Depth, area, and lake-id rules. Keep in step with fixtures/contract.json."""

from __future__ import annotations

import math

METERS_PER_FOOT = 0.3048
SQUARE_METERS_PER_ACRE = 4046.8564224


def round_half_away_from_zero(value: float, digits: int = 0) -> float:
    if not math.isfinite(value):
        return 0.0
    factor = 10**digits
    scaled = value * factor
    if scaled == 0:
        return 0.0
    rounded = math.copysign(math.floor(abs(scaled) + 0.5), scaled)
    return rounded / factor


def feet_to_meters(feet: float) -> float:
    return feet * METERS_PER_FOOT


def depth_feet_from_dnr(depth: float | None, abs_depth: float | None) -> float:
    if abs_depth is not None and math.isfinite(abs_depth):
        return abs(abs_depth)
    if depth is not None and math.isfinite(depth):
        return abs(depth)
    return 0.0


def square_meters_to_acres(square_meters: float) -> float:
    return square_meters / SQUARE_METERS_PER_ACRE


SAMPLE_VIEW = {"longitude": -93.27, "latitude": 44.98, "zoom": 13.4}


def is_dowlknum(value: str) -> bool:
    return len(value) == 8 and all("0" <= char <= "9" for char in value)


def is_wbic(value: str) -> bool:
    return len(value) == 7 and value.isascii() and all("0" <= char <= "9" for char in value)


def is_lake_id_for_state(state: str, lake_id: str) -> bool:
    if state == "mn":
        return is_dowlknum(lake_id)
    if state == "wi":
        return is_wbic(lake_id)
    return False


def lake_finder_url(dowlknum: str) -> str | None:
    if not is_dowlknum(dowlknum):
        return None
    return f"https://www.dnr.state.mn.us/lakefind/showreport.html?downum={dowlknum}"


def pmtiles_protocol_url(source: str) -> str:
    if source.startswith("pmtiles://"):
        return source
    return f"pmtiles://{source}"


def normalize_properties(properties: dict) -> dict:
    """Copy a DNR-shaped feature into the PMTiles property set."""
    depth = _num(properties.get("depth"))
    abs_depth = _num(properties.get("abs_depth"))
    explicit = _num(properties.get("depth_ft"))
    out = {
        "lake_name": properties.get("lake_name") or "Unnamed lake",
        "dowlknum": str(properties.get("dowlknum") or ""),
    }
    if abs_depth is not None or depth is not None:
        out["depth_ft"] = depth_feet_from_dnr(depth, abs_depth)
    elif explicit is not None:
        out["depth_ft"] = explicit
    if "area_acres" in properties and _num(properties.get("area_acres")) is not None:
        out["area_acres"] = float(properties["area_acres"])
    if "max_depth_ft" in properties and _num(properties.get("max_depth_ft")) is not None:
        out["max_depth_ft"] = float(properties["max_depth_ft"])
    return out


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None