/** One international foot in meters. */
export const METERS_PER_FOOT = 0.3048;

/** One acre in square meters. */
export const SQUARE_METERS_PER_ACRE = 4046.8564224;

/** Round half away from zero. Non-finite input becomes 0. */
export function roundHalfAwayFromZero(value: number, digits = 0): number {
  if (!Number.isFinite(value)) return 0;
  const factor = 10 ** digits;
  const scaled = value * factor;
  const rounded = Math.sign(scaled) * Math.round(Math.abs(scaled));
  return rounded / factor;
}

export function feetToMeters(feet: number): number {
  return feet * METERS_PER_FOOT;
}

export function metersToFeet(meters: number): number {
  return meters / METERS_PER_FOOT;
}

/**
 * Positive depth in feet from a DNR contour.
 * `depth` is negative feet (0 on shore). `abs_depth` wins when present.
 */
export function depthFeetFromDnr(depth: number | null, absDepth: number | null): number {
  if (absDepth !== null && Number.isFinite(absDepth)) return Math.abs(absDepth);
  if (depth !== null && Number.isFinite(depth)) return Math.abs(depth);
  return 0;
}

export function squareMetersToAcres(squareMeters: number): number {
  return squareMeters / SQUARE_METERS_PER_ACRE;
}

export type DepthUnit = "ft" | "m";

export function formatDepth(feet: number, unit: DepthUnit): string {
  if (unit === "ft") {
    return `${roundHalfAwayFromZero(feet, 0)} ft`;
  }
  return `${roundHalfAwayFromZero(feetToMeters(feet), 1)} m`;
}

export function formatArea(acres: number, unit: DepthUnit): string {
  if (unit === "ft") {
    return `${roundHalfAwayFromZero(acres, 1)} acres`;
  }
  const hectares = (acres * SQUARE_METERS_PER_ACRE) / 10_000;
  return `${roundHalfAwayFromZero(hectares, 1)} ha`;
}
