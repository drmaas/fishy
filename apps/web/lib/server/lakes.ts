import { depthFeetFromDnr, isDowlknum } from "@fishy/core";

const DNR_HOST = "https://enterprise.gisdata.mn.gov";
const CONTOURS_LAYER = `${DNR_HOST}/aghost/rest/services/us_mn_state_dnr/water_lake_bathymetry/MapServer/0`;
const OUTLINES_LAYER = `${DNR_HOST}/aghost/rest/services/us_mn_state_dnr/water_lake_bathymetry/MapServer/1`;

export const SUPPORTED_STATES = new Set(["mn"]);
export const SEARCH_LIMIT = 12;
export const MAX_BBOX_SPAN = 0.8;
export const CONTOUR_OFFSET = 0.0005;
/** Finer ArcGIS generalization for a selected lake outline and contours (~6 m). */
export const DETAIL_OFFSET = 0.00008;
export const GEOMETRY_PRECISION = 5;
export const LAKE_CACHE_CONTROL = "public, max-age=86400";
/** Bump when lake-detail geometry shaping changes so Cache API drops stale coarse rings. */
export const LAKE_DETAIL_CACHE_REV = "v2";

const SEARCH_CHARS = /^[A-Za-z0-9 '.\-&]+$/;

export interface LakeSearchHit {
  dowlknum: string;
  lake_name: string;
  cty_name: string;
  acres: number;
}

export interface Bbox {
  west: number;
  south: number;
  east: number;
  north: number;
}

export interface GeoJsonFeature {
  type: "Feature";
  properties: Record<string, unknown>;
  geometry: unknown;
}

export interface GeoJsonFeatureCollection {
  type: "FeatureCollection";
  features: GeoJsonFeature[];
}

export interface LakeDetail {
  dowlknum: string;
  name: string;
  maxDepthFt: number;
  areaAcres: number;
  extent: Bbox;
  outline: GeoJsonFeatureCollection;
  contours: GeoJsonFeatureCollection;
}

export function isSupportedState(state: string | null): boolean {
  return state !== null && SUPPORTED_STATES.has(state);
}

/** Escape a value for a single-quoted ArcGIS where clause. */
export function escapeWhereValue(value: string): string {
  return value.replaceAll("'", "''");
}

/**
 * Accept a lake name fragment or an 8-digit DOW id.
 * Returns null when the query is too short or contains disallowed characters.
 */
export function sanitizeSearchQuery(raw: string | null): string | null {
  if (raw === null) return null;
  const trimmed = raw.trim();
  if (trimmed.length < 2) return null;
  if (isDowlknum(trimmed)) return trimmed;
  if (trimmed.length > 80) return null;
  if (!SEARCH_CHARS.test(trimmed)) return null;
  return trimmed;
}

export function buildSearchWhere(query: string): string {
  if (isDowlknum(query)) {
    return `dowlknum='${escapeWhereValue(query)}' AND island='N'`;
  }
  const needle = escapeWhereValue(query.toUpperCase());
  return `island='N' AND UPPER(lake_name) LIKE '%${needle}%'`;
}

export function parseBbox(
  westRaw: string | null,
  southRaw: string | null,
  eastRaw: string | null,
  northRaw: string | null,
): Bbox | null {
  if (westRaw === null || southRaw === null || eastRaw === null || northRaw === null) return null;
  const west = Number(westRaw);
  const south = Number(southRaw);
  const east = Number(eastRaw);
  const north = Number(northRaw);
  if (![west, south, east, north].every(Number.isFinite)) return null;
  if (east <= west || north <= south) return null;
  if (east - west > MAX_BBOX_SPAN || north - south > MAX_BBOX_SPAN) return null;
  if (west < -180 || east > 180 || south < -90 || north > 90) return null;
  return { west, south, east, north };
}

function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

export function normalizeContourProperties(props: Record<string, unknown>): Record<string, unknown> {
  const depth = asNumber(props.depth);
  const absDepth = asNumber(props.abs_depth);
  const explicit = asNumber(props.depth_ft);
  const depthFt =
    absDepth !== null || depth !== null
      ? depthFeetFromDnr(depth, absDepth)
      : explicit !== null
        ? explicit
        : 0;
  const nameValue = props.lake_name;
  return {
    lake_name: typeof nameValue === "string" && nameValue.trim() !== "" ? nameValue : "Unnamed lake",
    dowlknum: String(props.dowlknum ?? ""),
    depth_ft: depthFt,
  };
}

export function normalizeOutlineProperties(props: Record<string, unknown>): Record<string, unknown> {
  const nameValue = props.lake_name;
  const acres = asNumber(props.acres) ?? asNumber(props.area_acres);
  const out: Record<string, unknown> = {
    lake_name: typeof nameValue === "string" && nameValue.trim() !== "" ? nameValue : "Unnamed lake",
    dowlknum: String(props.dowlknum ?? ""),
    island: String(props.island ?? "N"),
  };
  if (acres !== null) out.area_acres = acres;
  const county = props.cty_name;
  if (typeof county === "string" && county.trim() !== "") out.cty_name = county;
  return out;
}

type Position = [number, number];

function isClosedRing(ring: Position[]): boolean {
  if (ring.length < 2) return false;
  const first = ring[0];
  const last = ring[ring.length - 1];
  if (!first || !last) return false;
  return first[0] === last[0] && first[1] === last[1];
}

/** One Chaikin cut. Open lines keep endpoints. Closed rings stay closed. */
export function chaikinOnce(ring: Position[], closed: boolean): Position[] {
  if (ring.length < 2) return ring.map((point) => [point[0], point[1]] as Position);
  if (closed) {
    const body = isClosedRing(ring) ? ring.slice(0, -1) : ring;
    if (body.length < 3) return ring.map((point) => [point[0], point[1]] as Position);
    const out: Position[] = [];
    for (let i = 0; i < body.length; i += 1) {
      const a = body[i];
      const b = body[(i + 1) % body.length];
      if (!a || !b) continue;
      out.push([0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]]);
      out.push([0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]]);
    }
    const first = out[0];
    if (first) out.push([first[0], first[1]]);
    return out;
  }
  const out: Position[] = [[ring[0]![0], ring[0]![1]]];
  for (let i = 0; i < ring.length - 1; i += 1) {
    const a = ring[i];
    const b = ring[i + 1];
    if (!a || !b) continue;
    out.push([0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]]);
    out.push([0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]]);
  }
  const last = ring[ring.length - 1];
  if (last) out.push([last[0], last[1]]);
  return out;
}

