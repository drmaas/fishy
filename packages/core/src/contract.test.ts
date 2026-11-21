import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  depthFeetFromDnr,
  feetToMeters,
  isDowlknum,
  lakeFinderUrl,
  lakeFromProperties,
  pmtilesProtocolUrl,
  roundHalfAwayFromZero,
  SAMPLE_VIEW,
  squareMetersToAcres,
} from "./index.ts";

interface Contract {
  round: { value: number | null; digits: number; out: number }[];
  feetToMeters: { feet: number; meters: number }[];
  dnrDepthFeet: { depth: number | null; absDepth: number | null; feet: number }[];
  acres: { squareMeters: number; acres: number }[];
  dowlknum: { id: string; valid: boolean; lakeFinder?: string }[];
  pmtiles: { input: string; url: string }[];
  sampleView: { longitude: number; latitude: number; zoom: number };
}

const here = dirname(fileURLToPath(import.meta.url));
const contract = JSON.parse(
  readFileSync(resolve(here, "../../../fixtures/contract.json"), "utf8"),
) as Contract;

describe("contract", () => {
  it("rounds half away from zero", () => {
    for (const row of contract.round) {
      const value = row.value === null ? Number.NaN : row.value;
      expect(roundHalfAwayFromZero(value, row.digits)).toBe(row.out);
    }
  });

  it("converts feet and acres", () => {
    for (const row of contract.feetToMeters) {
      expect(feetToMeters(row.feet)).toBeCloseTo(row.meters, 6);
    }
    for (const row of contract.dnrDepthFeet) {
      expect(depthFeetFromDnr(row.depth, row.absDepth)).toBe(row.feet);
    }
    for (const row of contract.acres) {
      expect(squareMetersToAcres(row.squareMeters)).toBeCloseTo(row.acres, 6);
    }
  });

  it("checks DOW lake ids and PMTiles URLs", () => {
    for (const row of contract.dowlknum) {
      expect(isDowlknum(row.id)).toBe(row.valid);
      expect(lakeFinderUrl(row.id)).toBe(row.valid ? row.lakeFinder : null);
    }
    for (const row of contract.pmtiles) {
      expect(pmtilesProtocolUrl(row.input)).toBe(row.url);
    }
    expect(SAMPLE_VIEW).toEqual(contract.sampleView);
  });

  it("reads a lake click payload", () => {
    const lake = lakeFromProperties({
      lake_name: "Sample Pond",
      dowlknum: "99999900",
      max_depth_ft: 15,
      area_acres: 42.5,
    });
    expect(lake).toEqual({
      name: "Sample Pond",
      dowlknum: "99999900",
      maxDepthFt: 15,
      areaAcres: 42.5,
      synthetic: true,
    });
    expect(lakeFromProperties({ dowlknum: "nope" })).toBeNull();
  });
});
