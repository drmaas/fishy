import type { LakeInfo } from "@fishy/core";

export interface LakeSearchHit {
  dowlknum: string;
  lake_name: string;
  cty_name: string;
  acres: number;
}

export interface LakeExtent {
  west: number;
  south: number;
  east: number;
  north: number;
}

export interface GeoJsonFeatureCollection {
  type: "FeatureCollection";
  features: Array<{
    type: "Feature";
    properties: Record<string, unknown> | null;
    geometry: unknown;
  }>;
}

export interface LakeDetail {
  dowlknum: string;
  name: string;
  maxDepthFt: number;
  areaAcres: number;
  extent: LakeExtent;
  outline: GeoJsonFeatureCollection;
  contours: GeoJsonFeatureCollection;
}

export function lakeInfoFromDetail(detail: LakeDetail): LakeInfo {
  return {
    name: detail.name,
    dowlknum: detail.dowlknum,
    maxDepthFt: detail.maxDepthFt,
    areaAcres: detail.areaAcres,
    synthetic: detail.dowlknum.startsWith("99"),
  };
}

export async function searchLakes(state: string, query: string): Promise<LakeSearchHit[]> {
  const url = `/api/lakes?state=${encodeURIComponent(state)}&q=${encodeURIComponent(query)}`;
  const response = await fetch(url);
  if (!response.ok) return [];
  const body = (await response.json()) as { lakes?: LakeSearchHit[] };
  return body.lakes ?? [];
}

export async function fetchNearbyLakes(
  state: string,
  extent: LakeExtent,
): Promise<GeoJsonFeatureCollection | null> {
  const params = new URLSearchParams({
    state,
    west: String(extent.west),
    south: String(extent.south),
    east: String(extent.east),
    north: String(extent.north),
  });
  const response = await fetch(`/api/lakes/nearby?${params}`);
  if (!response.ok) return null;
  return (await response.json()) as GeoJsonFeatureCollection;
}

export async function fetchLakeDetail(state: string, dowlknum: string): Promise<LakeDetail | null> {
  const response = await fetch(`/api/lakes/${encodeURIComponent(dowlknum)}?state=${encodeURIComponent(state)}`);
  if (!response.ok) return null;
  return (await response.json()) as LakeDetail;
}
