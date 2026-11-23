import { OPENFREEMAP_DARK_STYLE, OPENFREEMAP_STYLE } from "@fishy/core";

export type ThemePreference = "light" | "dark" | "auto";
export type ColorScheme = "light" | "dark";

export const THEME_STORAGE_KEY = "fishy-theme";

export function isThemePreference(value: string | null | undefined): value is ThemePreference {
  return value === "light" || value === "dark" || value === "auto";
}

export function readThemePreference(stored: string | null | undefined): ThemePreference {
  return isThemePreference(stored) ? stored : "auto";
}

export function resolveColorScheme(
  preference: ThemePreference,
  prefersDark: boolean,
): ColorScheme {
  if (preference === "light") return "light";
  if (preference === "dark") return "dark";
  return prefersDark ? "dark" : "light";
}

export function applyDocumentTheme(scheme: ColorScheme) {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.theme = scheme;
  document.documentElement.style.colorScheme = scheme;
}

export function openFreeMapStyleUrl(scheme: ColorScheme): string {
  return scheme === "dark" ? OPENFREEMAP_DARK_STYLE : OPENFREEMAP_STYLE;
}

export function contoursOnlyBackground(scheme: ColorScheme): string {
  return scheme === "dark" ? "#1b2832" : "#d5e3ec";
}

export function maptilerStyleUrl(scheme: ColorScheme): string {
  return scheme === "dark" ? "/api/basemap/style-dark.json" : "/api/basemap/style.json";
}

export function mapStyleKey(
  basemap: "openfreemap" | "none" | "maptiler",
  scheme: ColorScheme,
  maptilerStyleUrl?: string,
): string {
  if (basemap === "maptiler" && maptilerStyleUrl) return maptilerStyleUrl;
  if (basemap === "none") return `contours:${scheme}`;
  return openFreeMapStyleUrl(scheme);
}

const THEME_CYCLE: ThemePreference[] = ["light", "dark", "auto"];

export function cycleThemePreference(current: ThemePreference): ThemePreference {
  const index = THEME_CYCLE.indexOf(current);
  return THEME_CYCLE[(index + 1) % THEME_CYCLE.length] ?? "auto";
}

export function themePreferenceLabel(preference: ThemePreference): string {
  if (preference === "light") return "Light";
  if (preference === "dark") return "Dark";
  return "Auto";
}
