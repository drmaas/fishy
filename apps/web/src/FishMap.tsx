import {
  lakeFromProperties,
  OPENFREEMAP_STYLE,
  SAMPLE_VIEW,
  type DepthUnit,
  type LakeInfo,
} from "@fishy/core";
import { useEffect, useRef } from "react";
import { DEPTH_COLOR, contourLabelField, fishSources } from "./layers.ts";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";

interface FishMapProps {
  contoursUrl: string;
  terrainUrl: string;
  hillshadeUrl: string;
  basemap: "openfreemap" | "none" | "maptiler";
  maptilerStyleUrl?: string;
  unit: DepthUnit;
  onLake: (lake: LakeInfo) => void;
}

export function FishMap({
  contoursUrl,
  terrainUrl,
  hillshadeUrl,
  basemap,
  maptilerStyleUrl,
  unit,
  onLake,
}: FishMapProps) {
  const nodeRef = useRef<HTMLDivElement>(null);
  const onLakeRef = useRef(onLake);
  const unitRef = useRef(unit);
  const mapRef = useRef<import("maplibre-gl").Map | null>(null);
  onLakeRef.current = onLake;
  unitRef.current = unit;

  useEffect(() => {
    const node = nodeRef.current;
    if (!node) return undefined;
    let map: import("maplibre-gl").Map | undefined;
    let cancelled = false;

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

      map = new maplibregl.Map({
        container: node,
        style,
        center: [SAMPLE_VIEW.longitude, SAMPLE_VIEW.latitude],
        zoom: SAMPLE_VIEW.zoom,
        pitch: 55,
        bearing: -18,
        attributionControl: false,
        maxPitch: 70,
      });
      mapRef.current = map;
      map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
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
        if (sources.terrain && !map.getSource("terrain")) {
          map.addSource("terrain", sources.terrain);
          map.setTerrain({ source: "terrain", exaggeration: 1.4 });
        }
      });
      map.on("click", (event) => {
        const hits = map?.queryRenderedFeatures(event.point, { layers: ["lake-fill"] }) ?? [];
        const lake = lakeFromProperties(hits[0]?.properties as Record<string, unknown> | undefined);
        if (lake) onLakeRef.current(lake);
      });
    })();

    return () => {
      cancelled = true;
      mapRef.current = null;
      map?.remove();
    };
  }, [basemap, contoursUrl, hillshadeUrl, maptilerStyleUrl, terrainUrl]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map?.getLayer("depth-labels")) return;
    map.setLayoutProperty("depth-labels", "text-field", contourLabelField(unit));
  }, [unit]);

  return <div ref={nodeRef} className="map" role="application" aria-label="Lake map" />;
}
