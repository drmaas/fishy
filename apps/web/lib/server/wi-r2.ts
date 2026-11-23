import { isWbic, type LakeIndexBbox, type LakeIndexRow } from "@fishy/core";
import {
  type Bbox,
  type GeoJsonFeatureCollection,
  type LakeSearchHit,
  normalizeOutlineCollection,
  SEARCH_LIMIT,
} from "./lakes.ts";

export const WI_CURRENT_POINTER_KEY = "wi/v1/current.json";
export const WI_PUBLISHED_QA_STATUS = "verified" as const;

export interface WiCurrentPointer {
  version: number;
  generation: string;
  prefix: string;
  lakes_index_key: string;
  lake_detail_keys: string[];
}

const WI_R2_KEY_PREFIX = "wi/v1/";
const WI_PREFIX_RE = /^wi\/v1\/[^/]+$/;

export function isSafeWiR2Key(key: string): boolean {
  return key.startsWith(WI_R2_KEY_PREFIX) && !key.includes("..") && !key.includes("\\") && !key.includes("\0");
}

export function parseWiCurrentPointer(body: unknown): WiCurrentPointer | null {
  if (!body || typeof body !== "object") return null;
  const record = body as Record<string, unknown>;
  if (record.version !== 1) return null;
  const generation = record.generation;
  const prefix = record.prefix;
  const lakesIndexKey = record.lakes_index_key;
  const lakeDetailKeys = record.lake_detail_keys;
  if (typeof generation !== "string" || typeof prefix !== "string" || typeof lakesIndexKey !== "string") {
    return null;
  }
  if (!WI_PREFIX_RE.test(prefix) || !isSafeWiR2Key(lakesIndexKey)) return null;
  if (!Array.isArray(lakeDetailKeys) || !lakeDetailKeys.every((entry) => typeof entry === "string")) {
    return null;
  }
  return {
    version: 1,
    generation,
    prefix,
    lakes_index_key: lakesIndexKey,
    lake_detail_keys: lakeDetailKeys,
  };
}

export function parseWiLakesIndex(body: unknown): LakeIndexRow[] | null {
  if (!Array.isArray(body)) return null;
  return body as LakeIndexRow[];
}

export function verifiedIndexRows(rows: LakeIndexRow[]): LakeIndexRow[] {
  return rows.filter((row) => row.qa_status === WI_PUBLISHED_QA_STATUS);
}

export function searchWiIndex(rows: LakeIndexRow[], query: string): LakeIndexRow[] {
  const verified = verifiedIndexRows(rows);
  if (isWbic(query)) {
    const hit = verified.find((row) => row.id === query);
    return hit ? [hit] : [];
  }
  const needle = query.toUpperCase();
  return verified
    .filter((row) => row.name.toUpperCase().includes(needle))
    .sort((left, right) => right.acres - left.acres)
    .slice(0, SEARCH_LIMIT);
}

export function wiSearchHits(rows: LakeIndexRow[]): LakeSearchHit[] {
  return rows.map((row) => ({
    dowlknum: row.id,
    lake_name: row.name,
    cty_name: row.county,
    acres: row.acres,
  }));
}

export function bboxIntersectsLakeIndex(bbox: LakeIndexBbox, query: Bbox): boolean {
  return bbox.west < query.east && bbox.east > query.west && bbox.south < query.north && bbox.north > query.south;
}

export function nearbyWiIndex(rows: LakeIndexRow[], query: Bbox): LakeIndexRow[] {
  return verifiedIndexRows(rows)
    .filter((row) => bboxIntersectsLakeIndex(row.bbox, query))
    .sort((left, right) => right.acres - left.acres);
}

function bboxRing(bbox: LakeIndexBbox): [number, number][] {
  const { west, south, east, north } = bbox;
  return [
    [west, south],
    [west, north],
    [east, north],
    [east, south],
    [west, south],
  ];
}

export function wiNearbyCollection(rows: LakeIndexRow[]): GeoJsonFeatureCollection {
  return normalizeOutlineCollection({
    type: "FeatureCollection",
    features: rows.map((row) => ({
      type: "Feature" as const,
      properties: {
        dowlknum: row.id,
        lake_name: row.name,
        cty_name: row.county,
        area_acres: row.acres,
        island: "N",
      },
      geometry: {
        type: "Polygon",
        coordinates: [bboxRing(row.bbox)],
      },
    })),
  });
}

/** Only the validated WBIC is interpolated into lake detail keys. */
export function wiLakeDetailKey(prefix: string, wbic: string): string | null {
  if (!isWbic(wbic) || !WI_PREFIX_RE.test(prefix)) return null;
  return `${prefix}/lakes/${wbic}.json`;
}