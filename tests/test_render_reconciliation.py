"""Regression tests for server-fragment/SSE-client reconciliation."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from contextlib import ExitStack
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PANEL_TOKEN", "render-audit-token")
os.environ.setdefault("PANEL_PASSWORD", "render-audit-password")
spec = importlib.util.spec_from_file_location(
    "panel_render_audit", REPO_ROOT / "dashboard-toggle-server.py"
)
panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(panel)
panel._run_hermes_cli = lambda *args, **kwargs: (-1, "disabled in tests")


class TestRenderReconciliation(unittest.TestCase):
    def _mock_fragment_dependencies(self, stack):
        values = {
            "is_dashboard_active": False,
            "service_active": False,
            "get_router_status": (False, 0.0, "router.local"),
            "get_router_uptime": "",
            "get_current_model": "model-a",
            "get_ram_info": (1.0, "1%"),
            "get_cpu_temp": (1.0, "1 C"),
            "get_disk_info": "1%",
            "get_disk_pct": 1.0,
            "get_uptime": "1h",
            "get_server_ips": ("", ""),
            "get_internet_status": (False, 0.0, "offline"),
            "get_emmc_health": ("up", "OK"),
            "get_zram_info": "OK",
            "get_gateway_info": "Mati",
            "get_gateway_platforms_summary": ("", "badge-muted", ""),
            "render_gateway_platforms_html": "",
            "render_gateway_log_card": "",
            "get_available_models_cached": {},
            "get_all_configured_providers": [],
            "get_router_update_result": {"status": "idle", "summary": ""},
            "tail_update_log": "",
            "get_hermes_update": {
                "status": "current", "local": "1", "remote": "1",
                "behind": 0, "channel": "",
            },
            "get_hermes_update_result": {
                "status": "idle", "running": False, "summary": "",
            },
            "tail_hermes_update_log": "",
            "get_cpu_percent": 1.0,
            "render_processes_table": "<table></table>",
            "render_clean_junk_card": "",
            "render_profiles_block": "<div></div>",
            "get_reasoning_effort": "low",
            "get_rate_limited_providers": [],
            "get_update_status": "available",
            "get_cached_router_info": {
                "version": "1<installed>", "latest_version": "2&latest",
                "npm_latest": "3\"npm", "npm_ahead": True,
            },
            "get_cached_router_version": "1",
            "get_9router_patch_notes": [],
            "render_patch_notes_block": "",
            "get_hermes_patch_notes": [],
            "get_9router_public_url": "http://router.local",
            "get_openclaw_open_block": "",
            "is_openclaw_control_ui_enabled": False,
            "render_aux_tasks_block": "",
            "render_backup_models_block": "",
            "render_docker_apps_block": "",
            "render_auth_setting_block": "",
        }
        for name, value in values.items():
            stack.enter_context(mock.patch.object(panel, name, return_value=value))
        stack.enter_context(mock.patch.object(panel.os.path, "isfile", return_value=True))
        stack.enter_context(mock.patch.object(panel.os.path, "exists", return_value=False))
        stack.enter_context(mock.patch.object(panel, "_ttl_cached", side_effect=lambda *args: ""))

    def test_update_block_live_payload_and_escaped_versions(self):
        with ExitStack() as stack:
            self._mock_fragment_dependencies(stack)
            full = panel.build_fragments()
            sse = panel.build_fragments(sse_only=True)
            payload = json.loads(panel._sse_payload(full))
        self.assertIn("update_block", sse)
        self.assertIn("update_block", payload)
        self.assertEqual(sse["update_block"], full["update_block"])
        self.assertIn("v1&lt;installed&gt;", full["update_block"])
        self.assertIn("v2&amp;latest", full["update_block"])
        self.assertNotIn("v1<installed>", full["update_block"])
        self.assertNotIn("v2&latest", full["update_block"])

    def test_patch_notes_fallback_is_cached_for_sse(self):
        with mock.patch.object(panel, "_router_patch_notes_cache", {"at": 0, "notes": []}), \
             mock.patch.object(panel, "get_cached_router_info", return_value={}), \
             mock.patch.object(panel.urllib.request, "urlopen", side_effect=OSError("offline")) as fetch:
            first = panel.get_9router_patch_notes()
            second = panel.get_9router_patch_notes()
        self.assertEqual(first, second)
        self.assertTrue(first)
        self.assertEqual(fetch.call_count, 1)

    def test_null_pid_uses_placeholder(self):
        process = [{
            "name": "x", "kind": "test", "status": "Berhenti",
            "is_active": False, "installed": True, "pid": None,
            "mem_mb": None, "stop_url": "", "start_url": "", "restart_url": "",
        }]
        with mock.patch.object(panel, "get_process_list", return_value=process):
            html = panel._render_processes_table_uncached()
        self.assertIn('<span class="td-val">-</span>', html)
        self.assertNotIn('<span class="td-val">None</span>', html)

    def test_rate_limit_card_tolerates_null_fields(self):
        rows = [{"provider": None, "name": None, "error_at": None, "error": None}]
        with ExitStack() as stack:
            self._mock_fragment_dependencies(stack)
            stack.enter_context(mock.patch.object(
                panel, "get_rate_limited_providers", return_value=rows,
            ))
            card = panel.build_fragments()["rate_limit_card"]
        self.assertIn("UNKNOWN · unknown", card)
        self.assertIn("--:--:-- WIB", card)
        self.assertNotIn("None", card)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_sse_reconciles_empty_and_deferred_fragments(self):
        src = panel.SSE_SCRIPT

        def between(start, end):
            i = src.index(start)
            return src[i:src.index(end, i)]

        helpers = "\n".join([
            between("function set(id,v)", "var _scrolling"),
            between("function stickySet(id,v)", "function apply(d)"),
            between("function apply(d)", "function setLive"),
        ])
        # Fake DOM deliberately mirrors innerHTML reconciliation. All assigned
        # values below are fixed test strings, never untrusted input.
        harness = r"""
