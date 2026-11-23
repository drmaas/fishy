# SPDX-License-Identifier: GPL-3.0-only
"""Regression tests for the WI gcp_review HTML shell."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fishy_pipeline.states.wi import gcp_review
from fishy_pipeline.states.wi.constants import DEFAULT_IOU_THRESHOLD
from fishy_pipeline.states.wi.pilot_data import pilot_wbics
from fishy_pipeline.states.wi.pngutil import RasterImage, write_png


class GcpReviewPageTest(unittest.TestCase):
    def test_lake_data_script_before_reader(self):
        html = gcp_review.render_review_page(
            [{"wbic": "0157900", "name": "Sample", "map_key": "m1"}]
        )
        lake_pos = html.find('<script type="application/json" id="lake-data">')
        reader_pos = html.find("getElementById('lake-data')")
        self.assertGreater(lake_pos, 0)
        self.assertGreater(reader_pos, 0)
        self.assertLess(lake_pos, reader_pos)

    def test_render_inlines_iou_gate_constant(self):
        html = gcp_review.render_review_page([])
        self.assertIn(f"const IOU_GATE = {DEFAULT_IOU_THRESHOLD};", html)
        self.assertNotIn("__IOU_THRESHOLD__", html)

    def test_render_escapes_script_breakout_characters(self):
        rows = [{"wbic": "0999900", "name": "Evil</script><script>", "map_key": "x"}]
        html = gcp_review.render_review_page(rows)
        self.assertNotIn("</script><script>", html)
        self.assertIn("\\u003c/script\\u003e", html)
        match = re.search(
            r'<script type="application/json" id="lake-data">(.*?)</script>',
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        parsed = json.loads(match.group(1))  # type: ignore[union-attr]
        self.assertEqual(parsed[0]["name"], "Evil</script><script>")

    def test_escape_json_for_script_tag(self):
        raw = json.dumps([{"name": "a<b&c>d"}])
        escaped = gcp_review.escape_json_for_script_tag(raw)
        self.assertNotIn("<", escaped)
        self.assertNotIn(">", escaped)
        self.assertNotIn("&", escaped)
        self.assertIn("\\u003c", escaped)
        self.assertIn("\\u003e", escaped)
        self.assertIn("\\u0026", escaped)

    def test_resolve_raster_for_every_pilot_wbic(self):
        wbics = pilot_wbics()
        self.assertGreaterEqual(len(wbics), 10)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            entries = []
            features = []
            for wbic in wbics:
                map_key = f"{wbic}a"
                raster = work / "rasters" / f"{map_key}.png"
                raster.parent.mkdir(parents=True, exist_ok=True)
                write_png(raster, RasterImage(8, 8, bytes([200, 200, 200] * 64)))
                entries.append(
                    {
                        "wbic": wbic,
                        "name": f"Lake {wbic}",
                        "map_key": map_key,
                        "raster_path": str(raster),
                    }
                )
                features.append(
                    {
                        "type": "Feature",
                        "properties": {"WATERBODY_WBIC": int(wbic)},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[0, 0], [100, 0], [100, 100], [0, 0]]],
                        },
                    }
                )
            manifest = work / "manifest.json"
            manifest.write_text(json.dumps({"entries": entries}), encoding="utf-8")
            hydro = work / "pilot-hydro.geojson"
            hydro.write_text(
                json.dumps(
                    {
                        "type": "FeatureCollection",
                        "crs": {
                            "type": "name",
                            "properties": {"name": "urn:ogc:def:crs:EPSG::3071"},
                        },
                        "features": features,
                    }
                ),
                encoding="utf-8",
            )
            ctx = gcp_review.ReviewContext(
                manifest_path=manifest, hydro_path=hydro, work_dir=work
            )
            for wbic in wbics:
                path, _page = ctx.resolve_raster(wbic)
                self.assertTrue(path.is_file(), msg=wbic)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_inline_boot_script_parses_lake_data(self):
        """Smoke-test the boot script against a minimal DOM stub in node."""
        rows = [
            {"wbic": "0157900", "name": "Lake A", "map_key": "m1"},
            {"wbic": "0158000", "name": "Lake B", "map_key": "m2"},
        ]
        html = gcp_review.render_review_page(rows)
        lake_match = re.search(
            r'<script type="application/json" id="lake-data">(.*?)</script>',
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(lake_match)
        lake_json = lake_match.group(1)  # type: ignore[union-attr]
        script_match = re.search(
            r"<script>\n(.*?)\n</script>\s*</body>",
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(script_match)
        page_script = script_match.group(1)  # type: ignore[union-attr]
        canvas_stub = (
            "{ getContext() { return { clearRect() {}, fillRect() {}, drawImage() {}, "
            "fillStyle: '', beginPath() {}, arc() {}, fill() {}, strokeStyle: '', "
            "lineWidth: 0, moveTo() {}, lineTo() {}, closePath() {}, stroke() {} }; }, "
            "width: 800, height: 600, addEventListener() {} }"
        )
        stub = f"""
