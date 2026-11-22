import { describe, expect, it } from "vitest";
import {
  buildLakeDetail,
  buildSearchWhere,
  chaikinOnce,
  CONTOUR_OFFSET,
  contoursQueryUrl,
  DETAIL_OFFSET,
  escapeWhereValue,
  LAKE_DETAIL_CACHE_REV,
  MAX_BBOX_SPAN,
  nearbyQueryUrl,
  normalizeContourProperties,
  normalizeOutlineProperties,
  normalizeSearchHits,
  outlineQueryUrl,
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
    expect(buildSearchWhere("O'Brien", "mn")).toContain("O''BRIEN");
  });

  it("sanitizes search queries", () => {
    expect(sanitizeSearchQuery("a", "mn")).toBeNull();
    expect(sanitizeSearchQuery("Wa", "mn")).toBe("Wa");
    expect(sanitizeSearchQuery("27013300", "mn")).toBe("27013300");
    expect(sanitizeSearchQuery("0417300", "wi")).toBe("0417300");
    expect(sanitizeSearchQuery("0417300", "mn")).toBeNull();
    expect(sanitizeSearchQuery("Lake <script>", "mn")).toBeNull();
    expect(sanitizeSearchQuery("St. Croix", "mn")).toBe("St. Croix");
    expect(buildSearchWhere("27013300", "mn")).toBe("dowlknum='27013300' AND island='N'");
    expect(buildSearchWhere("waconia", "mn")).toBe("island='N' AND UPPER(lake_name) LIKE '%WACONIA%'");
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
    const contour = normalizeContourProperties({
      depth: -15,
      abs_depth: 15,
      dowlknum: "27013300",
      lake_name: "Minnetonka",
    });
    expect(contour.depth_ft).toBe(15);
    expect(contour.depth_ft).toBeGreaterThanOrEqual(0);
    expect(contour).toEqual(
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

  it("uses a fine offset for selected lake geometry and a coarse offset for nearby shores", () => {
    expect(outlineQueryUrl("11030700")).toContain(`maxAllowableOffset=${DETAIL_OFFSET}`);
    expect(contoursQueryUrl("11030700")).toContain(`maxAllowableOffset=${DETAIL_OFFSET}`);
    expect(nearbyQueryUrl({ west: -94, south: 45, east: -93.5, north: 45.4 })).toContain(
      `maxAllowableOffset=${CONTOUR_OFFSET}`,
    );
  });

  it("exports a lake-detail cache revision for client and worker cache keys", () => {
    expect(LAKE_DETAIL_CACHE_REV).toMatch(/^v[0-9]+$/);
  });

  it("cuts a right angle with one Chaikin pass and keeps closed rings closed", () => {
    const open = chaikinOnce(
      [
        [0, 0],
        [0, 1],
        [1, 1],
      ],
      false,
    );
    expect(open[0]).toEqual([0, 0]);
    expect(open[open.length - 1]).toEqual([1, 1]);
    expect(open).toContainEqual([0, 0.75]);
    expect(open).toContainEqual([0.25, 1]);

    const closed = chaikinOnce(
      [
        [0, 0],
        [0, 1],
        [1, 1],
        [1, 0],
        [0, 0],
      ],
      true,
    );
    expect(closed[0]).toEqual(closed[closed.length - 1]);
    expect(closed.length).toBeGreaterThan(5);
  });

  it("smooths selected lake geometry while keeping contour properties", () => {
    const detail = buildLakeDetail({
      dowlknum: "11030700",
      name: "Norway",
      areaAcres: 514.9,
      maxDepthFt: 13,
      extent: { west: -95, south: 46, east: -94.9, north: 46.1 },
      outline: {
        type: "FeatureCollection",
        features: [
          {
            type: "Feature",
            properties: { dowlknum: "11030700", lake_name: "Norway", acres: 514.9, island: "N" },
            geometry: {
              type: "Polygon",
              coordinates: [
                [
                  [0, 0],
                  [0, 1],
                  [1, 1],
                  [1, 0],
                  [0, 0],
                ],
              ],
            },
          },
        ],
      },
      contours: {
        type: "FeatureCollection",
        features: [
          {
            type: "Feature",
            properties: { depth: -5, abs_depth: 5, dowlknum: "11030700", lake_name: "Norway" },
            geometry: {
              type: "LineString",
              coordinates: [
                [0, 0],
                [0, 1],
                [1, 1],
              ],
            },
          },
        ],
      },
    });
    expect(detail.contours.features[0]?.properties).toEqual({
      lake_name: "Norway",
      dowlknum: "11030700",
      depth_ft: 5,
    });
    const line = detail.contours.features[0]?.geometry as { type: string; coordinates: number[][] };
    expect(line.coordinates.length).toBeGreaterThan(3);
    const ring = (detail.outline.features[0]?.geometry as { coordinates: number[][][] }).coordinates[0];
    expect(ring?.[0]).toEqual(ring?.[ring.length - 1]);
  });
});
