import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ThemeIconButton, ThemeSwitch } from "./ThemeSwitch.tsx";

describe("ThemeSwitch", () => {
  it("renders the desktop pill and corner icon wrapper", () => {
    const html = renderToStaticMarkup(<ThemeSwitch value="auto" onChange={() => {}} />);
    expect(html).toContain('class="theme-controls"');
    expect(html).toContain('class="theme-switch"');
    expect(html).toContain('class="theme-switch-icon tap-target"');
    expect(html).toContain('aria-label="Color scheme: Auto. Activate to switch."');
  });

  it("renders an inline chrome header icon with touch sizing", () => {
    const html = renderToStaticMarkup(
      <ThemeIconButton className="chrome-theme-toggle" value="dark" onChange={() => {}} />,
    );
    expect(html).toContain("chrome-theme-toggle");
    expect(html).toContain('class="theme-switch-icon tap-target chrome-theme-toggle"');
    expect(html).toContain('aria-label="Color scheme: Dark. Activate to switch."');
  });
});