/** Supported lake id kinds by state (serving may be narrower than validation). */
export type LakeStateId = "mn" | "wi";

export interface LakeRef {
  state: LakeStateId;
  id: string;
}

export type LakeQaStatus = "auto" | "verified" | "needs_review";

/** Optional Wisconsin lake metadata on detail and index payloads. */
export interface WisconsinLakeFields {
  source_pdf_url?: string;
  qa_status?: LakeQaStatus;
  survey_date?: string;
}

/** Minnesota Division of Waters lake id — eight ASCII digits. */
export function isDowlknum(id: string): boolean {
  return /^[0-9]{8}$/.test(id);
}

/** Wisconsin WBIC — seven ASCII digits, zero-padded (e.g. `0417300`). */
export function isWbic(id: string): boolean {
  return /^[0-9]{7}$/.test(id);
}

export function isLakeIdForState(state: string, id: string): boolean {
  switch (state) {
    case "mn":
      return isDowlknum(id);
    case "wi":
      return isWbic(id);
    default:
      return false;
  }
}

/** Capture group 1 is the raw id segment; pair with `isLakeIdForState`. */
export const LAKE_DETAIL_PATH_RE = /^\/api\/lakes\/([0-9]+)$/;

export function lakeIdFromDetailPath(pathname: string): string | null {
  const match = LAKE_DETAIL_PATH_RE.exec(pathname);
  return match?.[1] ?? null;
}