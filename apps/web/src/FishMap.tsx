import {
  isLakeIdForState,
  lakeFromProperties,
  type DepthUnit,
  type LakeInfo,
} from "@fishy/core";
import { useEffect, useRef, type MutableRefObject } from "react";
import { DEPTH_COLOR, contourLabelField, fishSources } from "./layers.ts";
import type { GeoJsonFeatureCollection, LakeDetail, LakeExtent } from "./lakeApi.ts";
import { GEOLOCATION_ERROR, shouldClearLocationOnTrackingEnd } from "./locationBanner.ts";
import { prepareGeolocateButtonForRetry, readGeolocateWatchState, shouldInterceptGeolocateButtonClick } from "./geolocateRetry.ts";
import {
  armGeolocateHangTimer,
  createFishyGeolocateControl,
  geolocateButtonFromMap,
  shouldRecreateGeolocateAfterError,
  startFreshGeolocate as startFreshGeolocateSession,
  startGeolocateAfterPermissionGrant,
  type GeolocateSession,
} from "./mapGeolocateSession.ts";
import type { LocationHintState } from "./locationHint.ts";
import {
  contoursOnlyBackground,
  mapStyleKey,
  openFreeMapStyleUrl,
  type ColorScheme,
} from "./theme.ts";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";

interface FishMapProps {
  contoursUrl: string;
  terrainUrl: string;
  hillshadeUrl: string;
  basemap: "openfreemap" | "none" | "maptiler";
  maptilerStyleUrl?: string;
  colorScheme: ColorScheme;
  unit: DepthUnit;
  stateId: string;
  stateBounds: [number, number, number, number];
  bathymetryAttribution: string;
  nearby: GeoJsonFeatureCollection | null;
  selected: LakeDetail | null;
  onLake: (lake: LakeInfo) => void;
  onSelectDowlknum: (dowlknum: string) => void;
  onExtent: (extent: LakeExtent, zoom: number) => void;
  onLocation: (state: LocationHintState) => void;
  onLocationAttempt: () => void;
  locationRetryRef: MutableRefObject<(() => void) | null>;
  locationPermissionGrantRef: MutableRefObject<(() => void) | null>;
}

const EMPTY: GeoJsonFeatureCollection = { type: "FeatureCollection", features: [] };

function lakeBounds(selected: LakeDetail): [[number, number], [number, number]] {
  return [
    [selected.extent.west, selected.extent.south],
    [selected.extent.east, selected.extent.north],
  ];
}

function resolveBasemapStyle(
  basemap: "openfreemap" | "none" | "maptiler",
  colorScheme: ColorScheme,
  maptilerStyleUrl?: string,
) {
  if (basemap === "maptiler" && maptilerStyleUrl) return maptilerStyleUrl;
  if (basemap === "none") {
    return {
      version: 8 as const,
      sources: {},
      layers: [
        {
          id: "background",
          type: "background" as const,
          paint: { "background-color": contoursOnlyBackground(colorScheme) },
        },
      ],
    };
  }
  return openFreeMapStyleUrl(colorScheme);
}

function contourInk(scheme: ColorScheme) {
  if (scheme === "dark") {
    return {
      line: "#d7e6ef",
      text: "#eef5f9",
      halo: "#0b1620",
      outline: "#c5d8e4",
      nearby: "#7eb8d4",
    };
  }
  return {
    line: "#083044",
    text: "#082433",
    halo: "#f4f7f8",
    outline: "#062433",
    nearby: "#0b4a63",
  };
}

