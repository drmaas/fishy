import { formatArea, formatDepth, lakeFinderUrl, type DepthUnit, type LakeInfo } from "@fishy/core";

interface LakePanelProps {
  lake: LakeInfo;
  unit: DepthUnit;
  onClose: () => void;
  onHide: () => void;
}

export function LakePanel({ lake, unit, onClose, onHide }: LakePanelProps) {
  const finder = lakeFinderUrl(lake.dowlknum);
  return (
    <aside className="panel" id="lake-panel" aria-label="Lake">
      <div className="panel-head">
        <h2>{lake.name}</h2>
        <div className="panel-actions">
          <button type="button" className="tap-target" onClick={onHide} aria-expanded="true" aria-controls="lake-panel">
            Hide
          </button>
          <button type="button" className="tap-target" onClick={onClose} aria-label="Close lake info">
            Close
          </button>
        </div>
      </div>
      <dl>
        <div>
          <dt>Max depth</dt>
          <dd>{formatDepth(lake.maxDepthFt, unit)}</dd>
        </div>
        <div>
          <dt>Area</dt>
          <dd>{formatArea(lake.areaAcres, unit)}</dd>
        </div>
        <div>
          <dt>DNR lake ID</dt>
          <dd>{lake.dowlknum}</dd>
        </div>
      </dl>
      {finder && (
        <a href={finder} target="_blank" rel="noreferrer">
          LakeFinder report
        </a>
      )}
      {lake.synthetic && <p className="note">Synthetic sample. This id is not a surveyed lake.</p>}
    </aside>
  );
}
