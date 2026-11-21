import { describe, expect, it } from "vitest";
import { contourLabelField, fishSources } from "./layers.ts";

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

  it("labels contours in the selected unit", () => {
    expect(JSON.stringify(contourLabelField("ft"))).toContain(" ft");
    const meters = JSON.stringify(contourLabelField("m"));
    expect(meters).toContain(" m");
    expect(meters).toContain("0.3048");
    expect(meters).not.toContain(" ft");
  });
});