function asPositionRing(value: unknown): Position[] | null {
  if (!Array.isArray(value) || value.length === 0) return null;
  const ring: Position[] = [];
  for (const point of value) {
    if (!Array.isArray(point) || point.length < 2) return null;
    const x = point[0];
    const y = point[1];
    if (typeof x !== "number" || typeof y !== "number" || !Number.isFinite(x) || !Number.isFinite(y)) {
      return null;
    }
    ring.push([x, y]);
  }
  return ring;
}

export function smoothGeometry(geometry: unknown): unknown {
  if (!geometry || typeof geometry !== "object") return geometry;
  const geom = geometry as { type?: string; coordinates?: unknown };
  if (geom.type === "LineString") {
    const ring = asPositionRing(geom.coordinates);
    if (!ring) return geometry;
    return { type: "LineString", coordinates: chaikinOnce(ring, isClosedRing(ring)) };
  }
  if (geom.type === "MultiLineString") {
    if (!Array.isArray(geom.coordinates)) return geometry;
    return {
      type: "MultiLineString",
      coordinates: geom.coordinates.map((line) => {
        const ring = asPositionRing(line);
        if (!ring) return line;
        return chaikinOnce(ring, isClosedRing(ring));
      }),
    };
  }
  if (geom.type === "Polygon") {
    if (!Array.isArray(geom.coordinates)) return geometry;
    return {
      type: "Polygon",
      coordinates: geom.coordinates.map((line) => {
        const ring = asPositionRing(line);
        if (!ring) return line;
        return chaikinOnce(ring, true);
      }),
    };
  }
  if (geom.type === "MultiPolygon") {
    if (!Array.isArray(geom.coordinates)) return geometry;
    return {
      type: "MultiPolygon",
      coordinates: geom.coordinates.map((polygon) => {
        if (!Array.isArray(polygon)) return polygon;
        return polygon.map((line) => {
          const ring = asPositionRing(line);
          if (!ring) return line;
          return chaikinOnce(ring, true);
        });
      }),
    };
  }
  return geometry;
}

export function smoothCollection(collection: GeoJsonFeatureCollection): GeoJsonFeatureCollection {
  return {
    type: "FeatureCollection",
    features: collection.features.map((feature) => ({
      type: "Feature",
      geometry: smoothGeometry(feature.geometry),
      properties: feature.properties,
    })),
  };
}

export function normalizeContourCollection(collection: GeoJsonFeatureCollection): GeoJsonFeatureCollection {
  return {
    type: "FeatureCollection",
    features: collection.features.map((feature) => ({
      type: "Feature",
      geometry: feature.geometry,
      properties: normalizeContourProperties(feature.properties ?? {}),
    })),
  };
}

export function normalizeOutlineCollection(collection: GeoJsonFeatureCollection): GeoJsonFeatureCollection {
  return {
    type: "FeatureCollection",
    features: collection.features.map((feature) => ({
      type: "Feature",
      geometry: feature.geometry,
      properties: normalizeOutlineProperties(feature.properties ?? {}),
    })),
  };
}

