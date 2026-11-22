import type { LakeQaStatus, WisconsinLakeFields } from "./lakeId.ts";

export interface LakeIndexBbox {
  west: number;
  south: number;
  east: number;
  north: number;
}

/** Lakes index row (Wisconsin and future static indexes). */
export interface LakeIndexRow extends WisconsinLakeFields {
  id: string;
  name: string;
  county: string;
  acres: number;
  bbox: LakeIndexBbox;
  qa_status: LakeQaStatus;
}

/**
 * Shared lake detail fields returned by `/api/lakes/:id`.
 * Minnesota responses still serialize the lake id as `dowlknum`.
 */
export interface LakeDetailFields extends WisconsinLakeFields {
  name: string;
  maxDepthFt: number;
  areaAcres: number;
}