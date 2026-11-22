// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as maplibregl from "maplibre-gl";
import { FISHY_GEOLOCATE_OPTIONS } from "./mapGeolocateSession.ts";

const INTEGRATION_GEOLOCATE_OPTIONS = {
  ...FISHY_GEOLOCATE_OPTIONS,
  showUserLocation: false,
  showAccuracyCircle: false,
};

type WatchCall = {
  options: PositionOptions;
  success: PositionCallback;
  error: PositionErrorCallback;
};

const watchCalls: WatchCall[] = [];
let watchSerial = 0;

function sleep(ms: number) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function createStubMap(): maplibregl.Map {
  const controls: maplibregl.IControl[] = [];
  const canvasHost = document.createElement("div");
  const stub = {
    _getUIString: (key: string) => (key.includes("Find") ? "Find my location" : "Unavailable"),
    getCanvasContainer: () => canvasHost,
    on: () => stub,
    off: () => stub,
    getMaxBounds: () => null,
    project: () => ({ x: 0, y: 0 }),
    unproject: () => ({ distanceTo: () => 100 }),
    getBearing: () => 0,
    getZoom: () => 8,
    easeTo: () => undefined,
    fitBounds: () => undefined,
    isZooming: () => false,
    addControl(control: maplibregl.IControl) {
      control.onAdd(stub as unknown as maplibregl.Map);
      controls.push(control);
    },
    removeControl(control: maplibregl.IControl) {
      control.onRemove(stub as unknown as maplibregl.Map);
      const index = controls.indexOf(control);
      if (index >= 0) controls.splice(index, 1);
    },
  };
  return stub as unknown as maplibregl.Map;
}

describe("GeolocateControl deny then recreate", () => {
  let map: maplibregl.Map;

  beforeEach(() => {
    watchCalls.length = 0;
    watchSerial = 0;
    map = createStubMap();

    vi.stubGlobal("navigator", {
      permissions: {
        query: async () => ({
          state: "granted",
          addEventListener: () => undefined,
          removeEventListener: () => undefined,
        }),
      },
      geolocation: {
        watchPosition: (success: PositionCallback, error: PositionErrorCallback, options?: PositionOptions) => {
          watchCalls.push({ options: options ?? {}, success, error });
          const id = ++watchSerial;
          const index = watchCalls.length;
          queueMicrotask(() => {
            const call = watchCalls[index - 1];
            if (!call) return;
            if (index === 1) {
              call.error({
                code: 1,
                message: "denied",
                PERMISSION_DENIED: 1,
                TIMEOUT: 3,
                POSITION_UNAVAILABLE: 2,
              });
              return;
            }
            call.success({
              coords: {
                latitude: 46.8,
                longitude: -92.1,
                accuracy: 9,
                altitude: null,
                altitudeAccuracy: null,
                heading: null,
                speed: null,
                toJSON() {
                  return this;
                },
              },
              timestamp: Date.now(),
              toJSON() {
                return this;
              },
            });
          });
          return id;
        },
        clearWatch: vi.fn(),
      },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it(
    "uses fresh high-accuracy watch options after permission denial and control recreate",
    async () => {
    const first = new maplibregl.GeolocateControl(INTEGRATION_GEOLOCATE_OPTIONS);
    map.addControl(first);
    await sleep(50);
    expect((first as unknown as { _setup?: boolean })._setup).toBe(true);

    const denied = new Promise<void>((resolve) => {
      first.once("error", () => resolve());
    });
    expect(first.trigger()).toBe(true);
    await denied;

    expect(watchCalls[0]?.options).toMatchObject({
      enableHighAccuracy: true,
      maximumAge: 0,
      timeout: 10000,
    });

    map.removeControl(first);
    const recreated = new maplibregl.GeolocateControl(INTEGRATION_GEOLOCATE_OPTIONS);
    map.addControl(recreated);
    await sleep(50);

    const success = new Promise<void>((resolve) => {
      recreated.once("geolocate", () => resolve());
    });
    expect(recreated.trigger()).toBe(true);
    await success;

    expect(watchCalls).toHaveLength(2);
    expect(watchCalls[1]?.options).toMatchObject({
      enableHighAccuracy: true,
      maximumAge: 0,
      timeout: 10000,
    });
    expect(watchCalls[1]?.options.maximumAge).not.toBe(600000);
  },
    10_000,
  );
});