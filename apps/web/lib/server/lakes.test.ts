import { describe, expect, it } from "vitest";
import {
  buildSearchWhere,
  escapeWhereValue,
  MAX_BBOX_SPAN,
  normalizeContourProperties,
  normalizeOutlineProperties,
  normalizeSearchHits,
  parseBbox,
  parseExtent,
  parseMaxDepth,
  sanitizeSearchQuery,
  isSupportedState,
} from "./lakes.ts";

describe("lake proxy helpers", () => {
  it("accepts only minnesota as a state", () => {
    expect(isSupportedState("mn")).toBe(true);
    expect(isSupportedState("wi")).toBe(false);
    expect(isSupportedState(null)).toBe(false);
  });

  it("escapes single quotes in where values", () => {
    expect(escapeWhereValue("O'Brien")).toBe("O''Brien");
    expect(buildSearchWhere("O'Brien")).toContain("O''BRIEN");
  });

  it("sanitizes search queries", () => {
    expect(sanitizeSearchQuery("a")).toBeNull();
    expect(sanitizeSearchQuery("Wa")).toBe("Wa");
    expect(sanitizeSearchQuery("27013300")).toBe("27013300");
    expect(sanitizeSearchQuery("Lake <script>")).toBeNull();
    expect(sanitizeSearchQuery("St. Croix")).toBe("St. Croix");
    expect(buildSearchWhere("27013300")).toBe("dowlknum='27013300' AND island='N'");
    expect(buildSearchWhere("waconia")).toBe("island='N' AND UPPER(lake_name) LIKE '%WACONIA%'");
  });

  it("rejects a bbox that is too large or inverted", () => {
    expect(parseBbox("-93.5", "44.8", "-93.2", "45.0")).toEqual({
      west: -93.5,
      south: 44.8,
      east: -93.2,
      north: 45.0,
    });
    expect(parseBbox("-95", "44", "-93", "45")).toBeNull();
    expect(parseBbox("-93.2", "44.8", "-93.5", "45.0")).toBeNull();
    expect(parseBbox("-93.5", "45.0", "-93.2", "44.8")).toBeNull();
    expect(MAX_BBOX_SPAN).toBe(0.8);
  });

  it("normalizes depth and outline properties", () => {
    expect(normalizeContourProperties({ depth: -15, abs_depth: 15, dowlknum: "27013300", lake_name: "Minnetonka" })).toEqual(
      {
        lake_name: "Minnetonka",
        dowlknum: "27013300",
        depth_ft: 15,
      },
    );
    expect(normalizeOutlineProperties({ lake_name: "Waconia", dowlknum: "10005900", acres: 3075.189, island: "N" })).toEqual(
      {
        lake_name: "Waconia",
        dowlknum: "10005900",
        island: "N",
        area_acres: 3075.189,
      },
    );
  });

  it("builds search hits and parses extent and max depth", () => {
    expect(
      normalizeSearchHits([
        { attributes: { dowlknum: "10005900", lake_name: "Waconia", cty_name: "Carver", acres: 3075.189 } },
        { attributes: { dowlknum: "bad", lake_name: "Nope", acres: 1 } },
      ]),
    ).toEqual([
      { dowlknum: "10005900", lake_name: "Waconia", cty_name: "Carver", acres: 3075.189 },
    ]);
    expect(
      parseExtent({
        extent: { xmin: -93.7, ymin: 44.88, xmax: -93.48, ymax: 44.97 },
      }),
    ).toEqual({ west: -93.7, south: 44.88, east: -93.48, north: 44.97 });
    expect(parseMaxDepth({ features: [{ attributes: { max_depth: 100 } }] })).toBe(100);
  });
});
