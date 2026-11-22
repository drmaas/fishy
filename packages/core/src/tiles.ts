/** Camera that frames the committed Sample Pond fixture. */
export const SAMPLE_VIEW = {
  longitude: -93.27,
  latitude: 44.98,
  zoom: 13.4,
} as const;

export const OPENFREEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";
export const OPENFREEMAP_DARK_STYLE = "https://tiles.openfreemap.org/styles/dark";

/**
 * MapLibre PMTiles protocol URL.
 * Absolute site paths stay root-relative (`pmtiles:///samples/...`).
 * Remote archives keep their scheme (`pmtiles://https://...`).
 */
export function pmtilesProtocolUrl(source: string): string {
  if (source.startsWith("pmtiles://")) return source;
  if (source.startsWith("https://") || source.startsWith("http://")) {
    return `pmtiles://${source}`;
  }
  if (source.startsWith("/")) return `pmtiles://${source}`;
  return `pmtiles://${source}`;
}

export interface FishSources {
  contoursUrl: string;
  terrainUrl: string;
  hillshadeUrl: string;
}

export function emptyToUndefined(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : undefined;
}
