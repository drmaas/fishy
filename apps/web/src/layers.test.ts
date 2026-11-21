import { describe, expect, it } from "vitest";
import { fishSources } from "./layers.ts";

describe("fish sources", () => {
  it("points contours and terrain at the PMTiles protocol", () => {
    const sources = fishSources(
      "/samples/sample-pond.pmtiles",
      "/samples/sample-pond-terrain.pmtiles",
      "",
    );
    expect(sources.contours.url).toBe("pmtiles:///samples/sample-pond.pmtiles");
    expect(sources.terrain?.encoding).toBe("terrarium");
    expect(sources.hillshade).toBeNull();
  });
});
