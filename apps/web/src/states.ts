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
];

export const DEFAULT_STATE = STATES[0]!;

export function stateById(id: string | null): StateOption {
  return STATES.find((state) => state.id === id) ?? DEFAULT_STATE;
}
