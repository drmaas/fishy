// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as maplibregl from "maplibre-gl";
import { GEOLOCATE_FIND_LABEL } from "./geolocateRetry.ts";
import {
  FISHY_GEOLOCATE_OPTIONS,
  geolocateButtonFromMap,
  startFreshGeolocate,
  type GeolocateControlFactory,
  type GeolocateSession,
} from "./mapGeolocateSession.ts";

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

function createStubMap(): maplibregl.Map {
  const controls: maplibregl.IControl[] = [];
  const mapRoot = document.createElement("div");
  const canvasHost = document.createElement("div");
  const stub = {
    _getUIString: (key: string) => (key.includes("Find") ? GEOLOCATE_FIND_LABEL : "Unavailable"),
    getContainer: () => mapRoot,
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
      const root = control.onAdd(stub as unknown as maplibregl.Map);
      if (root) mapRoot.appendChild(root);
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

  it("starts a high-accuracy watch on the first production retry after denial", async () => {
    const createIntegrationControl: GeolocateControlFactory = (lib) =>
      new lib.GeolocateControl(INTEGRATION_GEOLOCATE_OPTIONS);
    const first = createIntegrationControl(maplibregl);
    map.addControl(first);
    await vi.waitFor(() => expect((first as unknown as { _setup?: boolean })._setup).toBe(true));

    const denied = new Promise<void>((resolve) => {
      first.once("error", () => resolve());
    });
    expect(first.trigger()).toBe(true);
    await denied;
    expect(watchCalls).toHaveLength(1);

    const oldButton = geolocateButtonFromMap(map);
    expect(oldButton).not.toBeNull();
    oldButton!.disabled = true;

    const session: GeolocateSession = { control: first, needsRecreate: true };
    let resolveSuccess: () => void = () => undefined;
    const success = new Promise<void>((resolve) => {
      resolveSuccess = resolve;
    });

    const errors: number[] = [];
    const started = await startFreshGeolocate(
      map,
      maplibregl,
      session,
      (control) => {
        session.control = control;
        control.once("geolocate", () => resolveSuccess());
      },
      (code) => errors.push(code),
      undefined,
      createIntegrationControl,
    );

    expect(started).toBe(true);
    expect(errors).toEqual([]);
    await success;

    const newButton = geolocateButtonFromMap(map);
    expect(newButton).not.toBeNull();
    expect(newButton).not.toBe(oldButton);
    expect(newButton!.title).toBe(GEOLOCATE_FIND_LABEL);
    expect(newButton!.getAttribute("aria-label")).toBe(GEOLOCATE_FIND_LABEL);

    expect(watchCalls).toHaveLength(2);
    expect(watchCalls[1]?.options).toMatchObject({
      enableHighAccuracy: true,
      maximumAge: 0,
      timeout: 10000,
    });
    expect(watchCalls[1]?.options.maximumAge).not.toBe(600000);
  }, 10_000);
});