# SPDX-License-Identifier: GPL-3.0-only
"""Local-only georeferencing review UI for Wisconsin lake sheets."""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fishy_pipeline.states.wi.hydro import load_hydro_polygons
from fishy_pipeline.states.wi.manifest import read_manifest
from fishy_pipeline.states.wi.pilot_data import (
    REVIEWED_GCP_DIR,
    load_reviewed_gcps,
    pilot_wbics,
    save_reviewed_gcps,
)
from fishy_pipeline.states.wi.pilot_setup import PILOT_HYDRO_NAME, PILOT_SETUP_CLI
from fishy_pipeline.states.wi.register import GroundControlPoint, registration_from_reviewed
from fishy_pipeline.states.wi.shoreline import extract_shoreline
from fishy_pipeline.states.wi.pngutil import read_png
from fishy_pipeline.states.wi.wbic import normalize_wbic

WI_CRS = "EPSG:3071"
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_WORK = REPO_ROOT / "pipeline" / "work" / "wi"

PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>WI georef review</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 1rem; }
    .row { display: flex; gap: 1rem; flex-wrap: wrap; align-items: flex-start; }
    canvas { border: 1px solid #888; max-width: 48vw; cursor: crosshair; }
    #map { background: #e8f0f8; }
    select, button { font-size: 1rem; margin: 0.25rem 0.5rem 0.25rem 0; }
    table { border-collapse: collapse; }
    td, th { border: 1px solid #ccc; padding: 0.25rem 0.5rem; }
  </style>
</head>
<body>
  <h1>WI bathymetry georef review</h1>
  <p>Click the sheet for pixel, the map for EPSG:3071. Save at least four pairs. Runs on localhost only.</p>
  <label>Lake <select id="lake"></select></label>
  <button id="undo">Undo last</button>
  <button id="save">Save GCPs</button>
  <span id="msg"></span>
  <div class="row">
    <canvas id="sheet" width="800" height="600"></canvas>
    <canvas id="map" width="800" height="600"></canvas>
  </div>
  <table id="pairs"><thead><tr><th>#</th><th>pixel</th><th>map</th></tr></thead><tbody></tbody></table>
<script type="application/json" id="lake-data">__LAKE_JSON__</script>
<script>
let lakes = [];
try {
  const lakeDataEl = document.getElementById('lake-data');
  if (!lakeDataEl) throw new Error('lake-data element not found');
  lakes = JSON.parse(lakeDataEl.textContent);
} catch (err) {
  const boot = document.getElementById('msg');
  boot.textContent = 'Failed to load lake list: ' + err.message;
  boot.style.color = '#c00';
}
let wbic = lakes[0]?.wbic;
let sheet = document.getElementById('sheet');
let map = document.getElementById('map');
let sh = sheet.getContext('2d');
let mh = map.getContext('2d');
let meta = null;
let pairs = [];
let pendingPixel = null;
let sheetImg = new Image();

function fillLakeSelect() {
  const sel = document.getElementById('lake');
  sel.innerHTML = '';
  for (const row of lakes) {
    const o = document.createElement('option');
    o.value = row.wbic;
    o.textContent = row.name + ' (' + row.wbic + ')';
    sel.appendChild(o);
  }
  sel.onchange = () => { wbic = sel.value; loadLake(); };
}

async function loadLake() {
  const msg = document.getElementById('msg');
  if (!wbic) {
    msg.textContent = 'No lake selected';
    msg.style.color = '#c00';
    return;
  }
  try {
    const res = await fetch('/api/lake/' + wbic);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    meta = await res.json();
    pairs = (meta.reviewed && meta.reviewed.gcps) ? meta.reviewed.gcps.map(g => ({pixel:[g.pixel,g.line], map:[g.easting,g.northing]})) : [];
    sheetImg.onload = () => drawAll();
    sheetImg.src = '/api/raster/' + wbic + '?' + Date.now();
    drawMapFrame();
    renderTable();
    msg.style.color = '';
    msg.textContent = meta.iou_preview != null ? ('Preview IoU ' + meta.iou_preview.toFixed(3)) : '';
  } catch (err) {
    msg.textContent = 'Failed to load lake: ' + err.message;
    msg.style.color = '#c00';
  }
}

function drawMapFrame() {
  if (!meta || !meta.hydro_ring) return;
  const xs = meta.hydro_ring.map(p => p[0]);
  const ys = meta.hydro_ring.map(p => p[1]);
  meta.bounds = { minx: Math.min(...xs), maxx: Math.max(...xs), miny: Math.min(...ys), maxy: Math.max(...ys) };
  drawAll();
}

function xyMap(e, canvas) {
  const r = canvas.getBoundingClientRect();
  const scaleX = canvas.width / r.width;
  const scaleY = canvas.height / r.height;
  return [(e.clientX - r.left) * scaleX, (e.clientY - r.top) * scaleY];
}

function mapToScreen(x, y) {
  const b = meta.bounds;
  const pad = 30;
  const w = map.width - pad * 2;
  const h = map.height - pad * 2;
  const sx = pad + (x - b.minx) / (b.maxx - b.minx) * w;
  const sy = pad + (b.maxy - y) / (b.maxy - b.miny) * h;
  return [sx, sy];
}

function screenToMap(px, py) {
  const b = meta.bounds;
  const pad = 30;
  const w = map.width - pad * 2;
  const h = map.height - pad * 2;
  const easting = b.minx + (px - pad) / w * (b.maxx - b.minx);
  const northing = b.maxy - (py - pad) / h * (b.maxy - b.miny);
  return [easting, northing];
}

function drawAll() {
  if (!meta) return;
  sh.clearRect(0,0,sheet.width,sheet.height);
  if (sheetImg.complete) {
    const sc = Math.min(sheet.width / sheetImg.width, sheet.height / sheetImg.height, 1);
    const w = sheetImg.width * sc;
    const h = sheetImg.height * sc;
    sh.drawImage(sheetImg, 0, 0, w, h);
    meta.sheetScale = { sc, w, h };
  }
  if (meta.sheetScale) {
    for (const p of pairs) {
      const x = p.pixel[0] * meta.sheetScale.sc;
      const y = p.pixel[1] * meta.sheetScale.sc;
      sh.fillStyle = '#e11';
      sh.beginPath(); sh.arc(x, y, 5, 0, Math.PI*2); sh.fill();
    }
  }
  mh.clearRect(0,0,map.width,map.height);
  mh.fillStyle = '#e8f0f8';
  mh.fillRect(0,0,map.width,map.height);
  if (meta.hydro_ring) {
    mh.strokeStyle = '#06c';
    mh.lineWidth = 2;
    mh.beginPath();
    meta.hydro_ring.forEach((p, i) => {
      const [x,y] = mapToScreen(p[0], p[1]);
      if (i === 0) mh.moveTo(x,y); else mh.lineTo(x,y);
    });
    mh.closePath();
    mh.stroke();
  }
  for (const p of pairs) {
    const [x,y] = mapToScreen(p.map[0], p.map[1]);
    mh.fillStyle = '#e11';
    mh.beginPath(); mh.arc(x, y, 5, 0, Math.PI*2); mh.fill();
  }
}

sheet.addEventListener('click', (e) => {
  if (!meta?.sheetScale) return;
  const [px, py] = xyMap(e, sheet);
  const col = px / meta.sheetScale.sc;
  const row = py / meta.sheetScale.sc;
  pendingPixel = [col, row];
  document.getElementById('msg').textContent = 'Now click the matching map location';
});

map.addEventListener('click', (e) => {
  if (!pendingPixel || !meta) return;
  const [px, py] = xyMap(e, map);
  const [e0, n0] = screenToMap(px, py);
  pairs.push({ pixel: pendingPixel, map: [e0, n0] });
  pendingPixel = null;
  renderTable();
  drawAll();
  previewIou();
});

async function previewIou() {
  if (pairs.length < 4) return;
  const res = await fetch('/api/preview/' + wbic, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ gcps: pairs.map(p => ({ pixel: p.pixel[0], line: p.pixel[1], easting: p.map[0], northing: p.map[1] })) })});
  const body = await res.json();
  document.getElementById('msg').textContent = 'Preview IoU ' + (body.iou != null ? body.iou.toFixed(3) : 'n/a');
}

