import {
  lakeFromProperties,
  OPENFREEMAP_STYLE,
  type DepthUnit,
  type LakeInfo,
} from "@fishy/core";
import { useEffect, useRef } from "react";
import { DEPTH_COLOR, contourLabelField, fishSources } from "./layers.ts";
import type { GeoJsonFeatureCollection, LakeDetail, LakeExtent } from "./lakeApi.ts";
import type { LocationHintState } from "./locationHint.ts";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";

interface FishMapProps {
  contoursUrl: string;
  terrainUrl: string;
  hillshadeUrl: string;
  basemap: "openfreemap" | "none" | "maptiler";
  maptilerStyleUrl?: string;
  unit: DepthUnit;
  stateBounds: [number, number, number, number];
  nearby: GeoJsonFeatureCollection | null;
  selected: LakeDetail | null;
  onLake: (lake: LakeInfo) => void;
  onSelectDowlknum: (dowlknum: string) => void;
  onExtent: (extent: LakeExtent, zoom: number) => void;
  onLocation: (state: LocationHintState) => void;
}

const EMPTY: GeoJsonFeatureCollection = { type: "FeatureCollection", features: [] };

function lakeBounds(selected: LakeDetail): [[number, number], [number, number]] {
  return [
    [selected.extent.west, selected.extent.south],
    [selected.extent.east, selected.extent.north],
  ];
}

