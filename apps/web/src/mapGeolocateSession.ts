import type { GeolocateControl, Map as MaplibreMap } from "maplibre-gl";
import { GEOLOCATION_ERROR } from "./locationBanner.ts";
import {
  prepareGeolocateButtonForRetry,
  readGeolocateWatchState,
  runGeolocateFreshStart,
  shouldStartGeolocationOnPermissionGrant,
} from "./geolocateRetry.ts";

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

export const GEOLOCATE_SETUP_TIMEOUT_MS = 3000;
export const GEOLOCATE_SETUP_POLL_MS = 10;

export type GeolocateControlFactory = (maplibregl: typeof import("maplibre-gl")) => GeolocateControl;

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
  createControl: GeolocateControlFactory = createFishyGeolocateControl,
): GeolocateControl {
  if (current) {
    map.removeControl(current);
  }
  const next = createControl(maplibregl);
  map.addControl(next, position);
  return next;
}

export function shouldRecreateGeolocateAfterError(code: number): boolean {
  return code === GEOLOCATION_ERROR.PERMISSION_DENIED;
}

export function geolocateButtonFromMap(map: MaplibreMap | null | undefined): HTMLButtonElement | null {
  return map?.getContainer().querySelector<HTMLButtonElement>(".maplibregl-ctrl-geolocate") ?? null;
}

export function isGeolocateControlReady(control: GeolocateControl): boolean {
  return (control as unknown as { _setup?: boolean })._setup === true;
}

export async function waitForGeolocateControlReady(
  control: GeolocateControl,
  timeoutMs: number = GEOLOCATE_SETUP_TIMEOUT_MS,
  pollMs: number = GEOLOCATE_SETUP_POLL_MS,
): Promise<boolean> {
  if (isGeolocateControlReady(control)) return true;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (isGeolocateControlReady(control)) return true;
    await new Promise<void>((resolve) => {
      setTimeout(resolve, pollMs);
    });
  }
  return isGeolocateControlReady(control);
}

export type GeolocateSession = {
  control: GeolocateControl;
  needsRecreate: boolean;
};

export function ensureGeolocateControl(
  map: MaplibreMap,
  maplibregl: typeof import("maplibre-gl"),
  session: GeolocateSession,
  onReplaced: (control: GeolocateControl) => void,
  onBeforeReplace?: () => void,
  createControl: GeolocateControlFactory = createFishyGeolocateControl,
): GeolocateControl {
  if (!session.needsRecreate) return session.control;
  onBeforeReplace?.();
  session.control = replaceGeolocateControl(map, maplibregl, session.control, "bottom-right", createControl);
  session.needsRecreate = false;
  onReplaced(session.control);
  return session.control;
}

export async function startFreshGeolocate(
  map: MaplibreMap,
  maplibregl: typeof import("maplibre-gl"),
  session: GeolocateSession,
  onReplaced: (control: GeolocateControl) => void,
  onError: (code: number) => void,
  onBeforeReplace?: () => void,
  createControl: GeolocateControlFactory = createFishyGeolocateControl,
): Promise<boolean> {
  const control = ensureGeolocateControl(map, maplibregl, session, onReplaced, onBeforeReplace, createControl);
  if (!(await waitForGeolocateControlReady(control))) {
    onError(GEOLOCATION_ERROR.TIMEOUT);
    return false;
  }
  const button = geolocateButtonFromMap(map);
  const { triggersSucceeded } = runGeolocateFreshStart(() => control.trigger(), button);
  if (!triggersSucceeded) {
    onError(GEOLOCATION_ERROR.TIMEOUT);
    return false;
  }
  return true;
}

export async function startGeolocateAfterPermissionGrant(
  map: MaplibreMap,
  maplibregl: typeof import("maplibre-gl"),
  session: GeolocateSession,
  onReplaced: (control: GeolocateControl) => void,
  onError: (code: number) => void,
  onBeforeReplace?: () => void,
  createControl: GeolocateControlFactory = createFishyGeolocateControl,
): Promise<boolean> {
  const control = ensureGeolocateControl(map, maplibregl, session, onReplaced, onBeforeReplace, createControl);
  if (!(await waitForGeolocateControlReady(control))) {
    onError(GEOLOCATION_ERROR.TIMEOUT);
    return false;
  }
  const button = geolocateButtonFromMap(map);
  const watchState = readGeolocateWatchState(button);
  if (!shouldStartGeolocationOnPermissionGrant(watchState)) {
    return false;
  }
  if (button?.disabled) {
    prepareGeolocateButtonForRetry(button);
  }
  if (!control.trigger()) {
    onError(GEOLOCATION_ERROR.TIMEOUT);
    return false;
  }
  return true;
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