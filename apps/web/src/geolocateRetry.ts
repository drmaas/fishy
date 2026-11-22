/**
 * MapLibre GeolocateControl (trackUserLocation) watch states.
 * Derived from maplibre-gl 6.19 geolocate_control.ts trigger() behavior.
 */
export type GeolocateWatchState =
  | "OFF"
  | "WAITING_ACTIVE"
  | "ACTIVE_LOCK"
  | "ACTIVE_ERROR"
  | "BACKGROUND"
  | "BACKGROUND_ERROR";

export type GeolocateFreshStartPlan = {
  triggerCount: number;
  enableButton: boolean;
};

const TOGGLE_OFF_STATES: ReadonlySet<GeolocateWatchState> = new Set([
  "WAITING_ACTIVE",
  "ACTIVE_LOCK",
  "ACTIVE_ERROR",
  "BACKGROUND_ERROR",
]);

export function readGeolocateWatchState(button: HTMLButtonElement | null): GeolocateWatchState {
  if (!button) return "OFF";
  if (button.classList.contains("maplibregl-ctrl-geolocate-active-error")) return "ACTIVE_ERROR";
  if (button.classList.contains("maplibregl-ctrl-geolocate-background-error")) return "BACKGROUND_ERROR";
  if (button.classList.contains("maplibregl-ctrl-geolocate-waiting")) return "WAITING_ACTIVE";
  if (button.classList.contains("maplibregl-ctrl-geolocate-background")) return "BACKGROUND";
  if (button.classList.contains("maplibregl-ctrl-geolocate-active")) return "ACTIVE_LOCK";
  return "OFF";
}

export function shouldStartGeolocationOnPermissionGrant(watchState: GeolocateWatchState): boolean {
  return watchState === "OFF";
}

export function shouldInterceptGeolocateButtonClick(
  watchState: GeolocateWatchState,
  buttonDisabled: boolean,
): boolean {
  if (buttonDisabled) return true;
  return watchState === "ACTIVE_ERROR" || watchState === "BACKGROUND_ERROR";
}

export const GEOLOCATE_FIND_LABEL = "Find my location";

export function planGeolocateFreshStart(
  watchState: GeolocateWatchState,
  buttonDisabled: boolean,
): GeolocateFreshStartPlan {
  if (watchState === "OFF") {
    return { triggerCount: 1, enableButton: buttonDisabled };
  }
  if (watchState === "BACKGROUND") {
    return { triggerCount: 3, enableButton: buttonDisabled };
  }
  if (TOGGLE_OFF_STATES.has(watchState)) {
    return { triggerCount: 2, enableButton: buttonDisabled };
  }
  return { triggerCount: 2, enableButton: buttonDisabled };
}

export function prepareGeolocateButtonForRetry(
  button: HTMLButtonElement | null,
  labels: { title: string; ariaLabel: string } = {
    title: GEOLOCATE_FIND_LABEL,
    ariaLabel: GEOLOCATE_FIND_LABEL,
  },
): void {
  if (!button) return;
  button.disabled = false;
  button.removeAttribute("aria-disabled");
  button.classList.remove("maplibregl-ctrl-geolocate-active-error");
  button.classList.remove("maplibregl-ctrl-geolocate-background-error");
  button.title = labels.title;
  button.setAttribute("aria-label", labels.ariaLabel);
}

/** Models a single MapLibre trigger() when trackUserLocation is true. */
export function nextWatchStateAfterTrigger(watchState: GeolocateWatchState): GeolocateWatchState {
  switch (watchState) {
    case "OFF":
      return "WAITING_ACTIVE";
    case "WAITING_ACTIVE":
    case "ACTIVE_LOCK":
    case "ACTIVE_ERROR":
    case "BACKGROUND_ERROR":
      return "OFF";
    case "BACKGROUND":
      return "ACTIVE_LOCK";
    default:
      return watchState;
  }
}

export function watchStateAfterFreshStartPlan(
  initial: GeolocateWatchState,
  plan: GeolocateFreshStartPlan,
): GeolocateWatchState {
  let state = initial;
  for (let i = 0; i < plan.triggerCount; i += 1) {
    state = nextWatchStateAfterTrigger(state);
  }
  return state;
}

export type GeolocateFreshStartResult = {
  plan: GeolocateFreshStartPlan;
  triggersSucceeded: boolean;
};

export function runGeolocateFreshStart(
  trigger: () => boolean,
  button: HTMLButtonElement | null,
): GeolocateFreshStartResult {
  const initialState = readGeolocateWatchState(button);
  const plan = planGeolocateFreshStart(initialState, button?.disabled ?? false);
  if (plan.enableButton) {
    prepareGeolocateButtonForRetry(button);
  }
  let triggersSucceeded = true;
  for (let i = 0; i < plan.triggerCount; i += 1) {
    if (!trigger()) {
      triggersSucceeded = false;
      break;
    }
  }
  return { plan, triggersSucceeded };
}