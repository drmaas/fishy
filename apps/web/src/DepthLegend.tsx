import { formatDepth, type DepthUnit } from "@fishy/core";

const STOPS_FT = [0, 5, 10, 20] as const;
const COLORS = ["#d6efe8", "#7eb8d4", "#2f6f9f", "#14324f"] as const;

interface DepthLegendProps {
  unit: DepthUnit;
}

export function DepthLegend({ unit }: DepthLegendProps) {
  return (
    <div className="legend" aria-label="Depth scale">
      <p className="legend-title">Depth</p>
      <ol>
        {STOPS_FT.map((feet, index) => (
          <li key={feet}>
            <span className="legend-swatch" style={{ background: COLORS[index] }} />
            <span>{formatDepth(feet, unit)}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}
