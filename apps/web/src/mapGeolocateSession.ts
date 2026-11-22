import type { GeolocateControl, Map as MaplibreMap } from "maplibre-gl";
import { GEOLOCATION_ERROR } from "./locationBanner.ts";
import type { LocationHintState } from "./locationHint.ts";
import { readGeolocateWatchState } from "./geolocateRetry.ts";

export const FISHY_GEOLOCATE_OPTIONS = {
  positionOptions: { enableHighAccuracy: true, maximumAge: 0, timeout: 10000 },
  trackUserLocation: true,
  showUserLocation: true,
  showAccuracyCircle: true,
  zoomToUserAccuracy: true,
  fitBoundsOptions: { maxZoom: 16, padding: 80 },
} as const;

/** MapLibre positionOptions.timeout plus a small buffer for hung WAITING_ACTIVE. */
export const GEOLOCATE_HANG_TIMEOUT_MS = FISHY_GEOLOCATE_OPTIONS.positionOptions.timeout + 500;

export function createFishyGeolocateControl(
  maplibregl: typeof import("maplibre-gl"),
): GeolocateControl {
  return new maplibregl.GeolocateControl(FISHY_GEOLOCATE_OPTIONS);
}

export function replaceGeolocateControl(
  map: MaplibreMap,
  maplibregl: typeof import("maplibre-gl"),
  current: GeolocateControl | null,
  position: "bottom-right" = "bottom-right",
): GeolocateControl {
  if (current) {
    map.removeControl(current);
  }
  const next = createFishyGeolocateControl(maplibregl);
  map.addControl(next, position);
  return next;
}

export function shouldRecreateGeolocateAfterError(code: number): boolean {
  return code === GEOLOCATION_ERROR.PERMISSION_DENIED;
}

export function armGeolocateHangTimer(
  button: HTMLButtonElement | null,
  onHung: () => void,
  timeoutMs: number = GEOLOCATE_HANG_TIMEOUT_MS,
): () => void {
  const timer = globalThis.setTimeout(() => {
    if (readGeolocateWatchState(button) === "WAITING_ACTIVE") {
      onHung();
    }
  }, timeoutMs);
  return () => globalThis.clearTimeout(timer);
}

export type GeolocateLocationSink = (state: LocationHintState) => void;