export function FishMap({
  contoursUrl,
  terrainUrl,
  hillshadeUrl,
  basemap,
  maptilerStyleUrl,
  unit,
  stateBounds,
  nearby,
  selected,
  onLake,
  onSelectDowlknum,
  onExtent,
  onLocation,
}: FishMapProps) {
  const nodeRef = useRef<HTMLDivElement>(null);
  const onLakeRef = useRef(onLake);
  const onSelectRef = useRef(onSelectDowlknum);
  const onExtentRef = useRef(onExtent);
  const onLocationRef = useRef(onLocation);
  const unitRef = useRef(unit);
  const selectedRef = useRef(selected);
  const nearbyRef = useRef(nearby);
  const mapRef = useRef<import("maplibre-gl").Map | null>(null);
  const mapReady = useRef(false);
  const fittedLake = useRef<string | null>(null);
  const locationLock = useRef(false);
  onLakeRef.current = onLake;
  onSelectRef.current = onSelectDowlknum;
  onExtentRef.current = onExtent;
  onLocationRef.current = onLocation;
  unitRef.current = unit;
  selectedRef.current = selected;
  nearbyRef.current = nearby;

  useEffect(() => {
    const node = nodeRef.current;
    if (!node) return undefined;
    let map: import("maplibre-gl").Map | undefined;
    let cancelled = false;
    mapReady.current = false;
    fittedLake.current = null;
    locationLock.current = false;

    void (async () => {
      const maplibregl = await import("maplibre-gl");
      maplibregl.setWorkerUrl(workerUrl);
      const { Protocol } = await import("pmtiles");
      const protocol = new Protocol();
      maplibregl.addProtocol("pmtiles", protocol.tile);
      if (cancelled) return;

      const style =
        basemap === "maptiler" && maptilerStyleUrl
          ? maptilerStyleUrl
          : basemap === "none"
            ? {
                version: 8 as const,
                sources: {},
                layers: [
                  {
                    id: "background",
                    type: "background" as const,
                    paint: { "background-color": "#d5e3ec" },
                  },
                ],
              }
            : OPENFREEMAP_STYLE;

      const fitSelectedLake = (lake: LakeDetail, duration: number) => {
        if (!map || !mapReady.current) return;
        if (fittedLake.current === lake.dowlknum) return;
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
        style,
        bounds: initialLake ? lakeBounds(initialLake) : stateBounds,
        fitBoundsOptions: { padding: initialLake ? 56 : 28, pitch: 0, bearing: 0 },
        attributionControl: false,
        maxPitch: 70,
      });
      mapRef.current = map;
      map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "bottom-right");
      const geolocate = new maplibregl.GeolocateControl({
        positionOptions: { enableHighAccuracy: true, maximumAge: 0, timeout: 10000 },
        trackUserLocation: true,
        showUserLocation: true,
        showAccuracyCircle: true,
        zoomToUserAccuracy: true,
        fitBoundsOptions: { maxZoom: 16, padding: 80 },
      });
      map.addControl(geolocate, "bottom-right");
      geolocate.on("trackuserlocationstart", () => {
        locationLock.current = true;
      });
      geolocate.on("geolocate", (event) => {
        const accuracy = event.coords.accuracy;
        onLocationRef.current({
          status: "active",
          accuracyMeters: typeof accuracy === "number" ? accuracy : null,
        });
      });
      geolocate.on("error", (event) => {
        onLocationRef.current({ status: "error", code: event.code });
      });
      geolocate.on("trackuserlocationend", () => {
        locationLock.current = false;
        onLocationRef.current({ status: "idle" });
        const lake = selectedRef.current;
        if (lake) fitSelectedLake(lake, 800);
      });

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

      map.on("load", () => {
        if (!map) return;
        const sources = fishSources(contoursUrl, terrainUrl, hillshadeUrl);
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
            paint: { "line-color": "#083044", "line-width": 1.4 },
          });
          map.addLayer({
            id: "depth-labels",
            type: "symbol",
            source: "contours",
            "source-layer": "contours",
            layout: {
              "text-field": contourLabelField(unitRef.current),
              "symbol-placement": "line",
              "text-size": 12,
            },
            paint: { "text-color": "#082433", "text-halo-color": "#f4f7f8", "text-halo-width": 1.2 },
          });
        }

        map.addSource("survey-nearby", { type: "geojson", data: nearbyRef.current ?? EMPTY });
        map.addSource("survey-outline", { type: "geojson", data: EMPTY });
        map.addSource("survey-contours", { type: "geojson", data: EMPTY });

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
          paint: { "line-color": "#0b4a63", "line-width": 1.2 },
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
          paint: { "line-color": "#062433", "line-width": 1.8 },
        });
        map.addLayer({
          id: "survey-depth-lines",
          type: "line",
          source: "survey-contours",
          layout: { "line-join": "round", "line-cap": "round" },
          paint: { "line-color": "#083044", "line-width": 1.35 },
        });
        map.addLayer({
          id: "survey-depth-labels",
          type: "symbol",
          source: "survey-contours",
          layout: {
            "text-field": contourLabelField(unitRef.current),
            "symbol-placement": "line",
            "text-size": 12,
          },
          paint: { "text-color": "#082433", "text-halo-color": "#f4f7f8", "text-halo-width": 1.2 },
        });

        if (sources.terrain && !map.getSource("terrain")) {
          map.addSource("terrain", sources.terrain);
          map.setTerrain({ source: "terrain", exaggeration: 1.4 });
        }

        mapReady.current = true;
        const lake = selectedRef.current;
        if (lake) {
          const outline = map.getSource("survey-outline") as import("maplibre-gl").GeoJSONSource;
          const contours = map.getSource("survey-contours") as import("maplibre-gl").GeoJSONSource;
          outline.setData(lake.outline);
          contours.setData(lake.contours);
          if (openedOnLakeId === lake.dowlknum && !locationLock.current) {
            fittedLake.current = lake.dowlknum;
          } else {
            fitSelectedLake(lake, 0);
          }
        } else {
          fittedLake.current = null;
        }
        reportExtent();
      });

      map.on("moveend", reportExtent);

      map.on("click", (event) => {
        const surveyLayers = ["survey-nearby-fill", "survey-outline-fill"];
        const surveyHits = map?.queryRenderedFeatures(event.point, { layers: surveyLayers }) ?? [];
        const dowlknum = String(surveyHits[0]?.properties?.dowlknum ?? "");
        if (/^[0-9]{8}$/.test(dowlknum)) {
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
      mapRef.current = null;
      mapReady.current = false;
      fittedLake.current = null;
      onLocationRef.current({ status: "idle" });
      map?.remove();
    };
  }, [basemap, contoursUrl, hillshadeUrl, maptilerStyleUrl, stateBounds, terrainUrl]);

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
