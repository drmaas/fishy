import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  depthFeetFromDnr,
  feetToMeters,
  isDowlknum,
  isLakeIdForState,
  isWbic,
  lakeFinderUrl,
  lakeFromProperties,
  pmtilesProtocolUrl,
  roundHalfAwayFromZero,
  SAMPLE_VIEW,
  squareMetersToAcres,
  type LakeIndexRow,
} from "./index.ts";

interface Contract {
  round: { value: number | null; digits: number; out: number }[];
  feetToMeters: { feet: number; meters: number }[];
  dnrDepthFeet: { depth: number | null; absDepth: number | null; feet: number }[];
  positiveDepthFt: {
    depth: number | null;
    absDepth: number | null;
    explicit?: number;
    depth_ft: number;
  }[];
  acres: { squareMeters: number; acres: number }[];
  dowlknum: { id: string; valid: boolean; lakeFinder?: string }[];
  wbic: { id: string; valid: boolean }[];
  lakeRef: { state: string; id: string; valid: boolean }[];
  wiLakeIndex: LakeIndexRow[];
  wiLakeDetail: {
    id: string;
    name: string;
    maxDepthFt: number;
    areaAcres: number;
    extent: { west: number; south: number; east: number; north: number };
    source_pdf_url?: string;
    qa_status?: string;
    survey_date?: string;
  };
  pmtiles: { input: string; url: string }[];
  sampleView: { longitude: number; latitude: number; zoom: number };
}

const here = dirname(fileURLToPath(import.meta.url));
const contract = JSON.parse(
  readFileSync(resolve(here, "../../../fixtures/contract.json"), "utf8"),
) as Contract;

function positiveDepthFt(
  depth: number | null,
  absDepth: number | null,
  explicit: number | null = null,
): number {
  if (explicit !== null && Number.isFinite(explicit)) return explicit;
  return depthFeetFromDnr(depth, absDepth);
}

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

  it("keeps published depth_ft positive", () => {
    for (const row of contract.positiveDepthFt) {
      const feet = positiveDepthFt(row.depth, row.absDepth, row.explicit ?? null);
      expect(feet).toBe(row.depth_ft);
      expect(feet).toBeGreaterThanOrEqual(0);
    }
  });

  it("checks lake ids, lake refs, and PMTiles URLs", () => {
    for (const row of contract.dowlknum) {
      expect(isDowlknum(row.id)).toBe(row.valid);
      expect(lakeFinderUrl(row.id)).toBe(row.valid ? row.lakeFinder : null);
    }
    for (const row of contract.wbic) {
      expect(isWbic(row.id)).toBe(row.valid);
    }
    for (const row of contract.lakeRef) {
      expect(isLakeIdForState(row.state, row.id)).toBe(row.valid);
    }
    for (const row of contract.pmtiles) {
      expect(pmtilesProtocolUrl(row.input)).toBe(row.url);
    }
    expect(SAMPLE_VIEW).toEqual(contract.sampleView);
  });

  it("describes Wisconsin index and detail fixtures", () => {
    expect(contract.wiLakeIndex).toHaveLength(1);
    const indexRow = contract.wiLakeIndex[0]!;
    expect(isWbic(indexRow.id)).toBe(true);
    expect(indexRow.qa_status).toBe("verified");

    const detail = contract.wiLakeDetail;
    expect(isWbic(detail.id)).toBe(true);
    expect(detail.maxDepthFt).toBeGreaterThan(0);
    expect(detail.qa_status).toBe("verified");
    expect(detail.source_pdf_url).toMatch(/^https:\/\//);
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