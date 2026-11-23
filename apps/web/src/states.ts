export interface StateOption {
  id: string;
  label: string;
  /** [west, south, east, north] in WGS84. */
  bounds: [number, number, number, number];
}

export const STATES: StateOption[] = [
  {
    id: "mn",
    label: "Minnesota",
    bounds: [-97.5, 43.4, -89.4, 49.4],
  },
  {
    id: "wi",
    label: "Wisconsin",
    bounds: [-92.9, 42.49, -86.25, 47.08],
  },
];

export function bathymetryAttribution(stateId: string): string {
  return stateId === "wi" ? "Bathymetry © Wisconsin DNR" : "Bathymetry © Minnesota DNR";
}

export const DEFAULT_STATE = STATES[0]!;

export function stateById(id: string | null): StateOption {
  return STATES.find((state) => state.id === id) ?? DEFAULT_STATE;
}
