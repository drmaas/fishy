import { isDowlknum } from "./lakeId.ts";

export { isDowlknum, isWbic, isLakeIdForState, lakeIdFromDetailPath, LAKE_DETAIL_PATH_RE, type LakeRef, type LakeQaStatus, type WisconsinLakeFields } from "./lakeId.ts";
export type { LakeDetailFields, LakeIndexRow, LakeIndexBbox } from "./lakeContract.ts";

export interface LakeInfo {
  name: string;
  dowlknum: string;
  maxDepthFt: number;
  areaAcres: number;
  synthetic: boolean;
}

const LAKEFINDER = "https://www.dnr.state.mn.us/lakefind/showreport.html?downum=";

export function lakeFinderUrl(dowlknum: string): string | null {
  if (!isDowlknum(dowlknum)) return null;
  return `${LAKEFINDER}${dowlknum}`;
}

function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

export function lakeFromProperties(props: Record<string, unknown> | null | undefined): LakeInfo | null {
  if (!props) return null;
  const dowlknum = String(props.dowlknum ?? "");
  if (!isDowlknum(dowlknum)) return null;
  const maxDepthFt = asNumber(props.max_depth_ft);
  const areaAcres = asNumber(props.area_acres);
  if (maxDepthFt === null || areaAcres === null) return null;
  const nameValue = props.lake_name;
  const name = typeof nameValue === "string" && nameValue.trim() !== "" ? nameValue : "Unnamed lake";
  return {
    name,
    dowlknum,
    maxDepthFt,
    areaAcres,
    synthetic: dowlknum.startsWith("99"),
  };
}
