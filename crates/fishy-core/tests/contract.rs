// SPDX-License-Identifier: GPL-3.0-only

use fishy_core::{
    depth_feet_from_dnr, feet_to_meters, is_dowlknum, lake_finder_url, pmtiles_protocol_url,
    round_half_away_from_zero, square_meters_to_acres,
};
use serde_json::Value;

fn contract() -> Value {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../fixtures/contract.json");
    let text = std::fs::read_to_string(path).expect("contract fixture");
    serde_json::from_str(&text).expect("contract json")
}

#[test]
fn matches_shared_contract() {
    let contract = contract();

    for row in contract["round"].as_array().unwrap() {
        let value = match &row["value"] {
            Value::Null => f64::NAN,
            other => other.as_f64().unwrap(),
        };
        let digits = row["digits"].as_i64().unwrap() as i32;
        let out = row["out"].as_f64().unwrap();
        let got = round_half_away_from_zero(value, digits);
        assert!((got - out).abs() < 1e-9, "{got} != {out}");
    }

    for row in contract["feetToMeters"].as_array().unwrap() {
        let got = feet_to_meters(row["feet"].as_f64().unwrap());
        let expect = row["meters"].as_f64().unwrap();
        assert!((got - expect).abs() < 1e-9);
    }

    for row in contract["dnrDepthFeet"].as_array().unwrap() {
        let depth = row["depth"].as_f64();
        let abs_depth = row["absDepth"].as_f64();
        let got = depth_feet_from_dnr(depth, abs_depth);
        assert_eq!(got, row["feet"].as_f64().unwrap());
    }

    for row in contract["acres"].as_array().unwrap() {
        let got = square_meters_to_acres(row["squareMeters"].as_f64().unwrap());
        let expect = row["acres"].as_f64().unwrap();
        assert!((got - expect).abs() < 1e-6);
    }

    for row in contract["dowlknum"].as_array().unwrap() {
        let id = row["id"].as_str().unwrap();
        let valid = row["valid"].as_bool().unwrap();
        assert_eq!(is_dowlknum(id), valid);
        assert_eq!(
            lake_finder_url(id),
            if valid {
                Some(row["lakeFinder"].as_str().unwrap().to_string())
            } else {
                None
            }
        );
    }

    for row in contract["pmtiles"].as_array().unwrap() {
        assert_eq!(
            pmtiles_protocol_url(row["input"].as_str().unwrap()),
            row["url"].as_str().unwrap()
        );
    }
}
