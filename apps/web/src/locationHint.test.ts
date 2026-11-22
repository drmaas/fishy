import { describe, expect, it } from "vitest";
import { locationHint } from "./locationHint.ts";

describe("locationHint", () => {
  it("returns null while idle", () => {
    expect(locationHint({ status: "idle" })).toBeNull();
  });

  it("formats a finite accuracy in meters", () => {
    expect(locationHint({ status: "active", accuracyMeters: 12.4 })).toBe("You are here, ±12 m.");
    expect(locationHint({ status: "active", accuracyMeters: 12.5 })).toBe("You are here, ±13 m.");
  });

  it("omits the radius when accuracy is missing", () => {
    expect(locationHint({ status: "active", accuracyMeters: null })).toBe("You are here.");
    expect(locationHint({ status: "active", accuracyMeters: 0 })).toBe("You are here.");
    expect(locationHint({ status: "active", accuracyMeters: Number.NaN })).toBe("You are here.");
  });

  it("maps geolocation error codes", () => {
    expect(locationHint({ status: "error", code: 1 })).toBe(
      "Location is blocked. Allow location for this site, then reload.",
    );
    expect(locationHint({ status: "error", code: 2 })).toBe("No GPS fix. Move outdoors and try again.");
    expect(locationHint({ status: "error", code: 3 })).toBe("GPS timed out. Try again.");
  });
});
