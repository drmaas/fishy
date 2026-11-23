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
        stateId="mn"
        onHide={() => {}}
      />,
    );
    expect(html).toContain("Sample Pond");
    expect(html).toContain("15 ft");
    expect(html).toContain("42.5 acres");
    expect(html).toContain("99999900");
    expect(html).toContain("https://www.dnr.state.mn.us/lakefind/showreport.html?downum=99999900");
    expect(html).toContain("Synthetic sample");
    expect(html).toContain("Hide");
    expect(html).not.toContain("Close");
    expect(html).toContain('aria-controls="lake-panel"');
  });

  it("renders Wisconsin tracing note, PDF link, and QA status", () => {
    const html = renderToStaticMarkup(
      <LakePanel
        lake={{
          name: "Lake Wissota",
          dowlknum: "0417300",
          maxDepthFt: 72,
          areaAcres: 6044,
          synthetic: false,
        }}
        unit="ft"
        stateId="wi"
        wiFields={{
          source_pdf_url: "https://example.dnr.wi.gov/bathymetry/0417300.pdf",
          qa_status: "verified",
        }}
        onHide={() => {}}
      />,
    );
    expect(html).toContain("Traced from WI DNR map");
    expect(html).toContain("https://example.dnr.wi.gov/bathymetry/0417300.pdf");
    expect(html).toContain("Source bathymetry map (PDF)");
    expect(html).toContain("Verified");
    expect(html).not.toContain("LakeFinder");
  });
});

describe("app shell", () => {
  it("renders the map frame, state control, hide control, and a credits button", () => {
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain("Fishy");
    expect(html).toContain("Minnesota");
    expect(html).toContain("Wisconsin");
    expect(html).toContain("Bathymetry © Minnesota DNR");
    expect(html).toContain("Find a lake");
    expect(html).toContain('class="map"');
    expect(html).toContain('class="credits"');
    expect(html).toContain('aria-controls="credits-panel"');
    expect(html).toContain(">Credits<");
    expect(html).not.toContain('id="credits-panel"');
    expect(html).toContain('aria-controls="fishy-chrome"');
    expect(html).toContain("Hide");
    expect(html).toContain("chrome-theme-toggle");
    expect(html).toContain('aria-label="Color scheme:');
  });
});
