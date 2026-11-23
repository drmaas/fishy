import contract from "../../../fixtures/contract.json";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchLakeDetail, fetchNearbyLakes, searchLakes } from "./lakeApi.ts";
import { STATES } from "./states.ts";

const wiDetail = {
  ...contract.wiLakeDetail,
  dowlknum: contract.wiLakeDetail.id,
  outline: { type: "FeatureCollection" as const, features: [] },
  contours: { type: "FeatureCollection" as const, features: [] },
};

function wiNearbyFixture() {
  const row = contract.wiLakeIndex[0]!;
  const { west, south, east, north } = row.bbox;
  return {
    type: "FeatureCollection" as const,
    features: [
      {
        type: "Feature" as const,
        properties: {
          dowlknum: row.id,
          lake_name: row.name,
          cty_name: row.county,
          area_acres: row.acres,
        },
        geometry: {
          type: "Polygon" as const,
          coordinates: [
            [
              [west, south],
              [west, north],
              [east, north],
              [east, south],
              [west, south],
            ],
          ],
        },
      },
    ],
  };
}

describe("state switching", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("includes Wisconsin with geographic bounds", () => {
    expect(STATES.map((state) => state.id)).toEqual(["mn", "wi"]);
    const wi = STATES.find((state) => state.id === "wi")!;
    expect(wi.bounds[0]).toBeLessThan(wi.bounds[2]);
    expect(wi.bounds[1]).toBeLessThan(wi.bounds[3]);
  });

  it("loads WI search, nearby, and detail from contract-shaped API fixtures", async () => {
    const row = contract.wiLakeIndex[0]!;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/lakes/nearby")) {
          return new Response(JSON.stringify(wiNearbyFixture()), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        if (url.includes("/api/lakes/0417300")) {
          return new Response(JSON.stringify(wiDetail), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        if (url.includes("/api/lakes?")) {
          return new Response(
            JSON.stringify({
              lakes: [
                {
                  dowlknum: row.id,
                  lake_name: row.name,
                  cty_name: row.county,
                  acres: row.acres,
                },
              ],
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return new Response("not found", { status: 404 });
      }),
    );

    const search = await searchLakes("wi", "wissota");
    expect(search).toEqual([
      { dowlknum: row.id, lake_name: row.name, cty_name: row.county, acres: row.acres },
    ]);

    const nearby = await fetchNearbyLakes("wi", row.bbox);
    expect(nearby?.features).toHaveLength(1);
    expect(nearby?.features[0]?.properties?.dowlknum).toBe(row.id);

    const detail = await fetchLakeDetail("wi", row.id);
    expect(detail).toMatchObject({
      name: contract.wiLakeDetail.name,
      source_pdf_url: contract.wiLakeDetail.source_pdf_url,
      qa_status: "verified",
    });
  });
});