var interacting = false;
function mkEl(id){
  var e = {id:id, _html:'', firstChild:null, style:{}};
  Object.defineProperty(e, 'innerHTML', {
    get:function(){ return this._html; },
    set:function(v){ this._html=String(v); this.firstChild={html:this._html}; }
  });
  e.contains=function(){ return interacting; };
  e.matches=function(){ return interacting; };
  e.querySelector=function(){ return null; };
  e.querySelectorAll=function(){ return []; };
  return e;
}
var els = {
  'process-table-slot':mkEl('process-table-slot'),
  'log-slot':mkEl('log-slot'),
  'hermes-log-slot':mkEl('hermes-log-slot'),
  'clean-log-slot':mkEl('clean-log-slot'),
  'gateway-log-slot':mkEl('gateway-log-slot'),
  'update-content-slot':mkEl('update-content-slot')
};
els['process-table-slot'].innerHTML='OLD_PROCESS';
els['log-slot'].innerHTML='OLD_ROUTER';
els['hermes-log-slot'].innerHTML='OLD_HERMES';
els['clean-log-slot'].innerHTML='OLD_CLEAN';
els['gateway-log-slot'].innerHTML='OLD_GATEWAY';
els['update-content-slot'].innerHTML='OLD_UPDATE';
var document = {
  activeElement:{},
  getElementById:function(id){return els[id]||null;},
  createElement:function(){return mkEl('temp');}
};
var window = {
  _lastRouterLogCard:'OLD_ROUTER', _hasRouterLog:true,
  _lastHermesLogCard:'OLD_HERMES', _hasHermesLog:true,
  _lastCleanJunkCard:'OLD_CLEAN', _hasCleanLog:true,
  _lastGatewayLogCard:'OLD_GATEWAY', _hasGatewayLog:true
};
function safeStore(){ return null; }
function updateSparkline(){}
function syncLogUI(){}
function restorePatchPages(){}
"""
        body = r"""