let drawImageCalls = [];
let rasterUrls = [];
const _byId = {{}};
_byId['lake-data'] = {{ textContent: {json.dumps(lake_json)} }};
const _msg = {{ textContent: '', style: {{ color: '' }} }};
Object.defineProperty(_msg, 'textContent', {{
  get() {{ return _msg._t || ''; }},
  set(v) {{ _msg._t = v; }},
}});
_byId.msg = _msg;
const _lakeSel = {{
  innerHTML: '',
  _children: [],
  appendChild(o) {{ this._children.push(o); }},
  onchange: null,
  get value() {{ return this._v ?? this._children[0]?.value; }},
  set value(v) {{ this._v = v; }},
}};
Object.defineProperty(_lakeSel, 'onchange', {{
  set(fn) {{ this._onchange = fn; }},
  get() {{ return this._onchange; }},
}});
_byId.lake = _lakeSel;
const _sheet = {canvas_stub};
const _sheetCtx = _sheet.getContext();
const _origDraw = _sheetCtx.drawImage.bind(_sheetCtx);
_sheetCtx.drawImage = (...args) => {{ drawImageCalls.push(args.length); _origDraw(...args); }};
_byId.sheet = _sheet;
_byId.map = {canvas_stub};
_byId.undo = {{ onclick: null }};
_byId.save = {{ onclick: null }};
const document = {{
  createElement() {{ return {{ value: '', textContent: '', appendChild() {{}} }}; }},
  getElementById(id) {{ return _byId[id] ?? null; }},
  querySelector() {{ return {{ innerHTML: '' }}; }},
}};
let fetch = async (url) => {{
  const wbic = url.split('/').pop();
  const ring = wbic === '0158000' ? [[0,0],[50,0],[50,50]] : [[0,0],[10,0],[10,10]];
  return {{
    ok: true,
    status: 200,
    json: async () => ({{
      wbic,
      hydro_ring: ring,
      reviewed: null,
      iou_preview: 0.9,
      map_key: 'mk',
      page: 'p.png',
    }}),
  }};
}};
let Image = function() {{
  this.onload = null;
  this.onerror = null;
  this.complete = false;
  this.naturalWidth = 0;
  this.width = 0;
  this.height = 0;
  let _src = '';
  Object.defineProperty(this, 'src', {{
    get() {{ return _src; }},
    set(v) {{
      _src = v;
      rasterUrls.push(v);
      this.width = 100;
      this.height = 80;
      this.naturalWidth = 100;
      this.complete = true;
      if (this.onload) this.onload();
    }},
  }});
}};
{page_script}
const _drawAll = drawAll;
let drawAllCalls = 0;
drawAll = function() {{ drawAllCalls += 1; _drawAll(); }};
;(async () => {{
  if (_lakeSel._children.length !== 2) {{
    throw new Error('expected 2 lake options, got ' + _lakeSel._children.length);
  }}
  await new Promise((r) => setTimeout(r, 0));
  wbic = '0158000';
  _lakeSel.value = '0158000';
  await loadLake();
  await new Promise((r) => setTimeout(r, 5));
  if (rasterUrls.length < 2) throw new Error('raster src not updated on lake switch');
  const last = rasterUrls[rasterUrls.length - 1];
  if (!last.includes('/api/raster/0158000')) throw new Error('second lake raster url: ' + last);
  if (drawAllCalls < 2) throw new Error('drawAll not invoked for lake switch, calls=' + drawAllCalls);
}})().then(() => process.exit(0)).catch((e) => {{ console.error(e); process.exit(1); }});
"""
        proc = subprocess.run(
            ["node", "-e", stub],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)