function installFishLayers(
  map: import("maplibre-gl").Map,
  options: {
    contoursUrl: string;
    terrainUrl: string;
    hillshadeUrl: string;
    unit: DepthUnit;
    colorScheme: ColorScheme;
    nearby: GeoJsonFeatureCollection | null;
    selected: LakeDetail | null;
    bathymetryAttribution: string;
  },
) {
  const sources = fishSources(
    options.contoursUrl,
    options.terrainUrl,
    options.hillshadeUrl,
    options.bathymetryAttribution,
  );
  const ink = contourInk(options.colorScheme);

  if (sources.hillshade && !map.getSource("hillshade")) {
    map.addSource("hillshade", sources.hillshade);
    map.addLayer({
      id: "hillshade",
      type: "raster",
      source: "hillshade",
      paint: { "raster-opacity": 0.45 },
    });
  }

  if (!map.getSource("contours")) {
    map.addSource("contours", sources.contours);
    map.addLayer({
      id: "depth-shade",
      type: "fill",
      source: "contours",
      "source-layer": "zones",
      paint: { "fill-color": DEPTH_COLOR, "fill-opacity": 0.72 },
    });
    map.addLayer({
      id: "lake-fill",
      type: "fill",
      source: "contours",
      "source-layer": "lakes",
      paint: { "fill-color": "#000000", "fill-opacity": 0.01 },
    });
    map.addLayer({
      id: "depth-contours",
      type: "line",
      source: "contours",
      "source-layer": "contours",
      layout: { "line-join": "round", "line-cap": "round" },
      paint: { "line-color": ink.line, "line-width": 1.4 },
    });
    map.addLayer({
      id: "depth-labels",
      type: "symbol",
      source: "contours",
      "source-layer": "contours",
      layout: {
        "text-field": contourLabelField(options.unit),
        "symbol-placement": "line",
        "text-size": 12,
      },
      paint: {
        "text-color": ink.text,
        "text-halo-color": ink.halo,
        "text-halo-width": 1.2,
      },
    });
  }

  if (!map.getSource("survey-nearby")) {
    map.addSource("survey-nearby", { type: "geojson", data: options.nearby ?? EMPTY });
  }
  if (!map.getSource("survey-outline")) {
    map.addSource("survey-outline", { type: "geojson", data: EMPTY });
  }
  if (!map.getSource("survey-contours")) {
    map.addSource("survey-contours", { type: "geojson", data: EMPTY });
  }

  if (!map.getLayer("survey-nearby-fill")) {
    map.addLayer({
      id: "survey-nearby-fill",
      type: "fill",
      source: "survey-nearby",
      paint: { "fill-color": "#1c6b8a", "fill-opacity": 0.18 },
    });
    map.addLayer({
      id: "survey-nearby-line",
      type: "line",
      source: "survey-nearby",
      paint: { "line-color": ink.nearby, "line-width": 1.2 },
    });
    map.addLayer({
      id: "survey-outline-fill",
      type: "fill",
      source: "survey-outline",
      paint: { "fill-color": "#0b4a63", "fill-opacity": 0.08 },
    });
    map.addLayer({
      id: "survey-outline-line",
      type: "line",
      source: "survey-outline",
      layout: { "line-join": "round", "line-cap": "round" },
      paint: { "line-color": ink.outline, "line-width": 1.8 },
    });
    map.addLayer({
      id: "survey-depth-lines",
      type: "line",
      source: "survey-contours",
      layout: { "line-join": "round", "line-cap": "round" },
      paint: { "line-color": ink.line, "line-width": 1.35 },
    });
    map.addLayer({
      id: "survey-depth-labels",
      type: "symbol",
      source: "survey-contours",
      layout: {
        "text-field": contourLabelField(options.unit),
        "symbol-placement": "line",
        "text-size": 12,
      },
      paint: {
        "text-color": ink.text,
        "text-halo-color": ink.halo,
        "text-halo-width": 1.2,
      },
    });
  }

  if (sources.terrain && !map.getSource("terrain")) {
    map.addSource("terrain", sources.terrain);
    map.setTerrain({ source: "terrain", exaggeration: 1.4 });
  }

  const nearbySource = map.getSource("survey-nearby") as import("maplibre-gl").GeoJSONSource;
  const outline = map.getSource("survey-outline") as import("maplibre-gl").GeoJSONSource;
  const contours = map.getSource("survey-contours") as import("maplibre-gl").GeoJSONSource;
  nearbySource.setData(options.nearby ?? EMPTY);
  outline.setData(options.selected?.outline ?? EMPTY);
  contours.setData(options.selected?.contours ?? EMPTY);
}

