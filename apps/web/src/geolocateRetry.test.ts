import { describe, expect, it } from "vitest";
import {
  nextWatchStateAfterTrigger,
  planGeolocateFreshStart,
  readGeolocateWatchState,
  runGeolocateFreshStart,
  shouldInterceptGeolocateButtonClick,
  shouldStartGeolocationOnPermissionGrant,
  watchStateAfterFreshStartPlan,
  type GeolocateWatchState,
} from "./geolocateRetry.ts";

function mockGeolocateButton(classes: string[], disabled = false): HTMLButtonElement {
  const classSet = new Set(classes);
  const attrs = new Map<string, string>();
  return {
    disabled,
    title: "",
    classList: {
      contains: (name: string) => classSet.has(name),
      add: (name: string) => {
        classSet.add(name);
      },
      remove: (name: string) => {
        classSet.delete(name);
      },
    },
    setAttribute: (name: string, value: string) => {
      attrs.set(name, value);
    },
    getAttribute: (name: string) => attrs.get(name) ?? null,
    removeAttribute: () => undefined,
  } as unknown as HTMLButtonElement;
}

describe("readGeolocateWatchState", () => {
  it.each([
    [[], "OFF"],
    [["maplibregl-ctrl-geolocate-waiting"], "WAITING_ACTIVE"],
    [["maplibregl-ctrl-geolocate-active"], "ACTIVE_LOCK"],
    [["maplibregl-ctrl-geolocate-active-error"], "ACTIVE_ERROR"],
    [["maplibregl-ctrl-geolocate-background"], "BACKGROUND"],
    [["maplibregl-ctrl-geolocate-background-error"], "BACKGROUND_ERROR"],
  ] as const)("maps %# to watch state", (classes, expected) => {
    expect(readGeolocateWatchState(mockGeolocateButton([...classes]))).toBe(expected);
  });
});

describe("planGeolocateFreshStart", () => {
  it.each([
    ["OFF", false, 1],
    ["OFF", true, 1],
    ["WAITING_ACTIVE", false, 2],
    ["ACTIVE_LOCK", false, 2],
    ["ACTIVE_ERROR", false, 2],
    ["BACKGROUND_ERROR", false, 2],
    ["BACKGROUND", false, 3],
  ] as const)("uses %s -> %i triggers", (watchState, disabled, triggerCount) => {
    const plan = planGeolocateFreshStart(watchState, disabled);
    expect(plan.triggerCount).toBe(triggerCount);
    expect(watchStateAfterFreshStartPlan(watchState, plan)).toBe("WAITING_ACTIVE");
  });

  it("does not leave error states on a single toggle-off trigger", () => {
    for (const state of ["ACTIVE_ERROR", "BACKGROUND_ERROR"] as GeolocateWatchState[]) {
      expect(nextWatchStateAfterTrigger(state)).toBe("OFF");
      expect(planGeolocateFreshStart(state, false).triggerCount).toBeGreaterThan(1);
    }
  });
});

describe("shouldStartGeolocationOnPermissionGrant", () => {
  it("starts only from OFF", () => {
    expect(shouldStartGeolocationOnPermissionGrant("OFF")).toBe(true);
    expect(shouldStartGeolocationOnPermissionGrant("WAITING_ACTIVE")).toBe(false);
    expect(shouldStartGeolocationOnPermissionGrant("ACTIVE_ERROR")).toBe(false);
  });
});

describe("shouldInterceptGeolocateButtonClick", () => {
  it("intercepts disabled and error states", () => {
    expect(shouldInterceptGeolocateButtonClick("OFF", true)).toBe(true);
    expect(shouldInterceptGeolocateButtonClick("ACTIVE_ERROR", false)).toBe(true);
    expect(shouldInterceptGeolocateButtonClick("ACTIVE_LOCK", false)).toBe(false);
  });
});

describe("runGeolocateFreshStart", () => {
  it("fires two triggers from ACTIVE_ERROR for a new watch", () => {
    const calls: number[] = [];
    const button = mockGeolocateButton(["maplibregl-ctrl-geolocate-active-error"]);
    const { plan, triggersSucceeded } = runGeolocateFreshStart(() => {
      calls.push(calls.length + 1);
      return true;
    }, button);
    expect(triggersSucceeded).toBe(true);
    expect(plan.triggerCount).toBe(2);
    expect(calls).toEqual([1, 2]);
  });

  it("re-enables a denied control before starting", () => {
    const button = mockGeolocateButton([], true);
    runGeolocateFreshStart(() => true, button);
    expect(button.disabled).toBe(false);
    expect(button.title).toBe("Find my location");
    expect(button.getAttribute("aria-label")).toBe("Find my location");
  });
});

describe("runGeolocateFreshStart trigger failures", () => {
  it("stops when trigger returns false", () => {
    const calls: number[] = [];
    const button = mockGeolocateButton(["maplibregl-ctrl-geolocate-active-error"]);
    const { plan, triggersSucceeded } = runGeolocateFreshStart(() => {
      calls.push(calls.length + 1);
      return calls.length === 1;
    }, button);
    expect(plan.triggerCount).toBe(2);
    expect(calls).toEqual([1, 2]);
    expect(triggersSucceeded).toBe(false);
  });
});