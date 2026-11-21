import { afterEach, describe, expect, it, vi } from "vitest";
import worker from "./index";

const KEY = "secret-key";
const ENV = { MAPTILER_API_KEY: KEY };

function call(path: string): Promise<Response> {
  return worker.fetch(new Request(`https://fishy.example${path}`), ENV);
}

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
});