export function FishMap({
  contoursUrl,
  terrainUrl,
  hillshadeUrl,
  basemap,
  maptilerStyleUrl,
  colorScheme,
  unit,
  stateId,
  stateBounds,
  bathymetryAttribution,
  nearby,
  selected,
  onLake,
  onSelectDowlknum,
  onExtent,
  onLocation,
  onLocationAttempt,
  locationRetryRef,
  locationPermissionGrantRef,
}: FishMapProps) {
  const nodeRef = useRef<HTMLDivElement>(null);
  const onLakeRef = useRef(onLake);
  const onSelectRef = useRef(onSelectDowlknum);
  const onExtentRef = useRef(onExtent);
  const onLocationRef = useRef(onLocation);
  const onLocationAttemptRef = useRef(onLocationAttempt);
  const unitRef = useRef(unit);
  const selectedRef = useRef(selected);
  const nearbyRef = useRef(nearby);
  const basemapRef = useRef(basemap);
  const colorSchemeRef = useRef(colorScheme);
  const maptilerStyleUrlRef = useRef(maptilerStyleUrl);
  const contoursUrlRef = useRef(contoursUrl);
  const terrainUrlRef = useRef(terrainUrl);
  const hillshadeUrlRef = useRef(hillshadeUrl);
  const stateIdRef = useRef(stateId);
  const bathymetryAttributionRef = useRef(bathymetryAttribution);
  const mapRef = useRef<import("maplibre-gl").Map | null>(null);
  const mapReady = useRef(false);
  const fittedLake = useRef<string | null>(null);
  const locationLock = useRef(false);
  const lastLocationRef = useRef<LocationHintState>({ status: "idle" });
  const appliedStyleKeyRef = useRef<string | null>(null);
  onLakeRef.current = onLake;
  onSelectRef.current = onSelectDowlknum;
  onExtentRef.current = onExtent;
  onLocationRef.current = (state) => {
    lastLocationRef.current = state;
    onLocation(state);
  };
  onLocationAttemptRef.current = onLocationAttempt;
  unitRef.current = unit;
  selectedRef.current = selected;
  nearbyRef.current = nearby;
  basemapRef.current = basemap;
  colorSchemeRef.current = colorScheme;
  maptilerStyleUrlRef.current = maptilerStyleUrl;
  contoursUrlRef.current = contoursUrl;
  terrainUrlRef.current = terrainUrl;
  hillshadeUrlRef.current = hillshadeUrl;
  stateIdRef.current = stateId;
  bathymetryAttributionRef.current = bathymetryAttribution;

  useEffect(() => {
    const node = nodeRef.current;
    if (!node) return undefined;
    let map: import("maplibre-gl").Map | undefined;
    let geolocateButton: HTMLButtonElement | null = null;
    let onGeolocateButtonClick: ((event: Event) => void) | null = null;
    let clearHangTimer: (() => void) | undefined;
    let cancelled = false;
    mapReady.current = false;
    fittedLake.current = null;
    locationLock.current = false;
    appliedStyleKeyRef.current = null;

    void (async () => {
      const maplibregl = await import("maplibre-gl");
      maplibregl.setWorkerUrl(workerUrl);
      const { Protocol } = await import("pmtiles");
      const protocol = new Protocol();
      maplibregl.addProtocol("pmtiles", protocol.tile);
      if (cancelled) return;

      const initialStyle = resolveBasemapStyle(
        basemapRef.current,
        colorSchemeRef.current,
        maptilerStyleUrlRef.current,
      );

      const fitSelectedLake = (lake: LakeDetail, duration: number, force = false) => {
        if (!map || !mapReady.current) return;
        if (!force && fittedLake.current === lake.dowlknum) return;
        // MapLibre fitBounds while ACTIVE_LOCK does not release the lock; the next
        // GPS fix recenters on the user and fittedLake would prevent a retry.
        if (locationLock.current) return;
        fittedLake.current = lake.dowlknum;
        map.fitBounds(lakeBounds(lake), { padding: 56, duration, pitch: 0 });
      };

      const initialLake = selectedRef.current;
      const openedOnLakeId = initialLake?.dowlknum ?? null;
      map = new maplibregl.Map({
        container: node,
        style: initialStyle,
        bounds: initialLake ? lakeBounds(initialLake) : stateBounds,
        fitBoundsOptions: { padding: initialLake ? 56 : 28, pitch: 0, bearing: 0 },
        attributionControl: false,
        maxPitch: 70,
      });
      mapRef.current = map;
      map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "bottom-right");
      let geolocate = createFishyGeolocateControl(maplibregl);
      map.addControl(geolocate, "bottom-right");
      const session: GeolocateSession = { control: geolocate, needsRecreate: false };
      const geolocateButtonFor = (): HTMLButtonElement | null => geolocateButtonFromMap(map);
      const detachGeolocateClick = () => {
        if (geolocateButton && onGeolocateButtonClick) {
          geolocateButton.removeEventListener("click", onGeolocateButtonClick, true);
        }
      };
      const attachGeolocateClick = () => {
        geolocateButton = geolocateButtonFor();
        if (geolocateButton && onGeolocateButtonClick) {
          geolocateButton.addEventListener("click", onGeolocateButtonClick, true);
        }
      };
      // BACKGROUND (user pan) fires trackuserlocationend then userlocationlostfocus sync.
      // Defer fit so BACKGROUND can skip; OFF (deselect) still snaps back to the lake.
      let gpsBackgrounded = false;
      let geolocationFailed = false;
      function wireGeolocate(control: typeof geolocate) {
        control.on("trackuserlocationstart", () => {
          geolocationFailed = false;
          clearHangTimer?.();
          onLocationAttemptRef.current();
          locationLock.current = true;
          fittedLake.current = null;
          gpsBackgrounded = false;
          clearHangTimer = armGeolocateHangTimer(geolocateButtonFor(), () => {
            geolocationFailed = true;
            onLocationRef.current({ status: "error", code: GEOLOCATION_ERROR.TIMEOUT });
          });
        });
        control.on("geolocate", (event) => {
          geolocationFailed = false;
          clearHangTimer?.();
          clearHangTimer = undefined;
          const button = geolocateButtonFor();
          prepareGeolocateButtonForRetry(button);
          const accuracy = event.coords.accuracy;
          onLocationRef.current({
            status: "active",
            accuracyMeters: typeof accuracy === "number" ? accuracy : null,
          });
        });
        control.on("error", (event) => {
          geolocationFailed = true;
          clearHangTimer?.();
          clearHangTimer = undefined;
          if (shouldRecreateGeolocateAfterError(event.code)) {
            session.needsRecreate = true;
          }
          onLocationRef.current({ status: "error", code: event.code });
        });
        control.on("userlocationlostfocus", () => {
          gpsBackgrounded = true;
        });
        control.on("trackuserlocationend", () => {
          locationLock.current = false;
          clearHangTimer?.();
          clearHangTimer = undefined;
          if (shouldClearLocationOnTrackingEnd(lastLocationRef.current, geolocationFailed)) {
            onLocationRef.current({ status: "idle" });
          }
          queueMicrotask(() => {
            if (gpsBackgrounded) {
              gpsBackgrounded = false;
              return;
            }
            const lake = selectedRef.current;
            if (lake) fitSelectedLake(lake, 800, true);
          });
        });
      }
      const onGeolocateControlReplaced = (control: typeof geolocate) => {
        geolocate = control;
        session.control = control;
        wireGeolocate(control);
        attachGeolocateClick();
      };
      const onGeolocateStartError = (code: number) => {
        geolocationFailed = true;
        onLocationRef.current({ status: "error", code });
      };
      const beforeGeolocateReplace = () => {
        detachGeolocateClick();
        clearHangTimer?.();
        clearHangTimer = undefined;
      };
      const beginFreshGeolocate = async () => {
        if (!map) return;
        await startFreshGeolocateSession(
          map,
          maplibregl,
          session,
          onGeolocateControlReplaced,
          onGeolocateStartError,
          beforeGeolocateReplace,
        );
      };
      locationRetryRef.current = () => {
        void beginFreshGeolocate();
      };
      locationPermissionGrantRef.current = () => {
        if (!map) return;
        void startGeolocateAfterPermissionGrant(
          map,
          maplibregl,
          session,
          onGeolocateControlReplaced,
          onGeolocateStartError,
          beforeGeolocateReplace,
        );
      };
      onGeolocateButtonClick = (event: Event) => {
        const button = event.currentTarget as HTMLButtonElement;
        onLocationAttemptRef.current();
        const watchState = readGeolocateWatchState(button);
        if (shouldInterceptGeolocateButtonClick(watchState, button.disabled) || session.needsRecreate) {
          event.stopImmediatePropagation();
          void beginFreshGeolocate();
        }
      };
      wireGeolocate(geolocate);
      attachGeolocateClick();

      const reportExtent = () => {
        if (!map) return;
        const bounds = map.getBounds();
        onExtentRef.current(
          {
            west: bounds.getWest(),
            south: bounds.getSouth(),
            east: bounds.getEast(),
            north: bounds.getNorth(),
          },
          map.getZoom(),
        );
      };

      const onStyleReady = () => {
        if (!map) return;
        installFishLayers(map, {
          contoursUrl: contoursUrlRef.current,
          terrainUrl: terrainUrlRef.current,
          hillshadeUrl: hillshadeUrlRef.current,
          unit: unitRef.current,
          colorScheme: colorSchemeRef.current,
          nearby: nearbyRef.current,
          selected: selectedRef.current,
          bathymetryAttribution: bathymetryAttributionRef.current,
        });
        appliedStyleKeyRef.current = mapStyleKey(
          basemapRef.current,
          colorSchemeRef.current,
          maptilerStyleUrlRef.current,
        );
        mapReady.current = true;
        const lake = selectedRef.current;
        if (lake) {
          if (openedOnLakeId === lake.dowlknum && !locationLock.current) {
            fittedLake.current = lake.dowlknum;
          } else if (!locationLock.current && fittedLake.current !== lake.dowlknum) {
            fitSelectedLake(lake, 0);
          }
        } else {
          fittedLake.current = null;
        }
        reportExtent();
      };

      map.on("load", onStyleReady);

      map.on("moveend", reportExtent);

      map.on("click", (event) => {
        const surveyLayers = ["survey-nearby-fill", "survey-outline-fill"];
        const surveyHits = map?.queryRenderedFeatures(event.point, { layers: surveyLayers }) ?? [];
        const dowlknum = String(surveyHits[0]?.properties?.dowlknum ?? "");
        if (isLakeIdForState(stateIdRef.current, dowlknum)) {
          onSelectRef.current(dowlknum);
          return;
        }
        const hits = map?.queryRenderedFeatures(event.point, { layers: ["lake-fill"] }) ?? [];
        const lake = lakeFromProperties(hits[0]?.properties as Record<string, unknown> | undefined);
        if (lake) onLakeRef.current(lake);
      });
    })();

    return () => {
      cancelled = true;
      clearHangTimer?.();
      clearHangTimer = undefined;
      if (geolocateButton && onGeolocateButtonClick) {
        geolocateButton.removeEventListener("click", onGeolocateButtonClick, true);
      }
      locationRetryRef.current = null;
      locationPermissionGrantRef.current = null;
      mapRef.current = null;
      mapReady.current = false;
      fittedLake.current = null;
      appliedStyleKeyRef.current = null;
      onLocationRef.current({ status: "idle" });
      map?.remove();
    };
  }, [bathymetryAttribution, contoursUrl, hillshadeUrl, locationPermissionGrantRef, locationRetryRef, stateBounds, terrainUrl]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const nextKey = mapStyleKey(basemap, colorScheme, maptilerStyleUrl);
    if (appliedStyleKeyRef.current === nextKey) return;

    let cancelled = false;
    mapReady.current = false;
    const onStyleLoad = () => {
      if (cancelled) return;
      installFishLayers(map, {
        contoursUrl: contoursUrlRef.current,
        terrainUrl: terrainUrlRef.current,
        hillshadeUrl: hillshadeUrlRef.current,
        unit: unitRef.current,
        colorScheme,
        nearby: nearbyRef.current,
        selected: selectedRef.current,
        bathymetryAttribution: bathymetryAttributionRef.current,
      });
      appliedStyleKeyRef.current = nextKey;
      mapReady.current = true;
    };
    map.once("style.load", onStyleLoad);
    // Full reload: style diff can keep the old contours-only background paint.
    map.setStyle(resolveBasemapStyle(basemap, colorScheme, maptilerStyleUrl), { diff: false });
    return () => {
      cancelled = true;
      map.off("style.load", onStyleLoad);
    };
  }, [basemap, colorScheme, maptilerStyleUrl]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map?.getLayer("depth-labels")) return;
    map.setLayoutProperty("depth-labels", "text-field", contourLabelField(unit));
    if (map.getLayer("survey-depth-labels")) {
      map.setLayoutProperty("survey-depth-labels", "text-field", contourLabelField(unit));
    }
  }, [unit]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady.current) return;
    const source = map.getSource("survey-nearby") as import("maplibre-gl").GeoJSONSource | undefined;
    source?.setData(nearby ?? EMPTY);
  }, [nearby]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady.current) return;
    const outline = map.getSource("survey-outline") as import("maplibre-gl").GeoJSONSource | undefined;
    const contours = map.getSource("survey-contours") as import("maplibre-gl").GeoJSONSource | undefined;
    outline?.setData(selected?.outline ?? EMPTY);
    contours?.setData(selected?.contours ?? EMPTY);
    if (!selected) {
      fittedLake.current = null;
      return;
    }
    if (fittedLake.current === selected.dowlknum) return;
    if (locationLock.current) return;
    fittedLake.current = selected.dowlknum;
    map.fitBounds(lakeBounds(selected), { padding: 56, duration: 800, pitch: 0 });
  }, [selected]);

  return <div ref={nodeRef} className="map" role="application" aria-label="Lake map" />;
}
