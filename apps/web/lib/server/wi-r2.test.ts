import { describe, expect, it } from "vitest";
import {
  bboxIntersectsLakeIndex,
  isSafeWiR2Key,
  nearbyWiIndex,
  parseWiCurrentPointer,
  parseWiLakesIndex,
  searchWiIndex,
  wiLakeDetailKey,
  wiNearbyCollection,
  wiSearchHits,
} from "./wi-r2.ts";

const index = [
  {
    id: "0417300",
    name: "Lake Wissota",
    county: "Chippewa",
    acres: 6044,
    bbox: { west: -91.35, south: 44.9, east: -91.2, north: 45.05 },
    qa_status: "verified" as const,
  },
  {
    id: "0980900",
    name: "Pike Lake",
    county: "Bayfield",
    acres: 373,
    bbox: { west: -91.5, south: 46.6, east: -91.4, north: 46.7 },
    qa_status: "needs_review" as const,
  },
];

describe("wi r2 helpers", () => {
  it("accepts only keys under wi/v1 without path traversal", () => {
    expect(isSafeWiR2Key("wi/v1/gen/lakes_index.json")).toBe(true);
    expect(isSafeWiR2Key("wi/v1/../secret")).toBe(false);
    expect(isSafeWiR2Key("mn/sample.pmtiles")).toBe(false);
  });

  it("parses the current pointer and rejects bad prefixes", () => {
    expect(
      parseWiCurrentPointer({
        version: 1,
        generation: "20261123T043000Z",
        prefix: "wi/v1/20261123T043000Z",
        lakes_index_key: "wi/v1/20261123T043000Z/lakes_index.json",
        lake_detail_keys: ["wi/v1/20261123T043000Z/lakes/0417300.json"],
      }),
    ).toMatchObject({ generation: "20261123T043000Z" });
    expect(
      parseWiCurrentPointer({
        version: 1,
        generation: "x",
        prefix: "wi/v1/../x",
        lakes_index_key: "wi/v1/gen/lakes_index.json",
        lake_detail_keys: [],
      }),
    ).toBeNull();
  });

  it("searches by name or wbic and only serves verified rows", () => {
    expect(searchWiIndex(index, "0417300")).toHaveLength(1);
    expect(searchWiIndex(index, "wissota")).toHaveLength(1);
    expect(searchWiIndex(index, "pike")).toHaveLength(0);
    expect(wiSearchHits(searchWiIndex(index, "wissota"))[0]).toEqual({
      dowlknum: "0417300",
      lake_name: "Lake Wissota",
      cty_name: "Chippewa",
      acres: 6044,
    });
  });

  it("filters nearby index rows by bbox and builds outline properties", () => {
    const bbox = { west: -91.4, south: 44.95, east: -91.25, north: 45.1 };
    expect(bboxIntersectsLakeIndex(index[0]!.bbox, bbox)).toBe(true);
    const nearby = nearbyWiIndex(index, bbox);
    expect(nearby).toHaveLength(1);
    const collection = wiNearbyCollection(nearby);
    expect(collection.features[0]?.properties).toMatchObject({
      dowlknum: "0417300",
      lake_name: "Lake Wissota",
      island: "N",
    });
  });

  it("builds lake detail keys only from validated wbic values", () => {
    expect(wiLakeDetailKey("wi/v1/gen", "0417300")).toBe("wi/v1/gen/lakes/0417300.json");
    expect(wiLakeDetailKey("wi/v1/gen", "../etc")).toBeNull();
    expect(wiLakeDetailKey("wi/v1/../evil", "0417300")).toBeNull();
    expect(parseWiLakesIndex(index)).toEqual(index);
  });
});