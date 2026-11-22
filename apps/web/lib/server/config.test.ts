import { describe, expect, it } from "vitest";
import {
  cacheControlForStatus,
  exposesMaptilerSecret,
  isAllowedMaptilerPath,
  isRedirectStatus,
  publicConfig,
  rewriteMaptilerStyle,
  upstreamMaptilerUrl,
} from "./config.ts";

describe("worker config", () => {
  it("stays keyless when the MapTiler secret is empty", () => {
    expect(publicConfig({ MAPTILER_API_KEY: "" }).maptiler).toBe(false);
    expect(publicConfig({}).basemap).toBe("openfreemap");
    expect(publicConfig({}).contoursUrl).toBe("/samples/sample-pond.pmtiles");
  });

  it("rewrites nested MapTiler URLs and keeps template placeholders", () => {
    const style = rewriteMaptilerStyle(
      {
        glyphs: "https://api.maptiler.com/fonts/{fontstack}/{range}.pbf?key=secret",
        sources: {
          a: {
            url: "https://api.maptiler.com/tiles/v3/tiles.json?key=secret",
            tiles: ["https://api.maptiler.com/tiles/v3/{z}/{x}/{y}.pbf?key=secret"],
          },
        },
      },
      "https://fishy.example",
    );
    const text = JSON.stringify(style);
    expect(text).toContain("https://fishy.example/api/basemap/upstream/fonts/{fontstack}/{range}.pbf");
    expect(text).toContain("https://fishy.example/api/basemap/upstream/tiles/v3/{z}/{x}/{y}.pbf");
    expect(text).toContain("https://fishy.example/api/basemap/upstream/tiles/v3/tiles.json");
    expect(text).not.toContain("secret");
    expect(text).not.toContain("api.maptiler.com");
    expect(text).not.toContain("%7B");
  });

  it("allows only the outdoor style, sprite, TileJSON, tiles, and glyphs", () => {
    expect(isAllowedMaptilerPath("/maps/outdoor-v2/style.json")).toBe(true);
    expect(isAllowedMaptilerPath("/maps/outdoor-v2/sprite@2x.png")).toBe(true);
    expect(isAllowedMaptilerPath("/sprites/general/sprite@2x.json")).toBe(true);
    expect(isAllowedMaptilerPath("/sprites/general/sprite@2x.png")).toBe(true);
    expect(isAllowedMaptilerPath("/tiles/v3/tiles.json")).toBe(true);
    expect(isAllowedMaptilerPath("/tiles/v3/12/34/56.pbf")).toBe(true);
    expect(isAllowedMaptilerPath("/tiles/terrain-rgb-v2/3/1/2")).toBe(true);
    expect(isAllowedMaptilerPath("/fonts/Open%20Sans%20Regular/0-255.pbf")).toBe(true);
    expect(isAllowedMaptilerPath("/geocoding/minneapolis.json")).toBe(false);
    expect(isAllowedMaptilerPath("/maps/streets-v2/style.json")).toBe(false);
    expect(isAllowedMaptilerPath("/tiles/v3/../secret")).toBe(false);
    expect(upstreamMaptilerUrl("/api/basemap/upstream/geocoding/minneapolis.json", "", "secret-key")).toBeNull();
  });

  it("adds the key only on an allowed upstream URL", () => {
    const url = upstreamMaptilerUrl("/api/basemap/upstream/tiles/v3/0/0/0.png", "", "secret-key");
    expect(url).toBe("https://api.maptiler.com/tiles/v3/0/0/0.png?key=secret-key");
    expect(upstreamMaptilerUrl("/api/basemap/upstream/tiles/terrain-rgb-v2/3/1/2", "", "secret-key")).toBe(
      "https://api.maptiler.com/tiles/terrain-rgb-v2/3/1/2?key=secret-key",
    );
    expect(upstreamMaptilerUrl("/api/basemap/upstream/sprites/general/sprite@2x.png", "", "secret-key")).toBe(
      "https://api.maptiler.com/sprites/general/sprite%402x.png?key=secret-key",
    );
  });

  it("does not cache errors and treats redirects as refused", () => {
    expect(cacheControlForStatus(200)).toBe("public, max-age=86400");
    expect(cacheControlForStatus(404)).toBe("no-store");
    expect(cacheControlForStatus(502)).toBe("no-store");
    expect(isRedirectStatus(302)).toBe(true);
    expect(isRedirectStatus(200)).toBe(false);
  });

  it("treats an error body or header that still contains the key as a leak", () => {
    expect(exposesMaptilerSecret("missing secret-key", "secret-key")).toBe(true);
    expect(exposesMaptilerSecret("session=secret-key", "secret-key")).toBe(true);
    expect(exposesMaptilerSecret("https://api.maptiler.com/tiles/v3/tiles.json", "secret-key")).toBe(true);
    const tilejson = rewriteMaptilerStyle(
      { tiles: ["https://api.maptiler.com/tiles/v3/{z}/{x}/{y}.pbf?key=secret-key"] },
      "https://fishy.example",
    );
    const body = JSON.stringify(tilejson);
    expect(exposesMaptilerSecret(body, "secret-key")).toBe(false);
    expect(exposesMaptilerSecret("public, max-age=86400", "secret-key")).toBe(false);
  });
});
