// SPDX-License-Identifier: GPL-3.0-only

//! Lake identity, depth units, and tile URL rules.
//! The TypeScript copy lives in `packages/core`. Change both and
//! `fixtures/contract.json` in the same change.

/// One international foot in meters.
pub const METERS_PER_FOOT: f64 = 0.3048;

/// One acre in square meters.
pub const SQUARE_METERS_PER_ACRE: f64 = 4046.8564224;

/// Round half away from zero. Non-finite input becomes 0.
pub fn round_half_away_from_zero(value: f64, digits: i32) -> f64 {
    if !value.is_finite() {
        return 0.0;
    }
    let factor = 10_f64.powi(digits);
    let scaled = value * factor;
    let rounded = scaled.abs().round() * scaled.signum();
    // `signum` of 0 is 0, and `round(0)` is 0. Keep a signed zero from
    // collapsing the positive zero case: 0 stays 0.
    if scaled == 0.0 {
        return 0.0;
    }
    rounded / factor
}

pub fn feet_to_meters(feet: f64) -> f64 {
    feet * METERS_PER_FOOT
}

pub fn meters_to_feet(meters: f64) -> f64 {
    meters / METERS_PER_FOOT
}

/// Positive depth in feet. `abs_depth` wins when present.
pub fn depth_feet_from_dnr(depth: Option<f64>, abs_depth: Option<f64>) -> f64 {
    if let Some(abs_depth) = abs_depth.filter(|value| value.is_finite()) {
        return abs_depth.abs();
    }
    if let Some(depth) = depth.filter(|value| value.is_finite()) {
        return depth.abs();
    }
    0.0
}

pub fn square_meters_to_acres(square_meters: f64) -> f64 {
    square_meters / SQUARE_METERS_PER_ACRE
}

/// Eight ASCII digits. County range is not enforced so sample ids (`99…`) pass.
pub fn is_dowlknum(id: &str) -> bool {
    id.len() == 8 && id.bytes().all(|byte| byte.is_ascii_digit())
}

pub fn lake_finder_url(dowlknum: &str) -> Option<String> {
    if !is_dowlknum(dowlknum) {
        return None;
    }
    Some(format!(
        "https://www.dnr.state.mn.us/lakefind/showreport.html?downum={dowlknum}"
    ))
}

pub fn pmtiles_protocol_url(source: &str) -> String {
    if source.starts_with("pmtiles://") {
        return source.to_string();
    }
    format!("pmtiles://{source}")
}
