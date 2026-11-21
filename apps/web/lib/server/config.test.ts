import { describe, expect, it } from "vitest";
import { publicConfig, rewriteMaptilerStyle, upstreamMaptilerUrl } from "./config.ts";

describe("worker config", () => {
  it("stays keyless when the MapTiler secret is empty", () => {
    expect(publicConfig({ MAPTILER_API_KEY: "" }).maptiler).toBe(false);
    expect(publicConfig({}).basemap).toBe("openfreemap");
    expect(publicConfig({}).contoursUrl).toBe("/samples/sample-pond.pmtiles");
  });

  it("rewrites MapTiler hosts and keeps the key off the style", () => {
    const style = rewriteMaptilerStyle(
      { sources: { a: { url: "https://api.maptiler.com/tiles/v3/tiles.json?key=secret" } } },
      "https://fishy.example",
    );
    const text = JSON.stringify(style);
    expect(text).toContain("https://fishy.example/api/basemap/upstream/tiles/v3/tiles.json");
    expect(text).not.toContain("secret");
    expect(text).not.toContain("api.maptiler.com");
  });

  it("adds the key only on the upstream URL", () => {
    const url = upstreamMaptilerUrl("/api/basemap/upstream/tiles/v3/0/0/0.png", "", "secret-key");
    expect(url).toBe("https://api.maptiler.com/tiles/v3/0/0/0.png?key=secret-key");
  });
});
