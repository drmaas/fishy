/**
 * @vitest-environment happy-dom
 */
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "./App.tsx";

describe("app shell attribution by state", () => {
  const initialSearch = window.location.search;

  afterEach(() => {
    window.history.replaceState(null, "", `${window.location.pathname}${initialSearch}`);
  });

  it("shows Wisconsin bathymetry credit and navigation disclaimer", () => {
    window.history.replaceState(null, "", "?state=wi");
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain("Bathymetry © Wisconsin DNR");
    expect(html).toContain("Not for navigation.");
    expect(html).not.toContain("Bathymetry © Minnesota DNR");
  });

  it("shows Minnesota bathymetry credit without the Wisconsin disclaimer", () => {
    window.history.replaceState(null, "", "?state=mn");
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain("Bathymetry © Minnesota DNR");
    expect(html).not.toContain("Not for navigation.");
    expect(html).not.toContain("Bathymetry © Wisconsin DNR");
  });
});