interacting=true;
apply({processes_table:'NEW_PROCESS'});
var afterBlocked=els['process-table-slot'].innerHTML;
interacting=false;
apply({});
var afterDeltaOmission=els['process-table-slot'].innerHTML;
var routerAfterOmission={
  html:els['log-slot'].innerHTML,
  last:window._lastRouterLogCard,
  has:window._hasRouterLog
};
apply({
  log_card:'',
  hermes_log_card:'',
  clean_junk_card:null,
  gateway_log_card:''
});
var routerAfterEmpty={
  html:els['log-slot'].innerHTML,
  last:window._lastRouterLogCard,
  has:window._hasRouterLog
};
var hermesAfterEmpty={
  html:els['hermes-log-slot'].innerHTML,
  last:window._lastHermesLogCard,
  has:window._hasHermesLog
};
var cleanAfterEmpty={
  html:els['clean-log-slot'].innerHTML,
  last:window._lastCleanJunkCard,
  has:window._hasCleanLog
};
var gatewayAfterEmpty={
  html:els['gateway-log-slot'].innerHTML,
  last:window._lastGatewayLogCard,
  has:window._hasGatewayLog
};
apply({update_block:'NEW_UPDATE'});
process.stdout.write(JSON.stringify({
  afterBlocked:afterBlocked,
  afterDeltaOmission:afterDeltaOmission,
  routerAfterOmission:routerAfterOmission,
  routerAfterEmpty:routerAfterEmpty,
  hermesAfterEmpty:hermesAfterEmpty,
  cleanAfterEmpty:cleanAfterEmpty,
  gatewayAfterEmpty:gatewayAfterEmpty,
  updateAfter:els['update-content-slot'].innerHTML
}));
"""
        run = subprocess.run(
            ["node", "-e", harness + helpers + body],
            text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["afterBlocked"], "OLD_PROCESS")
        self.assertEqual(result["afterDeltaOmission"], "NEW_PROCESS")
        self.assertEqual(
            result["routerAfterOmission"],
            {"html": "OLD_ROUTER", "last": "OLD_ROUTER", "has": True},
        )
        for key in ("routerAfterEmpty", "hermesAfterEmpty", "cleanAfterEmpty", "gatewayAfterEmpty"):
            with self.subTest(fragment=key):
                self.assertEqual(result[key], {"html": "", "last": "", "has": False})
        self.assertEqual(result["updateAfter"], "NEW_UPDATE")


if __name__ == "__main__":
    unittest.main()


class TestHermesUpdateBlockLive(TestRenderReconciliation):
    """Kartu update Hermes tidak boleh berhenti di 'Mengecek pembaruan Hermes…'.

    Akar masalah: fragmen dihitung SETELAH early return sse_only dan tidak ada di
    SSE_CLIENT_KEYS, jadi hasil cek hanya terlihat setelah reload penuh. Slot log
    juga dulu bersarang di dalam blok update, sehingga ikut tertimpa bila blok
    dikirim utuh.
    """

    def test_sse_carries_hermes_update_block_not_stuck_checking(self):
        with ExitStack() as stack:
            self._mock_fragment_dependencies(stack)
            stack.enter_context(mock.patch.object(panel, "get_hermes_update", return_value={
                "status": "available", "local": "0.22.5",
                "remote": "stable v0.22.6 (f0d94882e89a)", "behind": 3,
                "channel": "stable",
            }))
            sse = panel.build_fragments(sse_only=True)
            full = panel.build_fragments()
            payload = json.loads(panel._sse_payload(sse))
        self.assertIn("hermes_update_block", sse)
        self.assertIn("hermes_update_block", payload)
        self.assertEqual(sse["hermes_update_block"], full["hermes_update_block"])
        self.assertIn("Pembaruan tersedia", sse["hermes_update_block"])
        self.assertNotIn("Mengecek pembaruan Hermes", sse["hermes_update_block"])
        self.assertNotIn('id="hermes-log-slot"', sse["hermes_update_block"])

    def test_checking_transitions_to_current_via_delta(self):
        with ExitStack() as stack:
            self._mock_fragment_dependencies(stack)
            panel._sse_last_keys = {}
            stack.enter_context(mock.patch.object(panel, "get_hermes_update", return_value={
                "status": "checking", "local": "?", "remote": "?",
                "behind": 0, "channel": "",
            }))
            checking = json.loads(panel._sse_payload(panel.build_fragments(sse_only=True), delta=True))
            self.assertIn("Mengecek pembaruan Hermes", checking["hermes_update_block"])
            stack.enter_context(mock.patch.object(panel, "get_hermes_update", return_value={
                "status": "current", "local": "0.22.6", "remote": "0.22.6",
                "behind": 0, "channel": "stable",
            }))
            settled = json.loads(panel._sse_payload(panel.build_fragments(sse_only=True), delta=True))
        self.assertIn("hermes_update_block", settled)
        self.assertIn("sudah versi terbaru", settled["hermes_update_block"])

    def test_page_slots_and_js_apply(self):
        self.assertEqual(panel.PAGE.count('id="hermes-log-slot"'), 1)
        self.assertEqual(panel.PAGE.count('id="hermes-update-slot"'), 1)
        self.assertIn("hermes_update_block", panel.SSE_CLIENT_KEYS)
        js = panel.PAGE + getattr(panel, "SSE_SCRIPT", "")
        self.assertIn("set('hermes-update-slot', d.hermes_update_block)", js)


class TestPerfSparklineHistory(TestRenderReconciliation):
    """Riwayat performa: ring buffer server + seed klien tanpa data dummy.

    Negatif kontrol (pada kode tanpa patch): seluruh test kelas ini gagal —
    `_record_perf_sample` belum ada, seed dummy `[10, 15, 12, ...]` masih ada,
    dan label min/maks belum dirender.
    """

    def setUp(self):
        with panel._perf_ring_lock:
            panel._perf_ring.clear()
            self._saved_last = panel._perf_ring_last_sample
            panel._perf_ring_last_sample = 0.0
        self._t = [1000.0]

    def tearDown(self):
        with panel._perf_ring_lock:
            panel._perf_ring.clear()
            panel._perf_ring_last_sample = self._saved_last

    def test_record_respects_min_interval_and_rounds(self):
        with mock.patch.object(panel.time, "time", side_effect=lambda: self._t[0]):
            panel._record_perf_sample(33.456, 51.23, 44.56)
            panel._record_perf_sample(90.0, 90.0, 90.0)  # < interval → diabaikan
            self._t[0] += panel._PERF_SAMPLE_MIN_INTERVAL
            panel._record_perf_sample(10.05, 20.04, None)
        series = panel.get_perf_series()
        self.assertEqual(series["count"], 2)
        self.assertEqual(series["cpu"], [33.5, 10.1])
        self.assertEqual(series["ram"], [51.2, 20.0])
        self.assertEqual(series["temp"], [44.6, None])
        self.assertTrue(series["ok"])
        self.assertGreater(series["at"], 0)

    def test_ring_is_capped(self):
        t = 1000.0
        for i in range(panel._PERF_RING_MAX + 40):
            with mock.patch.object(panel.time, "time", return_value=t):
                panel._record_perf_sample(float(i), float(i), None)
            t += panel._PERF_SAMPLE_MIN_INTERVAL
        series = panel.get_perf_series()
        self.assertEqual(series["count"], panel._PERF_RING_MAX)
        self.assertEqual(series["cpu"][0], 40.0)  # titik tertua terpangkas

    def test_get_perf_series_empty(self):
        series = panel.get_perf_series()
        self.assertEqual(series, {"ok": True, "count": 0, "cpu": [], "ram": [], "temp": [], "at": 0.0})

    def test_build_fragments_records_sample(self):
        with ExitStack() as stack:
            self._mock_fragment_dependencies(stack)
            with mock.patch.object(panel, "_record_perf_sample") as rec:
                panel.build_fragments(sse_only=True)
        rec.assert_called_once()

    def test_api_route_and_js_seed_present(self):
        src = panel.PAGE + getattr(panel, "SSE_SCRIPT", "")
        self.assertIn("/api/perf/series", src)
        self.assertIn("function seedPerfHistory()", src)
        # Seed dummy HARUS hilang: tren palsu ±30 dtk pertama setelah reload.
        self.assertNotIn("var cpuHistory = [10, 15, 12, 18", src)
        self.assertNotIn("var ramHistory = [30, 30, 30, 31", src)
        self.assertIn("var cpuHistory = [];", src)
        self.assertIn("var ramHistory = [];", src)
        # Label min/maks/sekarang + slot meta.
        self.assertIn("cpu-spark-meta", panel.PAGE)
        self.assertIn("ram-spark-meta", panel.PAGE)
        self.assertIn("· sekarang ", src)
        # Lazy: seed hanya saat tab Performa aktif.
        self.assertIn("maybeSeedPerf", src)
        self.assertIn("tab-performance", src)
