import {
  formatArea,
  formatDepth,
  lakeFinderUrl,
  safeHttpsUrl,
  type DepthUnit,
  type LakeInfo,
  type LakeQaStatus,
  type WisconsinLakeFields,
} from "@fishy/core";

interface LakePanelProps {
  lake: LakeInfo;
  unit: DepthUnit;
  stateId: string;
  wiFields?: WisconsinLakeFields;
  onHide: () => void;
}

function qaStatusLabel(status: LakeQaStatus): string {
  switch (status) {
    case "verified":
      return "Verified";
    case "auto":
      return "Auto";
    case "needs_review":
      return "Needs review";
    default:
      return status;
  }
}

const EXTERNAL_REL = "noopener noreferrer";

export function LakePanel({ lake, unit, stateId, wiFields, onHide }: LakePanelProps) {
  const finder = stateId === "mn" ? lakeFinderUrl(lake.dowlknum) : null;
  const pdfUrl = stateId === "wi" ? safeHttpsUrl(wiFields?.source_pdf_url) : null;
  const showWi = stateId === "wi";
  return (
    <aside className="panel" id="lake-panel" aria-label="Lake">
      <div className="panel-head">
        <h2>{lake.name}</h2>
        <button type="button" className="tap-target" onClick={onHide} aria-expanded="true" aria-controls="lake-panel">
          Hide
        </button>
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
        {showWi && wiFields?.qa_status && (
          <div>
            <dt>QA status</dt>
            <dd>{qaStatusLabel(wiFields.qa_status)}</dd>
          </div>
        )}
      </dl>
      {finder && (
        <a href={finder} target="_blank" rel={EXTERNAL_REL}>
          LakeFinder report
        </a>
      )}
      {showWi && (
        <p className="note">Traced from WI DNR map</p>
      )}
      {pdfUrl && (
        <a href={pdfUrl} target="_blank" rel={EXTERNAL_REL}>
          Source bathymetry map (PDF)
        </a>
      )}
      {lake.synthetic && <p className="note">Synthetic sample. This id is not a surveyed lake.</p>}
    </aside>
  );
}