export function normalizeSearchHits(
  features: Array<{ attributes?: Record<string, unknown> }>,
): LakeSearchHit[] {
  const hits: LakeSearchHit[] = [];
  for (const feature of features) {
    const attrs = feature.attributes ?? {};
    const dowlknum = String(attrs.dowlknum ?? "");
    if (!isDowlknum(dowlknum)) continue;
    const acres = asNumber(attrs.acres);
    if (acres === null) continue;
    const nameValue = attrs.lake_name;
    const county = attrs.cty_name;
    hits.push({
      dowlknum,
      lake_name: typeof nameValue === "string" && nameValue.trim() !== "" ? nameValue : "Unnamed lake",
      cty_name: typeof county === "string" ? county : "",
      acres,
    });
  }
  return hits;
}

function queryUrl(layer: string, params: Record<string, string>): string {
  return `${layer}/query?${new URLSearchParams(params).toString()}`;
}

export function searchQueryUrl(where: string): string {
  return queryUrl(OUTLINES_LAYER, {
    where,
    outFields: "dowlknum,lake_name,cty_name,acres",
    returnGeometry: "false",
    orderByFields: "acres DESC",
    resultRecordCount: String(SEARCH_LIMIT),
    f: "json",
  });
}

export function nearbyQueryUrl(bbox: Bbox): string {
  return queryUrl(OUTLINES_LAYER, {
    where: "island='N'",
    geometry: `${bbox.west},${bbox.south},${bbox.east},${bbox.north}`,
    geometryType: "esriGeometryEnvelope",
    inSR: "4326",
    spatialRel: "esriSpatialRelIntersects",
    outFields: "dowlknum,lake_name,cty_name,acres,island",
    outSR: "4326",
    maxAllowableOffset: String(CONTOUR_OFFSET),
    geometryPrecision: String(GEOMETRY_PRECISION),
    f: "geojson",
  });
}

export function outlineQueryUrl(dowlknum: string): string {
  return queryUrl(OUTLINES_LAYER, {
    where: `dowlknum='${escapeWhereValue(dowlknum)}'`,
    outFields: "dowlknum,lake_name,cty_name,acres,island",
    outSR: "4326",
    maxAllowableOffset: String(DETAIL_OFFSET),
    geometryPrecision: String(GEOMETRY_PRECISION),
    f: "geojson",
  });
}

export function contoursQueryUrl(dowlknum: string): string {
  return queryUrl(CONTOURS_LAYER, {
    where: `dowlknum='${escapeWhereValue(dowlknum)}'`,
    outFields: "depth,abs_depth,dowlknum,lake_name",
    outSR: "4326",
    maxAllowableOffset: String(DETAIL_OFFSET),
    geometryPrecision: String(GEOMETRY_PRECISION),
    f: "geojson",
  });
}

export function extentQueryUrl(dowlknum: string): string {
  return queryUrl(OUTLINES_LAYER, {
    where: `dowlknum='${escapeWhereValue(dowlknum)}' AND island='N'`,
    returnExtentOnly: "true",
    outSR: "4326",
    f: "json",
  });
}

export function maxDepthQueryUrl(dowlknum: string): string {
  const outStatistics = JSON.stringify([
    { statisticType: "max", onStatisticField: "abs_depth", outStatisticFieldName: "max_depth" },
  ]);
  return queryUrl(CONTOURS_LAYER, {
    where: `dowlknum='${escapeWhereValue(dowlknum)}'`,
    outStatistics,
    f: "json",
  });
}

export function shoreAreaQueryUrl(dowlknum: string): string {
  return queryUrl(OUTLINES_LAYER, {
    where: `dowlknum='${escapeWhereValue(dowlknum)}' AND island='N'`,
    outFields: "dowlknum,lake_name,acres",
    returnGeometry: "false",
    f: "json",
  });
}

export function parseExtent(body: { extent?: Record<string, unknown> | null }): Bbox | null {
  const extent = body.extent;
  if (!extent) return null;
  const west = asNumber(extent.xmin);
  const south = asNumber(extent.ymin);
  const east = asNumber(extent.xmax);
  const north = asNumber(extent.ymax);
  if (west === null || south === null || east === null || north === null) return null;
  return { west, south, east, north };
}

export function parseMaxDepth(body: {
  features?: Array<{ attributes?: Record<string, unknown> }>;
}): number | null {
  const attrs = body.features?.[0]?.attributes;
  if (!attrs) return null;
  return asNumber(attrs.max_depth);
}

export function buildLakeDetail(args: {
  dowlknum: string;
  name: string;
  areaAcres: number;
  maxDepthFt: number;
  extent: Bbox;
  outline: GeoJsonFeatureCollection;
  contours: GeoJsonFeatureCollection;
}): LakeDetail {
  return {
    dowlknum: args.dowlknum,
    name: args.name,
    areaAcres: args.areaAcres,
    maxDepthFt: args.maxDepthFt,
    extent: args.extent,
    outline: smoothCollection(normalizeOutlineCollection(args.outline)),
    contours: smoothCollection(normalizeContourCollection(args.contours)),
  };
}

export function dnrFetchHeaders(): HeadersInit {
  return { "user-agent": "fishy-lakes/1.0", accept: "application/json, application/geo+json" };
}
