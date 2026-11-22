import { OPENFREEMAP_DARK_STYLE, OPENFREEMAP_STYLE } from "@fishy/core";
import { describe, expect, it } from "vitest";
import {
  contoursOnlyBackground,
  cycleThemePreference,
  mapStyleKey,
  maptilerStyleUrl,
  openFreeMapStyleUrl,
  readThemePreference,
  resolveColorScheme,
} from "./theme.ts";

describe("theme", () => {
  it("defaults bad storage to auto", () => {
    expect(readThemePreference(null)).toBe("auto");
    expect(readThemePreference("")).toBe("auto");
    expect(readThemePreference("sepia")).toBe("auto");
  });

  it("keeps a stored preference", () => {
    expect(readThemePreference("light")).toBe("light");
    expect(readThemePreference("dark")).toBe("dark");
    expect(readThemePreference("auto")).toBe("auto");
  });

  it("resolves auto from the OS preference", () => {
    expect(resolveColorScheme("light", true)).toBe("light");
    expect(resolveColorScheme("dark", false)).toBe("dark");
    expect(resolveColorScheme("auto", true)).toBe("dark");
    expect(resolveColorScheme("auto", false)).toBe("light");
  });

  it("picks OpenFreeMap and contours-only styles by scheme", () => {
    expect(openFreeMapStyleUrl("light")).toBe(OPENFREEMAP_STYLE);
    expect(openFreeMapStyleUrl("dark")).toBe(OPENFREEMAP_DARK_STYLE);
    expect(contoursOnlyBackground("light")).toBe("#d5e3ec");
    expect(contoursOnlyBackground("dark")).toBe("#1b2832");
    expect(mapStyleKey("openfreemap", "dark")).toBe(OPENFREEMAP_DARK_STYLE);
    expect(mapStyleKey("none", "light")).toBe("contours:light");
    expect(maptilerStyleUrl("light")).toBe("/api/basemap/style.json");
    expect(maptilerStyleUrl("dark")).toBe("/api/basemap/style-dark.json");
    expect(mapStyleKey("maptiler", "dark", maptilerStyleUrl("dark"))).toBe("/api/basemap/style-dark.json");
    expect(mapStyleKey("maptiler", "light", maptilerStyleUrl("light"))).toBe("/api/basemap/style.json");
  });

  it("cycles theme preferences in order", () => {
    expect(cycleThemePreference("light")).toBe("dark");
    expect(cycleThemePreference("dark")).toBe("auto");
    expect(cycleThemePreference("auto")).toBe("light");
  });
});
