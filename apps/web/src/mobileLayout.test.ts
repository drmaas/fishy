import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const stylesPath = resolve(dirname(fileURLToPath(import.meta.url)), "styles.css");
const styles = readFileSync(stylesPath, "utf8");

describe("mobile layout styles", () => {
  it("tucks the theme toggle into the chrome header on phone widths", () => {
    expect(styles).toContain(".chrome-theme-toggle");
    expect(styles).toContain(".app.chrome-open .theme-controls");
    expect(styles).toMatch(/@media \(max-width: 720px\), \(max-height: 520px\)/);
  });

  it("offsets the lake panel below the corner theme icon on short viewports", () => {
    expect(styles).toMatch(/@media \(max-height: 520px\)/);
    expect(styles).toContain("44px + 8px");
  });
});