# SPDX-License-Identifier: GPL-3.0-only
"""Regression tests for the WI gcp_review HTML shell."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest

from fishy_pipeline.states.wi import gcp_review


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

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_inline_boot_script_parses_lake_data(self):
        """Smoke-test the boot script against a minimal DOM stub in node."""
        rows = [{"wbic": "0157900", "name": "Lake A", "map_key": "m1"}]
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
const document = {{
  createElement() {{ return {{ value: '', textContent: '', appendChild() {{}} }}; }},
  getElementById(id) {{
    if (id === 'lake-data') {{
      return {{ textContent: {json.dumps(lake_json)} }};
    }}
    if (id === 'msg') {{
      const el = {{ textContent: '', style: {{ color: '' }} }};
      Object.defineProperty(el, 'textContent', {{
        get() {{ return el._t || ''; }},
        set(v) {{ el._t = v; }},
      }});
      return el;
    }}
    if (id === 'sheet' || id === 'map') return {canvas_stub};
    if (id === 'lake') {{
      const sel = {{ innerHTML: '', appendChild() {{}}, onchange: null }};
      Object.defineProperty(sel, 'onchange', {{ set(fn) {{ sel._onchange = fn; }}, get() {{ return sel._onchange; }} }});
      return sel;
    }}
    if (id === 'undo' || id === 'save') return {{ onclick: null }};
    return null;
  }},
  querySelector() {{ return {{ innerHTML: '' }}; }},
}};
let fetch = async () => ({{ ok: false, status: 500, json: async () => ({{}}) }});
let Image = function() {{ this.onload = null; this.complete = false; }};
{page_script}
"""
        proc = subprocess.run(
            ["node", "-e", stub],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)