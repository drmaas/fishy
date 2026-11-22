import { locationHint, type LocationHintState } from "./locationHint.ts";

export type LocationBannerModel = {
  location: LocationHintState;
  dismissed: boolean;
};

export type LocationBannerAction =
  | { type: "location"; state: LocationHintState }
  | { type: "dismiss" }
  | { type: "attempt" }
  | { type: "permission_granted" };

export const GEOLOCATION_ERROR = {
  PERMISSION_DENIED: 1,
  POSITION_UNAVAILABLE: 2,
  TIMEOUT: 3,
} as const;

export function reduceLocationBanner(
  model: LocationBannerModel,
  action: LocationBannerAction,
): LocationBannerModel {
  switch (action.type) {
    case "location":
      return { ...model, location: action.state };
    case "dismiss":
      if (model.location.status !== "error") return model;
      return { ...model, dismissed: true };
    case "attempt":
      return { ...model, dismissed: false };
    case "permission_granted":
      return { location: { status: "idle" }, dismissed: false };
    default:
      return model;
  }
}

export function locationBannerText(model: LocationBannerModel): string | null {
  return locationHint(model.location);
}

export function locationBannerVisible(model: LocationBannerModel): boolean {
  const text = locationBannerText(model);
  if (!text) return false;
  if (model.location.status === "error" && model.dismissed) return false;
  return true;
}

export function locationBannerShowRetry(model: LocationBannerModel): boolean {
  return model.location.status === "error";
}

export function locationBannerShowDismiss(model: LocationBannerModel): boolean {
  return model.location.status === "error";
}

export function permissionChangeShouldRetry(state: PermissionState): boolean {
  return state === "granted";
}

export function shouldClearLocationOnTrackingEnd(location: LocationHintState, geolocationFailed: boolean): boolean {
  if (geolocationFailed) return false;
  return location.status !== "error";
}