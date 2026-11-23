import { describe, expect, it } from "vitest";
import { safeHttpsUrl } from "./url.ts";

describe("safeHttpsUrl", () => {
  it("accepts https URLs", () => {
    expect(safeHttpsUrl("https://apps.dnr.wi.gov/doclink/lakes_maps/0417300a.pdf")).toBe(
      "https://apps.dnr.wi.gov/doclink/lakes_maps/0417300a.pdf",
    );
  });

  it("rejects other protocols and invalid input", () => {
    expect(safeHttpsUrl("http://example.com/map.pdf")).toBeNull();
    expect(safeHttpsUrl("javascript:alert(1)")).toBeNull();
    expect(safeHttpsUrl("data:text/html,hi")).toBeNull();
    expect(safeHttpsUrl("not a url")).toBeNull();
    expect(safeHttpsUrl("")).toBeNull();
    expect(safeHttpsUrl(null)).toBeNull();
  });
});