function renderTable() {
  const tb = document.querySelector('#pairs tbody');
  tb.innerHTML = '';
  pairs.forEach((p, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td>' + (i+1) + '</td><td>' + p.pixel.map(v=>v.toFixed(1)).join(', ') + '</td><td>' + p.map.map(v=>v.toFixed(1)).join(', ') + '</td>';
    tb.appendChild(tr);
  });
}

document.getElementById('undo').onclick = () => { pairs.pop(); pendingPixel=null; renderTable(); drawAll(); };
document.getElementById('save').onclick = async () => {
  if (pairs.length < 4) { document.getElementById('msg').textContent = 'Need four GCPs'; return; }
  const res = await fetch('/api/lake/' + wbic, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({
    crs: 'EPSG:3071', map_key: meta.map_key, page: meta.page, method: 'tps',
    gcps: pairs.map(p => ({ pixel: p.pixel[0], line: p.pixel[1], easting: p.map[0], northing: p.map[1] }))
  })});
  const body = await res.json();
  document.getElementById('msg').textContent = 'Saved ' + body.path + (body.iou != null ? ' IoU '+body.iou.toFixed(3) : '');
};

if (lakes.length) {
  fillLakeSelect();
  loadLake();
} else if (!document.getElementById('msg').textContent) {
  document.getElementById('msg').textContent = 'No lakes in manifest';
  document.getElementById('msg').style.color = '#c00';
}
</script>
</body>
</html>
"""


def escape_json_for_script_tag(payload: str) -> str:
    """Escape characters that could break out of an inline ``<script>`` tag."""
    return payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_review_page(lake_rows: list[dict]) -> str:
    lakes_json = escape_json_for_script_tag(json.dumps(lake_rows))
    return PAGE_HTML.replace("__LAKE_JSON__", lakes_json)


class ReviewContext:
    def __init__(self, *, manifest_path: Path, hydro_path: Path, work_dir: Path) -> None:
        self.manifest_path = manifest_path
        self.hydro_path = hydro_path
        self.work_dir = work_dir
        self.manifest = read_manifest(manifest_path)
        self.polygons = load_hydro_polygons(hydro_path)
        self.entries = {
            normalize_wbic(e["wbic"]): e
            for e in self.manifest.get("entries", [])
            if normalize_wbic(e.get("wbic"))
        }

    def lake_rows(self) -> list[dict]:
        rows = []
        for wbic, entry in sorted(self.entries.items()):
            rows.append({"wbic": wbic, "name": entry.get("name") or wbic, "map_key": entry.get("map_key")})
        return rows

    def resolve_raster(self, wbic: str) -> tuple[Path, str]:
        entry = self.entries[wbic]
        reviewed = load_reviewed_gcps(wbic)
        page = reviewed.get("page") if reviewed else None
        pages = entry.get("raster_pages") or []
        if page:
            for item in pages:
                if isinstance(item, dict) and Path(str(item.get("path", ""))).name == page:
                    return Path(str(item["path"])), page
        if pages:
            item = pages[0]
            path = item.get("path") if isinstance(item, dict) else item
            return Path(str(path)), Path(str(path)).name
        raster = entry.get("raster_path") or self.work_dir / "rasters" / f"{entry['map_key']}.png"
        return Path(str(raster)), Path(str(raster)).name

    def preview_iou(self, wbic: str, gcps: list[dict], method: str = "tps") -> float | None:
        water = self.polygons.get(wbic)
        if water is None:
            return None
        raster, _page = self.resolve_raster(wbic)
        if not raster.is_file():
            return None
        ring = extract_shoreline(read_png(raster))
        if not ring:
            return None
        points = [
            GroundControlPoint(
                pixel=float(g["pixel"]),
                line=float(g["line"]),
                easting=float(g["easting"]),
                northing=float(g["northing"]),
            )
            for g in gcps
        ]
        reg = registration_from_reviewed(ring, list(water.ring), points, method=method)
        return reg.iou


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fishy_pipeline.states.wi.gcp_review")
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--manifest", type=Path, help="ingest manifest JSON")
    parser.add_argument(
        "--hydro",
        type=Path,
        help=f"24K Hydro GeoJSON in EPSG:3071 (default: <work-dir>/{PILOT_HYDRO_NAME})",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="bind to a non-loopback --host (default refuses)",
    )
    return parser


def _loopback_host(host: str) -> bool:
    name = host.strip().lower()
    if name in {"127.0.0.1", "localhost", "::1"}:
        return True
    if name.startswith("127."):
        return True
    return False


def serve(context: ReviewContext, *, host: str, port: int) -> None:

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            return

        def _send_json(self, code: int, body: dict) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/lakes":
                self._send_json(200, {"lakes": context.lake_rows()})
                return
            if parsed.path == "/" or parsed.path == "/index.html":
                page = render_review_page(context.lake_rows())
                payload = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if parsed.path.startswith("/api/lake/"):
                wbic = normalize_wbic(parsed.path.rsplit("/", 1)[-1])
                if wbic is None or wbic not in context.entries:
                    self._send_json(404, {"error": "unknown lake"})
                    return
                entry = context.entries[wbic]
                water = context.polygons.get(wbic)
                reviewed = load_reviewed_gcps(wbic)
                raster, page = context.resolve_raster(wbic)
                body = {
                    "wbic": wbic,
                    "map_key": entry.get("map_key"),
                    "page": page,
                    "crs": WI_CRS,
                    "hydro_ring": list(water.ring) if water else [],
                    "reviewed": reviewed,
                    "iou_preview": reviewed and context.preview_iou(wbic, reviewed["gcps"]) if reviewed else None,
                    "raster_url": f"/api/raster/{wbic}",
                }
                self._send_json(200, body)
                return
            if parsed.path.startswith("/api/raster/"):
                wbic = normalize_wbic(parsed.path.rsplit("/", 1)[-1])
                if wbic is None or wbic not in context.entries:
                    self.send_error(404)
                    return
                raster, _ = context.resolve_raster(wbic)
                if not raster.is_file():
                    self.send_error(404)
                    return
                data = raster.read_bytes()
                mime = mimetypes.guess_type(str(raster))[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_error(404)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid json"})
                return
            if parsed.path.startswith("/api/preview/"):
                wbic = normalize_wbic(parsed.path.rsplit("/", 1)[-1])
                if wbic is None or wbic not in context.entries:
                    self._send_json(404, {"error": "unknown lake"})
                    return
                gcps = payload.get("gcps") or []
                iou = context.preview_iou(wbic, gcps)
                self._send_json(200, {"iou": iou})
                return
            if parsed.path.startswith("/api/lake/"):
                wbic = normalize_wbic(parsed.path.rsplit("/", 1)[-1])
                if wbic is None or wbic not in context.entries:
                    self._send_json(404, {"error": "unknown lake"})
                    return
                path = save_reviewed_gcps(wbic, payload)
                iou = context.preview_iou(wbic, payload.get("gcps") or [])
                self._send_json(200, {"path": str(path.relative_to(REPO_ROOT)), "iou": iou})
                return
            self._send_json(404, {"error": "not found"})

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"WI georef review at http://{host}:{port}/ (localhost only)", flush=True)
    print(f"Committed GCPs: {REVIEWED_GCP_DIR}", flush=True)
    server.serve_forever()


def _require_pilot_work_dir(manifest_path: Path, hydro_path: Path) -> None:
    if not manifest_path.is_file():
        raise SystemExit(
            f"manifest not found: {manifest_path}\n\n"
            f"From the repository root, bootstrap the pilot work tree:\n"
            f"  {PILOT_SETUP_CLI}\n"
        )
    manifest = read_manifest(manifest_path)
    entries = manifest.get("entries") or []
    if not entries:
        raise SystemExit(
            f"manifest has no lakes: {manifest_path}\n\n"
            f"From the repository root, bootstrap the pilot work tree:\n"
            f"  {PILOT_SETUP_CLI}\n"
        )
    found = {
        normalize_wbic(entry.get("wbic"))
        for entry in entries
        if normalize_wbic(entry.get("wbic"))
    }
    missing = [wbic for wbic in pilot_wbics() if wbic not in found]
    if missing:
        raise SystemExit(
            f"manifest is missing pilot lakes: {', '.join(missing)}\n\n"
            f"Re-run pilot setup (safe to repeat):\n"
            f"  {PILOT_SETUP_CLI}\n"
        )
    if not hydro_path.is_file():
        raise SystemExit(
            f"hydro not found: {hydro_path}\n\n"
            f"From the repository root, bootstrap the pilot work tree:\n"
            f"  {PILOT_SETUP_CLI}\n"
        )


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    manifest_path = args.manifest or (args.work_dir / "manifest.json")
    hydro_path = args.hydro or (args.work_dir / PILOT_HYDRO_NAME)
    _require_pilot_work_dir(manifest_path, hydro_path)
    if not args.allow_remote and not _loopback_host(args.host):
        raise SystemExit(f"refusing non-loopback --host {args.host!r}; pass --allow-remote")
    context = ReviewContext(manifest_path=manifest_path, hydro_path=hydro_path, work_dir=args.work_dir)
    if not context.lake_rows():
        raise SystemExit(
            f"no lakes available for review (manifest: {manifest_path})\n\n"
            f"  {PILOT_SETUP_CLI}\n"
        )
    serve(context, host=args.host, port=args.port)


if __name__ == "__main__":
    main(sys.argv[1:])