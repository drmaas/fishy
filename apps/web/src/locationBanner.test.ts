import { describe, expect, it } from "vitest";
import {
  planGeolocateFreshStart,
  shouldStartGeolocationOnPermissionGrant,
  watchStateAfterFreshStartPlan,
} from "./geolocateRetry.ts";
import {
  GEOLOCATION_ERROR,
  locationBannerShowDismiss,
  locationBannerShowRetry,
  locationBannerText,
  locationBannerVisible,
  permissionChangeShouldRetry,
  reduceLocationBanner,
  shouldClearLocationOnTrackingEnd,
  type LocationBannerModel,
} from "./locationBanner.ts";

const denied: LocationBannerModel = {
  location: { status: "error", code: GEOLOCATION_ERROR.PERMISSION_DENIED },
  dismissed: false,
};

describe("reduceLocationBanner", () => {
  it("dismisses only error messages", () => {
    const dismissed = reduceLocationBanner(denied, { type: "dismiss" });
    expect(dismissed.dismissed).toBe(true);
    expect(locationBannerVisible(dismissed)).toBe(false);

    const active = reduceLocationBanner(
      { location: { status: "active", accuracyMeters: 10 }, dismissed: false },
      { type: "dismiss" },
    );
    expect(active.dismissed).toBe(false);
  });

  it("clears dismiss on a new attempt", () => {
    const model = reduceLocationBanner(
      { ...denied, dismissed: true },
      { type: "attempt" },
    );
    expect(model.dismissed).toBe(false);
    expect(locationBannerVisible(model)).toBe(true);
  });

  it("clears error on permission granted", () => {
    const model = reduceLocationBanner(denied, { type: "permission_granted" });
    expect(model.location).toEqual({ status: "idle" });
    expect(model.dismissed).toBe(false);
    expect(locationBannerVisible(model)).toBe(false);
  });

  it("updates location from the map", () => {
    const model = reduceLocationBanner(denied, {
      type: "location",
      state: { status: "active", accuracyMeters: 8 },
    });
    expect(locationBannerText(model)).toBe("You are here, ±8 m.");
    expect(locationBannerShowRetry(model)).toBe(false);
  });
});

describe("locationBannerVisible", () => {
  it("shows active fixes", () => {
    expect(
      locationBannerVisible({ location: { status: "active", accuracyMeters: null }, dismissed: false }),
    ).toBe(true);
  });

  it("hides dismissed errors", () => {
    expect(locationBannerVisible({ ...denied, dismissed: true })).toBe(false);
  });
});

describe("locationBanner controls", () => {
  it("offers retry and dismiss for errors", () => {
    expect(locationBannerShowRetry(denied)).toBe(true);
    expect(locationBannerShowDismiss(denied)).toBe(true);
  });

  it("maps geolocation error codes to copy", () => {
    expect(
      locationBannerText({
        location: { status: "error", code: GEOLOCATION_ERROR.PERMISSION_DENIED },
        dismissed: false,
      }),
    ).toContain("browser settings");
    expect(
      locationBannerText({
        location: { status: "error", code: GEOLOCATION_ERROR.POSITION_UNAVAILABLE },
        dismissed: false,
      }),
    ).toBe("No GPS fix. Move outdoors and try again.");
    expect(
      locationBannerText({
        location: { status: "error", code: GEOLOCATION_ERROR.TIMEOUT },
        dismissed: false,
      }),
    ).toBe("GPS timed out. Try again.");
  });
});

describe("permissionChangeShouldRetry", () => {
  it("retries when permission becomes granted", () => {
    expect(permissionChangeShouldRetry("granted")).toBe(true);
    expect(permissionChangeShouldRetry("denied")).toBe(false);
    expect(permissionChangeShouldRetry("prompt")).toBe(false);
  });
});

describe("MapLibre geolocate retry planner", () => {
  it.each([
    ["OFF", 1],
    ["WAITING_ACTIVE", 2],
    ["ACTIVE_ERROR", 2],
    ["BACKGROUND_ERROR", 2],
    ["BACKGROUND", 3],
  ] as const)("fresh start from %s uses %i triggers", (watchState, count) => {
    const plan = planGeolocateFreshStart(watchState, false);
    expect(plan.triggerCount).toBe(count);
    expect(watchStateAfterFreshStartPlan(watchState, plan)).toBe("WAITING_ACTIVE");
  });

  it("skips permission-grant trigger unless watch is OFF", () => {
    expect(shouldStartGeolocationOnPermissionGrant("OFF")).toBe(true);
    expect(shouldStartGeolocationOnPermissionGrant("WAITING_ACTIVE")).toBe(false);
  });
});

describe("shouldClearLocationOnTrackingEnd", () => {
  it("keeps errors when tracking stops after a failed fix", () => {
    const error = { status: "error" as const, code: GEOLOCATION_ERROR.PERMISSION_DENIED };
    expect(shouldClearLocationOnTrackingEnd(error, true)).toBe(false);
    expect(shouldClearLocationOnTrackingEnd(error, false)).toBe(false);
  });

  it("clears active and idle states when tracking ends cleanly", () => {
    expect(shouldClearLocationOnTrackingEnd({ status: "active", accuracyMeters: 5 }, false)).toBe(true);
    expect(shouldClearLocationOnTrackingEnd({ status: "idle" }, false)).toBe(true);
  });
});