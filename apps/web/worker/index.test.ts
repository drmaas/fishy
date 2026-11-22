import { afterEach, describe, expect, it, vi } from "vitest";
import worker from "./index";

const KEY = "secret-key";
const ENV = { MAPTILER_API_KEY: KEY };

function call(path: string): Promise<Response> {
  return worker.fetch(new Request(`https://fishy.example${path}`), ENV);
}

describe("lake worker routes", () => {
  it("rejects an unsupported state or bad lake id before the upstream request", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    expect((await call("/api/lakes?state=wi&q=waconia")).status).toBe(404);
    expect((await call("/api/lakes?state=mn&q=x")).status).toBe(404);
    expect((await call("/api/lakes/nearby?state=mn&west=-95&south=44&east=-93&north=45")).status).toBe(404);
    expect((await call("/api/lakes/270133?state=mn")).status).toBe(404);
    expect((await call("/api/lakes/not-a-lake?state=mn")).status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns search hits from a mocked DNR response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        return new Response(
          JSON.stringify({
            features: [
              {
                attributes: {
                  dowlknum: "10005900",
                  lake_name: "Waconia",
                  cty_name: "Carver",
                  acres: 3075.189,
                },
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }),
    );
    const response = await call("/api/lakes?state=mn&q=waconia");
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("public, max-age=86400");
    expect(await response.json()).toEqual({
      lakes: [{ dowlknum: "10005900", lake_name: "Waconia", cty_name: "Carver", acres: 3075.189 }],
    });
  });

  it("returns a smoothed lake detail from mocked DNR responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("returnExtentOnly")) {
          return new Response(
            JSON.stringify({ extent: { xmin: -95.12, ymin: 46.68, xmax: -95.05, ymax: 46.72 } }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        if (url.includes("outStatistics")) {
          return new Response(JSON.stringify({ features: [{ attributes: { max_depth: 13 } }] }), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        if (url.includes("returnGeometry=false")) {
          return new Response(
            JSON.stringify({
              features: [{ attributes: { dowlknum: "11030700", lake_name: "Norway", acres: 514.9 } }],
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        if (url.includes("/MapServer/0/")) {
          return new Response(
            JSON.stringify({
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
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({
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
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }),
    );
    const response = await call("/api/lakes/11030700?state=mn");
    expect(response.status).toBe(200);
    const body = (await response.json()) as {
      name: string;
      contours: { features: Array<{ properties: Record<string, unknown>; geometry: { coordinates: number[][] } }> };
    };
    expect(body.name).toBe("Norway");
    expect(body.contours.features[0]?.properties).toEqual({
      lake_name: "Norway",
      dowlknum: "11030700",
      depth_ft: 5,
    });
    expect(body.contours.features[0]?.geometry.coordinates.length).toBeGreaterThan(3);
  });
});

function headers(response: Response): string {
  return [...response.headers.entries()].map(([name, value]) => `${name}: ${value}`).join("\n");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("maptiler worker proxy", () => {
  it("rejects geocoding before the upstream request", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const response = await call("/api/basemap/upstream/geocoding/minneapolis.json");
    expect(response.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(await response.text()).not.toContain(KEY);
    expect(headers(response)).not.toContain(KEY);
  });

  it("rewrites TileJSON and keeps glyph placeholders", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        expect(init?.redirect).toBe("manual");
        return new Response(
          JSON.stringify({
            tiles: [`https://api.maptiler.com/tiles/v3/{z}/{x}/{y}.pbf?key=${KEY}`],
            glyphs: `https://api.maptiler.com/fonts/{fontstack}/{range}.pbf?key=${KEY}`,
          }),
          {
            status: 200,
            headers: { "content-type": "application/json", "set-cookie": `session=${KEY}` },
          },
        );
      }),
    );
    const response = await call("/api/basemap/upstream/tiles/v3/tiles.json");
    const body = await response.text();
    expect(response.status).toBe(200);
    expect(body).toContain("/tiles/v3/{z}/{x}/{y}.pbf");
    expect(body).toContain("/fonts/{fontstack}/{range}.pbf");
    expect(body).not.toContain("%7B");
    expect(body).not.toContain(KEY);
    expect(body).not.toContain("api.maptiler.com");
    const listed = headers(response);
    expect(listed).not.toContain(KEY);
    expect(listed.toLowerCase()).not.toContain("set-cookie");
  });

  it("does not cache an error or copy the key into the response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        return new Response(`denied ${KEY}`, {
          status: 500,
          headers: {
            "content-type": "text/plain",
            "set-cookie": KEY,
            "cache-control": "public, max-age=86400",
          },
        });
      }),
    );
    const response = await call("/api/basemap/upstream/tiles/v3/1/2/3.pbf");
    const body = await response.text();
    expect(response.status).toBe(500);
    expect(body).toBe("basemap unavailable");
    expect(body).not.toContain(KEY);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(headers(response)).not.toContain(KEY);
    expect(headers(response).toLowerCase()).not.toContain("set-cookie");
    expect(headers(response)).not.toContain("max-age");
  });

  it("passes binary tile and sprite bytes through without UTF-8 corruption", async () => {
    const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0xff, 0xfe, 0xfd]);
    const pbf = new Uint8Array([0x1a, 0x80, 0xff]);
    const webp = new Uint8Array([0x52, 0x49, 0x46, 0x46, 0x00, 0x00, 0x00, 0x00, 0x57, 0x45, 0x42, 0x50]);
    const cases: Array<{ path: string; body: Uint8Array; contentType: string }> = [
      {
        path: "/api/basemap/upstream/maps/outdoor-v2/sprite.png",
        body: png,
        contentType: "image/png",
      },
      {
        path: "/api/basemap/upstream/sprites/general/sprite@2x.png",
        body: png,
        contentType: "image/png",
      },
      {
        path: "/api/basemap/upstream/tiles/v3/0/0/0.pbf",
        body: pbf,
        contentType: "application/vnd.mapbox-vector-tile",
      },
      {
        path: "/api/basemap/upstream/tiles/terrain-rgb-v2/3/1/2",
        body: webp,
        contentType: "image/webp",
      },
    ];
    for (const { path, body, contentType } of cases) {
      vi.stubGlobal(
        "fetch",
        vi.fn(async () => {
          return new Response(body, {
            status: 200,
            headers: { "content-type": contentType },
          });
        }),
      );
      const response = await worker.fetch(new Request(`https://fishy.example${path}`), ENV);
      const out = new Uint8Array(await response.arrayBuffer());
      expect(response.status).toBe(200);
      expect(out).toEqual(body);
      expect(response.headers.get("content-type")).toBe(contentType);
      expect(response.headers.get("cache-control")).toBe("public, max-age=86400");
      vi.unstubAllGlobals();
    }
  });

  it("refuses binary bodies that embed the upstream key", async () => {
    const keyBytes = new TextEncoder().encode(KEY);
    const body = new Uint8Array([0x1a, 0x00, ...keyBytes, 0xff]);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        return new Response(body, {
          status: 200,
          headers: { "content-type": "application/octet-stream" },
        });
      }),
    );
    const response = await call("/api/basemap/upstream/tiles/v3/0/0/0.pbf");
    expect(response.status).toBe(502);
    expect(await response.text()).toBe("basemap unavailable");
    expect(response.headers.get("cache-control")).toBe("no-store");
  });
});
