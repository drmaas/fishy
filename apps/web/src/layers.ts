import { pmtilesProtocolUrl, type DepthUnit } from "@fishy/core";
import type { DataDrivenPropertyValueSpecification, ExpressionSpecification } from "maplibre-gl";

export const DEPTH_COLOR = [
  "interpolate",
  ["linear"],
  ["get", "depth_ft"],
  0,
  "#d6efe8",
  5,
  "#7eb8d4",
  10,
  "#2f6f9f",
  20,
  "#14324f",
] as DataDrivenPropertyValueSpecification<string>;

export function contourLabelField(unit: DepthUnit): ExpressionSpecification {
  if (unit === "ft") {
    return ["concat", ["to-string", ["get", "depth_ft"]], " ft"] as ExpressionSpecification;
  }
  return [
    "concat",
    [
      "number-format",
      ["*", ["get", "depth_ft"], 0.3048],
      { "min-fraction-digits": 1, "max-fraction-digits": 1 },
    ],
    " m",
  ] as ExpressionSpecification;
}

export function fishSources(contoursUrl: string, terrainUrl: string, hillshadeUrl: string) {
  return {
    contours: {
      type: "vector" as const,
      url: pmtilesProtocolUrl(contoursUrl),
      attribution: "Bathymetry © Minnesota DNR",
    },
    terrain: terrainUrl
      ? {
          type: "raster-dem" as const,
          url: pmtilesProtocolUrl(terrainUrl),
          encoding: "terrarium" as const,
          tileSize: 256,
          // The sample terrain archive is a single zoom-10 tile.
          maxzoom: 10,
        }
      : null,
    hillshade: hillshadeUrl
      ? {
          type: "raster" as const,
          url: pmtilesProtocolUrl(hillshadeUrl),
          tileSize: 256,
          maxzoom: 10,
        }
      : null,
  };
}
