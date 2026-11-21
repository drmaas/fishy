export interface LakeInfo {
  name: string;
  dowlknum: string;
  maxDepthFt: number;
  areaAcres: number;
  synthetic: boolean;
}

const LAKEFINDER = "https://www.dnr.state.mn.us/lakefind/showreport.html?downum=";

/** Eight-digit DNR Division of Waters lake id. Basin suffixes other than 00 are allowed. */
export function isDowlknum(id: string): boolean {
  return /^[0-9]{8}$/.test(id);
}

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
