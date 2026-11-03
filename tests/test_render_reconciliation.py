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
  'hermes-log-slot':mkEl('hermes-log-slot'),
  'update-content-slot':mkEl('update-content-slot')
};
els['process-table-slot'].innerHTML='OLD_PROCESS';
els['hermes-log-slot'].innerHTML='OLD_HERMES';
els['update-content-slot'].innerHTML='OLD_UPDATE';
var document = {
  activeElement:{},
  getElementById:function(id){return els[id]||null;},
  createElement:function(){return mkEl('temp');}
};
var window = {_lastHermesLogCard:'OLD_HERMES', _hasHermesLog:true};
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
apply({hermes_log_card:''});
var hermesAfterEmpty={
  html:els['hermes-log-slot'].innerHTML,
  last:window._lastHermesLogCard,
  has:window._hasHermesLog
};
apply({update_block:'NEW_UPDATE'});
process.stdout.write(JSON.stringify({
  afterBlocked:afterBlocked,
  afterDeltaOmission:afterDeltaOmission,
  hermesAfterEmpty:hermesAfterEmpty,
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
        self.assertEqual(result["hermesAfterEmpty"], {"html": "", "last": "", "has": False})
        self.assertEqual(result["updateAfter"], "NEW_UPDATE")


if __name__ == "__main__":
    unittest.main()
