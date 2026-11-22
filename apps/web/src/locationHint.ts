import { roundHalfAwayFromZero } from "@fishy/core";

export type LocationHintState =
  | { status: "idle" }
  | { status: "active"; accuracyMeters: number | null }
  | { status: "error"; code: number };

export function locationHint(state: LocationHintState): string | null {
  if (state.status === "idle") return null;
  if (state.status === "error") {
    if (state.code === 1) {
      return "Location is blocked. Allow location for this site in browser settings.";
    }
    if (state.code === 3) return "GPS timed out. Try again.";
    return "No GPS fix. Move outdoors and try again.";
  }
  const accuracy = state.accuracyMeters;
  if (accuracy != null && Number.isFinite(accuracy) && accuracy > 0) {
    return `You are here, ±${roundHalfAwayFromZero(accuracy, 0)} m.`;
  }
  return "You are here.";
}
