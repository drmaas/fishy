import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { LakePanel } from "./LakePanel.tsx";
import { App } from "./App.tsx";

describe("lake panel", () => {
  it("renders depth, area, and a LakeFinder link", () => {
    const html = renderToStaticMarkup(
      <LakePanel
        lake={{
          name: "Sample Pond",
          dowlknum: "99999900",
          maxDepthFt: 15,
          areaAcres: 42.5,
          synthetic: true,
        }}
        unit="ft"
        onClose={() => {}}
      />,
    );
    expect(html).toContain("Sample Pond");
    expect(html).toContain("15 ft");
    expect(html).toContain("42.5 acres");
    expect(html).toContain("99999900");
    expect(html).toContain("https://www.dnr.state.mn.us/lakefind/showreport.html?downum=99999900");
    expect(html).toContain("Synthetic sample");
  });
});

describe("app shell", () => {
  it("renders the map frame and required credits", () => {
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain("Fishy");
    expect(html).toContain("OpenStreetMap contributors");
    expect(html).toContain("Minnesota DNR");
    expect(html).toContain('class="map"');
  });
});
