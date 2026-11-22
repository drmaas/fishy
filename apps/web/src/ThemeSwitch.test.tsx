/**
 * @vitest-environment happy-dom
 */
import { createRoot, type Root } from "react-dom/client";
import { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ThemeSwitch } from "./ThemeSwitch.tsx";
import { THEME_STORAGE_KEY } from "./theme.ts";

describe("ThemeSwitch", () => {
  let container: HTMLDivElement;
  let root: Root;

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    window.localStorage.clear();
  });

  function mount(value: "light" | "dark" | "auto", onChange: (value: "light" | "dark" | "auto") => void) {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => {
      root.render(<ThemeSwitch value={value} onChange={onChange} />);
    });
  }

  it("cycles light, dark, and auto from the compact control", () => {
    const onChange = vi.fn();
    mount("light", onChange);
    const button = container.querySelector(".theme-switch-icon") as HTMLButtonElement;
    act(() => button.click());
    expect(onChange).toHaveBeenCalledWith("dark");

    act(() => {
      root.render(<ThemeSwitch value="dark" onChange={onChange} />);
    });
    act(() => button.click());
    expect(onChange).toHaveBeenCalledWith("auto");
  });

  it("exposes the current mode on the icon control", () => {
    mount("auto", () => undefined);
    const button = container.querySelector(".theme-switch-icon") as HTMLButtonElement;
    expect(button.getAttribute("aria-label")).toBe("Color scheme: Auto. Activate to switch.");
    expect(button.getAttribute("title")).toBe("Auto mode — tap to switch");
  });

  it("persists through the storage key when wired by the parent", () => {
    mount("light", (value) => {
      window.localStorage.setItem(THEME_STORAGE_KEY, value);
    });
    const button = container.querySelector(".theme-switch-icon") as HTMLButtonElement;
    act(() => button.click());
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  });
});