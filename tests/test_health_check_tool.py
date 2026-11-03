"""Test untuk tools/panel-health-check.py (stdlib, tanpa dependensi).

Menjalankan server HTTP mini di thread lalu memeriksa:
- exit 0 + ringkasan SEHAT saat semua endpoint sehat;
- exit 1 saat ada endpoint yang gagal (mis. fragmen wajib hilang);
- mode --json menghasilkan struktur yang bisa di-parse;
- panel tak terjangkau dilaporkan sebagai kegagalan, bukan traceback.
"""

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "panel_health_check", os.path.join(ROOT, "tools", "panel-health-check.py"))
health = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(health)

BIG = b"x" * (health.MIN_STATUS_BYTES + 1024)


class _Handler(BaseHTTPRequestHandler):
    # Dikonfigurasi per-test lewat atribut kelas server.
    fragments = None
    status_body = BIG
    series = None

    def log_message(self, *args):  # senyap
        pass

    def do_GET(self):
        if self.path == "/status":
            self._send(200, self.status_body, "text/html")
        elif self.path == "/api/status":
            self._send(200, json.dumps(self.fragments or {}).encode(), "application/json")
        elif self.path == "/api/perf/series":
            self._send(200, json.dumps(self.series or {}).encode(), "application/json")
        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"event: update\n")
            self.wfile.write(b'data: {"cpu_pct": 12.5}\n\n')
            self.wfile.flush()
        else:
            self._send(404, b"no", "text/plain")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestHealthCheckTool(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        _Handler.fragments = {k: "<div/>" for k in health.REQUIRED_FRAGMENTS}
        _Handler.status_body = BIG
        _Handler.series = {"ok": True, "count": 5, "cpu": [1, 2, 3, 4, 5]}

    def test_healthy_panel_exits_zero(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(health.main(["--base", self.base, "--timeout", "5"]), 0)
        result = health.run(self.base, 5, 2.5)
        self.assertTrue(result["ok"])
        self.assertEqual(result["failed"], 0)
        self.assertEqual(len(result["checks"]), 4)

    def test_missing_fragment_fails(self):
        _Handler.fragments = {"cpu_pct": "<div/>"}
        result = health.run(self.base, 5, 2.5)
        self.assertFalse(result["ok"])
        failing = [c["check"] for c in result["checks"] if not c["ok"]]
        self.assertIn("GET /api/status", failing)

    def test_small_status_page_is_suspicious(self):
        _Handler.status_body = b"<html>kecil</html>"
        result = health.run(self.base, 5, 2.5)
        self.assertFalse(result["ok"])
        self.assertIn("GET /status", [c["check"] for c in result["checks"] if not c["ok"]])

    def test_bad_series_shape_fails(self):
        _Handler.series = {"ok": False}
        result = health.run(self.base, 5, 2.5)
        self.assertIn("GET /api/perf/series",
                      [c["check"] for c in result["checks"] if not c["ok"]])

    def test_unreachable_panel_is_reported_not_raised(self):
        result = health.run("http://127.0.0.1:9", 2, 2.5)  # port discard
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed"], len(result["checks"]))

    def test_json_output_is_parseable(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = health.main(["--base", self.base, "--json", "--timeout", "5"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["base"], self.base)


if __name__ == "__main__":
    unittest.main()
