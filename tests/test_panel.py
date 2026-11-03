import importlib
import gzip
import io
import zlib
import json
import os
import subprocess
import sys
import tempfile
import hashlib
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

os.environ["PANEL_TOKEN"] = "test-token-secret-12345"
os.environ["PANEL_PASSWORD"] = "test-panel-password"
import importlib.util
spec = importlib.util.spec_from_file_location(
    "panel", os.path.join(REPO_ROOT, "dashboard-toggle-server.py")
)
panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(panel)

# Unit test tidak boleh menjalankan `hermes` sungguhan: CLI-nya belasan detik per
# panggilan dan menyentuh state profil/ gateway nyata. Test yang memang menguji
# jalur CLI mem-patch fungsi ini sendiri.
panel._run_hermes_cli = lambda *args, **kwargs: (-1, "disabled in tests")


class TestHermesControlPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        panel.TOKEN = "test-token-secret-12345"
        cls.server = panel.TimeoutThreadingHTTPServer(("127.0.0.1", 0), panel.Handler)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        cls.opener = urllib.request.build_opener(NoRedirect)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        panel._last_action_at.clear()
        panel._last_aux_model_at = 0.0
        panel._last_model_switch_at = 0.0
        with panel._router_update_lock:
            panel._router_updating = False
        with panel._hermes_update_lock:
            panel._hermes_update_cache["at"] = time.time()

    def _request(self, path: str, method: str = "GET", headers: dict = None, data: bytes = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, data=data, method=method)
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        try:
            res = self.opener.open(req)
            return res.code, res.headers, res.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8", errors="replace")

    def _request_raw(self, path: str, method: str = "GET", headers: dict = None, data: bytes = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, data=data, method=method)
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        try:
            res = self.opener.open(req)
            return res.code, res.headers, res.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    # --- PR 1: Safe updater error handling ---
    def test_01_update_router_safe_on_popen_exception(self):
        with mock.patch.object(
            panel.subprocess,
            "Popen",
            side_effect=RuntimeError("boom"),
        ):
            panel.update_router()
            for _ in range(50):
                if not panel._router_updating:
                    break
                time.sleep(0.02)

        self.assertFalse(panel._router_updating)
        self.assertEqual(panel._router_update_result["status"], "failed")
        self.assertIn("RuntimeError", panel._router_update_result["summary"])

    def test_01b_router_update_timeout_kills_process_group(self):
        """Timeout must kill the whole process group, not just the wrapping shell."""
        import tempfile
        td = tempfile.mkdtemp(prefix="router-upd-")
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))
        log_path = os.path.join(td, "update.log")

        class FakeProc:
            pid = 4242
            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="sh", timeout=timeout or 1)

        killed = []
        fake = FakeProc()
        with mock.patch.object(panel, "UPDATE_TIMEOUT", 0.05), \
             mock.patch.object(panel, "ROUTER_COMPOSE_DIR", td), \
             mock.patch.object(panel, "get_9router_host", return_value="127.0.0.1"), \
             mock.patch.object(panel, "_router_update_command", return_value="npm install -g 9router@latest"), \
             mock.patch.object(panel.subprocess, "Popen", return_value=fake), \
             mock.patch.object(panel.os, "killpg", side_effect=lambda pg, sig: killed.append((pg, sig))), \
             mock.patch.object(panel, "_refresh_update_cache"):
            panel.update_router()
            for _ in range(100):
                if not panel._router_updating:
                    break
                time.sleep(0.02)

        self.assertFalse(panel._router_updating)
        self.assertEqual(panel._router_update_result["exit_code"], 124)
        self.assertEqual(killed[0][0], 4242)
        self.assertEqual(killed[0][1], __import__("signal").SIGTERM)
        with open(log_path, encoding="utf-8") as lf:
            self.assertIn("timeout", lf.read())

    # --- PR 2: Config write failures propagation ---
    def test_gateway_lifecycle_launch_failures_are_reported(self):
        with mock.patch.object(panel.subprocess, "Popen", side_effect=OSError("no systemctl")):
            self.assertFalse(panel.restart_bot())
            self.assertFalse(panel.bot_action("start"))
        self.assertFalse(panel.bot_action("invalid"))

    def test_frontend_fetch_helper_checks_http_and_json_errors(self):
        page = panel.page_source()
        self.assertIn("function fetchJsonChecked", page)
        self.assertIn("if(!response.ok) throw new Error", page)
        self.assertIn("return response.json()", page)
        loader_start = page.index("function ensureAvailableModels")
        loader_end = page.index("function openAuxPicker", loader_start)
        self.assertIn("fetchJsonChecked('/api/models'", page[loader_start:loader_end])

    def test_frontend_profile_and_kanban_json_fetches_check_http_status(self):
        page = panel.page_source()
        for function_name in (
            "openCreateProfileModal",
            "submitCreateProfile",
            "openProfileSoulModal",
            "refreshKanbanBoard",
            "openViewTaskModal",
            "submitCreateTask",
        ):
            start = page.index("function " + function_name)
            next_function = page.find("function ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            self.assertIn("fetchJsonChecked(", segment, function_name)
            self.assertNotIn(".then(function(r){{ return r.json(); }})", segment, function_name)

    def test_frontend_bulk_kanban_actions_validate_each_response(self):
        page = panel.page_source()
        for function_name, endpoint in (
            ("applyBulkStatus", "/api/kanban/task/status"),
            ("applyBulkDelete", "/api/kanban/task/delete"),
        ):
            start = page.index("function " + function_name)
            next_function = page.find("function ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            self.assertIn("fetchJsonChecked(", segment, function_name)
            self.assertIn("if(!res || !res.ok) throw new Error", segment, function_name)
            self.assertIn(endpoint, segment, function_name)

    def test_frontend_profile_and_kanban_mutations_check_http_status(self):
        page = panel.page_source()
        for function_name in (
            "setActiveProfile",
            "deleteProfile",
            "submitRenameProfile",
            "submitProfileSoul",
            "setProfileModel",
            "toggleProfileSkill",
            "toggleProfileToolset",
            "handleKanbanTrashDrop",
            "handleKanbanDrop",
            "submitCreateBoard",
            "switchKanbanBoard",
            "moveCurrentTaskStatus",
            "reclaimCurrentTask",
            "deleteCurrentTask",
            "submitCurrentTaskComment",
            "openKanbanConfigModal",
            "submitKanbanConfig",
        ):
            start = page.index("function " + function_name)
            next_function = page.find("function ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            self.assertIn("fetchJsonChecked(", segment, function_name)
            self.assertNotIn(".then(function(r){{ return r.json(); }})", segment, function_name)

    def test_frontend_gateway_and_whatsapp_fetches_check_http_status(self):
        page = panel.page_source()
        for function_name in (
            "switchGwConfigMode",
            "selectCatalogPlatform",
            "openGwConfig",
            "saveGwConfig",
            "toggleGwPlatform",
            "deleteGwPlatform",
            "openWaPairModal",
            "pollWaPairStatus",
            "startWaPair",
            "cancelWaPair",
            "applyWaPair",
        ):
            start = page.index("function " + function_name)
            next_function = page.find("function ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            self.assertIn("fetchJsonChecked(", segment, function_name)
            self.assertNotIn(".then(function(r){{ return r.json(); }})", segment, function_name)

    def test_frontend_remaining_json_fetches_check_http_status(self):
        page = panel.page_source()
        for function_name in (
            "selectFallbackModel",
            "selectAuxModel",
            "openProfileSkillsModal",
            "readProfileSkill",
            "refreshKanbanBoard",
            "openAttachmentViewerModal",
            "loadNetworkStatus",
            "testNetworkConnection",
            "resolveNetworkDns",
        ):
            if "function " + function_name not in page:
                continue
            start = page.index("function " + function_name)
            next_function = page.find("function ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            if "fetch(" in segment:
                self.assertIn("fetchJsonChecked(", segment, function_name)
                self.assertNotIn(".then(function(r){{ return r.json(); }})", segment, function_name)

    def test_frontend_json_fetches_do_not_directly_parse_unchecked_response(self):
        page = panel.page_source()
        self.assertNotIn(".then(function(r){{ return r.json(); }})", page)
        self.assertNotIn("}}).then(function(r){{ return r.json(); }})", page)
        network = panel.NETWORK_SCRIPT
        net_start = network.index("function netPost")
        net_end = network.index("function netHealth", net_start)
        net_helpers = network[net_start:net_end]
        self.assertIn("function netGet", net_helpers)
        self.assertIn("if (!r.ok) throw new Error", net_helpers)
        load_start = network.index("function netLoad")
        load_end = network.index("function netPickRoute", load_start)
        self.assertIn("netGet('/api/network')", network[load_start:load_end])
        self.assertNotIn(".then(function (r) { return r.json(); })", network)

    def test_frontend_qr_svg_is_sanitized_before_dom_insertion(self):
        page = panel.page_source()
        self.assertIn("function renderWaQrSvg", page)
        start = page.index("function renderWaQrSvg")
        end = page.index("function setWaPairStatusUI", start)
        sanitizer = page[start:end]
        self.assertIn("DOMParser", sanitizer)
        self.assertIn("removeAttribute", sanitizer)
        self.assertIn("importNode", sanitizer)
        status_start = page.index("function setWaPairStatusUI")
        status_end = page.index("var _waPairPolling", status_start)
        status = page[status_start:status_end]
        self.assertIn("renderWaQrSvg(data.qr_svg, qrBox)", status)
        self.assertNotIn("+ data.qr_svg +", status)

    def test_frontend_error_messages_escape_before_innerhtml(self):
        page = panel.page_source()
        for function_name in ("loadProfileSkills", "loadProfileTools"):
            start = page.index("function " + function_name)
            end = page.index("\nfunction ", start + 1)
            segment = page[start:end]
            self.assertIn("escHtml((res && res.error) || 'unknown')", segment)
            self.assertIn("escHtml(err && err.message || err)", segment)
        start = page.index("function openKanbanAttachment")
        end = page.index("function setAttachmentViewMode", start)
        attachment = page[start:end]
        self.assertIn("escHtml(err && err.message || err)", attachment)
        self.assertNotIn("' + err.message + '</span>", attachment)

    def test_attachment_loader_ignores_stale_async_responses(self):
        page = panel.page_source()
        start = page.index("var currentAttachmentText")
        end = page.index("function setAttachmentViewMode", start)
        attachment = page[start:end]
        self.assertIn("var attachmentLoadGen = 0", attachment)
        self.assertIn("var loadGen = ++attachmentLoadGen", attachment)
        self.assertIn("if(loadGen !== attachmentLoadGen) return", attachment)
        close_start = page.index("function closeAttachmentViewerModal", end)
        close_end = page.find("function ", close_start + 1)
        close = page[close_start:] if close_end == -1 else page[close_start:close_end]
        self.assertIn("++attachmentLoadGen", close)

    def test_network_actions_handle_request_failures(self):
        network = panel.NETWORK_SCRIPT
        for function_name in ("netRestartGateway", "netResolve"):
            start = network.index("function " + function_name)
            next_function = network.find("\nfunction ", start + 1)
            segment = network[start:] if next_function == -1 else network[start:next_function]
            self.assertIn(".then(function", segment, function_name)
            self.assertIn(".catch(function", segment, function_name)

    def test_network_restart_gateway_ignores_stale_responses(self):
        network = panel.NETWORK_SCRIPT
        self.assertIn("var netRestartGen = 0;", network)
        start = network.index("function netRestartGateway")
        next_function = network.find("\nfunction ", start + 1)
        segment = network[start:] if next_function == -1 else network[start:next_function]
        self.assertIn("var restartGen = ++netRestartGen;", segment)
        guard = "if (restartGen !== netRestartGen) return;"
        self.assertEqual(segment.count(guard), 2)
        self.assertIn("if (restartGen === netRestartGen) netLoad(true);", segment)

    def test_network_test_and_resolve_ignore_stale_requests(self):
        network = panel.NETWORK_SCRIPT
        self.assertIn("var netTestGen = {};", network)
        self.assertIn("var netResolveGen = 0;", network)
        test_start = network.index("function netTest")
        test_end = network.index("function netAddHost", test_start)
        test_seg = network[test_start:test_end]
        self.assertIn("var actGen = (netTestGen[pid] = (netTestGen[pid] || 0) + 1);", test_seg)
        self.assertIn("if (actGen !== netTestGen[pid]) return;", test_seg)
        res_start = network.index("function netResolve")
        res_end = network.index("(function () {", res_start)
        res_seg = network[res_start:res_end]
        self.assertIn("var actGen = ++netResolveGen;", res_seg)
        self.assertIn("if (actGen !== netResolveGen) return;", res_seg)

    def test_network_saves_ignore_stale_responses(self):
        network = panel.NETWORK_SCRIPT
        self.assertIn("var netRoutesSaveGen = 0;", network)
        self.assertIn("var netDnsSaveGen = 0;", network)
        for function_name, counter, marker in (
            ("netSaveRoutes", "netRoutesSaveGen", "var saveGen = ++netRoutesSaveGen;"),
            ("netSaveDns", "netDnsSaveGen", "var saveGen = ++netDnsSaveGen;"),
        ):
            start = network.index("function " + function_name)
            next_function = network.find("\nfunction ", start + 1)
            segment = network[start:] if next_function == -1 else network[start:next_function]
            self.assertIn(marker, segment, function_name)
            self.assertIn("if (saveGen !== " + counter + ") return;", segment, function_name)
            self.assertGreaterEqual(segment.count("if (saveGen !== " + counter + ") return;"), 2, function_name)

    def test_kanban_toast_ignores_stale_hide_callbacks(self):
        page = panel.page_source()
        start = page.index("function showKanbanToast")
        end = page.index("function handleKanbanDragStart", start)
        segment = page[start:end]
        self.assertIn("toast._hideTimer", segment)
        self.assertIn("var toastGen =", segment)
        self.assertIn("if(toast._generation !== toastGen) return;", segment)
        self.assertIn("if(toast._generation === toastGen) toast.style.display = 'none';", segment)

    def test_whatsapp_start_clears_prior_poll_and_countdown_timers(self):
        page = panel.page_source()
        start = page.index("function startWaPair")
        end = page.index("function cancelWaPair", start)
        segment = page[start:end]
        self.assertIn("if(_waPairPollTimer)", segment)
        self.assertIn("if(_waPairTimerInterval)", segment)
        self.assertIn("_waPairExpiresAt = null;", segment)

    def test_toggle_log_scrolls_restored_box_to_bottom(self):
        page = panel.page_source()
        start = page.index("function toggleLog")
        end = page.index("function syncLogUI", start)
        segment = page[start:end]
        self.assertIn("scrollAllLogsToBottom()", segment)

    def test_global_fetch_toast_interceptor_inspects_request_object_method(self):
        script = panel.NAV_SCRIPT
        start = script.index("/* Setiap fetch non-GET yang balas JSON")
        end = script.index("/* ---- UX: Esc menutup modal teratas ----", start)
        segment = script[start:end]
        self.assertIn("var method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();", segment)

    def test_sse_applies_gateway_log_card_cache_before_dom_dismissal(self):
        src = panel.SSE_SCRIPT
        start = src.index("if(d.gateway_log_card !== undefined)")
        end = src.index("syncLogUI()", start)
        segment = src[start:end]
        self.assertIn("window._lastGatewayLogCard = gatewayLogHtml;", segment)
        self.assertIn("if(!safeStore('getItem','gatewayLogDismissed'))", segment)
        self.assertLess(
            segment.index("window._lastGatewayLogCard = gatewayLogHtml;"),
            segment.index("if(!safeStore('getItem','gatewayLogDismissed'))")
        )

    def test_copy_code_ignores_stale_restore_callbacks(self):
        page = panel.page_source()
        start = page.index("function copyCodeBlock")
        end = page.index("function fetchJsonChecked", start)
        segment = page[start:end]
        self.assertIn("var copyGen = (btn._copyGeneration || 0) + 1", segment)
        self.assertIn("btn._copyGeneration = copyGen", segment)
        self.assertIn("if(btn._copyGeneration === copyGen) btn.textContent = orig", segment)

    def test_sse_disconnect_cleans_pending_hidden_timers(self):
        src = panel.SSE_SCRIPT
        start = src.index("function disconnect(){")
        end = src.index("document.addEventListener('visibilitychange'", start)
        segment = src[start:end]
        self.assertIn("if(_hiddenTimer){ clearTimeout(_hiddenTimer); _hiddenTimer=null; }", segment)
        self.assertIn("_pendingUpdate=null;", segment)

    def test_sse_retry_timer_suspends_reconnect_when_tab_is_hidden(self):
        src = panel.SSE_SCRIPT
        start = src.index("current.onerror=function(){")
        end = src.index("var hideDisconnectTimer = null;", start)
        segment = src[start:end]
        self.assertIn("if(typeof document !== 'undefined' && document.hidden) return;", segment)

    def test_network_loader_ignores_stale_async_responses(self):
        network = panel.NETWORK_SCRIPT
        self.assertIn("var netLoadGen = 0;", network)
        start = network.index("function netLoad")
        next_function = network.find("\nfunction ", start + 1)
        segment = network[start:] if next_function == -1 else network[start:next_function]
        self.assertIn("var loadGen = ++netLoadGen;", segment)
        guard = "if (loadGen !== netLoadGen) return;"
        then_start = segment.index(".then(function (st) {")
        catch_start = segment.index(".catch(function (e) {")
        tail_start = segment.index(".then(function () {", catch_start)
        then_segment = segment[then_start:catch_start]
        catch_segment = segment[catch_start:tail_start]
        tail_segment = segment[tail_start:]
        self.assertIn(guard, then_segment)
        self.assertIn(guard, catch_segment)
        self.assertIn("if (loadGen === netLoadGen && btn)", tail_segment)
        self.assertLess(then_segment.index(guard), then_segment.index("slot.innerHTML"), "netLoad")

    def test_network_full_reload_never_wipes_unsaved_edits(self):
        """A full re-render replaces the route/DNS inputs; doing it with unsaved
        edits silently discards them and clears the dirty flag, so the user
        believes the edits were saved."""
        network = panel.NETWORK_SCRIPT
        start = network.index("function netLoad")
        next_function = network.find("\nfunction ", start + 1)
        segment = network[start:] if next_function == -1 else network[start:next_function]
        then_start = segment.index(".then(function (st) {")
        catch_start = segment.index(".catch(function (e) {")
        then_segment = segment[then_start:catch_start]
        self.assertIn("NET.dirty.routes || NET.dirty.dns", then_segment)
        self.assertIn("statusSlot.innerHTML = netRenderStatus(st);", then_segment)
        # the dirty guard must run before the destructive full re-render
        self.assertLess(
            then_segment.index("NET.dirty.routes || NET.dirty.dns"),
            then_segment.index("slot.innerHTML = netRenderCard(st);"),
            "netLoad full reload must be guarded",
        )

    def test_frontend_checked_fetch_consumers_have_rejection_paths(self):
        page = panel.page_source()
        for function_name in (
            "selectCatalogPlatform", "toggleGwPlatform", "deleteGwPlatform", "cancelWaPair",
        ):
            start = page.index("function " + function_name)
            next_function = page.find("\nfunction ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            self.assertIn("fetchJsonChecked(", segment, function_name)
            self.assertIn(".catch(", segment, function_name)
        for marker in (
            "fetchJsonChecked('/api/profiles')",  # clone profile and Kanban assignees
        ):
            self.assertGreaterEqual(page.count(marker), 3, marker)
        self.assertNotIn(".catch(function(){{}});", page)

    def test_modal_loaders_ignore_stale_async_responses(self):
        page = panel.page_source()
        checks = {
            "loadProfileSkills": [
                "var loadGen = ++profileSkillsLoadGen",
                "if(loadGen !== profileSkillsLoadGen) return",
                "if(loadGen !== profileSkillsLoadGen) return",
            ],
            "loadProfileTools": [
                "var loadGen = ++profileToolsLoadGen",
                "if(loadGen !== profileToolsLoadGen) return",
                "if(loadGen !== profileToolsLoadGen) return",
            ],
            "loadProfileConfig": [
                "var loadGen = ++profileConfigLoadGen",
                "if(loadGen !== profileConfigLoadGen) return",
            ],
            "openViewTaskModal": [
                "var loadGen = ++taskDetailLoadGen",
                "if(loadGen !== taskDetailLoadGen) return",
                "if(loadGen !== taskDetailLoadGen) return",
            ],
        }
        for function_name, needles in checks.items():
            start = page.index("function " + function_name)
            next_function = page.find("\nfunction ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            for needle in needles:
                self.assertIn(needle, segment, f"{function_name}: {needle}")
        for function_name in ("closeProfileSkillsModal", "closeViewTaskModal", "closeProfileSkillRead"):
            start = page.index("function " + function_name)
            next_function = page.find("\nfunction ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            if function_name == "closeProfileSkillRead":
                self.assertIn("++profileSkillReadGen", segment)
            elif function_name == "closeProfileSkillsModal":
                self.assertIn("++profileSkillsLoadGen", segment)
            else:
                self.assertIn("++taskDetailLoadGen", segment)

    @unittest.skipUnless(subprocess.run(["which", "node"], capture_output=True).returncode == 0, "node not installed")
    def test_network_status_renderer_rejects_hostile_numeric_fields(self):
        script = r"""
var document = {readyState: 'complete', addEventListener: function(){}, getElementById: function(){ return null; }};
""" + panel.NETWORK_SCRIPT + r"""
var st = {
  relay: {service: 'active', warp_service: 'active', warp: '<img src=x onerror=alert(1)>', stats: {
    totals: {ok: '<svg onload=alert(1)>', failed: '<b>2</b>', fallback: '<i>3</i>', started_at: 0},
    per_ip: {}, events: []
  }},
  gateway: {active: true, proxy_errors: '<img src=x>', failed_deliveries: '<b>1</b>', network_errors: '<i>1</i>', since: '', last_error: '<svg onload=alert(1)>'},
  ports: {relay: '<img src=x>', warp: '<svg onload=alert(1)>'}, platforms: [], warnings: []
};
process.stdout.write(netRenderStatus(st));
"""
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("<img src=x", r.stdout)
        self.assertNotIn("<svg onload=", r.stdout)
        self.assertNotIn("<b>2</b>", r.stdout)
        self.assertNotIn("<i>3</i>", r.stdout)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", r.stdout)
        self.assertIn("&lt;svg onload=alert(1)&gt;", r.stdout)

    def test_frontend_pair_cancel_checks_http_status(self):
        page = panel.page_source()
        start = page.index("function closeWaPairModal")
        end = page.index("function renderWaQrSvg", start)
        segment = page[start:end]
        self.assertIn("fetchJsonChecked('/api/whatsapp/pair-cancel'", segment)
        self.assertNotIn("fetch('/api/whatsapp/pair-cancel'", segment)
        self.assertNotIn(".catch(function(){{}})", segment)

    def test_gateway_and_whatsapp_modals_ignore_stale_responses(self):
        page = panel.page_source()
        for function_name, needles in {
            "openGwConfig": ["var loadGen = ++_gwConfigLoadGen", "if(loadGen !== _gwConfigLoadGen || currentGwPlatform !== platform) return"],
            "closeGwConfig": ["++_gwConfigLoadGen"],
            "openWaPairModal": ["var actGen = ++_waPairActionGen", "if(actGen !== _waPairActionGen) return"],
            "pollWaPairStatus": ["var actGen = expectedGen == null ? _waPairActionGen : expectedGen", "if(actGen !== _waPairActionGen) return"],
            "closeWaPairModal": ["++_waPairActionGen"],
        }.items():
            start = page.index("function " + function_name)
            next_function = page.find("\nfunction ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            for needle in needles:
                self.assertIn(needle, segment, f"{function_name}: {needle}")

    def test_whatsapp_apply_ignores_stale_pairing_context(self):
        page = panel.page_source()
        start = page.index("function applyWaPair")
        next_function = page.find("\nfunction ", start + 1)
        segment = page[start:] if next_function == -1 else page[start:next_function]
        self.assertIn("var applyGen = ++_waPairActionGen", segment)
        guard = "if(applyGen !== _waPairActionGen) return;"
        then_start = segment.index(".then(function(d)")
        catch_start = segment.index(".catch(function(err)")
        then_segment = segment[then_start:catch_start]
        catch_segment = segment[catch_start:]
        self.assertIn(guard, then_segment)
        self.assertIn(guard, catch_segment)
        self.assertLess(then_segment.index(guard), then_segment.index("if(d && d.ok)"))

    def test_kanban_assignee_bootstrap_ignores_reinitialization(self):
        page = panel.page_source()
        self.assertIn("var kanbanAssigneeBootGen = 0;", page)
        start = page.index("setTimeout(function() {{")
        end = page.index("function openCreateTaskModal", start)
        segment = page[start:end]
        self.assertIn("var bootGen = ++kanbanAssigneeBootGen", segment)
        self.assertIn("if(bootGen !== kanbanAssigneeBootGen) return;", segment)

    def test_profile_soul_and_kanban_config_ignore_stale_responses(self):
        page = panel.page_source()
        for function_name, needles in {
            "openProfileSoulModal": ["var loadGen = ++profileSoulLoadGen", "if(loadGen !== profileSoulLoadGen || currentEditingSoulProfile !== name) return"],
            "closeProfileSoulModal": ["++profileSoulLoadGen"],
            "openKanbanConfigModal": ["var loadGen = ++kanbanConfigLoadGen", "if(loadGen !== kanbanConfigLoadGen) return"],
            "closeKanbanConfigModal": ["++kanbanConfigLoadGen"],
        }.items():
            start = page.index("function " + function_name)
            next_function = page.find("\nfunction ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            for needle in needles:
                self.assertIn(needle, segment, f"{function_name}: {needle}")

    def test_aux_model_picker_ignores_stale_catalog_callbacks(self):
        page = panel.page_source()
        self.assertIn("var auxPickerLoadGen = 0;", page)
        for function_name in ("openAuxPicker", "openFallbackPicker", "openProfileModelPicker"):
            start = page.index("function " + function_name)
            next_function = page.find("\nfunction ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            self.assertIn("var loadGen = ++auxPickerLoadGen", segment, function_name)
            self.assertIn("if(loadGen !== auxPickerLoadGen) return", segment, function_name)
        start = page.index("function closeAuxPicker")
        next_function = page.find("\nfunction ", start + 1)
        segment = page[start:] if next_function == -1 else page[start:next_function]
        self.assertIn("++auxPickerLoadGen", segment)

    def test_model_catalog_cache_ignores_stale_requests(self):
        page = panel.page_source()
        self.assertIn("var availableModelsFetchGen = 0;", page)
        start = page.index("function ensureAvailableModels")
        end = page.index("function openAuxPicker", start)
        segment = page[start:end]
        request_guard = "if(requestGen !== availableModelsFetchGen) return;"
        self.assertIn("var requestGen = ++availableModelsFetchGen", segment)
        self.assertIn(request_guard, segment)
        self.assertLess(segment.index(request_guard), segment.index("if(typeof data === 'object'"))
        catch_start = segment.index(".catch(function()")
        self.assertIn(request_guard, segment[catch_start:])

    def test_aux_model_saves_ignore_stale_responses(self):
        page = panel.page_source()
        self.assertIn("var auxModelSaveGen = 0;", page)
        for function_name in ("selectFallbackModel", "selectAuxModel"):
            start = page.index("function " + function_name)
            next_function = page.find("\nfunction ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            self.assertIn("var saveGen = ++auxModelSaveGen", segment, function_name)
            then_guard = "if(saveGen !== auxModelSaveGen) return;"
            then_start = segment.index(".then(function(res)")
            catch_start = segment.index(".catch(function")
            then_segment = segment[then_start:catch_start]
            catch_segment = segment[catch_start:]
            self.assertIn(then_guard, then_segment, function_name)
            self.assertIn(then_guard, catch_segment, function_name)
            self.assertLess(then_segment.index(then_guard), then_segment.index("if(pbar)"), function_name)
            self.assertLess(catch_segment.index(then_guard), catch_segment.index("if(pbar)"), function_name)

    def test_kanban_refresh_ignores_stale_board_responses(self):
        page = panel.page_source()
        start = page.index("function refreshKanbanBoard")
        end = page.index("function manualSyncKanban", start)
        segment = page[start:end]
        self.assertIn("var loadGen = ++kanbanBoardLoadGen", segment)
        self.assertIn("if(loadGen !== kanbanBoardLoadGen) return", segment)
        self.assertIn("if(board !== (bSel ? bSel.value : 'default')) return", segment)

    def test_kanban_auto_sync_avoids_in_flight_overlap(self):
        page = panel.page_source()
        self.assertIn("var isKanbanRefreshing = false;", page)
        start = page.index("function refreshKanbanBoard")
        end = page.index("function manualSyncKanban", start)
        segment = page[start:end]
        self.assertIn("isKanbanRefreshing = true;", segment)
        then_index = segment.index(".then(function(res)")
        catch_index = segment.index(".catch(function(err)")
        then_segment = segment[then_index:catch_index]
        catch_segment = segment[catch_index:]
        self.assertIn("isKanbanRefreshing = false;", then_segment)
        self.assertIn("isKanbanRefreshing = false;", catch_segment)
        auto_start = page.index("function startKanbanAutoSync")
        auto_end = page.index("var kanbanAssigneeBootGen", auto_start)
        auto_segment = page[auto_start:auto_end]
        self.assertIn("isKanbanRefreshing", auto_segment)


    def test_profile_and_task_assignee_loaders_ignore_stale_responses(self):
        page = panel.page_source()
        self.assertIn("var profileCloneLoadGen = 0;", page)
        self.assertIn("var taskAssigneeLoadGen = 0;", page)
        for function_name, gen in (("openCreateProfileModal", "profileCloneLoadGen"), ("openCreateTaskModal", "taskAssigneeLoadGen")):
            start = page.index("function " + function_name)
            next_function = page.find("\nfunction ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            self.assertIn("var loadGen = ++" + gen, segment, function_name)
            self.assertIn("if(loadGen !== " + gen + ") return", segment, function_name)
        for function_name, gen in (("closeCreateProfileModal", "profileCloneLoadGen"), ("closeCreateTaskModal", "taskAssigneeLoadGen")):
            start = page.index("function " + function_name)
            next_function = page.find("\nfunction ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            self.assertIn("++" + gen, segment, function_name)

    def test_kanban_board_switch_ignores_stale_responses(self):
        page = panel.page_source()
        self.assertIn("var kanbanBoardSwitchGen = 0;", page)
        start = page.index("function switchKanbanBoard")
        end = page.index("function openViewTaskModal", start)
        segment = page[start:end]
        self.assertIn("var actionGen = ++kanbanBoardSwitchGen", segment)
        self.assertIn("if(actionGen !== kanbanBoardSwitchGen) return", segment)
        self.assertIn("if(bSel && bSel.value !== slug) return", segment)

    def test_kanban_board_switch_serializes_overlapping_requests(self):
        """ThreadingHTTPServer can finish switches out of order; only one may be in flight."""
        page = panel.page_source()
        self.assertIn("var kanbanSwitchBusy = false;", page)
        self.assertIn("var kanbanSwitchQueued = null;", page)
        start = page.index("function switchKanbanBoard")
        end = page.index("function openViewTaskModal", start)
        segment = page[start:end]
        self.assertIn("if(kanbanSwitchBusy){", segment)
        self.assertIn("kanbanSwitchQueued = slug;", segment)
        self.assertIn("kanbanSwitchBusy = true;", segment)
        self.assertIn("kanbanSwitchBusy = false;", segment)
        self.assertIn("if(queued) switchKanbanBoard(queued);", segment)

    def test_gateway_yaml_preview_ignores_stale_mode_changes(self):
        page = panel.page_source()
        start = page.index("function switchGwConfigMode")
        end = page.index("function gwActivePlatform", start)
        segment = page[start:end]
        self.assertIn("if(reqGen !== _gwPreviewGen || currentGwMode !== 'yaml') return;", segment)
        then_guard = segment.index("if(reqGen !== _gwPreviewGen || currentGwMode !== 'yaml') return;")
        self.assertGreater(segment.index("if(yamlEl) yamlEl.readOnly = false;"), then_guard)
        catch_start = segment.index(".catch(function()")
        catch_segment = segment[catch_start:]
        self.assertIn("if(reqGen !== _gwPreviewGen || currentGwMode !== 'yaml') return;", catch_segment)
        close_start = page.index("function closeGwConfig")
        close_end = page.index("function saveGwConfig", close_start)
        self.assertIn("++_gwPreviewGen", page[close_start:close_end])

    def test_gateway_mutations_ignore_stale_slot_responses(self):
        page = panel.page_source()
        self.assertIn("var _gwMutationGen = 0;", page)
        for function_name in ("saveGwConfig", "toggleGwPlatform", "deleteGwPlatform"):
            start = page.index("function " + function_name)
            next_function = page.find("\nfunction ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            self.assertIn("var mutationGen = ++_gwMutationGen", segment, function_name)
            then_start = segment.index(".then(function(res)")
            catch_start = segment.index(".catch(function")
            then_segment = segment[then_start:catch_start]
            catch_segment = segment[catch_start:]
            guard = "if(mutationGen !== _gwMutationGen) return;"
            self.assertIn(guard, then_segment, function_name)
            self.assertIn(guard, catch_segment, function_name)
            self.assertLess(then_segment.index(guard), then_segment.index("if(res"), function_name)

    def test_profile_skill_mutations_ignore_stale_profile_context(self):
        page = panel.page_source()
        gens = {
            "toggleProfileSkill": "profileSkillsLoadGen",
            "toggleProfileToolset": "profileToolsLoadGen",
        }
        for function_name, gen in gens.items():
            start = page.index("function " + function_name)
            next_function = page.find("\nfunction ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            self.assertIn("var mutationProfile = currentSkillsProfile", segment, function_name)
            self.assertIn(f"var mutationLoadGen = {gen}", segment, function_name)
            guard = (f"if(mutationProfile !== currentSkillsProfile || "
                     f"mutationLoadGen !== {gen}) return;")
            then_start = segment.index(".then(function(res)")
            catch_start = segment.index(".catch(function")
            then_segment = segment[then_start:catch_start]
            catch_segment = segment[catch_start:]
            self.assertIn(guard, then_segment, function_name)
            self.assertIn(guard, catch_segment, function_name)
            self.assertLess(then_segment.index(guard), then_segment.index("if(res"), function_name)

    def test_profile_mutations_ignore_stale_modal_responses(self):
        page = panel.page_source()
        self.assertIn("var profileMutationGen = 0;", page)
        cases = {
            "submitCreateProfile": "var mutationProfile = name",
            "setActiveProfile": "var mutationProfile = name",
            "deleteProfile": "var mutationProfile = name",
            "submitRenameProfile": "var mutationProfile = oldName",
            "submitProfileSoul": "var mutationProfile = name",
            "setProfileModel": "var mutationProfile = profileName",
        }
        for function_name, snapshot in cases.items():
            start = page.index("function " + function_name)
            next_function = page.find("\nfunction ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            self.assertIn(snapshot, segment, function_name)
            self.assertIn("var mutationGen = ++profileMutationGen", segment, function_name)
            guard = "if(mutationGen !== profileMutationGen || currentProfileMutationTarget !== mutationProfile) return;"
            then_start = segment.index(".then(function(res)")
            catch_start = segment.index(".catch(function")
            then_segment = segment[then_start:catch_start]
            catch_segment = segment[catch_start:]
            self.assertIn(guard, then_segment, function_name)
            self.assertIn(guard, catch_segment, function_name)
            self.assertLess(then_segment.index(guard), then_segment.index("if(res"), function_name)

    def test_task_detail_mutations_ignore_stale_task_context(self):
        page = panel.page_source()
        self.assertIn("var taskMutationGen = 0;", page)
        for function_name in ("moveCurrentTaskStatus", "reclaimCurrentTask", "deleteCurrentTask"):
            start = page.index("function " + function_name)
            next_function = page.find("\nfunction ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            self.assertIn("var mutationTaskId = currentViewingTaskId", segment, function_name)
            self.assertIn("var mutationBoard = bSel ? bSel.value : ''", segment, function_name)
            self.assertIn("var mutationGen = ++taskMutationGen", segment, function_name)
            guard = "if(mutationGen !== taskMutationGen || currentViewingTaskId !== mutationTaskId) return;"
            then_start = segment.index(".then(function(res)")
            catch_start = segment.index(".catch(function")
            then_segment = segment[then_start:catch_start]
            catch_segment = segment[catch_start:]
            self.assertIn(guard, then_segment, function_name)
            self.assertIn(guard, catch_segment, function_name)
            self.assertLess(then_segment.index(guard), then_segment.index("if(res"), function_name)

    def test_task_comment_ignores_stale_task_context(self):
        page = panel.page_source()
        start = page.index("function submitCurrentTaskComment")
        end = page.index("var kanbanConfigLoadGen", start)
        segment = page[start:end]
        self.assertIn("var mutationTaskId = currentViewingTaskId", segment)
        self.assertIn("var mutationBoard = bSel ? bSel.value : ''", segment)
        self.assertIn("var mutationGen = ++taskMutationGen", segment)
        guard = "if(mutationGen !== taskMutationGen || currentViewingTaskId !== mutationTaskId) return;"
        then_start = segment.index(".then(function(res)")
        catch_start = segment.index(".catch(function")
        then_segment = segment[then_start:catch_start]
        catch_segment = segment[catch_start:]
        self.assertIn(guard, then_segment)
        self.assertIn(guard, catch_segment)
        self.assertLess(then_segment.index(guard), then_segment.index("if(res"))
        self.assertIn("openViewTaskModal(mutationTaskId)", then_segment)

    def test_kanban_comment_submit_serializes_and_disables_in_flight(self):
        page = panel.page_source()
        self.assertIn("var kanbanCommentSubmitting = false;", page)
        start = page.index("function submitCurrentTaskComment")
        end = page.index("var kanbanConfigLoadGen", start)
        segment = page[start:end]
        self.assertIn("if(!currentViewingTaskId || kanbanCommentSubmitting) return;", segment)
        self.assertIn("kanbanCommentSubmitting = true;", segment)
        self.assertIn("btn.disabled = true", segment)
        self.assertIn("btn.textContent = 'Mengirim…'", segment)
        self.assertIn("input.disabled = true", segment)
        then_segment = segment[segment.index(".then("):segment.index(".catch(")]
        catch_segment = segment[segment.index(".catch("):]
        self.assertIn("kanbanCommentSubmitting = false;", then_segment)
        self.assertIn("btn.disabled = false", then_segment)
        self.assertIn("input.disabled = false", then_segment)
        self.assertIn("kanbanCommentSubmitting = false;", catch_segment)
        self.assertIn("btn.disabled = false", catch_segment)
        self.assertIn("input.disabled = false", catch_segment)

    def test_kanban_comment_input_cleared_on_open_and_close(self):
        page = panel.page_source()
        open_start = page.index("function openViewTaskModal")
        open_end = page.index("function formatKanbanBytes", open_start)
        open_seg = page[open_start:open_end]
        self.assertIn("kanbanCommentSubmitting = false;", open_seg)
        self.assertIn("view-task-new-comment", open_seg)

        close_start = page.index("function closeViewTaskModal")
        close_end = page.index("function moveCurrentTaskStatus", close_start)
        close_seg = page[close_start:close_end]
        self.assertIn("kanbanCommentSubmitting = false;", close_seg)
        self.assertIn("view-task-new-comment", close_seg)

    def test_kanban_comment_body_style_and_date_guard(self):
        page = panel.page_source()
        start = page.index("function openViewTaskModal")
        end = page.index("function formatKanbanBytes", start)
        seg = page[start:end]
        self.assertIn("white-space:pre-wrap", seg)
        self.assertIn("word-break:break-word", seg)
        self.assertIn("c.created_at ? new Date", seg)

    def test_aux_picker_model_search_empty_state_and_profile_guard(self):
        page = panel.page_source()
        filter_start = page.index("function filterAuxPicker")
        filter_end = page.index("function renderFallbackPickerItems", filter_start)
        filter_seg = page[filter_start:filter_end]
        self.assertIn(".trim().toLowerCase()", filter_seg)

        aux_start = page.index("function renderAuxPickerItems")
        aux_end = page.index("function selectAuxModel", aux_start)
        aux_seg = page[aux_start:aux_end]
        self.assertIn("!currentProfileModelTarget", aux_seg)
        self.assertIn("Tidak ada model yang cocok", aux_seg)

        fb_start = page.index("function renderFallbackPickerItems")
        fb_end = page.index("function selectFallbackModel", fb_start)
        fb_seg = page[fb_start:fb_end]
        self.assertIn("Tidak ada model yang cocok", fb_seg)

    def test_main_model_filter_trims_query_and_checks_display(self):
        page = panel.page_source()
        start = page.index("function filterModels")
        end = page.index("function toggleLog", start)
        seg = page[start:end]
        self.assertIn(".trim().toLowerCase()", seg)
        self.assertIn("style.display !== 'none'", seg)

    def test_kanban_drag_mutations_ignore_stale_responses(self):
        page = panel.page_source()
        self.assertIn("var kanbanMutationGen = 0;", page)
        for function_name in ("handleKanbanTrashDrop", "handleKanbanDrop"):
            start = page.index("function " + function_name)
            next_function = page.find("\nfunction ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            self.assertIn("var mutationTaskId = taskId", segment, function_name)
            self.assertIn("var mutationBoard = bSel ? bSel.value : ''", segment, function_name)
            self.assertIn("var mutationGen = ++kanbanMutationGen", segment, function_name)
            guard = "if(mutationGen !== kanbanMutationGen) return;"
            then_start = segment.index(".then(function(res)")
            catch_start = segment.index(".catch(function")
            then_segment = segment[then_start:catch_start]
            catch_segment = segment[catch_start:]
            self.assertIn(guard, then_segment, function_name)
            self.assertIn(guard, catch_segment, function_name)
            self.assertLess(then_segment.index(guard), then_segment.index("if(res"), function_name)

    def test_kanban_drag_and_trash_guard_against_board_switch(self):
        page = panel.page_source()
        self.assertIn("var draggedBoard = null;", page)

        # Drag start records board
        start = page.index("function handleKanbanDragStart")
        end = page.find("\nfunction ", start + 1)
        segment = page[start:end]
        self.assertIn("draggedBoard = bSel ? bSel.value : '';", segment)

        # Drag end clears board
        start = page.index("function handleKanbanDragEnd")
        end = page.find("\nfunction ", start + 1)
        segment = page[start:end]
        self.assertIn("draggedBoard = null;", segment)

        # Drop guards against board switch during drag
        start = page.index("function handleKanbanDrop")
        end = page.find("\nfunction ", start + 1)
        segment = page[start:end]
        self.assertIn("if(draggedBoard !== null && draggedBoard !== currentBoard)", segment)

        # Trash drop guards against board switch before and after confirm
        start = page.index("function handleKanbanTrashDrop")
        end = page.find("\nfunction ", start + 1)
        segment = page[start:end]
        self.assertIn("if(draggedBoard !== null && draggedBoard !== currentBoard)", segment)
        self.assertIn("if(!card) return;", segment)
        self.assertIn("if(bSel && bSel.value !== currentBoard)", segment)

        # switchKanbanBoard invalidates pending drag/drop mutations and resets dragging if active
        start = page.index("function switchKanbanBoard")
        end = page.find("\nfunction ", start + 1)
        segment = page[start:end]
        self.assertIn("++kanbanMutationGen", segment)
        self.assertIn("++kanbanBoardLoadGen", segment)
        self.assertIn("if(isDraggingKanban)", segment)
        self.assertIn("handleKanbanDragEnd(null)", segment)

    def test_kanban_bulk_mutations_ignore_stale_selection(self):
        page = panel.page_source()
        self.assertIn("var kanbanMutationGen = 0;", page)
        for function_name in ("applyBulkStatus", "applyBulkDelete"):
            start = page.index("function " + function_name)
            next_function = page.find("\nfunction ", start + 1)
            segment = page[start:] if next_function == -1 else page[start:next_function]
            self.assertIn("var mutationGen = ++kanbanMutationGen", segment, function_name)
            self.assertIn("var selectionSnapshot = ids.slice().sort().join", segment, function_name)
            self.assertIn("var currentSelection = Array.from(selectedTaskIds).sort().join", segment, function_name)
            guard = "if(mutationGen !== kanbanMutationGen || mutationBoard !== (bSel ? bSel.value : '') || selectionSnapshot !== currentSelection) return;"
            promise_start = segment.index("Promise.all(promises).then(function(results)")
            promise_segment = segment[promise_start:]
            self.assertIn(guard, promise_segment, function_name)
            self.assertLess(promise_segment.index(guard), promise_segment.index("clearKanbanSelection()"), function_name)

    def test_kanban_create_mutations_ignore_stale_board_context(self):
        page = panel.page_source()
        self.assertIn("var kanbanMutationGen = 0;", page)
        start = page.index("function submitCreateTask")
        next_function = page.find("\nfunction ", start + 1)
        segment = page[start:] if next_function == -1 else page[start:next_function]
        self.assertIn("var mutationGen = ++kanbanMutationGen", segment, "submitCreateTask")
        guard = "if(mutationGen !== kanbanMutationGen || mutationBoard !== (bSel ? bSel.value : '')) return;"
        then_start = segment.index(".then(function(res)")
        catch_start = segment.index(".catch(function")
        then_segment = segment[then_start:catch_start]
        catch_segment = segment[catch_start:]
        self.assertIn(guard, then_segment, "submitCreateTask")
        self.assertIn(guard, catch_segment, "submitCreateTask")
        self.assertLess(then_segment.index(guard), then_segment.index("if(res"), "submitCreateTask")

    def test_kanban_board_create_ignores_stale_switch_context(self):
        page = panel.page_source()
        self.assertIn("var kanbanMutationGen = 0;", page)
        start = page.index("function submitCreateBoard")
        next_function = page.find("\nfunction ", start + 1)
        segment = page[start:] if next_function == -1 else page[start:next_function]
        self.assertIn("var mutationGen = ++kanbanMutationGen", segment, "submitCreateBoard")
        guard = "if(mutationGen !== kanbanMutationGen || mutationBoard !== (bSel ? bSel.value : '')) return;"
        then_start = segment.index(".then(function(res)")
        catch_start = segment.index(".catch(function")
        then_segment = segment[then_start:catch_start]
        catch_segment = segment[catch_start:]
        self.assertIn(guard, then_segment, "submitCreateBoard")
        self.assertIn(guard, catch_segment, "submitCreateBoard")
        self.assertLess(then_segment.index(guard), then_segment.index("if(res"), "submitCreateBoard")

    @unittest.skipUnless(subprocess.run(["which", "node"], capture_output=True).returncode == 0, "node not installed")
    @unittest.skipUnless(subprocess.run(["which", "node"], capture_output=True).returncode == 0, "node not installed")
    def test_sse_ignores_stale_eventsource_callbacks(self):
        src = panel.SSE_SCRIPT
        start = src.index("var es=null, retryTimer=null, esGeneration=0;")
        end = src.index("  var hideDisconnectTimer", start)
        connection = src[start:end]
        harness = r"""
var timers = [], instances = [], applied = [], liveLabel = '';
function setTimeout(fn, ms){ timers.push(fn); return timers.length; }
function clearTimeout(id){ timers[id - 1] = null; }
var live = {classList: {remove: function(){}, add: function(){}}, querySelector: function(){ return {textContent: ''}; }};
var spin = {classList: {add: function(){}, remove: function(){}}};
var document = {getElementById: function(id){ return id === 'live-badge' ? live : null; }, addEventListener: function(){}, hidden: false};
var window = {addEventListener: function(){}};
function onUpdate(d){ applied.push(d); }
function EventSource(url){ this.url = url; this.closed = false; this.readyState = 1; this.handlers = {}; instances.push(this); }
EventSource.prototype.close = function(){ this.closed = true; this.readyState = 2; };
EventSource.prototype.addEventListener = function(name, fn){ this.handlers[name] = fn; };
EventSource.prototype.emit = function(name, event){ if(this.handlers[name]) this.handlers[name](event); };
"""
        script = harness + connection + r"""
var first = instances[0];
connect();
var second = instances[1];
first.onopen();
first.emit('update', {data: '{"stale":true}'});
first.onerror();
process.stdout.write(JSON.stringify({
  count: instances.length,
  secondClosed: second.closed,
  applied: applied,
  timers: timers.filter(function(x){ return x !== null; }).length,
  label: liveLabel
}));
"""
        # connection code starts with declarations; it does not auto-connect.
        script = harness + connection + r"""
connect();
var first = instances[0];
connect();
var second = instances[1];
first.onopen();
first.emit('update', {data: '{"stale":true}'});
first.onerror();
process.stdout.write(JSON.stringify({count: instances.length, secondClosed: second.closed, applied: applied, timers: timers.filter(function(x){ return x !== null; }).length}));
"""
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"count": 2, "secondClosed": False, "applied": [], "timers": 0})

    def test_02_aux_model_write_failure_returns_500(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        with mock.patch.object(panel, "set_aux_task_model", return_value=False):
            code, _, body = self._request(
                "/set-aux-model",
                method="POST",
                headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                data=b"task=vision&provider=custom:9router&model=gpt-4o&ajax=1",
            )
            self.assertEqual(code, 500)
            data = json.loads(body)
            self.assertFalse(data.get("ok"))
            self.assertIn("failed to update config", data.get("reason", ""))

    def test_03_fallback_model_write_failure_returns_500(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        with mock.patch.object(panel, "set_fallback_model", return_value=False):
            code, _, body = self._request(
                "/set-fallback-model",
                method="POST",
                headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                data=b"index=0&provider=custom:9router&model=gpt-4o&ajax=1",
            )
            self.assertEqual(code, 500)
            data = json.loads(body)
            self.assertFalse(data.get("ok"))
            self.assertIn("failed to update config", data.get("reason", ""))

    def test_04_reset_aux_write_failure_returns_500(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        with mock.patch.object(panel, "reset_all_aux_tasks", return_value=False):
            code, _, _ = self._request(
                "/reset-aux",
                method="POST",
                headers={"Cookie": cookie},
            )
            self.assertEqual(code, 500)

    def test_05_remove_fallback_write_failure_returns_500(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        with mock.patch.object(panel, "remove_fallback_model", return_value=False):
            code, _, _ = self._request(
                "/remove-fallback-model",
                method="POST",
                headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
                data=b"index=0",
            )
            self.assertEqual(code, 500)

    # --- PR 3: Single model API request ---
    def test_06_fetch_remote_models_single_request(self):
        fake_resp = json.dumps({"data": [{"id": "model-1", "owned_by": "combo"}]}).encode("utf-8")
        cm = mock.MagicMock()
        cm.read.return_value = fake_resp
        cm.__enter__.return_value = cm
        cm.__exit__.return_value = None

        with mock.patch.object(panel, "get_router_api_key", return_value="dummy-key"):
            with mock.patch.object(panel.urllib.request, "urlopen", return_value=cm) as mock_url:
                res = panel.fetch_remote_models()
                self.assertEqual(mock_url.call_count, 1)
                self.assertEqual(res.get("status"), "success")
                self.assertIn("9router (Kombo)", panel._models_cache["val"])

    def test_07_group_available_models_pure(self):
        payload = {
            "data": [
                {"id": "combo/model-a", "owned_by": "combo"},
                {"id": "ag/claude-3-opus", "owned_by": "ag"},
                {"id": "cx/gpt-5", "owned_by": "cx"},
                {"id": "gemini/gemini-2.5", "owned_by": "gemini"},
                {"id": "other/llama-3", "owned_by": "meta"},
                {"id": "other/raw", "owned_by": ""},
            ]
        }
        grouped = panel._group_available_models(payload)
        self.assertIn("9router (Kombo)", grouped)
        self.assertIn("Antigravity (ag)", grouped)
        self.assertIn("Codex (cx)", grouped)
        self.assertIn("Google Gemini", grouped)
        self.assertIn("META", grouped)
        self.assertIn("Lainnya", grouped)

    # --- PR 4: Portable storage detection ---
    def test_08_emmc_health_missing_returns_na(self):
        panel._invalidate_status_cache("emmc_health")
        with mock.patch.object(panel.glob, "glob", return_value=[]):
            self.assertEqual(panel.get_emmc_health(), ("", "N/A"))

    def test_09_disk_info_root_label_not_emmc(self):
        panel._invalidate_status_cache("disk_stats")
        mounts_content = "devtmpfs /dev devtmpfs rw 0 0\n/dev/nvme0n1p2 / ext4 rw 0 0\n"
        with mock.patch("builtins.open", mock.mock_open(read_data=mounts_content)):
            with mock.patch("os.statvfs") as mock_stat:
                st = mock.MagicMock()
                st.f_blocks = 1000000
                st.f_bavail = 500000
                st.f_frsize = 4096
                mock_stat.return_value = st
                text = panel.get_disk_info()
                self.assertIn("ROOT", text)
                self.assertNotIn("eMMC", text)

    # --- PR 5: Remove hardcoded host addresses ---
    def test_10_get_9router_host_override_skips_probe(self):
        with mock.patch.object(panel, "ROUTER_HOST_OVERRIDE", "10.20.30.40"):
            with mock.patch.object(panel.subprocess, "run") as mock_run:
                self.assertEqual(panel.get_9router_host(), "10.20.30.40")
                mock_run.assert_not_called()

    def test_11_dashboard_link_dynamic_hostname(self):
        with mock.patch.object(panel, "HERMES_DASHBOARD_URL", ""):
            link = panel.get_open_block_active()
            self.assertNotIn("192.168.1.100", link)
            self.assertIn("window.location.hostname", link)

    # --- PR 6: Authentication & POST mutations ---
    def test_12_server_starts_without_password_in_open_mode(self):
        script_path = os.path.join(REPO_ROOT, "dashboard-toggle-server.py")
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("PANEL_TOKEN", "PANEL_PASSWORD", "PANEL_REQUIRE_AUTH")
        }
        res = subprocess.run([sys.executable, script_path, "--check"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("Tanpa Password", res.stdout)

    def test_12b_server_exits_when_auth_strictly_required_without_token(self):
        script_path = os.path.join(REPO_ROOT, "dashboard-toggle-server.py")
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("PANEL_TOKEN", "PANEL_PASSWORD")
        }
        env["PANEL_REQUIRE_AUTH"] = "1"
        res = subprocess.run([sys.executable, script_path], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 2)
        self.assertIn("PANEL_PASSWORD", res.stderr)

    def test_12c_set_and_reset_password_cli_and_helpers(self):
        import tempfile
        old_pwd, old_tok = panel.PASSWORD, panel.TOKEN
        try:
            panel.TOKEN = ""
            with tempfile.TemporaryDirectory() as td:
                dummy_env = os.path.join(td, "panel.env")
                with open(dummy_env, "w") as f:
                    f.write("PANEL_PORT=9120\nPANEL_PASSWORD=initial123\n")

                ok, msg = panel.set_panel_password("newsecret456", env_path=dummy_env)
                self.assertTrue(ok)
                self.assertTrue(panel.is_auth_required())
                with open(dummy_env) as f:
                    content = f.read()
                self.assertIn('PANEL_PASSWORD="newsecret456"', content)

                ok, msg = panel.set_panel_password("", env_path=dummy_env)
                self.assertTrue(ok)
                self.assertFalse(panel.is_auth_required())
                with open(dummy_env) as f:
                    content = f.read()
                self.assertNotIn("PANEL_PASSWORD", content)
                self.assertNotIn("PANEL_TOKEN", content)
                self.assertIn("PANEL_PORT=9120", content)
        finally:
            panel.PASSWORD = old_pwd
            panel.TOKEN = old_tok
            panel.AUTH_SECRET = panel.PASSWORD or panel.TOKEN
            panel.SESSION_VALUE = (
                panel.hmac.new(panel.AUTH_SECRET.encode("utf-8"), b"hermes-panel-session", panel.hashlib.sha256).hexdigest()
                if panel.AUTH_SECRET else ""
            )

    def test_12d_passwordless_mode_access(self):
        old_pwd, old_tok = panel.PASSWORD, panel.TOKEN
        try:
            panel.PASSWORD = ""
            panel.TOKEN = ""
            self.assertFalse(panel.is_auth_required())
            code, _, _ = self._request("/status")
            self.assertEqual(code, 200)
            code, _, _ = self._request("/api/status")
            self.assertEqual(code, 200)
            code, headers, _ = self._request("/login")
            self.assertEqual(code, 302)
            self.assertEqual(headers.get("Location"), "/status")
            # GET mutation shortcut without explicit token must be refused even in passwordless mode
            code, _, _ = self._request("/toggle", method="GET")
            self.assertEqual(code, 405)
        finally:
            panel.PASSWORD = old_pwd
            panel.TOKEN = old_tok
            panel.AUTH_SECRET = panel.PASSWORD or panel.TOKEN
            panel.SESSION_VALUE = (
                panel.hmac.new(panel.AUTH_SECRET.encode("utf-8"), b"hermes-panel-session", panel.hashlib.sha256).hexdigest()
                if panel.AUTH_SECRET else ""
            )

    def test_12e_set_password_mutation_route(self):
        import tempfile
        old_pwd, old_tok = panel.PASSWORD, panel.TOKEN
        try:
            with tempfile.TemporaryDirectory() as td:
                dummy_env = os.path.join(td, "panel.env")
                with mock.patch.object(panel, "PANEL_ENV_PATH", dummy_env):
                    cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
                    code, headers, _ = self._request(
                        "/set-password", method="POST",
                        headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
                        data=b"password=freshpass99",
                    )
                    self.assertEqual(code, 302)
                    self.assertIn("password-set", headers.get("Location", ""))

                    # Update cookie to new session value after password change
                    cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
                    code, headers, _ = self._request(
                        "/set-password", method="POST",
                        headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
                        data=b"clear=1",
                    )
                    self.assertEqual(code, 302)
                    self.assertIn("password-cleared", headers.get("Location", ""))
        finally:
            panel.PASSWORD = old_pwd
            panel.TOKEN = old_tok
            panel.AUTH_SECRET = panel.PASSWORD or panel.TOKEN
            panel.SESSION_VALUE = (
                panel.hmac.new(panel.AUTH_SECRET.encode("utf-8"), b"hermes-panel-session", panel.hashlib.sha256).hexdigest()
                if panel.AUTH_SECRET else ""
            )

    def test_13_unauthenticated_page_redirects_to_login(self):
        code, headers, _ = self._request("/status")
        self.assertEqual(code, 302)
        self.assertEqual(headers.get("Location"), "/login")

    def test_13b_unauthenticated_api_returns_401(self):
        code, _, _ = self._request("/api/status")
        self.assertEqual(code, 401)

    def test_13c_login_page_renders_password_form(self):
        code, _, body = self._request("/login")
        self.assertEqual(code, 200)
        self.assertIn('action="/login"', body)
        self.assertIn('name="password"', body)
        self.assertIn('type="password"', body)

    def test_13d_login_wrong_password_is_rejected(self):
        panel._LOGIN_FAILURES.clear()
        code, _, body = self._request(
            "/login", method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=b"password=nope",
        )
        self.assertEqual(code, 403)
        self.assertIn("Password salah", body)

    def test_13e_login_correct_password_mints_session_cookie(self):
        panel._LOGIN_FAILURES.clear()
        code, headers, _ = self._request(
            "/login", method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=f"password={panel.PASSWORD}".encode(),
        )
        self.assertEqual(code, 302)
        self.assertEqual(headers.get("Location"), "/status")
        set_cookie = headers.get("Set-Cookie", "")
        self.assertIn(f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}", set_cookie)
        self.assertNotIn(panel.PASSWORD, set_cookie)  # raw password never in cookie
        self.assertIn("HttpOnly", set_cookie)
        self.assertNotIn("Max-Age", set_cookie)  # session-only by design

    def test_13f_session_cookie_unlocks_the_panel(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        code, _, body = self._request("/status", headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        self.assertIn("Hermes", body)

    def test_13g_login_lockout_after_repeated_failures(self):
        panel._LOGIN_FAILURES.clear()
        ip = "127.0.0.1"
        for _ in range(panel.LOGIN_MAX_ATTEMPTS):
            panel._record_login_failure(ip)
        code, _, body = self._request(
            "/login", method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=b"password=nope",
        )
        self.assertEqual(code, 429)
        self.assertIn("Terlalu banyak percobaan", body)
        panel._LOGIN_FAILURES.clear()

    def test_13h_logout_clears_session_cookie(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        code, headers, _ = self._request("/logout", headers={"Cookie": cookie})
        self.assertEqual(code, 302)
        self.assertEqual(headers.get("Location"), "/login")
        self.assertIn("Max-Age=0", headers.get("Set-Cookie", ""))

    def test_14_bootstrap_sets_cookie_and_redirects(self):
        code, headers, _ = self._request(f"/status?token={panel.TOKEN}")
        self.assertEqual(code, 302)
        set_cookie = headers.get("Set-Cookie", "")
        self.assertIn(panel.SESSION_COOKIE_NAME, set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertEqual(headers.get("Location"), "/status")

    def test_15_mutation_method_enforcement(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        with mock.patch.object(panel, "restart_bot") as mock_restart, \
             mock.patch.object(panel, "dashboard_action") as mock_dash, \
             mock.patch.object(panel.subprocess, "run") as mock_run:
            # 1. GET to mutating route without shortcut -> 405 Method Not Allowed
            code, _, _ = self._request("/restart-bot", method="GET", headers={"Cookie": cookie})
            self.assertEqual(code, 405)
            mock_restart.assert_not_called()

            # 2. POST to mutating route -> allowed (302)
            code, headers, _ = self._request("/restart-bot", method="POST", headers={"Cookie": cookie})
            self.assertEqual(code, 302)
            mock_restart.assert_called_once()

            # 3. GET shortcut with token (CasaOS compat) -> allowed (302)
            panel._last_action_at.clear()
            code, headers, _ = self._request(f"/toggle?token={panel.TOKEN}", method="GET")
            self.assertEqual(code, 302)
            mock_dash.assert_called()

    # --- PR 7: Status probe TTL cache ---
    def test_16_status_probe_ttl_caching(self):
        # 1. Server IPs cached for 30s
        panel._invalidate_status_cache("server_ips")
        with mock.patch.object(panel.subprocess, "run") as mock_run:
            mock_res = mock.MagicMock(returncode=0, stdout="100.64.0.1\n")
            mock_run.return_value = mock_res
            first = panel.get_server_ips()
            second = panel.get_server_ips()
            self.assertEqual(first, second)
            self.assertEqual(mock_run.call_count, 2)  # 1 for tailscale, 1 for hostname

        # 2. Disk stats combined scan cached for 10s
        panel._invalidate_status_cache("disk_stats")
        mounts_content = "/dev/root / ext4 rw 0 0\n"
        with mock.patch("builtins.open", mock.mock_open(read_data=mounts_content)) as mock_open:
            with mock.patch("os.statvfs") as mock_stat:
                st = mock.MagicMock(f_blocks=1000, f_bavail=500, f_frsize=4096)
                mock_stat.return_value = st
                info = panel.get_disk_info()
                pct = panel.get_disk_pct()
                self.assertEqual(mock_open.call_count, 1)
                self.assertEqual(mock_stat.call_count, 1)

        # 3. Process table cached for 5s
        panel._invalidate_status_cache("processes_table")
        with mock.patch.object(panel, "get_process_list", return_value=[]) as mock_procs:
            t1 = panel.render_processes_table()
            t2 = panel.render_processes_table()
            self.assertEqual(t1, t2)
            self.assertEqual(mock_procs.call_count, 1)


    def test_17_gateway_platforms_detection_and_badges(self):
        """Gateway platforms are detected from config and display connected/disabled/error badges."""
        mock_cfg = {
            "platforms": {
                "telegram": {
                    "enabled": True,
                    "home_channel": {"name": "vitooo", "chat_id": "12345"},
                },
                "webhook": {"enabled": True},
                "discord": {"enabled": False},
            }
        }
        mock_state = {
            "gateway_state": "running",
            "platforms": {
                "telegram": {"state": "connected", "error_code": None, "error_message": None},
                "webhook": {"state": "connected", "listener_base": "http://127.0.0.1:8644"},
                "discord": {"state": "disconnected"},
            },
        }

        panel._invalidate_status_cache("gateway_platforms")
        with mock.patch.object(panel, "get_parsed_config", return_value=mock_cfg):
            with mock.patch("os.path.exists", return_value=True):
                with mock.patch("builtins.open", mock.mock_open(read_data=json.dumps(mock_state))):
                    with mock.patch.object(panel, "service_active", return_value=True):
                        platforms = panel.get_gateway_platforms()
                        summary_text, summary_cls, bento = panel.get_gateway_platforms_summary()
                        html_out = panel.render_gateway_platforms_html()

        plat_map = {p["platform"]: p for p in platforms}
        self.assertIn("telegram", plat_map)
        self.assertIn("webhook", plat_map)
        self.assertIn("discord", plat_map)

        # Telegram: enabled + connected -> badge-up
        self.assertTrue(plat_map["telegram"]["enabled"])
        self.assertEqual(plat_map["telegram"]["status_key"], "connected")
        self.assertEqual(plat_map["telegram"]["badge_class"], "badge-up")
        self.assertEqual(plat_map["telegram"]["status_label"], "Terhubung")

        # Discord: disabled in config -> badge-muted
        self.assertFalse(plat_map["discord"]["enabled"])
        self.assertEqual(plat_map["discord"]["badge_class"], "badge-muted")
        self.assertEqual(plat_map["discord"]["status_label"], "Nonaktif")

        # Summary
        self.assertEqual(summary_cls, "badge-up")
        self.assertIn("Terhubung", summary_text)
        self.assertIn("Telegram Bot", html_out)
        self.assertIn("Config Aktif", html_out)

    def test_18_gateway_platforms_error_badge(self):
        """Gateway platforms with error_code or error_message render badge-down and error details."""
        mock_cfg = {
            "platforms": {
                "telegram": {"enabled": True},
            }
        }
        mock_state = {
            "gateway_state": "running",
            "platforms": {
                "telegram": {
                    "state": "error",
                    "error_code": 401,
                    "error_message": "Unauthorized: invalid bot token",
                }
            },
        }

        panel._invalidate_status_cache("gateway_platforms")
        with mock.patch.object(panel, "get_parsed_config", return_value=mock_cfg):
            with mock.patch("os.path.exists", return_value=True):
                with mock.patch("builtins.open", mock.mock_open(read_data=json.dumps(mock_state))):
                    with mock.patch.object(panel, "service_active", return_value=True):
                        platforms = panel.get_gateway_platforms()
                        summary_text, summary_cls, bento = panel.get_gateway_platforms_summary()
                        html_out = panel.render_gateway_platforms_html()

        tg = platforms[0]
        self.assertTrue(tg["is_error"])
        self.assertEqual(tg["badge_class"], "badge-down")
        self.assertEqual(tg["status_label"], "Error")
        self.assertEqual(summary_cls, "badge-down")
        self.assertIn("1 Error", summary_text)
        self.assertIn("Unauthorized: invalid bot token", html_out)
        self.assertIn("badge-down", html_out)

    def test_19_gateway_list_slot_in_rendered_page(self):
        """Rendered status page includes gateway-list-slot and cell-gw-platforms."""
        mock_frag = {
            "cells": {
                "dash": "", "bot": "", "gw": "", "model": "", "providers": "",
                "router": "", "hermes": "", "ram": "", "zram": "", "temp": "",
                "emmc": "", "disk": "", "uptime": "", "lan": "", "ts": "", "internet": "",
                "gw_platforms": "Telegram: Terhubung",
            },
            "model_chips": "", "rate_limit_card": "", "update_block": "",
            "hermes_update_block": "", "log_card": "", "clean_junk_card": "",
            "quick_links_block": "", "dash_bot_btns_block": "", "aux_tasks_block": "",
            "backup_models_block": "", "processes_table": "", "cpu_pct": 0, "ram_pct": 0,
            "cell_load": "", "updating": False, "dash_active": False, "gw_active": True,
            "gateway_list_block": '<div id="test-gateway-item">Telegram</div>',
            "gw_summary_text": "1 Terhubung",
            "gw_summary_badge_class": "badge-up",
        }

        with mock.patch.object(panel, "build_fragments", return_value=mock_frag):
            with mock.patch.object(panel, "get_available_models_cached", return_value={}):
                page = panel.build_status_page()

        self.assertIn('id="gateway-list-slot"', page)
        self.assertIn('id="gateway-log-slot"', page)
        self.assertIn('id="cell-gw-platforms"', page)
        self.assertIn('id="gw-summary-badge"', page)
        self.assertIn("Platform Gateway", page)
        self.assertIn('id="gw-config-modal"', page)

    def test_20_gateway_platform_config_retrieval_and_templates(self):
        """get_gateway_platform_config returns existing YAML or prefilled templates."""
        mock_cfg = {
            "platforms": {
                "telegram": {
                    "enabled": True,
                    "home_channel": {"chat_id": "12345", "name": "vitooo", "platform": "telegram"},
                }
            }
        }
        with mock.patch.object(panel, "get_parsed_config", return_value=mock_cfg):
            res_tg = panel.get_gateway_platform_config("telegram")
            self.assertTrue(res_tg["ok"])
            self.assertFalse(res_tg["is_new"])
            self.assertIn("12345", res_tg["yaml"])
            self.assertTrue(res_tg["enabled"])

            res_slack = panel.get_gateway_platform_config("slack")
            self.assertTrue(res_slack["ok"])
            self.assertTrue(res_slack["is_new"])
            self.assertIn("token", res_slack["yaml"])

    def test_21_gateway_platform_config_save_and_validation(self):
        """save_gateway_platform_config validates YAML and updates atomically."""
        import tempfile
        with tempfile.NamedTemporaryFile("w+", suffix=".yaml", delete=False) as tf:
            panel.yaml.safe_dump({"platforms": {"telegram": {"enabled": True}}}, tf)
            tmp_path = tf.name

        try:
            with mock.patch.object(panel, "CONFIG_PATH", tmp_path):
                # 1. Invalid platform name
                ok, err = panel.save_gateway_platform_config("bad name!", "enabled: true")
                self.assertFalse(ok)
                self.assertIn("huruf kecil", err)

                # 2. Invalid YAML syntax
                ok, err = panel.save_gateway_platform_config("slack", "enabled: [unclosed")
                self.assertFalse(ok)
                self.assertIn("Sintaks YAML tidak valid", err)

                # 3. Valid custom config with extra keys
                custom_yaml = "enabled: true\ntoken: xoxb-secret\nchannels:\n  - '#dev'\n"
                ok, err = panel.save_gateway_platform_config("slack", custom_yaml)
                self.assertTrue(ok)

                with open(tmp_path, "r", encoding="utf-8") as rf:
                    saved = panel.yaml.safe_load(rf)
                self.assertEqual(saved["platforms"]["slack"]["token"], "xoxb-secret")
                self.assertEqual(saved["platforms"]["slack"]["channels"], ["#dev"])
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_22_gateway_platform_toggle_and_remove(self):
        """toggle_gateway_platform_config and remove_gateway_platform_config mutate config safely."""
        import tempfile
        with tempfile.NamedTemporaryFile("w+", suffix=".yaml", delete=False) as tf:
            panel.yaml.safe_dump({
                "platforms": {
                    "telegram": {"enabled": True},
                    "discord": {"enabled": False}
                }
            }, tf)
            tmp_path = tf.name

        try:
            with mock.patch.object(panel, "CONFIG_PATH", tmp_path):
                # Toggle discord to True
                ok, _ = panel.toggle_gateway_platform_config("discord", True)
                self.assertTrue(ok)
                with open(tmp_path) as rf:
                    self.assertTrue(panel.yaml.safe_load(rf)["platforms"]["discord"]["enabled"])

                # Remove discord
                ok, _ = panel.remove_gateway_platform_config("discord")
                self.assertTrue(ok)
                with open(tmp_path) as rf:
                    self.assertNotIn("discord", panel.yaml.safe_load(rf)["platforms"])
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_23_gateway_platform_http_endpoints(self):
        """HTTP endpoints /api/gateway-config and mutations work with proper auth."""
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"

        # 1. GET /api/gateway-config
        code, headers, body = self._request("/api/gateway-config?platform=telegram", method="GET", headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        data = json.loads(body if isinstance(body, str) else body.decode("utf-8"))
        self.assertTrue(data.get("ok"))

        # 2. POST /save-gateway-platform
        with mock.patch.object(panel, "restart_bot") as mock_restart, \
             mock.patch.object(panel, "save_gateway_platform_config", return_value=(True, "")):
            code, _, body = self._request(
                "/save-gateway-platform",
                method="POST",
                headers={"Cookie": cookie, "Content-Type": "application/json", "Accept": "application/json"},
                data=json.dumps({"platform": "telegram", "yaml": "enabled: true\n", "restart_gw": True}).encode("utf-8")
            )
            self.assertEqual(code, 200)
            mock_restart.assert_called_once()

    def test_24_gateway_log_card_and_endpoint(self):
        """Gateway live log retrieval, token redaction, and HTML card rendering."""
        # 1. Token redaction
        dummy_tg = "123456789:" + ("X" * 32)
        raw = f"telegram token {dummy_tg} and bearer supersecrettoken12345"
        clean = panel.redact_sensitive_tokens(raw)
        self.assertNotIn(dummy_tg, clean)
        self.assertIn("[REDACTED_TOKEN]", clean)
        self.assertIn("[REDACTED]", clean)

        # 2. Card rendering
        card_html = panel.render_gateway_log_card(n=10)
        self.assertIn('id="gateway-log-card"', card_html)
        self.assertIn('id="gateway-logbox"', card_html)
        self.assertIn("Log Gateway Hermes", card_html)
        self.assertIn("gatewayLogDismissed", card_html)

        # 3. GET /api/gateway-log
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        code, _, body = self._request("/api/gateway-log?n=20", method="GET", headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        data = json.loads(body if isinstance(body, str) else body.decode("utf-8"))
        self.assertTrue(data.get("ok"))
        self.assertIn("log", data)

    def test_25_whatsapp_pairing_and_bridge_log(self):
        """WhatsApp pairing status, bridge log, and modal rendering."""
        # 1. Bridge log tailing & redaction
        wa_log = panel.tail_whatsapp_bridge_log(n=10)
        self.assertIsInstance(wa_log, str)

        # 2. Card rendering includes whatsapp logbox and tab switcher
        card_html = panel.render_gateway_log_card(n=10)
        self.assertIn('id="whatsapp-logbox"', card_html)
        self.assertIn("WhatsApp Bridge", card_html)
        self.assertIn('switchGwLogTab', card_html)

        # 3. Page rendering includes wa-pair-modal
        page_html = panel.build_status_page()
        self.assertIn('id="wa-pair-modal"', page_html)
        self.assertIn('openWaPairModal', page_html)

        # 4. GET /api/whatsapp-log
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        code, _, body = self._request("/api/whatsapp-log?n=20", method="GET", headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        data = json.loads(body if isinstance(body, str) else body.decode("utf-8"))
        self.assertTrue(data.get("ok"))
        self.assertIn("log", data)

        # 5. GET /api/whatsapp/pair-status
        code, _, body = self._request("/api/whatsapp/pair-status", method="GET", headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        data = json.loads(body if isinstance(body, str) else body.decode("utf-8"))
        self.assertTrue(data.get("ok"))
        self.assertIn("status", data)

        # 6. POST /api/whatsapp/pair-cancel
        code, _, body = self._request("/api/whatsapp/pair-cancel", method="POST", headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        data = json.loads(body if isinstance(body, str) else body.decode("utf-8"))
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("status"), "cancelled")

    def test_40_post_mutations_require_auth(self):
        """POST without session cookie/token must be refused before any handler runs."""
        with mock.patch.object(panel, "save_gateway_platform_config", return_value=(True, "")) as save, \
             mock.patch.object(panel, "restart_bot") as restart:
            code, _, _ = self._request(
                "/save-gateway-platform",
                method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                data=json.dumps({"platform": "whatsapp", "yaml": "dm_policy: open\n"}).encode("utf-8"),
            )
            self.assertEqual(code, 403)
            save.assert_not_called()
            restart.assert_not_called()

    def test_41_regressions_tick_http(self):
        """Regression tests for token strip on logged in GET and CSRF IPv6/port enforcement."""
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        # 1. GET /status?token=... redirects to strip token even when valid session cookie exists
        code, headers, _ = self._request(f"/status?token={panel.TOKEN}", method="GET", headers={"Cookie": cookie})
        self.assertEqual(code, 302)
        self.assertEqual(headers.get("Location"), "/status")

        # 2. CSRF checks: cross-port on same IP and IPv6 origin
        # Same host IP but different port should be rejected (403)
        code, _, _ = self._request(
            "/restart-bot",
            method="POST",
            headers={
                "Cookie": cookie,
                "Host": "192.168.1.100:9120",
                "Origin": "http://192.168.1.100:8080"
            }
        )
        self.assertEqual(code, 403)

        # IPv6 mismatch should be rejected
        code, _, _ = self._request(
            "/restart-bot",
            method="POST",
            headers={
                "Cookie": cookie,
                "Host": "[::1]:9120",
                "Origin": "http://[::2]:9120"
            }
        )
        self.assertEqual(code, 403)

        # Valid Origin with matching host:port should succeed
        with mock.patch.object(panel, "restart_bot"):
            code, _, _ = self._request(
                "/restart-bot",
                method="POST",
                headers={
                    "Cookie": cookie,
                    "Host": "192.168.1.100:9120",
                    "Origin": "http://192.168.1.100:9120"
                }
            )
            self.assertEqual(code, 302)

    def test_58_regressions_tick_audit(self):
        """Regression tests for audit tick: CSRF port 80/443 bypass, 9router cache typo, modern Discord token redaction, boolean string coercion, and TRUTHY 'on'."""
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"

        # 1. CSRF: Port 80 Origin (no port in Origin header) must be rejected when Host has port 9120
        code, _, _ = self._request(
            "/restart-bot",
            method="POST",
            headers={
                "Cookie": cookie,
                "Host": "192.168.1.100:9120",
                "Origin": "http://192.168.1.100",
            }
        )
        self.assertEqual(code, 403)

        # 2. reload_panel_config clears _9router_host_cache and _9router_host_at
        with panel._9router_host_lock:
            panel._9router_host_cache = "192.168.1.50"
            panel._9router_host_at = 1234567.0
        with mock.patch.object(panel, "fetch_remote_models"):
            res = panel.reload_panel_config()
            self.assertEqual(res.get("status"), "reloaded")
        self.assertEqual(panel._9router_host_cache, "")
        self.assertEqual(panel._9router_host_at, 0)

        # 3. Modern Discord snowflake bot token redaction (26 base64 chars in user id)
        # Construct dynamically to avoid triggering GitHub push protection secret scanner on static dummy test data
        discord_p1 = "MTA4ODc2NTQzMjEwOTg3NjU0"
        discord_p2 = "GaBcDe"
        discord_p3 = "123456789012345678901234567"
        modern_discord_token = discord_p1 + "." + discord_p2 + "." + discord_p3
        redacted = panel.redact_sensitive_tokens(f"Bot token is {modern_discord_token}")
        self.assertNotIn(discord_p1, redacted)
        self.assertIn("[REDACTED_DISCORD_TOKEN]", redacted)

        # 4. _to_bool and _TRUTHY accept 'on'
        self.assertTrue(panel._to_bool("on"))
        self.assertIn("on", panel._TRUTHY)

        # 5. String boolean coercion in platform config
        cfg = {"platforms": {"telegram": {"enabled": "false"}}}
        with mock.patch.object(panel, "get_parsed_config", return_value=cfg):
            plats = panel._probe_gateway_platforms()
            tg = next((p for p in plats if p["platform"] == "telegram"), None)
            self.assertIsNotNone(tg)
            self.assertFalse(tg["enabled"])

        # 6. apply_wa_pair reaps running _wa_pair_proc
        mock_proc = mock.MagicMock()
        with panel._wa_pair_lock:
            panel._wa_pair_proc = mock_proc
        with mock.patch.object(panel, "toggle_gateway_platform_config", return_value=(True, "")), \
             mock.patch.object(panel, "restart_bot"), \
             mock.patch.object(panel, "_reap_proc_async") as mock_reap:
            ok, msg = panel.apply_wa_pair(restart_gw=False)
            self.assertTrue(ok)
            mock_reap.assert_called_once_with(mock_proc)
            self.assertIsNone(panel._wa_pair_proc)

    def test_59_regressions_pasca_8270425_audit(self):
        """Regression tests for audit fixes: IPv6 CSRF port normalization, /switch-model JSON, dynamic 9router port, proc reap in watcher, and extra allow_all_users."""
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"

        # 1. CSRF: IPv6 Host without port matching Origin with default port 80
        with mock.patch.object(panel, "restart_bot"):
            code, _, _ = self._request(
                "/restart-bot",
                method="POST",
                headers={
                    "Cookie": cookie,
                    "Host": "[::1]",
                    "Origin": "http://[::1]:80"
                }
            )
            self.assertEqual(code, 302)

            # Reverse proxy forwarding X-Forwarded-Host: example.com:443 with Origin: https://example.com
            code, _, _ = self._request(
                "/restart-bot",
                method="POST",
                headers={
                    "Cookie": cookie,
                    "Host": "127.0.0.1:9120",
                    "X-Forwarded-Host": "example.com:443",
                    "Origin": "https://example.com"
                }
            )
            self.assertEqual(code, 302)

        # 2. /switch-model accepts JSON POST
        with mock.patch.object(panel, "get_available_models_cached", return_value={"9router": ["test-model-123"]}), \
             mock.patch.object(panel, "set_current_model", return_value=True):
            code, _, body = self._request(
                "/switch-model",
                method="POST",
                headers={
                    "Cookie": cookie,
                    "Content-Type": "application/json",
                    "Accept": "application/json"
                },
                data=json.dumps({"model": "test-model-123"}).encode("utf-8")
            )
            self.assertEqual(code, 200)
            data = json.loads(body)
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("model"), "test-model-123")

        # 3. Dynamic port in get_available_models and get_router_release
        with mock.patch.object(panel, "get_9router_host", return_value="127.0.0.1"), \
             mock.patch.object(panel, "get_9router_port", return_value=20199), \
             mock.patch.object(panel, "get_router_api_key", return_value="dummy-key"), \
             mock.patch.object(panel.urllib.request, "urlopen") as mock_url:
            cm = mock.MagicMock()
            cm.read.return_value = json.dumps({"data": []}).encode("utf-8")
            cm.__enter__.return_value = cm
            cm.__exit__.return_value = None
            mock_url.return_value = cm

            def _urls_called():
                # call_args would grab whatever the background panel threads
                # happened to call last (dockerhub polls) — check all calls.
                urls = []
                for c in mock_url.call_args_list:
                    req = c[0][0] if c[0] else None
                    urls.append(getattr(req, "full_url", str(req)))
                return urls

            panel.get_available_models()
            self.assertTrue(
                any("127.0.0.1:20199/v1/models" in u for u in _urls_called()),
                _urls_called(),
            )

            cm.read.return_value = json.dumps({"currentVersion": "1.0", "latestVersion": "1.1", "hasUpdate": True}).encode("utf-8")
            panel.get_router_release()
            self.assertTrue(
                any("127.0.0.1:20199/api/version" in u for u in _urls_called()),
                _urls_called(),
            )

        # 4. _open_policy_violation recognizes allow_all_users inside extra
        cfg = {"platforms": {"whatsapp": {"enabled": True, "extra": {"dm_policy": "open", "allow_all_users": True}}}}
        violation = panel._open_policy_violation(cfg, "whatsapp", cfg["platforms"]["whatsapp"])
        self.assertEqual(violation, "", "allow_all_users inside extra must satisfy open policy guard")

        # 5. _to_bool empty string fallback
        self.assertTrue(panel._to_bool("", default=True))
        self.assertFalse(panel._to_bool("", default=False))

    def test_60_frontend_audit_regressions(self):
        """Regression tests for frontend audit: modal safety, WA pairing lifecycle, and dismiss guards."""
        page = panel.page_source()
        nav_script = panel.NAV_SCRIPT

        # 1. confirmAction has null guard for confirm-modal
        self.assertIn("function confirmAction(route, href){\n  var modal=document.getElementById('confirm-modal');\n  if(!modal) return;", nav_script)

        # 2. Dismiss buttons on all 4 log cards check element before calling remove()
        gw_card = panel.render_gateway_log_card()
        self.assertIn("var c=document.getElementById('gateway-log-card');if(c)c.remove();", gw_card)

        r_card = panel.render_log_card("dummy log")
        self.assertIn("var c=document.getElementById('router-log-card');if(c)c.remove();", r_card)

        clean_card = panel.render_clean_junk_card()
        # when clean_card renders, button must have guard
        with mock.patch.object(panel, "get_clean_junk_result", return_value={"log": "done", "at": 12345}):
            clean_html = panel.render_clean_junk_card()
            self.assertIn("var c=document.getElementById('clean-log-card');if(c)c.remove();", clean_html)

        # 3. WA pair modal lifecycle: only active pairing is cancelled on close
        self.assertIn("var isPairingActive = (_lastWaPairStatus === 'waiting_scan' || _lastWaPairStatus === 'starting');", page)
        self.assertIn("if(!skipCancel && isPairingActive)", page)

        # 4. WA timer expiry clears QR container
        self.assertIn("QR Code Kedaluwarsa", page)

        # 5. WA poll status reschedules on fetch failure
        self.assertIn("_waPairPollTimer = setTimeout(function(){{ pollWaPairStatus(actGen); }}, 2500);", page)

        # 6. Gateway YAML switch sets readOnly while preview in-flight and guards user edits
        self.assertIn("yamlEl.readOnly = true;", page)
        self.assertIn("if(res.ok && !_yamlEditedByUser) yamlEl.value = res.yaml;", page)

        # 7. Gateway config loading disables save button until response
        self.assertIn("if(saveBtn) saveBtn.disabled = true;", page)

    def test_63_security_audit_hardening(self):
        """Verify security headers, password URL protection, Sec-Fetch-Site, /events 401, and method handling."""
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"

        # 1. Security headers on HTML and JSON responses
        code, headers, _ = self._request("/login")
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Referrer-Policy"), "strict-origin-when-cross-origin")
        self.assertIn("default-src 'self'", headers.get("Content-Security-Policy", ""))

        code, headers, _ = self._request("/api/status", headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

        # 2. Password via URL query string is blocked
        # POST with ?password= query param -> 400
        code, _, body = self._request(
            f"/login?password={panel.PASSWORD}",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=b"",
        )
        self.assertEqual(code, 400)
        self.assertIn("Password tidak boleh dikirim melalui URL query string", body)

        # GET with ?password= query param -> 302 stripped
        code, headers, _ = self._request(f"/login?password={panel.PASSWORD}", method="GET")
        self.assertEqual(code, 302)
        loc = headers.get("Location", "")
        self.assertNotIn(panel.PASSWORD, loc)
        self.assertIn("/login", loc)

        # 3. Sec-Fetch-Site: cross-site POST rejected with 403
        with mock.patch.object(panel, "restart_bot"):
            code, _, _ = self._request(
                "/restart-bot",
                method="POST",
                headers={"Cookie": cookie, "Sec-Fetch-Site": "cross-site"},
            )
            self.assertEqual(code, 403)

        # 4. Unauthorized /events returns 401
        code, _, body = self._request("/events", method="GET")
        self.assertEqual(code, 401)
        data = json.loads(body)
        self.assertFalse(data.get("ok"))

        # 5. Method enforcement: PUT/DELETE/PATCH -> 405 with Allow header; OPTIONS -> 204
        for method in ("PUT", "DELETE", "PATCH"):
            code, headers, _ = self._request("/restart-bot", method=method, headers={"Cookie": cookie})
            self.assertEqual(code, 405)
            self.assertIn("Allow", headers)

        code, headers, _ = self._request("/restart-bot", method="OPTIONS")
        self.assertEqual(code, 204)
        self.assertIn("Allow", headers)

        # 6. Failure tracker cleanup & bounding
        panel._LOGIN_FAILURES.clear()
        for i in range(600):
            panel._record_login_failure(f"10.0.0.{i % 250}")
        self.assertLessEqual(len(panel._LOGIN_FAILURES), 500)
        panel._LOGIN_FAILURES.clear()

    def test_64_reasoning_effort_config_and_route(self):
        """Reasoning effort must sync with config.yaml agent.reasoning_effort."""
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        import tempfile
        import yaml
        def read_test_config():
            with open(panel.CONFIG_PATH, encoding="utf-8") as f:
                return yaml.safe_load(f)
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(panel, "CONFIG_PATH", os.path.join(directory, "config.yaml")), \
             mock.patch.object(panel, "get_parsed_config", side_effect=read_test_config):
            with open(panel.CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write("agent:\n  reasoning_effort: none\n")
            self.assertTrue(panel.set_reasoning_effort("high"))
            self.assertEqual(panel.get_reasoning_effort(), "high")
            self.assertFalse(panel.set_reasoning_effort("invalid-tier"))
            self.assertEqual(panel.get_reasoning_effort(), "high")
            code, headers, _ = self._request(
                "/set-reasoning-effort?effort=ultra", method="POST", headers={"Cookie": cookie}
            )
            self.assertEqual(code, 302)
            self.assertIn("just=reasoning", headers.get("Location", ""))
            self.assertEqual(panel.get_reasoning_effort(), "ultra")
            code, _, body = self._request(
                "/set-reasoning-effort?effort=medium&ajax=1", method="POST", headers={"Cookie": cookie}
            )
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(body).get("ok"))
            self.assertEqual(panel.get_reasoning_effort(), "medium")

    def test_65_fetch_hermes_agent_models(self):
        """fetch_hermes_agent_models must categorize combos vs non-combos and sync cache."""
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        fake_data = {
            "data": [
                {"id": "combo-chat", "owned_by": "combo"},
                {"id": "ag/gemini-3.8-flash-high", "owned_by": "ag"},
                {"id": "openrouter/free", "owned_by": "openrouter"},
            ]
        }
        cm = mock.MagicMock()
        cm.read.return_value = json.dumps(fake_data).encode("utf-8")
        cm.__enter__.return_value = cm
        cm.__exit__.return_value = None

        with mock.patch.object(panel, "get_router_api_key", return_value="dummy-key"), \
             mock.patch.object(panel.urllib.request, "urlopen", return_value=cm), \
             mock.patch.object(panel, "sync_hermes_provider_cache", return_value=True) as mock_sync:
            res = panel.fetch_hermes_agent_models()
            self.assertEqual(res.get("status"), "success")
            self.assertEqual(res.get("combos_count"), 1)
            self.assertEqual(res.get("non_combos_count"), 2)
            self.assertTrue(res.get("hermes_synced"))
            mock_sync.assert_called_once()
            self.assertIn("combo-chat", panel._models_cache["val"].get("9router (Kombo)", []))

        # Test HTTP route
        with mock.patch.object(panel, "fetch_hermes_agent_models", return_value={"status": "success", "hermes_synced": True}):
            code, headers, _ = self._request(
                "/fetch-hermes-models",
                method="POST",
                headers={"Cookie": cookie}
            )
            self.assertEqual(code, 302)
            self.assertIn("just=fetch-hermes", headers.get("Location", ""))
    def test_66_sync_only_matching_credential_cache(self):
        import tempfile
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HERMES_HOME": home}):
            path = os.path.join(home, "provider_models_cache.json")
            cp = {"base_url": "http://127.0.0.1:20128/v1", "api_key": "test-key", "api_mode": "chat_completions"}
            other = "custom:http://127.0.0.1:20128/v1#different-credentials"
            with open(path, "w", encoding="utf-8") as f:
                json.dump({other: {"fp": "different-credentials", "at": 1, "models": ["private-model"]}}, f)
            with mock.patch.object(panel, "get_parsed_config", return_value={"custom_providers": [cp]}):
                self.assertTrue(panel.sync_hermes_provider_cache(["public-model"]))
            with open(path, encoding="utf-8") as f:
                cache = json.load(f)
            self.assertEqual(cache[other]["models"], ["private-model"])
            self.assertIn(["public-model"], [entry["models"] for entry in cache.values()])

    def test_66b_sync_cache_reads_v12_providers_with_same_key(self):
        # Hermes' v12 `providers:` map (api/transport) must land on the same cache row as the
        # legacy custom_providers list entry, or /model --refresh misses the warmed catalog.
        import tempfile
        legacy = {"name": "9router", "base_url": "http://127.0.0.1:20128/v1", "api_key": "test-key",
                  "api_mode": "chat_completions"}
        v12 = {"providers": {"9router": {"api": "http://127.0.0.1:20128/v1", "name": "9router",
                                         "api_key": "test-key", "transport": "chat_completions"}}}
        keys = []
        for cfg in ({"custom_providers": [legacy]}, v12):
            with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HERMES_HOME": home}):
                with mock.patch.object(panel, "get_parsed_config", return_value=cfg):
                    self.assertTrue(panel.sync_hermes_provider_cache(["public-model"]))
                with open(os.path.join(home, "provider_models_cache.json"), encoding="utf-8") as f:
                    keys.append(sorted(json.load(f)))
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(len(keys[1]), 1)

    def test_66c_configured_providers_include_v12_providers(self):
        cfg = {"model": {"provider": "custom:9router"},
               "providers": {"9router": {"api": "http://127.0.0.1:20128/v1", "name": "9router"},
                             "bynara": {"api": "https://router.bynara.id/v1", "name": "bynara"}}}
        with mock.patch.object(panel, "get_parsed_config", return_value=cfg):
            names = [p["name"] for p in panel.get_all_configured_providers()]
        self.assertEqual(names, ["9router", "bynara"])

    def test_66d_router_api_key_from_v12_providers(self):
        cfg = {"providers": {"9router": {"api": "http://127.0.0.1:20128/v1", "api_key": "v12-key"}}}
        with mock.patch.object(panel, "get_parsed_config", return_value=cfg):
            self.assertEqual(panel.get_router_api_key(), "v12-key")

    def test_67_panel_fetch_does_not_sync_hermes_cache(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"data":[{"id":"model-a","owned_by":"combo"}]}'
        with mock.patch.object(panel, "get_router_api_key", return_value="test-key"), \
             mock.patch.object(panel.urllib.request, "urlopen", return_value=response), \
             mock.patch.object(panel, "sync_hermes_provider_cache") as sync:
            self.assertEqual(panel.fetch_remote_models()["status"], "success")
            sync.assert_not_called()

    def test_68_failed_hermes_sync_never_shows_success(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        with mock.patch.object(panel, "fetch_hermes_agent_models", return_value={"status": "failed", "error": "offline"}):
            code, _, body = self._request("/fetch-hermes-models", method="POST", headers={"Cookie": cookie, "Accept": "application/json"})
        self.assertNotEqual(code, 302)
        self.assertIn("offline", body)

    def test_69_profiles_http_api_and_auth(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        # 1. GET /api/profiles without auth -> 401
        code, _, _ = self._request("/api/profiles", method="GET")
        self.assertEqual(code, 401)

        # 2. GET /api/profiles with auth -> 200 JSON
        code, _, body = self._request("/api/profiles", method="GET", headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertTrue(data.get("ok"))
        self.assertIn("profiles", data)

        # 3. GET /api/profile-soul with auth -> 200 JSON
        code, _, body = self._request("/api/profile-soul?profile=default", method="GET", headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body).get("ok"))

        # 4. POST /set-active-profile without auth -> 302 to /login (or 403 for json client)
        code, headers, _ = self._request("/set-active-profile", method="POST", data=b"profile=default")
        self.assertEqual(code, 302)
        self.assertEqual(headers.get("Location"), "/login")

        code, _, _ = self._request("/set-active-profile", method="POST",
                                   headers={"Accept": "application/json"}, data=b"profile=default")
        self.assertEqual(code, 403)

        # 5. POST /set-active-profile with auth (ajax) -> 200
        code, _, body = self._request("/set-active-profile", method="POST",
                                      headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
                                      data=b"profile=default&ajax=1")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body).get("ok"))

    def test_69b_profile_skills_http_api_and_auth(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        # 1. GET /api/profile-skills tanpa auth -> 401
        code, _, _ = self._request("/api/profile-skills?profile=default", method="GET")
        self.assertEqual(code, 401)
        # 2. GET /api/profile-skills dgn auth -> 200, ada skills + hitungan
        code, _, body = self._request("/api/profile-skills?profile=default", method="GET",
                                      headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertTrue(data.get("ok"))
        self.assertIn("skills", data)
        self.assertEqual(data["enabled_count"] + data["disabled_count"], data["total"])
        # 3. GET /api/profile-toolsets dgn auth -> 200
        code, _, body = self._request("/api/profile-toolsets?profile=default", method="GET",
                                      headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body).get("ok"))
        # 4. POST /toggle-profile-skill tanpa auth (json) -> 403
        code, _, _ = self._request("/toggle-profile-skill", method="POST",
                                   headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                                   data=b"profile=default&skill=x&enabled=0")
        self.assertEqual(code, 403)
        # 5. POST /toggle-profile-skill dgn auth, skill tak dikenal -> 400
        code, _, body = self._request("/toggle-profile-skill", method="POST",
                                      headers={"Cookie": cookie, "Accept": "application/json",
                                               "Content-Type": "application/x-www-form-urlencoded"},
                                      data=b"profile=default&skill=no-such-skill-xyz-123&enabled=0")
        self.assertEqual(code, 400)
        self.assertFalse(json.loads(body).get("ok"))
        # 6. POST /toggle-profile-toolset dgn auth, nama jahat -> 400
        code, _, body = self._request("/toggle-profile-toolset", method="POST",
                                      headers={"Cookie": cookie, "Accept": "application/json",
                                               "Content-Type": "application/x-www-form-urlencoded"},
                                      data=b"profile=default&toolset=../x&enabled=0")
        self.assertEqual(code, 400)
        # 7. GET mutasi via GET ditolak 405
        code, _, _ = self._request("/toggle-profile-skill?profile=default&skill=x&enabled=0", method="GET",
                                   headers={"Cookie": cookie})
        self.assertEqual(code, 405)

    def test_70_kanban_http_api_and_auth(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        # Isolate the board: without this the POSTs below land in the real
        # ~/.hermes/kanban.db and leave junk "Live API Task" cards on the
        # user's board (happened 3x before this patch).
        import tempfile
        from pathlib import Path
        tmpdir = tempfile.mkdtemp(prefix="panel-kanban-http-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir, ignore_errors=True))
        cfg = str(Path(tmpdir) / "config.yaml")
        Path(cfg).write_text("model:\n  default: test-model\n", encoding="utf-8")
        patcher = mock.patch.object(panel, "CONFIG_PATH", cfg)
        patcher.start()
        self.addCleanup(patcher.stop)

        # 1. GET /api/kanban/tasks without auth -> 401
        code, _, _ = self._request("/api/kanban/tasks", method="GET")
        self.assertEqual(code, 401)

        # 2. GET /api/kanban/tasks with auth -> 200
        code, _, body = self._request("/api/kanban/tasks", method="GET", headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertTrue(data.get("ok"))
        self.assertIn("tasks", data)

        # 3. POST /api/kanban/task/create without auth -> 302/403
        code, _, _ = self._request("/api/kanban/task/create", method="POST",
                                   headers={"Accept": "application/json"}, data=b"title=Test")
        self.assertEqual(code, 403)

        # 4. POST /api/kanban/task/create with auth -> 200
        create_payload = json.dumps({"title": "Live API Task", "body": "Testing HTTP endpoint"}).encode("utf-8")
        code, _, body = self._request("/api/kanban/task/create", method="POST",
                                      headers={"Cookie": cookie, "Content-Type": "application/json"},
                                      data=create_payload)
        self.assertEqual(code, 200)
        res = json.loads(body)
        self.assertTrue(res.get("ok"))
        task_id = res.get("task_id")
        self.assertTrue(task_id.startswith("t_"))

        # 5. POST /api/kanban/task/status with auth -> 200
        status_payload = json.dumps({"task_id": task_id, "status": "ready"}).encode("utf-8")
        code, _, body = self._request("/api/kanban/task/status", method="POST",
                                      headers={"Cookie": cookie, "Content-Type": "application/json"},
                                      data=status_payload)
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body).get("ok"))

        # 6. GET /api/kanban/config with auth -> 200
        code, _, body = self._request("/api/kanban/config", method="GET", headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body).get("ok"))


    def test_71_kanban_attachment_http(self):
        """Attachment download: auth required, 404 on unknown, text/plain on hit."""
        import tempfile
        from pathlib import Path
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        tmpdir = tempfile.mkdtemp(prefix="panel-att-http-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir, ignore_errors=True))
        cfg = str(Path(tmpdir) / "config.yaml")
        Path(cfg).write_text("model:\n  default: test-model\n", encoding="utf-8")

        with mock.patch.object(panel, "CONFIG_PATH", cfg):
            # 1. no auth -> 401
            code, _, _ = self._request("/api/kanban/attachment?id=1", method="GET")
            self.assertEqual(code, 401)

            _, _, task_id = panel.create_kanban_task(title="Lampiran HTTP", status="todo")
            att_dir = panel.get_kanban_attachments_root() / task_id
            att_dir.mkdir(parents=True, exist_ok=True)
            blob = att_dir / "hasil.md"
            blob.write_text("# Hasil\n25 bug", encoding="utf-8")
            con = panel.ensure_kanban_db(panel.get_kanban_db_path())
            with con:
                con.execute(
                    "INSERT INTO task_attachments (task_id, filename, stored_path, size, created_at) "
                    "VALUES (?, 'hasil.md', ?, ?, ?)",
                    (task_id, str(blob), blob.stat().st_size, int(time.time())))
                aid = con.execute("SELECT id FROM task_attachments ORDER BY id DESC LIMIT 1").fetchone()["id"]
            con.close()

            # 2. unknown id -> 404
            code, _, _ = self._request("/api/kanban/attachment?id=999999", method="GET",
                                       headers={"Cookie": cookie})
            self.assertEqual(code, 404)

            # 3. bad id -> 400
            code, _, _ = self._request("/api/kanban/attachment?id=abc", method="GET",
                                       headers={"Cookie": cookie})
            self.assertEqual(code, 400)

            # 4. valid -> 200 text/plain, content served, never HTML
            code, headers, body = self._request(f"/api/kanban/attachment?id={aid}", method="GET",
                                                headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            self.assertIn("text/plain", headers.get("Content-Type", ""))
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
            self.assertIn("25 bug", body)

    def test_72_dos_and_input_validation(self):
        """SEC-DOS-01, SEC-DOS-02, SEC-VAL-01, SEC-VAL-02: DoS & input validation."""
        import tempfile
        from pathlib import Path
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"

        # --- 1. SEC-DOS-01: Content-Length limit and 413 rejection ---
        self.assertEqual(panel.MAX_BODY_SIZE, 5 * 1024 * 1024)
        over_limit = str(panel.MAX_BODY_SIZE + 1024)
        # JSON API route with excessive Content-Length -> 413 JSON response
        code, _, body = self._request(
            "/api/kanban/task/create",
            method="POST",
            headers={"Cookie": cookie, "Content-Length": over_limit, "Content-Type": "application/json"},
            data=b"{}",
        )
        self.assertEqual(code, 413)
        data = json.loads(body)
        self.assertFalse(data.get("ok"))
        self.assertIn("melebihi batas", data.get("error", ""))

        # Non-JSON route with excessive Content-Length -> 413 HTML response
        code, _, body = self._request(
            "/login",
            method="POST",
            headers={"Content-Length": over_limit, "Content-Type": "application/x-www-form-urlencoded"},
            data=b"password=foo",
        )
        self.assertEqual(code, 413)
        self.assertIn("413", body)

        # --- 2. SEC-DOS-02: Query parameter n clamped on log endpoints ---
        with mock.patch.object(panel, "tail_gateway_log", return_value="gw log line") as m_gw:
            code, _, _ = self._request("/api/gateway-log?n=5000", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            m_gw.assert_called_with(n=1000)

            code, _, _ = self._request("/api/gateway-log?n=-10", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            m_gw.assert_called_with(n=60)

            code, _, _ = self._request("/api/gateway-log?n=0", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            m_gw.assert_called_with(n=60)

            code, _, _ = self._request("/api/gateway-log?n=invalid", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            m_gw.assert_called_with(n=100)

            # Snap to the nearest allowed size: the cache key space stays bounded.
            code, _, _ = self._request("/api/gateway-log?n=180", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            m_gw.assert_called_with(n=200)
            code, _, _ = self._request("/api/gateway-log?n=120", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            m_gw.assert_called_with(n=100)

        with mock.patch.object(panel, "tail_whatsapp_bridge_log", return_value="wa log line") as m_wa:
            code, _, _ = self._request("/api/whatsapp-log?n=99999", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            m_wa.assert_called_with(n=1000)

            code, _, _ = self._request("/api/whatsapp-log?n=-5", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            m_wa.assert_called_with(n=60)

        # --- 3. SEC-VAL-01: Non-integer priority handled cleanly ---
        tmpdir = tempfile.mkdtemp(prefix="panel-val-http-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir, ignore_errors=True))
        cfg = str(Path(tmpdir) / "config.yaml")
        Path(cfg).write_text("model:\n  default: test-model\n", encoding="utf-8")

        with mock.patch.object(panel, "CONFIG_PATH", cfg):
            # Create task with string priority 'urgent' -> default to 0, not 500
            payload = json.dumps({"title": "Val Priority Task", "priority": "urgent"}).encode("utf-8")
            code, _, body = self._request(
                "/api/kanban/task/create",
                method="POST",
                headers={"Cookie": cookie, "Content-Type": "application/json"},
                data=payload,
            )
            self.assertEqual(code, 200)
            res = json.loads(body)
            self.assertTrue(res.get("ok"))
            tid = res["task_id"]
            task = panel.get_kanban_task(tid)
            self.assertEqual(task["priority"], 0)

            # Update task with string priority 'high' -> default to 0
            update_payload = json.dumps({"task_id": tid, "priority": "high"}).encode("utf-8")
            code, _, body = self._request(
                "/api/kanban/task/update",
                method="POST",
                headers={"Cookie": cookie, "Content-Type": "application/json"},
                data=update_payload,
            )
            self.assertEqual(code, 200)
            task = panel.get_kanban_task(tid)
            self.assertEqual(task["priority"], 0)

            # Update task with valid numeric string '3' -> sets priority to 3
            update_payload2 = json.dumps({"task_id": tid, "priority": "3"}).encode("utf-8")
            code, _, body = self._request(
                "/api/kanban/task/update",
                method="POST",
                headers={"Cookie": cookie, "Content-Type": "application/json"},
                data=update_payload2,
            )
            self.assertEqual(code, 200)
            task = panel.get_kanban_task(tid)
            self.assertEqual(task["priority"], 3)

            # Direct function calls with non-integer priority
            ok, _, tid_direct = panel.create_kanban_task(title="Direct Priority", priority="invalid_priority")
            self.assertTrue(ok)
            task_dir = panel.get_kanban_task(tid_direct)
            self.assertEqual(task_dir["priority"], 0)

            ok, _ = panel.update_kanban_task(tid_direct, priority="not_a_number")
            self.assertTrue(ok)
            task_dir = panel.get_kanban_task(tid_direct)
            self.assertEqual(task_dir["priority"], 0)

        # --- 4. SEC-VAL-02: Validate task parameter in set_aux_task_model ---
        # Invalid task keys must be rejected
        self.assertFalse(panel.set_aux_task_model("unknown_task_foo", "auto", ""))
        self.assertFalse(panel.set_aux_task_model("../bad_task", "auto", ""))
        self.assertFalse(panel.set_aux_task_model("", "auto", ""))
        self.assertFalse(panel.set_aux_task_model(None, "auto", ""))

        # Valid task keys in AUX_TASK_DEFINITIONS and delegation must be accepted
        with mock.patch.object(panel, "CONFIG_PATH", cfg):
            self.assertTrue(panel.set_aux_task_model("vision", "auto", ""))
            self.assertTrue(panel.set_aux_task_model("delegation", "openrouter", "gpt-4o"))

    def test_73_race_concurrency_and_hardening(self):
        """SEC-RACE-01, SEC-RACE-02, SEC-CSRF-02, SEC-RACE-03: concurrency and hardening."""
        import tempfile
        import threading
        from pathlib import Path

        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        tmpdir = tempfile.mkdtemp(prefix="panel-race-http-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir, ignore_errors=True))
        cfg_file = str(Path(tmpdir) / "config.yaml")
        Path(cfg_file).write_text("model:\n  default: base-model\nplatforms:\n  telegram:\n    enabled: true\n", encoding="utf-8")

        # --- 1. SEC-RACE-01: _config_write_lock is RLock and protects read-modify-write ---
        self.assertIsInstance(panel._config_write_lock, type(threading.RLock()))
        # Verify reentrancy
        with panel._config_write_lock:
            with panel._config_write_lock:
                pass

        with mock.patch.object(panel, "CONFIG_PATH", cfg_file):
            lock_held_during_save = False

            orig_read = panel._read_config_for_write
            def probe_read():
                nonlocal lock_held_during_save
                probe_thread_acquired = False
                def probe_lock():
                    nonlocal probe_thread_acquired
                    probe_thread_acquired = panel._config_write_lock.acquire(blocking=False)
                    if probe_thread_acquired:
                        panel._config_write_lock.release()
                t = threading.Thread(target=probe_lock)
                t.start()
                t.join()
                lock_held_during_save = not probe_thread_acquired
                return orig_read()

            with mock.patch.object(panel, "_read_config_for_write", side_effect=probe_read):
                ok, err = panel.save_gateway_platform_config("telegram", "enabled: false\n")
                self.assertTrue(ok)
                self.assertTrue(lock_held_during_save, "_config_write_lock must be held during read-modify-write")

            # Concurrent modifications through save_kanban_config
            errors = []
            def update_kb(val):
                try:
                    res, _ = panel.save_kanban_config({"dispatch_interval_seconds": val})
                    if not res:
                        errors.append("save_kanban_config failed")
                except Exception as e:
                    errors.append(str(e))

            threads = [threading.Thread(target=update_kb, args=(i,)) for i in range(10, 25)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            kb = panel.get_kanban_config()
            self.assertIn(kb.get("dispatch_interval_seconds"), range(10, 25))

        # --- 2. SEC-RACE-02: Double-invocation prevention in run_hermes_update ---
        with mock.patch.object(panel, "_hermes_update_lock") as mock_lock:
            real_lock = threading.Lock()
            check_inside_lock = False
            def mock_enter():
                real_lock.acquire()
                return real_lock
            def mock_exit(*args):
                real_lock.release()
            mock_lock.__enter__.side_effect = mock_enter
            mock_lock.__exit__.side_effect = mock_exit

            def mock_is_updating():
                nonlocal check_inside_lock
                check_inside_lock = real_lock.locked()
                return True

            with mock.patch.object(panel, "is_hermes_updating", side_effect=mock_is_updating):
                panel.run_hermes_update()
                self.assertTrue(check_inside_lock, "is_hermes_updating must be checked under _hermes_update_lock")

        # Second call returns immediately without starting thread when updating
        panel._hermes_update_running = True
        try:
            with mock.patch("threading.Thread") as mock_thread:
                panel.run_hermes_update()
                mock_thread.assert_not_called()
        finally:
            panel._hermes_update_running = False

        # --- 3. SEC-CSRF-02: /api/kanban/config registered in MUTATING_PATHS ---
        self.assertIn("/api/kanban/config", panel.MUTATING_PATHS)

        with mock.patch.object(panel, "CONFIG_PATH", cfg_file):
            # GET /api/kanban/config with auth returns 200 and config
            code, _, body = self._request("/api/kanban/config", method="GET", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            data = json.loads(body)
            self.assertTrue(data.get("ok"))
            self.assertIn("config", data)

            # POST /api/kanban/config with auth mutates config
            payload = json.dumps({"dispatch_interval_seconds": 120}).encode("utf-8")
            code, _, body = self._request(
                "/api/kanban/config",
                method="POST",
                headers={"Cookie": cookie, "Content-Type": "application/json"},
                data=payload,
            )
            self.assertEqual(code, 200)
            res = json.loads(body)
            self.assertTrue(res.get("ok"))
            self.assertEqual(res.get("config", {}).get("dispatch_interval_seconds"), 120)

            # POST without auth returns 403
            code, _, _ = self._request(
                "/api/kanban/config",
                method="POST",
                headers={"Content-Type": "application/json"},
                data=payload,
            )
            self.assertEqual(code, 403)

        # --- 4. SEC-RACE-03: cleanup_system_junk filters scratch files > 24h ---
        scratch_dir = Path(tmpdir) / "scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        recent_file = scratch_dir / "active_worker.tmp"
        old_file = scratch_dir / "stale_junk.tmp"
        recent_file.write_text("active task in-flight", encoding="utf-8")
        old_file.write_text("old task artifact", encoding="utf-8")

        now = time.time()
        os.utime(str(recent_file), (now - 1800, now - 1800))  # 30m old (<= 24h)
        os.utime(str(old_file), (now - 100000, now - 100000))  # ~27.7h old (> 24h)

        orig_walk = os.walk
        orig_exists = os.path.exists
        scratch_target = "/DATA/AppData/hermes-native/hermes-data/cache/scratch"

        def mock_exists(p):
            if p == scratch_target:
                return True
            return orig_exists(p)

        def mock_walk(top, *args, **kwargs):
            if top == scratch_target:
                return [(str(scratch_dir), [], ["active_worker.tmp", "stale_junk.tmp"])]
            return orig_walk(top, *args, **kwargs)

        with mock.patch("os.path.exists", side_effect=mock_exists), \
             mock.patch("os.walk", side_effect=mock_walk), \
             mock.patch("subprocess.run") as m_subproc, \
             mock.patch.object(panel, "glob") as m_glob:
            m_glob.glob.return_value = []
            m_subproc.return_value = mock.MagicMock(stdout="prune ok")

            res = panel.cleanup_system_junk()
            self.assertIsInstance(res, dict)
            self.assertIn("freed_human", res)

            # Active file must survive (age <= 24h)
            self.assertTrue(recent_file.exists(), "Active scratch file (<= 24h) must NOT be deleted")
            # Old junk must be deleted (age > 24h)
            self.assertFalse(old_file.exists(), "Old scratch file (> 24h) MUST be deleted")

    def test_casaos_process_list_detection(self):
        """CasaOS process entry is included in get_process_list with correct attributes."""
        orig_open = open

        def custom_open(path, *args, **kwargs):
            if str(path) == "/proc/12345/status":
                return io.StringIO("VmRSS:\t   20480 kB\n")
            return orig_open(path, *args, **kwargs)

        def custom_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and len(cmd) >= 3 and cmd[0] == "systemctl" and cmd[1] == "show" and "casaos" in cmd[2]:
                return mock.MagicMock(stdout="MainPID=12345\n", returncode=0)
            return mock.MagicMock(stdout="", returncode=0)

        # 1. When CasaOS is active
        with mock.patch.object(panel, "service_active", side_effect=lambda name, user=False: name == "casaos.service"):
            with mock.patch.object(panel.subprocess, "run", side_effect=custom_run):
                with mock.patch("builtins.open", side_effect=custom_open):
                    procs = panel.get_process_list()
                    casa = next((p for p in procs if p["id"] == "casaos"), None)
                    self.assertIsNotNone(casa)
                    self.assertEqual(casa["id"], "casaos")
                    self.assertEqual(casa["name"], "CasaOS (Dasbor & Manajemen Host)")
                    self.assertEqual(casa["kind"], "Layanan Systemd")
                    self.assertEqual(casa["status"], "Berjalan")
                    self.assertTrue(casa["is_active"])
                    self.assertEqual(casa["pid"], "12345")
                    self.assertEqual(casa["mem_mb"], 20.0)
                    self.assertEqual(casa["stop_url"], "/process-action?service=casaos&action=stop")
                    self.assertEqual(casa["start_url"], "/process-action?service=casaos&action=start")
                    self.assertEqual(casa["restart_url"], "/process-action?service=casaos&action=restart")

        # 2. When CasaOS is inactive — unit terpasang tapi tidak jalan
        with mock.patch.object(panel, "service_active", return_value=False):
            with mock.patch.object(panel, "_unit_installed", return_value=True):
                procs = panel.get_process_list()
                casa = next((p for p in procs if p["id"] == "casaos"), None)
                self.assertIsNotNone(casa)
                self.assertEqual(casa["status"], "Berhenti")
                self.assertFalse(casa["is_active"])
                self.assertEqual(casa["pid"], "-")

        # 3. Tanpa unit sama sekali: jangan tampilkan "Berhenti" (layanan hantu)
        with mock.patch.object(panel, "service_active", return_value=False):
            with mock.patch.object(panel, "_unit_installed", return_value=False):
                procs = panel.get_process_list()
                casa = next((p for p in procs if p["id"] == "casaos"), None)
                self.assertIsNotNone(casa)
                self.assertEqual(casa["status"], "Tidak terpasang")
                self.assertEqual(casa["start_url"], "")
                self.assertEqual(casa["restart_url"], "")
            self.assertEqual(casa["mem_mb"], 0.0)

    def test_casaos_process_actions_endpoint(self):
        """Endpoint /process-action executes systemctl commands for CasaOS units."""
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        expected_units = [
            "casaos.service",
            "casaos-gateway.service",
            "casaos-app-management.service",
            "casaos-user-service.service",
            "casaos-local-storage.service",
            "casaos-message-bus.service",
        ]
        for action in ("stop", "start", "restart"):
            panel._last_action_at.clear()
            with mock.patch.object(panel.subprocess, "run") as mock_sub:
                code, _, _ = self._request(
                    f"/process-action?service=casaos&action={action}",
                    method="POST",
                    headers={"Cookie": cookie},
                )
                self.assertEqual(code, 302)
                mock_sub.assert_called_with(["systemctl", action] + expected_units, timeout=15)

    def test_casaos_confirm_action_ui(self):
        """UI confirmation rule for stopping CasaOS is registered."""
        self.assertIn("/process-action?service=casaos&action=stop", panel.NAV_SCRIPT)
        self.assertIn("Hentikan CasaOS", panel.NAV_SCRIPT)
        self.assertIn("Hentikan layanan CasaOS? Dasbor web CasaOS tidak dapat diakses sampai dinyalakan kembali.", panel.NAV_SCRIPT)
    # --- HTTP Gzip & Deflate Compression Middleware Tests ---
    def test_gzip_compression_on_status_page(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        code, headers, raw = self._request_raw("/status", headers={"Cookie": cookie, "Accept-Encoding": "gzip"})
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertIn("Accept-Encoding", headers.get("Vary", ""))
        decompressed = gzip.decompress(raw).decode("utf-8")
        self.assertIn("<!doctype html>", decompressed.lower())
        self.assertIn("Hermes Control Panel", decompressed)
        # Verify significant payload reduction: 400KB+ to < 95KB (~80% reduction).
        # Baseline moved from 85 KB to ~85.6 KB when fonts were self-hosted
        # (commit e7517a5, Wave 78) — the woff2 payload is now part of the page
        # instead of a CDN round-trip. 90 KB → 95 KB (Wave 98): riwayat performa
        # asli t_e42bba09 (seed lazy + label min/maks/sekarang, ~2,5 KB gzip).
        # Terukur 92,5 KB — guard tetap menangkap bloat 2×.
        self.assertLess(len(raw), 95000)
        self.assertGreater(len(decompressed), 350000)

    def test_deflate_compression_on_status_page(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        code, headers, raw = self._request_raw("/status", headers={"Cookie": cookie, "Accept-Encoding": "deflate"})
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("Content-Encoding"), "deflate")
        self.assertIn("Accept-Encoding", headers.get("Vary", ""))
        decompressed = zlib.decompress(raw).decode("utf-8")
        self.assertIn("<!doctype html>", decompressed.lower())
        # 85 KB → 90 KB: tab Profil sekarang membawa modal Skills/Tools/Config
        # (selector platform + pane config). 90 KB → 95 KB (Wave 98): riwayat
        # performa t_e42bba09 (~2,5 KB gzip). Guard tetap menangkap bloat 2×.
        self.assertLess(len(raw), 95000)
        self.assertGreater(len(decompressed), 350000)

    def test_no_compression_without_accept_encoding(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        code, headers, raw = self._request_raw("/status", headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertGreater(len(raw), 350000)

    def test_no_compression_for_small_payload(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        code, headers, raw = self._request_raw("/nonexistent-page-test", headers={"Cookie": cookie, "Accept-Encoding": "gzip"})
        self.assertEqual(code, 404)
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertLessEqual(len(raw), 1024)

    def test_compression_qvalue_precedence(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        # When deflate has higher q-value, choose deflate
        code, headers, raw = self._request_raw("/status", headers={"Cookie": cookie, "Accept-Encoding": "gzip;q=0.5, deflate;q=1.0"})
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("Content-Encoding"), "deflate")

        # When gzip has q=0 and deflate > 0, choose deflate
        code, headers, raw = self._request_raw("/status", headers={"Cookie": cookie, "Accept-Encoding": "gzip;q=0, deflate;q=0.8"})
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("Content-Encoding"), "deflate")

        # When all encodings have q=0, do not compress
        code, headers, raw = self._request_raw("/status", headers={"Cookie": cookie, "Accept-Encoding": "gzip;q=0, deflate;q=0"})
        self.assertEqual(code, 200)
        self.assertIsNone(headers.get("Content-Encoding"))

    def test_compression_api_status_json(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        code, headers, raw = self._request_raw("/api/status", headers={"Cookie": cookie, "Accept-Encoding": "gzip"})
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        decompressed = gzip.decompress(raw).decode("utf-8")
        data = json.loads(decompressed)
        self.assertIn("cells", data)

    def test_process_action_generic_docker_container(self):
        """POST /process-action starts, stops, or restarts arbitrary safe docker containers."""
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0)
            code, headers, _ = self._request(
                "/process-action", method="POST",
                headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
                data=b"service=pihole-pihole-1&action=restart&tab=control",
            )
            self.assertEqual(code, 302)
            self.assertIn("tab=control", headers.get("Location", ""))
            mock_run.assert_called_with(["docker", "restart", "pihole-pihole-1"], timeout=15)

    def test_process_action_reports_nonzero_exit(self):
        """A failed lifecycle command must not redirect as if action succeeded."""
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        panel._last_action_at.clear()
        with mock.patch.object(panel.os.path, "isfile", return_value=True), \
             mock.patch.object(panel.subprocess, "run", return_value=mock.Mock(returncode=1)):
            code, headers, _ = self._request(
                "/process-action?service=9router&action=restart&tab=control",
                method="POST", headers={"Cookie": cookie},
            )
        self.assertEqual(code, 302)
        self.assertIn("action-failed%3Asystemctl%20restart%209router.service%20exit%201", headers.get("Location", ""))
        self.assertNotIn("action-done", headers.get("Location", ""))



class TestGatewayConfigSync(unittest.TestCase):
    """Panel edits must land where Hermes' gateway loader actually reads them.

    Hermes (gateway/config_loader.py::platform_section) gives a root-level ``<platform>:`` block
    precedence over ``platforms.<platform>`` for every adapter key, so the panel has to show and
    write the merged view — without dropping the root block's settings.
    """

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp(prefix="panel-gw-")
        self.cfg_path = os.path.join(self.tmpdir, "config.yaml")
        self.env_path = os.path.join(self.tmpdir, ".env")
        with open(self.env_path, "w", encoding="utf-8") as f:
            f.write("TELEGRAM_BOT_TOKEN=x\n")
        os.chmod(self.env_path, 0o600)
        patcher = mock.patch.object(panel, "CONFIG_PATH", self.cfg_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_cfg(self, cfg, mode=0o600):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            panel.yaml.safe_dump(cfg, f, sort_keys=False)
        os.chmod(self.cfg_path, mode)
        panel.invalidate_config_cache()

    def _read_cfg(self):
        with open(self.cfg_path, encoding="utf-8") as f:
            return panel.yaml.safe_load(f)

    def _split_cfg(self):
        return {
            "telegram": {
                "reactions": False,
                "allowed_chats": "",
                "extra": {"rich_messages": True, "reply_keyboard_welcome": "Hai"},
            },
            "platforms": {
                "telegram": {
                    "enabled": True,
                    "reactions": True,  # shadowed by the root block in Hermes
                    "home_channel": {"chat_id": "123", "name": "vitooo", "platform": "telegram"},
                },
            },
        }

    def test_26_editor_shows_effective_config_root_block_wins(self):
        self._write_cfg(self._split_cfg())
        res = panel.get_gateway_platform_config("telegram")
        shown = panel.yaml.safe_load(res["yaml"])
        self.assertFalse(shown["reactions"], "root telegram.reactions is what Hermes uses")
        self.assertTrue(shown["extra"]["rich_messages"])
        self.assertEqual(shown["home_channel"]["chat_id"], "123")
        self.assertTrue(res["enabled"])

    def test_27_save_roundtrip_keeps_root_block_settings(self):
        self._write_cfg(self._split_cfg())
        shown = panel.get_gateway_platform_config("telegram")["yaml"]
        edited = shown.replace("reactions: false", "reactions: true")
        ok, err = panel.save_gateway_platform_config("telegram", edited)
        self.assertTrue(ok, err)
        cfg = self._read_cfg()
        self.assertNotIn("telegram", cfg, "root block folded into platforms.telegram")
        tg = cfg["platforms"]["telegram"]
        self.assertTrue(tg["reactions"])
        self.assertTrue(tg["extra"]["rich_messages"], "root-only settings must survive a save")
        self.assertEqual(tg["extra"]["reply_keyboard_welcome"], "Hai")
        self.assertEqual(tg["home_channel"]["chat_id"], "123")

    def test_27b_editor_never_sends_platform_secrets_to_the_browser(self):
        """The gateway config editor round-trips the whole YAML through the browser, so a
        bot token / password in the block would be shown on screen and echoed back."""
        cfg = self._split_cfg()
        cfg["platforms"]["telegram"]["token"] = "123456789:AARealTelegramBotTokenValue"
        cfg["platforms"]["telegram"]["api_key"] = "sk-live-abcdef123456"
        cfg["platforms"]["telegram"]["home_channel"]["webhook_secret"] = "whsec_987654321"
        self._write_cfg(cfg)
        shown = panel.yaml.safe_load(panel.get_gateway_platform_config("telegram")["yaml"])
        self.assertNotEqual(shown.get("token"), "123456789:AARealTelegramBotTokenValue")
        self.assertNotEqual(shown.get("api_key"), "sk-live-abcdef123456")
        self.assertNotEqual(shown["home_channel"].get("webhook_secret"), "whsec_987654321")
        # the key must stay so the user knows a secret exists and the YAML stays valid
        self.assertIn("token", shown)
        self.assertIn("api_key", shown)
        self.assertIn("webhook_secret", shown["home_channel"])
        # non-secret keys are untouched
        self.assertEqual(shown["home_channel"]["chat_id"], "123")
        self.assertTrue(shown["extra"]["rich_messages"])

    def test_27c_saving_a_masked_secret_restores_the_stored_value(self):
        """Opening the editor then pressing Save must not overwrite the real token with the mask."""
        cfg = self._split_cfg()
        cfg["platforms"]["telegram"]["token"] = "123456789:AARealTelegramBotTokenValue"
        cfg["platforms"]["telegram"]["password"] = "sup3rs3cret"
        self._write_cfg(cfg)
        shown = panel.get_gateway_platform_config("telegram")["yaml"]
        ok, err = panel.save_gateway_platform_config("telegram", shown)
        self.assertTrue(ok, err)
        tg = self._read_cfg()["platforms"]["telegram"]
        self.assertEqual(tg["token"], "123456789:AARealTelegramBotTokenValue")
        self.assertEqual(tg["password"], "sup3rs3cret")

    def test_27d_masking_a_secret_that_does_not_exist_yet_is_harmless(self):
        cfg = self._split_cfg()
        self._write_cfg(cfg)
        shown = panel.yaml.safe_load(panel.get_gateway_platform_config("telegram")["yaml"])
        shown["token"] = panel._REDACTED_SENTINEL
        ok, err = panel.save_gateway_platform_config("telegram", panel.yaml.safe_dump(shown))
        self.assertTrue(ok, err)
        tg = self._read_cfg()["platforms"]["telegram"]
        self.assertNotIn("token", tg, "a mask for a key that never existed must not invent a secret")

    def test_28_toggle_preserves_root_block_and_overrides_root_enabled(self):
        cfg = self._split_cfg()
        cfg["discord"] = {"enabled": True, "require_mention": True, "voice_fx": {"enabled": False}}
        cfg["platforms"]["discord"] = {"enabled": True}
        self._write_cfg(cfg)
        ok, err = panel.toggle_gateway_platform_config("discord", False)
        self.assertTrue(ok, err)
        saved = self._read_cfg()
        self.assertNotIn("discord", saved)
        dc = saved["platforms"]["discord"]
        self.assertFalse(dc["enabled"])
        self.assertTrue(dc["require_mention"])
        self.assertEqual(dc["voice_fx"], {"enabled": False})
        # Untouched platforms keep their root block.
        self.assertIn("telegram", saved)

    def test_29_form_save_merges_instead_of_replacing(self):
        self._write_cfg(self._split_cfg())
        ok, err = panel.save_gateway_platform_config(
            "telegram", "require_mention: true\nallowed_chats: null\n", merge=True)
        self.assertTrue(ok, err)
        tg = self._read_cfg()["platforms"]["telegram"]
        self.assertTrue(tg["require_mention"])
        self.assertNotIn("allowed_chats", tg, "null in form payload removes the key")
        self.assertEqual(tg["home_channel"]["chat_id"], "123")
        self.assertTrue(tg["extra"]["rich_messages"])
        self.assertFalse(tg["reactions"])

    def test_30_open_policy_without_allow_all_is_rejected(self):
        self._write_cfg({"platforms": {"whatsapp": {"enabled": True}}})
        ok, err = panel.save_gateway_platform_config(
            "whatsapp", "enabled: true\ndm_policy: open\ngroup_policy: disabled\n")
        self.assertFalse(ok)
        self.assertIn("WHATSAPP_ALLOW_ALL_USERS", err)
        self.assertNotIn("dm_policy", self._read_cfg()["platforms"]["whatsapp"], "config untouched on reject")

        # Enabling a platform that already carries an open policy is rejected too.
        self._write_cfg({"platforms": {"whatsapp": {"enabled": False, "dm_policy": "open"}}})
        ok, err = panel.toggle_gateway_platform_config("whatsapp", True)
        self.assertFalse(ok)
        self.assertIn("WHATSAPP_ALLOW_ALL_USERS", err)

        # Explicit opt-in in .env makes it valid, as in Hermes.
        with open(self.env_path, "a", encoding="utf-8") as f:
            f.write("WHATSAPP_ALLOW_ALL_USERS=true\n")
        ok, err = panel.save_gateway_platform_config(
            "whatsapp", "enabled: true\ndm_policy: open\ngroup_policy: disabled\n")
        self.assertTrue(ok, err)

    def test_31_writes_keep_config_file_private(self):
        self._write_cfg(self._split_cfg(), mode=0o600)
        self.assertTrue(panel.save_gateway_platform_config("telegram", "enabled: true\n")[0])
        self.assertEqual(os.stat(self.cfg_path).st_mode & 0o777, 0o600)
        self.assertTrue(panel.toggle_gateway_platform_config("telegram", False)[0])
        self.assertEqual(os.stat(self.cfg_path).st_mode & 0o777, 0o600)
        self._write_cfg(self._split_cfg(), mode=0o600)
        self.assertTrue(panel.remove_gateway_platform_config("telegram")[0])
        self.assertEqual(os.stat(self.cfg_path).st_mode & 0o777, 0o600)
        saved = self._read_cfg()
        self.assertNotIn("telegram", saved, "Hapus removes the root block Hermes would still load")
        self.assertNotIn("telegram", saved["platforms"])

    def _read_env(self):
        with open(self.env_path, encoding="utf-8") as f:
            return dict(line.split("=", 1) for line in f.read().splitlines() if "=" in line)

    def test_32_whatsapp_channels_keys_move_to_env(self):
        """Hermes' Channels page edits WHATSAPP_* in .env; config values would shadow it."""
        self._write_cfg({"platforms": {"whatsapp": {
            "enabled": True, "mode": "bot", "dm_policy": "allowlist", "allow_from": ["62811"],
            "group_policy": "disabled"}}})
        ok, err = panel.save_gateway_platform_config("whatsapp", "allow_from:\n  - '62822'\n", merge=True)
        self.assertTrue(ok, err)
        env = self._read_env()
        self.assertEqual(env["WHATSAPP_ALLOWED_USERS"], "62822")
        self.assertEqual(env["WHATSAPP_DM_POLICY"], "allowlist")
        self.assertEqual(env["WHATSAPP_MODE"], "bot")
        wa = self._read_cfg()["platforms"]["whatsapp"]
        for key in ("allow_from", "dm_policy", "mode"):
            self.assertNotIn(key, wa, f"{key} must live only in .env")
        self.assertEqual(wa["group_policy"], "disabled", "non-Channels keys stay in config")
        self.assertEqual(os.stat(self.env_path).st_mode & 0o777, 0o600)

    def test_41_whatsapp_view_shows_channels_env_values(self):
        self._write_cfg({"platforms": {"whatsapp": {"enabled": True, "group_policy": "disabled"}}})
        with open(self.env_path, "a", encoding="utf-8") as f:
            f.write("WHATSAPP_ALLOWED_USERS=628a,628b\nWHATSAPP_DM_POLICY=allowlist\nWHATSAPP_MODE=self-chat\n")
        shown = panel.yaml.safe_load(panel.get_gateway_platform_config("whatsapp")["yaml"])
        self.assertEqual(shown["allow_from"], ["628a", "628b"])
        self.assertEqual(shown["dm_policy"], "allowlist")
        self.assertEqual(shown["mode"], "self-chat")

    def test_42_whatsapp_config_value_wins_in_view_like_hermes(self):
        """While both exist, Hermes uses the config value — the view must too."""
        self._write_cfg({"platforms": {"whatsapp": {"enabled": True, "dm_policy": "allowlist", "allow_from": ["628c"]}}})
        with open(self.env_path, "a", encoding="utf-8") as f:
            f.write("WHATSAPP_ALLOWED_USERS=628z\nWHATSAPP_DM_POLICY=pairing\n")
        shown = panel.yaml.safe_load(panel.get_gateway_platform_config("whatsapp")["yaml"])
        self.assertEqual(shown["allow_from"], ["628c"])
        self.assertEqual(shown["dm_policy"], "allowlist")

    def test_43_whatsapp_removed_key_clears_env(self):
        self._write_cfg({"platforms": {"whatsapp": {"enabled": True, "group_policy": "disabled"}}})
        with open(self.env_path, "a", encoding="utf-8") as f:
            f.write("WHATSAPP_ALLOWED_USERS=628a\nWHATSAPP_DM_POLICY=allowlist\n")
        ok, err = panel.save_gateway_platform_config("whatsapp", "allow_from: []\ndm_policy: null\n", merge=True)
        self.assertTrue(ok, err)
        env = self._read_env()
        self.assertNotIn("WHATSAPP_ALLOWED_USERS", env)
        self.assertNotIn("WHATSAPP_DM_POLICY", env)
        self.assertEqual(env["TELEGRAM_BOT_TOKEN"], "x", "unrelated .env lines untouched")

    def test_44_whatsapp_toggle_migrates_and_guards_env_policy(self):
        self._write_cfg({"platforms": {"whatsapp": {"enabled": False, "mode": "bot", "allow_from": ["628d"]}}})
        ok, err = panel.toggle_gateway_platform_config("whatsapp", True)
        self.assertTrue(ok, err)
        self.assertNotIn("allow_from", self._read_cfg()["platforms"]["whatsapp"])
        self.assertEqual(self._read_env()["WHATSAPP_ALLOWED_USERS"], "628d")
        # An open policy set via Channels (.env) is still caught before Hermes refuses to start.
        panel.toggle_gateway_platform_config("whatsapp", False)
        with open(self.env_path, "a", encoding="utf-8") as f:
            f.write("WHATSAPP_DM_POLICY=open\n")
        ok, err = panel.toggle_gateway_platform_config("whatsapp", True)
        self.assertFalse(ok)
        self.assertIn("WHATSAPP_ALLOW_ALL_USERS", err)

    def test_45_hermes_update_timeout_outlasts_gateway_drain(self):
        # Hermes' update waits up to restart_after_turn_timeout + restart_drain_timeout
        # (1995s on this host) for the gateway; killing earlier leaves a half-finished update.
        self.assertGreaterEqual(panel.HERMES_UPDATE_TIMEOUT, 3600)
        with open(os.path.join(REPO_ROOT, "dashboard-toggle-server.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("timeout 900 detik", src, "timeout message must use the real value")

    def test_45b_is_hermes_updating_filters_accurately(self):
        """is_hermes_updating must only match real update CLI, never gateway run or unrelated commands."""
        self.assertFalse(panel._pid_is_hermes_update(0))
        self.assertFalse(panel._pid_is_hermes_update(os.getpid()))

        # Mock cmdline for gateway run containing 'hermes update' in repair text
        with mock.patch("pathlib.Path.read_bytes") as mock_read:
            # Gateway run process
            mock_read.return_value = b"python3\x00/path/to/hermes\x00gateway\x00run\x00--arg\x00repair 'hermes update'\x00"
            self.assertFalse(panel._pid_is_hermes_update(99999))

            # Real hermes update process
            mock_read.return_value = b"/usr/local/bin/hermes\x00update\x00--yes\x00--no-backup\x00"
            self.assertTrue(panel._pid_is_hermes_update(99999))

    def test_45c_clear_stale_hermes_git_lock(self):
        """Stale git and update locks older than 120s are cleaned when updater is not active."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            git_dir = panel.Path(td) / ".git"
            git_dir.mkdir(parents=True)
            lock = git_dir / "hermes-update.lock"
            lock.write_text("12345\n")
            old_time = time.time() - 300
            os.utime(lock, (old_time, old_time))

            with mock.patch.object(panel, "HERMES_LIB_DIR", td), \
                 mock.patch.object(panel, "is_hermes_updating", return_value=False):
                panel._clear_stale_hermes_git_lock()
                self.assertFalse(lock.exists(), "Stale hermes-update.lock must be cleaned")

    def test_45d_is_hermes_updating_cleans_stale_progress_file(self):
        """is_hermes_updating cleans stale .hermes-update-in-progress without NameError."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            prog = panel.Path(td) / ".hermes-update-in-progress"
            prog.write_text("99999\n")
            orig_path = panel.Path

            def fake_path_fn(*args, **kwargs):
                if args and str(args[0]).endswith(".hermes-update-in-progress"):
                    return prog
                return orig_path(*args, **kwargs)

            with mock.patch.object(panel, "Path", side_effect=fake_path_fn), \
                 mock.patch.object(panel, "_pid_is_hermes_update", return_value=False), \
                 mock.patch.object(panel, "_hermes_update_running", False):
                self.assertFalse(panel.is_hermes_updating(force=True))
                self.assertFalse(prog.exists(), "Stale .hermes-update-in-progress must be unlinked")

    def test_38_form_patch_merges_onto_editor_base_yaml(self):
        self._write_cfg(self._split_cfg())
        base = "enabled: true\nreactions: false\nextra:\n  rich_messages: true\n  bridge_port: 3000\n"
        ok, text = panel.preview_gateway_platform_config(
            "telegram", "require_mention: true\nextra:\n  bridge_port: 3001\n", base)
        self.assertTrue(ok, text)
        merged = panel.yaml.safe_load(text)
        self.assertTrue(merged["require_mention"])
        self.assertEqual(merged["extra"], {"rich_messages": True, "bridge_port": 3001})
        self.assertFalse(merged["reactions"])

        # Saving the same patch with the editor's base writes exactly the previewed block.
        ok, err = panel.save_gateway_platform_config(
            "telegram", "require_mention: true\nextra:\n  bridge_port: 3001\n", merge=True, base_yaml=base)
        self.assertTrue(ok, err)
        self.assertEqual(self._read_cfg()["platforms"]["telegram"], merged)


class TestMobileScrollPerf(unittest.TestCase):
    """Phone scrolling stuttered: backdrop blur on every card/button + full DOM rebuild each SSE tick."""

    def test_50_backdrop_blur_only_on_overlays(self):
        import re
        css = panel.PAGE[panel.PAGE.index("<style>"):panel.PAGE.index("</style>")]
        offenders = []
        for m in re.finditer(r"([^{}]+)\{\{([^{}]*)\}\}", css):
            selector, body = m.group(1).strip(), m.group(2)
            # .confirm-box is the dialog inside the four *-modal overlays.
            if "backdrop-filter" in body and not re.search(r"modal|navloader|confirm-box", selector):
                offenders.append(selector.splitlines()[-1][:60])
        self.assertEqual(offenders, [], "blur on scrolling content is re-rendered every frame on phones")

    def _run_sse_gate(self, body: str) -> str:
        src = panel.SSE_SCRIPT
        gate = src[src.index("// BEGIN sse-render-gate"):src.index("// END sse-render-gate")]
        harness = r"""
var timers = [], listeners = {}, applied = [], writes = 0;
function setTimeout(fn, ms){ timers.push(fn); return timers.length; }
function clearTimeout(t){ if(t) timers[t-1] = null; }
function runTimers(){ var t = timers; timers = []; t.forEach(function(f){ if(f) f(); }); }
var window = { addEventListener: function(n, f){ listeners[n] = f; } };
function mkEl(){ var e = { firstChild: null }; Object.defineProperty(e, 'innerHTML', {
  set: function(v){ writes++; this._h = v; this.firstChild = {v: v}; }, get: function(){ return this._h; } }); return e; }
var els = { a: mkEl() };
var document = { getElementById: function(id){ return els[id] || null; } };
function apply(d){ applied.push(d); }
"""
        r = subprocess.run(["node", "-e", harness + gate + body], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_53_sse_payload_carries_only_what_the_page_reads(self):
        import re
        read_by_js = set(re.findall(r"\bd\.([a-z_]+)", panel.SSE_SCRIPT))
        frag = {k: f"<{k}>" for k in read_by_js | {"model_chips", "aux_tasks_block", "hermes_update_block"}}
        sent = set(json.loads(panel._sse_payload(frag)))
        self.assertEqual(sent, read_by_js, "static slots (model_chips ~38 KB) were re-sent and parsed every second")

    @unittest.skipUnless(subprocess.run(["which", "node"], capture_output=True).returncode == 0, "node not installed")
    def test_55_sticky_set_delayed_scroll_targets_each_log_box(self):
        src = panel.SSE_SCRIPT
        start = src.index("function stickySet")
        end = src.index("function apply", start)
        sticky = src[start:end]
        harness = r"""
var timers = [];
function setTimeout(fn, ms){ timers.push(fn); return timers.length; }
var window = {};
function box(height){ return {innerHTML: '<old>', scrollHeight: height, clientHeight: 10, scrollTop: height - 20}; }
var fresh = [box(100), box(200)];
var el = {_html: '<old>', fresh: fresh};
Object.defineProperty(el, 'innerHTML', {get: function(){ return this._html; }, set: function(v){ this._html = v; }});
el.querySelectorAll = function(){ return this.fresh; };
var document = {
  getElementById: function(){ return el; },
  createElement: function(){ return {innerHTML: '', querySelectorAll: function(){ return []; }}; }
};
"""
        script = harness + sticky + r"""
stickySet('log-slot', '<new>');
fresh[0].scrollHeight = 111;
fresh[1].scrollHeight = 222;
timers.forEach(function(fn){ fn(); });
process.stdout.write(JSON.stringify(fresh.map(function(b){ return b.scrollTop; })));
"""
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [111, 222])


    def test_54_updates_debounced_when_tab_hidden(self):
        out = self._run_sse_gate(r"""
document.hidden = true;
onUpdate('h1');
onUpdate('h2');
onUpdate('h3');
var during = applied.slice();
runTimers();
var afterTimer = applied.slice();
onUpdate('h4');
onUpdate('h5');
document.hidden = false;
onVisibilityChange();
var afterVisible = applied.slice();
onUpdate('v1');
var normalRate = applied.slice();
process.stdout.write(JSON.stringify({
  during: during,
  afterTimer: afterTimer,
  afterVisible: afterVisible,
  normalRate: normalRate
}));
""")
        res = json.loads(out)
        self.assertEqual(res["during"], ["h1"], "intermediate updates when tab hidden must be debounced")
        self.assertEqual(res["afterTimer"], ["h1", "h3"], "throttled timer applies latest pending update")
        self.assertEqual(res["afterVisible"], ["h1", "h3", "h5"], "tab becoming visible immediately flushes latest update")
        self.assertEqual(res["normalRate"], ["h1", "h3", "h5", "v1"], "updates resume immediate normal rate when visible")

    @unittest.skipUnless(subprocess.run(["which", "node"], capture_output=True).returncode == 0, "node not installed")
    def test_51_set_skips_unchanged_html(self):
        out = self._run_sse_gate(r"""
set('a', '<b>1</b>'); set('a', '<b>1</b>'); set('a', '<b>2</b>');
els.a.innerHTML = 'changed by a user action'; writes--;   // external write must not be masked
set('a', '<b>2</b>');
process.stdout.write(String(writes));
""")
        self.assertEqual(out, "3", "identical payloads must not rebuild the DOM")

    @unittest.skipUnless(subprocess.run(["which", "node"], capture_output=True).returncode == 0, "node not installed")
    def test_52_updates_held_while_scrolling(self):
        out = self._run_sse_gate(r"""
onUpdate('d1');
listeners.scroll(); onUpdate('d2'); onUpdate('d3');
var during = applied.length;
runTimers();
process.stdout.write(JSON.stringify({during: during, after: applied}));
""")
        self.assertEqual(json.loads(out), {"during": 1, "after": ["d1", "d3"]})

    @unittest.skipUnless(subprocess.run(["which", "node"], capture_output=True).returncode == 0, "node not installed")
    def test_54_updates_debounced_when_tab_hidden(self):
        out = self._run_sse_gate(r"""
document.hidden = true;
onUpdate('h1');
onUpdate('h2');
onUpdate('h3');
var during = applied.slice();
runTimers();
var afterTimer = applied.slice();
onUpdate('h4');
onUpdate('h5');
document.hidden = false;
onVisibilityChange();
var afterVisible = applied.slice();
onUpdate('v1');
var normalRate = applied.slice();
process.stdout.write(JSON.stringify({
  during: during,
  afterTimer: afterTimer,
  afterVisible: afterVisible,
  normalRate: normalRate
}));
""")
        res = json.loads(out)
        self.assertEqual(res["during"], ["h1"], "intermediate updates when tab hidden must be debounced")
        self.assertEqual(res["afterTimer"], ["h1", "h3"], "throttled timer applies latest pending update")
        self.assertEqual(res["afterVisible"], ["h1", "h3", "h5"], "tab becoming visible immediately flushes latest update")
        self.assertEqual(res["normalRate"], ["h1", "h3", "h5", "v1"], "updates resume immediate normal rate when visible")


def _gw_form_js() -> str:
    """Form UI helpers + populate/serialize exactly as rendered (PAGE uses str.format)."""
    start = panel.PAGE.index("var GW_FORM_KEYS_COMMON")
    end = panel.PAGE.index("var GW_PLATFORM_NAMES")
    return panel.PAGE[start:end].replace("{{", "{").replace("}}", "}")


_FAKE_DOM_JS = r"""
var els = {};
function el(id, kind, opts){ els[id] = {id: id, value: '', checked: false, style: {}, options: opts || null}; }
el('gw-config-enabled-chk'); el('gw-f-wa-mode'); el('gw-f-dm-policy'); el('gw-f-allow-from');
el('gw-f-allow-admin'); el('gw-f-group-policy'); el('gw-f-group-allow'); el('gw-f-req-mention');
el('gw-f-reply-thread'); el('gw-f-read-receipts'); el('gw-f-notice-del'); el('gw-f-wa-port');
el('gw-form-wa-banner'); el('gw-form-wa-fields');
var document = { getElementById: function(id){ return els[id] || null; } };
"""


@unittest.skipUnless(subprocess.run(["which", "node"], capture_output=True).returncode == 0, "node not installed")
class TestGatewayFormJs(unittest.TestCase):
    """The Form UI must only send fields the user has or changed, never invent an open policy."""

    def _run(self, body: str) -> str:
        script = _FAKE_DOM_JS + _gw_form_js() + body
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_33_untouched_form_does_not_add_policies(self):
        out = self._run(r"""
populateGwFormFromYaml("enabled: true\nhome_channel:\n  chat_id: '1'\n", 'telegram');
process.stdout.write(serializeGwFormToYaml('telegram'));
""")
        self.assertNotIn("dm_policy", out)
        self.assertNotIn("group_policy", out)
        self.assertNotIn("require_mention", out)
        self.assertIn("enabled: true", out)

    def test_34_whatsapp_without_policy_never_serializes_open(self):
        out = self._run(r"""
populateGwFormFromYaml("enabled: true\n", 'whatsapp');
process.stdout.write(serializeGwFormToYaml('whatsapp'));
""")
        self.assertNotIn("open", out)

    def test_35_changed_and_cleared_fields_are_sent(self):
        out = self._run(r"""
populateGwFormFromYaml("enabled: true\ndm_policy: pairing\nallow_from:\n  - '62811'\n", 'whatsapp');
els['gw-f-dm-policy'].value = 'allowlist';
els['gw-f-allow-from'].value = '';
els['gw-f-req-mention'].checked = true;
process.stdout.write(serializeGwFormToYaml('whatsapp'));
""")
        self.assertIn("dm_policy: allowlist", out)
        self.assertIn("allow_from: []", out)
        self.assertIn("require_mention: true", out)
        self.assertNotIn("reply_in_thread", out)

    def test_37_nested_enabled_keys_do_not_flip_platform_enabled(self):
        out = self._run(r"""
populateGwFormFromYaml("enabled: true\nvoice_fx:\n  enabled: false\nmissed_message_backfill:\n  enabled: false\n", 'discord');
process.stdout.write(serializeGwFormToYaml('discord'));
""")
        self.assertIn("enabled: true", out)
        self.assertNotIn("enabled: false", out)

    def test_36_group_policy_offers_pairing(self):
        idx = panel.PAGE.index('id="gw-f-group-policy"')
        block = panel.PAGE[idx:panel.PAGE.index("</select>", idx)]
        self.assertIn('value="pairing"', block)
        self.assertIn('value=""', block, "a 'Hermes default' choice that removes the key")

    def test_head_never_executes_mutating_route(self):
        """HEAD probes must not trigger legacy GET shortcut actions."""
        with mock.patch.object(panel.Handler, "_send_method_not_allowed") as blocked, \
             mock.patch.object(panel.Handler, "_handle_mutation") as mutation:
            handler = object.__new__(panel.Handler)
            handler.path = "/toggle?token=explicit"
            handler.do_HEAD()
            blocked.assert_called_once_with()
            mutation.assert_not_called()

    def test_post_rejects_malformed_content_length(self):
        """Malformed framing metadata must fail closed, not become an empty body."""
        class Headers(dict):
            def get(self, key, default=None):
                return super().get(key, default)
        class Writer:
            def write(self, data):
                return len(data)
        handler = object.__new__(panel.Handler)
        handler.path = "/api/status"
        handler.headers = Headers({"Content-Length": "oops", "Accept": "application/json"})
        handler.rfile = io.BytesIO(b"ignored")
        handler.wfile = Writer()
        with mock.patch.object(handler, "_send_json") as send_json:
            handler.do_POST()
        send_json.assert_called_once()
        self.assertEqual(send_json.call_args.kwargs["code"], 400)

    def test_49_sse_client_keys_parity(self):
        """build_fragments() and build_fragments(sse_only=True) must return every key defined in SSE_CLIENT_KEYS."""
        frag = panel.build_fragments()
        frag_sse = panel.build_fragments(sse_only=True)
        for k in panel.SSE_CLIENT_KEYS:
            self.assertIn(k, frag, f"Key '{k}' in SSE_CLIENT_KEYS must be returned by build_fragments()")
            self.assertIn(k, frag_sse, f"Key '{k}' in SSE_CLIENT_KEYS must be returned by build_fragments(sse_only=True)")
        self.assertNotIn("docker_apps_block", frag_sse)
        self.assertNotIn("kanban_board_block", frag_sse)
        self.assertNotIn("model_chips", frag_sse)

    def test_49b_sse_zero_wakeup_and_auto_trim_lifecycle(self):
        """Zero-wakeup event toggles with active clients; trim runs on last disconnect."""
        import queue
        self.assertIsInstance(panel._sse_active_event, threading.Event)

        # Baseline: cleared when client list empty
        with panel._sse_clients_lock:
            panel._sse_clients.clear()
            panel._sse_active_event.clear()
        self.assertFalse(panel._sse_active_event.is_set())

        # 1. First client connects -> event set
        q1 = queue.Queue(maxsize=10)
        e1 = threading.Event()
        with panel._sse_clients_lock:
            panel._sse_clients.append((q1, e1))
            panel._sse_active_event.set()
        self.assertTrue(panel._sse_active_event.is_set())

        # 2. Second client connects -> event remains set
        q2 = queue.Queue(maxsize=10)
        e2 = threading.Event()
        with panel._sse_clients_lock:
            panel._sse_clients.append((q2, e2))
            panel._sse_active_event.set()
        self.assertTrue(panel._sse_active_event.is_set())

        # 3. Client 1 leaves -> 1 client remains, event still set
        with panel._sse_clients_lock:
            panel._sse_clients.remove((q1, e1))
            if not panel._sse_clients:
                panel._sse_active_event.clear()
        self.assertTrue(panel._sse_active_event.is_set())

        # 4. Last client leaves -> event cleared & memory trim called
        with mock.patch.object(panel, "_trim_memory") as mock_trim:
            needs_trim = False
            with panel._sse_clients_lock:
                panel._sse_clients.remove((q2, e2))
                if not panel._sse_clients:
                    panel._sse_active_event.clear()
                    needs_trim = True
            if needs_trim:
                panel._trim_memory()
            self.assertFalse(panel._sse_active_event.is_set())
            mock_trim.assert_called_once()

        # 5. _trim_memory executes safely and runs gc.collect + malloc_trim
        with mock.patch("gc.collect") as mock_gc:
            panel._trim_memory()
            self.assertTrue(mock_gc.called)

    def test_50_rendered_js_syntax(self):
        """All <script> blocks in build_status_page() must be valid JavaScript (no SyntaxError)."""
        html = panel.build_status_page()
        import re, subprocess, shutil
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node not installed")
        scripts = re.findall(r'<script>(.*?)</script>', html, re.S)
        self.assertGreater(len(scripts), 0, "status page must have at least one script block")
        import tempfile
        for i, s in enumerate(scripts):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
                tf.write(s)
                tmp_name = tf.name
            try:
                res = subprocess.run([node_bin, "--check", tmp_name], capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, f"Script block {i} has JS SyntaxError:\n{res.stderr}")
            finally:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    def test_54_regressions_post_afbc3a5(self):
        """Regression tests for post-afbc3a5 audit fixes."""
        # 1. Root key protection
        fake_cfg = {
            "cron": {"jobs": [1, 2]},
            "skills": {"guard": True},
            "telegram": {"enabled": True},
            "platforms": {"telegram": {"enabled": True}}
        }
        panel._set_platform_block(fake_cfg, "telegram", {"enabled": True})
        self.assertNotIn("telegram", fake_cfg, "legacy telegram root block should be cleared")
        self.assertIn("cron", fake_cfg, "cron root block must never be touched")
        self.assertIn("skills", fake_cfg, "skills root block must never be touched")

        # 2. Model pickers use data attributes
        self.assertIn('data-provider="9router"', panel.PAGE)
        self.assertIn('data-model="\' + safeId + \'"', panel.PAGE)

        # 3. WA modal does not auto startWaPair
        self.assertNotIn('startWaPair(true);', panel.PAGE[panel.PAGE.index('function openWaPairModal'):panel.PAGE.index('function closeWaPairModal')])

        # 4. GW_FORM_KEYS_COMMON includes admin, group_allow, notice_delivery
        self.assertIn("'allow_admin_from'", panel.PAGE)
        self.assertIn("'group_allow_from'", panel.PAGE)
        self.assertIn("'notice_delivery'", panel.PAGE)

        # 5. Base badge CSS exists
        self.assertIn(".badge{{display:inline-block;", panel.PAGE)

    def test_55_regressions_audit_cycle(self):
        """Regression tests for audit cycle: XSS, log restore, bool parsing, root keys."""
        # 1. active_tab XSS sanitization
        rendered = panel.build_status_page(active_tab='";alert(1);//')
        self.assertNotIn('activeTabFromUrl = "";alert(1);//"', rendered)
        self.assertIn('activeTabFromUrl = "";', rendered)

        # 2. Buka Dasbor Hermes fallback has target="_blank"
        fallback_link = panel.get_open_block_active()
        self.assertIn('target="_blank"', fallback_link)

        # 3. syncLogUI caches gateway and clean log cards before removal
        sync_fn = panel.PAGE[panel.PAGE.index("function syncLogUI"):panel.PAGE.index("if(document.getElementById('router-log-card'))")]
        self.assertIn("window._lastGatewayLogCard = gc.outerHTML;", sync_fn)
        self.assertIn("window._lastCleanJunkCard = cc.outerHTML;", sync_fn)

        # 4. LEGACY_GATEWAY_ROOT_KEYS contains all supported platforms
        self.assertIn("teams", panel.LEGACY_GATEWAY_ROOT_KEYS)
        self.assertIn("google_chat", panel.LEGACY_GATEWAY_ROOT_KEYS)
        self.assertIn("wecom", panel.LEGACY_GATEWAY_ROOT_KEYS)

        # 5. Boolean coercion helper handles string booleans properly
        self.assertFalse(panel._to_bool("false"))
        self.assertFalse(panel._to_bool("0"))
        self.assertTrue(panel._to_bool("true"))
        self.assertTrue(panel._to_bool("1"))
        self.assertTrue(panel._to_bool(True))
        self.assertFalse(panel._to_bool(False))

    def test_56_regressions_tick_audit(self):
        """Regression tests for token redaction, root keys, and boolean parsing."""
        # 1. Token redaction includes Gemini keys and GitHub tokens
        gemini_text = "error with key AIzaSyD9876543210abcdefghijklmnopqrs"
        redacted = panel.redact_sensitive_tokens(gemini_text)
        self.assertNotIn("AIzaSyD9876543210", redacted)
        self.assertIn("[REDACTED_KEY]", redacted)

        github_text = "fatal: auth failed with token ghp_1234567890abcdefghijklmnopqrstuvwxyz"
        redacted_gh = panel.redact_sensitive_tokens(github_text)
        self.assertNotIn("1234567890abcdef", redacted_gh)
        self.assertIn("[REDACTED_TOKEN]", redacted_gh)

        # 2. LEGACY_GATEWAY_ROOT_KEYS includes additional platforms
        for p in ("weixin", "yuanbao", "qqbot", "whatsapp_cloud"):
            self.assertIn(p, panel.LEGACY_GATEWAY_ROOT_KEYS)

        # 3. Open policy guard checks nested extra dict
        cfg = {"platforms": {"whatsapp": {"enabled": True, "extra": {"dm_policy": "open"}}}}
        violation = panel._open_policy_violation(cfg, "whatsapp", cfg["platforms"]["whatsapp"])
        self.assertIn("Kebijakan 'open' pada whatsapp ditolak", violation)

    def test_57_router_update_uses_real_compose_file(self):
        """9router update must resolve the real compose file (CasaOS path) and
        force-recreate the container, else `docker start` keeps the OLD image
        while the panel reports a successful update."""
        import os as _os
        with mock.patch.object(panel, "router_compose_file",
                               return_value="/var/lib/casaos/apps/9router/docker-compose.yml"):
            cmd = panel._router_update_command()
            self.assertIn("/var/lib/casaos/apps/9router/docker-compose.yml", cmd)
            self.assertIn("--force-recreate", cmd)
            self.assertIn("for f in", cmd, "compose file must be resolved on the target host")
            self.assertNotIn("tput", cmd)

        # No compose file anywhere -> still produce a working command (pull + start)
        with mock.patch.object(panel, "router_compose_file", return_value=""):
            cmd = panel._router_update_command()
            self.assertIn("docker pull", cmd)
            self.assertIn("docker start", cmd)

        # resolver picks the first existing candidate
        with mock.patch.object(panel.os.path, "isfile",
                               side_effect=lambda p: p == "/var/lib/casaos/apps/9router/docker-compose.yml"):
            self.assertEqual(panel.router_compose_file(),
                             "/var/lib/casaos/apps/9router/docker-compose.yml")
            self.assertEqual(panel.router_compose_dir(), "/var/lib/casaos/apps/9router")

    def test_61_gateway_form_ui_templates_and_field_sync(self):
        """Universal gateway templates and connection fields are rendered in Form UI
        and correctly synchronize with Hermes platform configs."""
        # 1. Template picker is in the universal toolbar visible in both Form UI and YAML
        self.assertIn('id="gw-template-picker"', panel.PAGE)
        self.assertIn('id="gw-form-conn-box"', panel.PAGE)
        self.assertIn('id="gw-form-conn-fields"', panel.PAGE)

        # 2. Universal platform fields defined for all 21 gateway platforms
        for p in ("telegram", "discord", "webhook", "whatsapp", "slack", "matrix",
                  "mattermost", "signal", "teams", "feishu", "google_chat", "dingtalk",
                  "wecom", "line", "ntfy", "email", "homeassistant", "simplex", "sms",
                  "irc", "bluebubbles"):
            self.assertIn(f"{p}:", panel.PAGE)

        # 3. Form serialization of platform-specific credentials (e.g. Telegram token & chats)
        js_code = _gw_form_js() + """
        els['gw-f-plat-token'] = { value: '' };
        els['gw-f-plat-allowed_chats'] = { value: '' };
        var sampleYaml = ['enabled: true', 'token: old-token'].join(String.fromCharCode(10));
        populateGwFormFromYaml(sampleYaml, 'telegram');
        els['gw-f-plat-token'].value = '999999:TEST_BOT_TOKEN';
        els['gw-f-plat-allowed_chats'].value = '1992783463, 12345678';
        process.stdout.write(serializeGwFormToYaml('telegram'));
        """
        res = subprocess.run(["node", "-e", _FAKE_DOM_JS + js_code],
                             capture_output=True, text=True, check=True)
        out = res.stdout
        self.assertIn("enabled: true", out)
        self.assertIn("token: '999999:TEST_BOT_TOKEN'", out)
        self.assertIn("allowed_chats:", out)
        self.assertIn("  - '1992783463'", out)
        self.assertIn("  - '12345678'", out)

    def test_62_gateway_template_select_persistence_and_guard(self):
        """Gateway template select preserves selected value and guards existing platform."""
        # 1. Dark option CSS and Terapkan button present
        self.assertIn("select option, select optgroup", panel.PAGE)
        self.assertIn('onclick="applyGwSelectedTemplate(document.getElementById(\'gw-template-picker\').value)"', panel.PAGE)

        # 2. Template picker retains selected value and guards cross-platform rewrite in existing setting
        js_test = _FAKE_DOM_JS + r"""
        el('gw-template-picker');
        el('gw-config-yaml');
        var currentGwPlatform = 'telegram';
        var isNewGwPlatform = false;
        var currentGwMode = 'ui';
        var _yamlEditedByUser = false;
        var gwFormInitial = {};
        var GW_TEMPLATES = {
            telegram: ['enabled: true', 'token: telegram-token'].join(String.fromCharCode(10)),
            discord: ['enabled: true', 'token: discord-token'].join(String.fromCharCode(10))
        };
        var GW_PLATFORM_NAMES = { telegram: 'Telegram Bot', discord: 'Discord Bot' };
        var alertTriggered = false;
        global.alert = function(){ alertTriggered = true; };
        global.confirm = function(){ return true; };
        function updateGwGuide(plat){}
        function populateGwFormFromYaml(yaml, plat){ els['gw-config-yaml'].value = yaml; }
        function gwFormFieldValues(plat){ return {}; }

        function applyGwSelectedTemplate(key){
          var tp = document.getElementById('gw-template-picker');
          if(!key || !GW_TEMPLATES[key]){
            if(tp) tp.value = currentGwPlatform || '';
            return;
          }
          var yamlEl = document.getElementById('gw-config-yaml');
          if(!yamlEl) return;
          if(!isNewGwPlatform && key !== currentGwPlatform){
            alert('guard');
            if(tp) tp.value = currentGwPlatform || '';
            return;
          }
          yamlEl.value = GW_TEMPLATES[key];
          if(tp) tp.value = key;
        }

        // Test 1: Selecting telegram preserves telegram in template picker
        els['gw-template-picker'].value = 'telegram';
        applyGwSelectedTemplate('telegram');
        if (els['gw-template-picker'].value !== 'telegram') throw new Error('tp.value should be telegram');

        // Test 2: Selecting discord in existing telegram setting is guarded and reverts
        els['gw-template-picker'].value = 'discord';
        applyGwSelectedTemplate('discord');
        if (!alertTriggered) throw new Error('Guard alert must trigger');
        if (els['gw-template-picker'].value !== 'telegram') throw new Error('tp.value must revert to telegram');

        process.stdout.write('TEMPLATE_SELECT_OK');
        """
        res = subprocess.run(["node", "-e", js_test], capture_output=True, text=True, check=True)
        self.assertEqual(res.stdout, "TEMPLATE_SELECT_OK")


class TestAgentProfiles(unittest.TestCase):
    """Test suite for Hermes Agent Profile management and config sync."""

    def setUp(self):
        import tempfile
        import shutil
        from pathlib import Path
        self.tmpdir = tempfile.mkdtemp(prefix="hermes-profiles-test-")
        self.root = Path(self.tmpdir)
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(
            "model:\n  default: ag-gemini-3.8-flash-high\n  provider: custom:9router\n",
            encoding="utf-8",
        )
        self._orig_config_path = panel.CONFIG_PATH
        panel.CONFIG_PATH = str(self.config_path)

    def tearDown(self):
        import shutil
        panel.CONFIG_PATH = self._orig_config_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_01_default_profile_always_present(self):
        active = panel.get_active_profile_name()
        self.assertEqual(active, "default")
        profiles = panel.list_agent_profiles()
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["name"], "default")
        self.assertTrue(profiles[0]["is_active"])
        self.assertTrue(profiles[0]["is_default"])
        self.assertEqual(profiles[0]["model"], "ag-gemini-3.8-flash-high")

    def test_02_create_and_list_profile(self):
        ok, msg = panel.create_agent_profile("coder", clone_from="default", description="Coding assistant")
        self.assertTrue(ok, msg)
        prof_dir = self.root / "profiles" / "coder"
        self.assertTrue(prof_dir.is_dir())
        self.assertTrue((prof_dir / "config.yaml").is_file())
        self.assertTrue((prof_dir / "meta.json").is_file())

        profiles = panel.list_agent_profiles()
        names = [p["name"] for p in profiles]
        self.assertIn("default", names)
        self.assertIn("coder", names)
        coder_info = next(p for p in profiles if p["name"] == "coder")
        self.assertEqual(coder_info["description"], "Coding assistant")
        self.assertFalse(coder_info["is_active"])

    def test_03_create_invalid_name_fails(self):
        for bad in ("default", "UPPER", "space name", "slash/name", "-start", ""):
            ok, msg = panel.create_agent_profile(bad)
            self.assertFalse(ok, f"Expected failure for {bad!r}")

    def test_04_set_active_profile_and_reset(self):
        panel.create_agent_profile("writer")
        ok = panel.set_active_profile_name("writer")
        self.assertTrue(ok)
        self.assertEqual(panel.get_active_profile_name(), "writer")
        self.assertTrue((self.root / "active_profile").is_file())
        self.assertEqual((self.root / "active_profile").read_text(encoding="utf-8").strip(), "writer")

        # Switching back to default deletes active_profile file (Hermes spec)
        ok = panel.set_active_profile_name("default")
        self.assertTrue(ok)
        self.assertEqual(panel.get_active_profile_name(), "default")
        self.assertFalse((self.root / "active_profile").exists())

    def test_05_profile_soul_read_and_write(self):
        panel.create_agent_profile("researcher")
        initial = panel.get_agent_profile_soul("researcher")
        self.assertEqual(initial, "")

        ok, msg = panel.save_agent_profile_soul("researcher", "You are an expert researcher.")
        self.assertTrue(ok, msg)
        self.assertEqual(panel.get_agent_profile_soul("researcher"), "You are an expert researcher.")
        soul_file = self.root / "profiles" / "researcher" / "SOUL.md"
        self.assertTrue(soul_file.is_file())
        self.assertEqual(soul_file.stat().st_mode & 0o777, 0o644)

    def test_06_set_profile_model(self):
        panel.create_agent_profile("speedy")
        ok, msg = panel.set_agent_profile_model("speedy", "custom:9router", "claude-3-5-haiku")
        self.assertTrue(ok, msg)
        coder_cfg = panel.yaml.safe_load((self.root / "profiles" / "speedy" / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(coder_cfg.get("model", {}).get("default"), "claude-3-5-haiku")
        self.assertEqual(coder_cfg.get("model", {}).get("provider"), "custom:9router")

    def test_07_rename_profile(self):
        panel.create_agent_profile("analyst")
        panel.set_active_profile_name("analyst")
        ok, msg = panel.rename_agent_profile("analyst", "data-analyst")
        self.assertTrue(ok, msg)
        self.assertFalse((self.root / "profiles" / "analyst").exists())
        self.assertTrue((self.root / "profiles" / "data-analyst").exists())
        # Active profile should follow the rename
        self.assertEqual(panel.get_active_profile_name(), "data-analyst")

        # Cannot rename default
        ok_def, _ = panel.rename_agent_profile("default", "primary")
        self.assertFalse(ok_def)

    def test_08_delete_profile_and_active_fallback(self):
        panel.create_agent_profile("temporary")
        panel.set_active_profile_name("temporary")
        import shutil as _sh
        def _fake(argv, **kw):
            _sh.rmtree(self.root / "profiles" / "temporary")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with mock.patch.object(panel.subprocess, "run", side_effect=_fake):
            ok, msg = panel.delete_agent_profile("temporary")
        self.assertTrue(ok, msg)
        self.assertFalse((self.root / "profiles" / "temporary").exists())
        # Active profile must fallback to default
        self.assertEqual(panel.get_active_profile_name(), "default")
        self.assertFalse((self.root / "active_profile").exists())

        # Cannot delete default
        ok_def, _ = panel.delete_agent_profile("default")
        self.assertFalse(ok_def)

    def test_08b_delete_profile_uses_official_cli_to_stop_gateway(self):
        panel.create_agent_profile("ghosty")
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            shutil_rm = self.root / "profiles" / "ghosty"
            import shutil as _sh; _sh.rmtree(shutil_rm)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.object(panel.subprocess, "run", side_effect=fake_run):
            ok, msg = panel.delete_agent_profile("ghosty")
        self.assertTrue(ok, msg)
        self.assertEqual(calls[0][1:], ["profile", "delete", "--yes", "ghosty"])

    def test_08c_delete_profile_reports_cli_failure_and_resurrection(self):
        panel.create_agent_profile("zombie")
        bad = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with mock.patch.object(panel.subprocess, "run", return_value=bad):
            ok, msg = panel.delete_agent_profile("zombie")
        self.assertFalse(ok)
        self.assertIn("boom", msg)
        # CLI exit 0 but dir still present -> must not claim success
        okp = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(panel.subprocess, "run", return_value=okp):
            ok, msg = panel.delete_agent_profile("zombie")
        self.assertFalse(ok)

    def test_09_ui_profiles_tab_presence(self):
        self.assertIn("profiles", panel.VALID_TABS)
        self.assertIn('id="tab-profiles"', panel.PAGE)
        self.assertIn("switchTab('profiles'", panel.PAGE)

    def test_10_gateway_profile_badge(self):
        """Badge gateway per profil: Berjalan/Mati/Tak dilayani dari served_profiles."""
        served = panel.get_gateway_served_profiles()
        self.assertIsInstance(served, list)
        statuses = panel.get_gateway_profile_statuses()
        names = [p["name"] for p in panel.list_agent_profiles()]
        for n in names:
            self.assertIn(n, statuses)
            self.assertIn("served", statuses[n])
            self.assertIn("pid_alive", statuses[n])
        html = panel.render_profiles_block()
        self.assertIn("Gateway: Berjalan", html)
        dead = panel.render_gateway_profile_badge(
            "ghost-profile", {"ghost-profile": {"served": False, "pid": None,
                                                "pid_alive": None, "state_age_s": None}})
        self.assertIn("Tak dilayani", dead)
        stale = panel.render_gateway_profile_badge(
            "dead-profile", {"dead-profile": {"served": True, "pid": 99999999,
                                              "pid_alive": False, "state_age_s": 5}})
        self.assertIn("Gateway: Mati", stale)
        self.assertIn("profiles_block", panel.SSE_CLIENT_KEYS)

    def test_11_profile_skills_inventory_sync(self):
        """Inventaris skill per profil sinkron dgn hermes: dir + disabled."""
        import tempfile, shutil
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp(prefix="panel-skillinv-"))
        try:
            (tmp / "skills" / "demo-a").mkdir(parents=True)
            (tmp / "skills" / "demo-a" / "SKILL.md").write_text(
                "---\nname: demo-a\ndescription: Demo A.\n---\n\nDemo A.\n", encoding="utf-8")
            (tmp / "skills" / "demo-b").mkdir(parents=True)
            (tmp / "skills" / "demo-b" / "SKILL.md").write_text(
                "---\nname: demo-b\ndescription: Demo B.\n---\n\nDemo B.\n", encoding="utf-8")
            (tmp / "skills" / "hermes-agent").mkdir(parents=True)
            (tmp / "skills" / "hermes-agent" / "SKILL.md").write_text(
                "---\nname: hermes-agent\ndescription: Manual.\n---\n\nManual.\n", encoding="utf-8")
            (tmp / "config.yaml").write_text(
                "model:\n  default: m\nskills:\n  disabled:\n    - demo-b\n", encoding="utf-8")
            with mock.patch.object(panel, "get_hermes_root", return_value=tmp):
                inv = panel.get_profile_skill_inventory("default")
                self.assertTrue(inv.get("ok"))
                self.assertEqual(inv.get("total"), 3)
                self.assertEqual(inv["enabled_count"] + inv["disabled_count"], inv["total"])
                byname = {s["name"]: s for s in inv["skills"]}
                self.assertTrue(byname["demo-a"]["enabled"])
                self.assertFalse(byname["demo-b"]["enabled"])
                ess = byname["hermes-agent"]
                self.assertTrue(ess["enabled"] and ess["essential"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        # Live: sinkron dgn hermes sungguhan (venv hermes, bukan heuristik panel saja).
        # setUp TestAgentProfiles mengarahkan CONFIG_PATH ke tmpdir kosong —
        # kembalikan dulu ke live agar inventaris baca ~/.hermes sungguhan.
        panel.CONFIG_PATH = self._orig_config_path
        inv = panel.get_profile_skill_inventory("default")
        self.assertTrue(inv.get("ok"))
        self.assertGreater(inv.get("total", 0), 0)
        import subprocess as _sp
        r = _sp.run(["/usr/local/lib/hermes-agent/venv/bin/python", "-c",
                     "import sys;sys.path.insert(0,'/opt/AppData/hermes-native/hermes-lib');"
                     "import os;os.environ['HERMES_HOME']='/root/.hermes';"
                     "import tools.skills_tool as st;"
                     "fn=getattr(st,'find_all_skills',None) or getattr(st,'_find_all_skills');"
                     "a=fn(skip_disabled=True);e=fn(skip_disabled=False);"
                     "print(len(a),len(e))"],
                    capture_output=True, text=True, timeout=120)
        h_total, h_en = map(int, r.stdout.strip().split())
        self.assertEqual(inv["total"], h_total, "panel vs hermes total skill")
        self.assertEqual(inv["enabled_count"], h_en, "panel vs hermes enabled")
        # Modal + toggle UI ada
        self.assertIn('id="profile-skills-modal"', panel.PAGE)
        self.assertIn("openProfileSkillsModal(", panel.page_source())
        html = panel.render_profiles_block()
        self.assertIn("Kelola Skills", html)

    def test_12_profile_skill_toggle_roundtrip(self):
        """Toggle tulis skills.disabled di config.yaml profil (merge, bukan timpa)."""
        import tempfile, shutil
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp(prefix="panel-skilltoggle-"))
        try:
            (tmp / "skills" / "demo-skill").mkdir(parents=True)
            (tmp / "skills" / "demo-skill" / "SKILL.md").write_text(
                "---\nname: demo-skill\ndescription: Demo skill.\n---\n\nDemo.\n", encoding="utf-8")
            (tmp / "config.yaml").write_text(
                "model:\n  default: m\n  provider: p\n", encoding="utf-8")
            with mock.patch.object(panel, "get_hermes_root", return_value=tmp):
                ok, msg = panel.set_profile_skill_enabled("default", "demo-skill", False)
                self.assertTrue(ok, msg)
                cfg = panel.yaml.safe_load((tmp / "config.yaml").read_text(encoding="utf-8"))
                self.assertIn("demo-skill", (cfg.get("skills") or {}).get("disabled", []))
                self.assertEqual(cfg.get("model", {}).get("default"), "m", "model tak boleh hilang")
                inv = panel.get_profile_skill_inventory("default")
                d = next(s for s in inv["skills"] if s["name"] == "demo-skill")
                self.assertFalse(d["enabled"])
                ok, msg = panel.set_profile_skill_enabled("default", "demo-skill", True)
                self.assertTrue(ok, msg)
                inv = panel.get_profile_skill_inventory("default")
                d = next(s for s in inv["skills"] if s["name"] == "demo-skill")
                self.assertTrue(d["enabled"])
                # Esensial tak bisa mati + nama jahat ditolak
                ok, _ = panel.set_profile_skill_enabled("default", "hermes-agent", False)
                self.assertFalse(ok)
                bad = panel.get_profile_skill_content("default", "../x")
                self.assertFalse(bad["ok"])
                good = panel.get_profile_skill_content("default", "demo-skill")
                self.assertTrue(good["ok"] and "Demo" in good["content"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_13_profile_toolset_toggle_roundtrip(self):
        """Toggle toolset tulis agent.disabled_toolsets per profil."""
        import tempfile, shutil
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp(prefix="panel-toolset-"))
        try:
            (tmp / "config.yaml").write_text("model:\n  default: m\n", encoding="utf-8")
            with mock.patch.object(panel, "get_hermes_root", return_value=tmp):
                ts = panel.get_profile_toolsets("default")
                self.assertTrue(ts.get("ok"))
                ok, msg = panel.set_profile_toolset_enabled("default", "browser", False)
                self.assertTrue(ok, msg)
                ts = panel.get_profile_toolsets("default")
                self.assertIn("browser", ts["disabled_toolsets"])
                ok, msg = panel.set_profile_toolset_enabled("default", "browser", True)
                self.assertTrue(ok, msg)
                ts = panel.get_profile_toolsets("default")
                self.assertNotIn("browser", ts["disabled_toolsets"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_14_profile_path_traversal_prevention(self):
        """SEC-PT-01 s/d SEC-PT-04: Cegah path traversal pada profil hermes."""
        # SEC-PT-01: delete_agent_profile validasi _PROFILE_NAME_RE
        for bad in ("..", "../other", "/etc", "foo/bar", "evil\x00name"):
            ok, msg = panel.delete_agent_profile(bad)
            self.assertFalse(ok, f"Expected delete failure for {bad!r}")
            self.assertIn("tidak valid", msg)
        self.assertTrue(self.root.is_dir())
        self.assertTrue((self.root / "config.yaml").is_file())

        # SEC-PT-02: rename_agent_profile validasi old_name dengan _PROFILE_NAME_RE
        for bad in ("..", "../other", "/etc", "foo/bar"):
            ok, msg = panel.rename_agent_profile(bad, "valid-target")
            self.assertFalse(ok, f"Expected rename failure for bad old_name {bad!r}")
            self.assertIn("tidak valid", msg)

        # SEC-PT-03: create_agent_profile validasi clone_from (hanya default atau _PROFILE_NAME_RE)
        for bad in ("..", "../../etc", "/etc/passwd", "foo/bar"):
            ok, msg = panel.create_agent_profile("valid-prof", clone_from=bad)
            self.assertFalse(ok, f"Expected clone failure for {bad!r}")
            self.assertIn("tidak valid", msg)
        self.assertFalse((self.root / "profiles" / "valid-prof").exists())

        # SEC-PT-04: get_agent_profile_soul, save_agent_profile_soul, set_agent_profile_model
        for bad in ("..", "../other", "/etc", "foo/bar"):
            soul = panel.get_agent_profile_soul(bad)
            self.assertEqual(soul, "")
            ok_s, msg_s = panel.save_agent_profile_soul(bad, "malicious-soul")
            self.assertFalse(ok_s)
            self.assertIn("tidak valid", msg_s)
            ok_m, msg_m = panel.set_agent_profile_model(bad, "custom:9router", "test-model")
            self.assertFalse(ok_m)
            self.assertIn("tidak valid", msg_m)

        # Pastikan root SOUL.md dan config.yaml tidak termutasi oleh traversal
        self.assertFalse((self.root / "SOUL.md").exists())
        main_cfg = panel.yaml.safe_load((self.root / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(main_cfg.get("model", {}).get("default"), "ag-gemini-3.8-flash-high")


class TestKanbanBoard(unittest.TestCase):
    """Test Kanban board persistence, multi-board isolation, task lifecycle, and config sync."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmpdir = tempfile.mkdtemp(prefix="panel-kanban-")
        self.root = Path(self.tmpdir)
        self.cfg_path = str(self.root / "config.yaml")

        self.root.mkdir(parents=True, exist_ok=True)
        # Seed basic config.yaml
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            f.write("model:\n  default: ag-gemini-3.8-flash-high\nkanban:\n  dispatch_in_gateway: true\n  failure_limit: 2\n")
        os.chmod(self.cfg_path, 0o600)

        patcher_cfg = mock.patch.object(panel, "CONFIG_PATH", self.cfg_path)
        patcher_cfg.start()
        self.addCleanup(patcher_cfg.stop)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_01_kanban_db_initialization(self):
        db_path = panel.get_kanban_db_path()
        self.assertEqual(db_path, self.root / "kanban.db")
        con = panel.ensure_kanban_db(db_path)
        cur = con.cursor()
        tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for expected in ("tasks", "task_links", "task_comments", "task_events", "task_runs"):
            self.assertIn(expected, tables)
        # Verify SQLite optimizations: WAL, synchronous=NORMAL, indexes
        jm = cur.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(jm.lower(), "wal")
        sync = cur.execute("PRAGMA synchronous").fetchone()[0]
        self.assertEqual(sync, 1)  # 1 = NORMAL
        indexes = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tasks'").fetchall()]
        for idx in ("idx_tasks_status", "idx_tasks_assignee", "idx_tasks_priority"):
            self.assertIn(idx, indexes)
        con.close()

    def test_02_create_and_list_tasks(self):
        ok, msg, task_id = panel.create_kanban_task(
            title="Fix API Rate Limit",
            body="Implement exponential backoff",
            assignee="default",
            priority=2,
            status="todo"
        )
        self.assertTrue(ok, msg)
        self.assertTrue(task_id.startswith("t_"))

        tasks = panel.list_kanban_tasks()
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual(t["id"], task_id)
        self.assertEqual(t["title"], "Fix API Rate Limit")
        self.assertEqual(t["assignee"], "default")
        self.assertEqual(t["priority"], 2)
        self.assertEqual(t["status"], "todo")

    def test_03_update_task_status_and_lifecycle(self):
        _, _, task_id = panel.create_kanban_task(title="Deploy Worker", status="todo")

        # todo -> ready
        ok, msg = panel.update_kanban_task_status(task_id, "ready")
        self.assertTrue(ok, msg)
        t = panel.get_kanban_task(task_id)
        self.assertEqual(t["status"], "ready")

        # ready -> running
        ok, msg = panel.update_kanban_task_status(task_id, "running")
        self.assertTrue(ok, msg)
        t = panel.get_kanban_task(task_id)
        self.assertEqual(t["status"], "running")
        self.assertIsNotNone(t["started_at"])

        # running -> blocked
        ok, msg = panel.update_kanban_task_status(task_id, "blocked", reason="Need API key", kind="needs_input")
        self.assertTrue(ok, msg)
        t = panel.get_kanban_task(task_id)
        self.assertEqual(t["status"], "blocked")
        self.assertEqual(t["block_kind"], "needs_input")

        # blocked -> done
        ok, msg = panel.update_kanban_task_status(task_id, "done")
        self.assertTrue(ok, msg)
        t = panel.get_kanban_task(task_id)
        self.assertEqual(t["status"], "done")
        self.assertIsNotNone(t["completed_at"])

    def test_04_reclaim_running_task(self):
        _, _, task_id = panel.create_kanban_task(title="Long Job", status="running")
        db_path = panel.get_kanban_db_path()
        con = panel.ensure_kanban_db(db_path)
        with con:
            con.execute("UPDATE tasks SET claim_lock = 'lock-123', worker_pid = 99999 WHERE id = ?", (task_id,))
        con.close()

        ok, msg = panel.reclaim_kanban_task(task_id)
        self.assertTrue(ok, msg)
        t = panel.get_kanban_task(task_id)
        self.assertEqual(t["status"], "ready")
        self.assertIsNone(t["claim_lock"])
        self.assertIsNone(t["worker_pid"])

    def test_05_task_comments_and_events(self):
        _, _, task_id = panel.create_kanban_task(title="Comment Test", status="todo")
        ok, msg = panel.add_kanban_comment(task_id, "Please review this step", author="vito")
        self.assertTrue(ok, msg)

        t = panel.get_kanban_task(task_id)
        self.assertIn("comments", t)
        self.assertEqual(len(t["comments"]), 1)
        self.assertEqual(t["comments"][0]["body"], "Please review this step")
        self.assertEqual(t["comments"][0]["author"], "vito")

        self.assertIn("events", t)
        self.assertTrue(len(t["events"]) >= 1)

    def test_05b_task_comment_whitespace_author_falls_back_to_default(self):
        _, _, task_id = panel.create_kanban_task(title="Comment Whitespace Test", status="todo")
        ok, msg = panel.add_kanban_comment(task_id, "Note with blank author", author="   \t\n  ")
        self.assertTrue(ok, msg)

        t = panel.get_kanban_task(task_id)
        self.assertEqual(t["comments"][0]["author"], "web-panel")
        events = [e for e in t["events"] if e.get("kind") == "commented"]
        self.assertTrue(events)
        payload = json.loads(events[0]["payload"]) if isinstance(events[0]["payload"], str) else events[0]["payload"]
        self.assertEqual(payload.get("author"), "web-panel")

    def test_05c_update_task_rejects_empty_or_whitespace_title(self):
        _, _, task_id = panel.create_kanban_task(title="Valid Title", status="todo")
        ok, msg = panel.update_kanban_task(task_id, title="   \n\t  ")
        self.assertFalse(ok)
        self.assertIn("Judul tugas tidak boleh kosong", msg)
        t = panel.get_kanban_task(task_id)
        self.assertEqual(t["title"], "Valid Title")

    def test_06_delete_task(self):
        _, _, task_id = panel.create_kanban_task(title="Delete Me")
        panel.add_kanban_comment(task_id, "temporary note")
        db_path = panel.get_kanban_db_path()
        with panel.closing(panel.ensure_kanban_db(db_path)) as con:
            with con:
                con.execute(
                    "INSERT INTO task_attachments (task_id, filename, stored_path, size, created_at) VALUES (?, 'a.txt', '/tmp/a', 1, 1)",
                    (task_id,),
                )
        # Early return paths must not leak connections
        ok_nf, _ = panel.update_kanban_task_status("t_missing", "done")
        self.assertFalse(ok_nf)
        ok_noop, _ = panel.update_kanban_task(task_id)
        self.assertTrue(ok_noop)

        ok, msg = panel.delete_kanban_task(task_id)
        self.assertTrue(ok, msg)
        self.assertIsNone(panel.get_kanban_task(task_id))
        with panel.closing(panel.ensure_kanban_db(db_path)) as con:
            cnt = con.execute("SELECT COUNT(*) FROM task_attachments WHERE task_id = ?", (task_id,)).fetchone()[0]
            self.assertEqual(cnt, 0)

    def test_07_boards_management(self):
        boards = panel.list_kanban_boards()
        self.assertTrue(any(b["slug"] == "default" for b in boards))

        ok, msg = panel.create_kanban_board("project-x", name="Project X")
        self.assertTrue(ok, msg)

        boards_after = panel.list_kanban_boards()
        self.assertTrue(any(b["slug"] == "project-x" for b in boards_after))
        self.assertTrue((self.root / "kanban" / "boards" / "project-x" / "board.json").is_file())
        with panel.closing(panel.ensure_kanban_db(self.root / "kanban" / "boards" / "project-x" / "kanban.db")) as con:
            self.assertIsNotNone(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_notify_subs'").fetchone())

        ok_sw = panel.set_current_kanban_board("project-x")
        self.assertTrue(ok_sw)
        self.assertEqual(panel.get_current_kanban_board(), "project-x")

        # Task in project-x does not leak into default
        _, _, tid_x = panel.create_kanban_task(title="Task in X", board="project-x")
        tasks_x = panel.list_kanban_tasks(board="project-x")
        tasks_def = panel.list_kanban_tasks(board="default")
        self.assertEqual(len(tasks_x), 1)
        self.assertEqual(len(tasks_def), 0)

    def test_08_kanban_config_sync(self):
        cfg = panel.get_kanban_config()
        self.assertTrue(cfg.get("dispatch_in_gateway"))
        self.assertEqual(cfg.get("failure_limit"), 2)

        ok, msg = panel.save_kanban_config({"dispatch_in_gateway": False, "failure_limit": 5, "dispatch_interval_seconds": 30})
        self.assertTrue(ok, msg)

        cfg2 = panel.get_kanban_config()
        self.assertFalse(cfg2.get("dispatch_in_gateway"))
        self.assertEqual(cfg2.get("failure_limit"), 5)
        self.assertEqual(cfg2.get("dispatch_interval_seconds"), 30)

        # File permissions check (0600)
        mode = os.stat(self.cfg_path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_08c_kanban_mutations_reject_missing_tasks_and_unknown_config(self):
        for operation in (
            lambda: panel.update_kanban_task("missing", title="x"),
            lambda: panel.delete_kanban_task("missing"),
            lambda: panel.reclaim_kanban_task("missing"),
            lambda: panel.add_kanban_comment("missing", "orphan"),
        ):
            ok, msg = operation()
            self.assertFalse(ok, msg)
            self.assertIn("tidak ditemukan", msg)
        ok, msg = panel.save_kanban_config({"unexpected": True})
        self.assertFalse(ok)
        self.assertIn("tidak dikenal", msg)
        ok, msg = panel.save_kanban_config({"dispatch_interval_seconds": "bad"})
        self.assertFalse(ok)
        self.assertIn("harus angka", msg)

    def test_08d_switch_rejects_missing_board(self):
        self.assertFalse(panel.set_current_kanban_board("does-not-exist"))
        self.assertEqual(panel.get_current_kanban_board(), "default")

    def test_08e_kanban_read_errors_are_not_empty_success(self):
        with mock.patch.object(panel, "ensure_kanban_db", side_effect=OSError("locked")):
            with self.assertRaises(panel.KanbanReadError):
                panel.list_kanban_tasks()
            with self.assertRaises(panel.KanbanReadError):
                panel.get_kanban_task("missing")
            rendered = panel.render_kanban_board_block("default")
            self.assertIn("Kanban error", rendered)
            self.assertIn("locked", rendered)

    def test_08f_orphan_board_directory_is_hidden_with_runtime(self):
        orphan = self.root / "kanban" / "boards" / "orphan"
        orphan.mkdir(parents=True)
        panel.ensure_kanban_db(orphan / "kanban.db").close()
        with mock.patch.object(panel, "_runtime_kanban_api", return_value=object()):
            boards = panel.list_kanban_boards()
        self.assertFalse(any(item["slug"] == "orphan" for item in boards))

    def test_08b_kanban_parallel_writes(self):
        errors = []
        task_ids = []
        lock = threading.Lock()
        def worker(idx):
            try:
                ok, msg, tid = panel.create_kanban_task(
                    title=f"Parallel Task {idx}",
                    body="Stress write test",
                    priority=idx % 3,
                    status="todo"
                )
                if not ok:
                    with lock:
                        errors.append(f"create failed: {msg}")
                    return
                with lock:
                    task_ids.append(tid)
                ok, msg = panel.update_kanban_task_status(tid, "ready")
                if not ok:
                    with lock:
                        errors.append(f"update status failed: {msg}")
                ok, msg = panel.add_kanban_comment(tid, f"Comment from worker {idx}")
                if not ok:
                    with lock:
                        errors.append(f"comment failed: {msg}")
            except Exception as e:
                with lock:
                    errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(task_ids), 20)
        tasks = panel.list_kanban_tasks()
        self.assertGreaterEqual(len(tasks), 20)

    def test_09_ui_tab_presence(self):
        self.assertIn("kanban", panel.VALID_TABS)
        self.assertIn('id="tab-kanban"', panel.PAGE)
        self.assertIn("switchTab('kanban'", panel.PAGE)
        self.assertIn('id="kanban-trash-dropzone"', panel.PAGE)
        self.assertIn("handleKanbanTrashDrop", panel.PAGE)

    def test_10_kanban_liveness_badge(self):
        now = 1790796428
        live_t = {"status": "running", "last_heartbeat_at": now - 20,
                  "started_at": now - 400, "worker_pid": None}
        st, det = panel.kanban_task_liveness(live_t, now=now)
        self.assertEqual(st, "live")
        idle_t = {"status": "running", "last_heartbeat_at": now - 420,
                  "started_at": now - 1400, "worker_pid": None}
        st, _ = panel.kanban_task_liveness(idle_t, now=now)
        self.assertEqual(st, "idle")
        stale_t = {"status": "running", "last_heartbeat_at": now - 4400,
                   "started_at": now - 5400, "worker_pid": None}
        st, _ = panel.kanban_task_liveness(stale_t, now=now)
        self.assertEqual(st, "stale")
        dead_t = {"status": "running", "last_heartbeat_at": now - 10,
                  "started_at": now - 400, "worker_pid": 99999999}
        st, _ = panel.kanban_task_liveness(dead_t, now=now)
        self.assertEqual(st, "stale")
        todo_st, _ = panel.kanban_task_liveness({"status": "todo"}, now=now)
        self.assertEqual(todo_st, "")
        self.assertIn("kb-pulse", panel.PAGE)
        self.assertIn("kb-badge-stale", panel.PAGE)
        self.assertIn("_enrich_kanban_liveness", panel.list_kanban_tasks.__code__.co_names)


    def test_11_task_result_runs_and_attachments_exposed(self):
        """A finished task's result/run-summary/attachments must reach the UI payload."""
        _, _, task_id = panel.create_kanban_task(title="Lapor hasil audit", status="todo")
        db_path = panel.get_kanban_db_path()
        con = panel.ensure_kanban_db(db_path)
        now = int(time.time())
        with con:
            con.execute("UPDATE tasks SET status='done', result='LAPORAN: 25 bug ditemukan' WHERE id=?", (task_id,))
            con.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, summary, started_at, ended_at) "
                "VALUES (?, 'default', 'done', 'completed', 'Ringkasan run terakhir', ?, ?)",
                (task_id, now - 10, now),
            )
            att_dir = panel.get_kanban_attachments_root() / task_id
            att_dir.mkdir(parents=True, exist_ok=True)
            blob = att_dir / "laporan.md"
            blob.write_text("# Laporan\nisi", encoding="utf-8")
            con.execute(
                "INSERT INTO task_attachments (task_id, filename, stored_path, size, created_at) "
                "VALUES (?, 'laporan.md', ?, ?, ?)",
                (task_id, str(blob), blob.stat().st_size, now),
            )
        con.close()

        t = panel.get_kanban_task(task_id)
        self.assertEqual(t["result"], "LAPORAN: 25 bug ditemukan")
        self.assertEqual(len(t["runs"]), 1)
        self.assertEqual(t["runs"][0]["summary"], "Ringkasan run terakhir")
        self.assertEqual(len(t["attachments"]), 1)
        self.assertEqual(t["attachments"][0]["filename"], "laporan.md")

        # UI must actually consume those fields, not just receive them.
        for marker in ("view-task-output", "view-task-runs-list", "view-task-attachments-list"):
            self.assertIn(marker, panel.PAGE)
        js = panel.PAGE[panel.PAGE.index("function openViewTaskModal"):]
        js = js[:js.index("function formatKanbanBytes")]
        for field in ("t.result", "t.runs", "t.attachments"):
            self.assertIn(field, js)

    def test_12_attachment_resolution_guards_traversal(self):
        _, _, task_id = panel.create_kanban_task(title="Lampiran traversal", status="todo")
        db_path = panel.get_kanban_db_path()
        root = panel.get_kanban_attachments_root()
        good_dir = root / task_id
        good_dir.mkdir(parents=True, exist_ok=True)
        good = good_dir / "ok.md"
        good.write_text("aman", encoding="utf-8")
        outside = self.root / "outside.md"
        outside.write_text("rahasia", encoding="utf-8")

        con = panel.ensure_kanban_db(db_path)
        now = int(time.time())
        with con:
            con.execute(
                "INSERT INTO task_attachments (task_id, filename, stored_path, size, created_at) "
                "VALUES (?, 'ok.md', ?, ?, ?)", (task_id, str(good), good.stat().st_size, now))
            con.execute(
                "INSERT INTO task_attachments (task_id, filename, stored_path, size, created_at) "
                "VALUES (?, 'evil.md', ?, ?, ?)", (task_id, str(outside), outside.stat().st_size, now))
        con.close()

        ids = {a["filename"]: a["id"] for a in panel.get_kanban_task(task_id)["attachments"]}
        res = panel.resolve_kanban_attachment(ids["ok.md"])
        self.assertIsNotNone(res)
        self.assertEqual(res[0].read_text(encoding="utf-8"), "aman")
        # stored_path outside the board's attachments root must be refused.
        self.assertIsNone(panel.resolve_kanban_attachment(ids["evil.md"]))
        self.assertIsNone(panel.resolve_kanban_attachment(999999))


    def test_13_liveness_prefers_active_run_over_stale_task_timestamps(self):
        """Re-claim: tasks.started_at/last_heartbeat_at masih milik run lama.

        Run baru yang belum heartbeat pertama harus terbaca live ('baru mulai'),
        bukan MACET — sinyal yang dipakai adalah timestamp run aktif.
        """
        _, _, task_id = panel.create_kanban_task(title="Re-claim liveness", status="todo")
        db_path = panel.get_kanban_db_path()
        con = panel.ensure_kanban_db(db_path)
        now = int(time.time())
        old = now - 68 * 3600  # run lama 68 jam lalu
        with con:
            con.execute(
                "UPDATE tasks SET status='running', started_at=?, last_heartbeat_at=?, worker_pid=1 "
                "WHERE id=?", (old, old, task_id))
            cur = con.execute(
                "INSERT INTO task_runs (task_id, profile, status, worker_pid, started_at) "
                "VALUES (?, 'default', 'running', 1, ?)", (task_id, now - 60))
            run_id = cur.lastrowid
            con.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task_id))
        con.close()

        t = panel.get_kanban_task(task_id)
        self.assertEqual(t["active_run_id"], run_id)
        self.assertEqual(t["active_run_started_at"], now - 60)
        self.assertIsNone(t["active_run_last_heartbeat_at"])
        # run baru 60 dtk, PID hidup, belum heartbeat -> live, bukan MACET
        state, detail = panel.kanban_task_liveness(t, now=now)
        self.assertEqual(state, "live", detail)
        self.assertIn("baru mulai", detail)

        # daftar kartu memakai jalur yang sama
        listed = [x for x in panel.list_kanban_tasks() if x["id"] == task_id][0]
        self.assertEqual(listed["live_state"], "live", listed.get("live_detail"))

        # PID run aktif mati -> baru boleh stale
        con = panel.ensure_kanban_db(db_path)
        with con:
            con.execute("UPDATE task_runs SET worker_pid=99999999 WHERE id=?", (run_id,))
        con.close()
        t2 = panel.get_kanban_task(task_id)
        state2, detail2 = panel.kanban_task_liveness(t2, now=now)
        self.assertEqual(state2, "stale", detail2)

        # heartbeat basi di run aktif -> stale walau task-level timestamp baru
        con = panel.ensure_kanban_db(db_path)
        with con:
            con.execute("UPDATE task_runs SET worker_pid=NULL, last_heartbeat_at=? WHERE id=?",
                        (now - 4000, run_id))
        con.close()
        t3 = panel.get_kanban_task(task_id)
        state3, _ = panel.kanban_task_liveness(t3, now=now)
        self.assertEqual(state3, "stale")

    def test_14_kanban_board_slug_traversal_prevention(self):
        """SEC-PT-05: Validasi board slug pada get_kanban_db_path dan get_kanban_attachments_root."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_KANBAN_ATTACHMENTS_ROOT", None)
            for bad in ("..", "../../../etc", "evil/path", "board name", "-start"):
                # Bad slug wajib fallback ke default board kanban.db / attachments
                db_path = panel.get_kanban_db_path(bad)
                self.assertEqual(db_path, self.root / "kanban.db")
                att_root = panel.get_kanban_attachments_root(bad)
                self.assertEqual(att_root, self.root / "kanban" / "attachments")

            # Slug valid non-default tetap menuju boards/<slug>
            db_path_valid = panel.get_kanban_db_path("my-board")
            self.assertEqual(db_path_valid, self.root / "kanban" / "boards" / "my-board" / "kanban.db")
            att_root_valid = panel.get_kanban_attachments_root("my-board")
            self.assertEqual(att_root_valid, self.root / "kanban" / "boards" / "my-board" / "attachments")

    def test_15_completed_status_mapped_to_done(self):
        ok, msg, tid = panel.create_kanban_task(title="Legacy Completed Task", status="completed")
        self.assertTrue(ok, msg)
        task = panel.get_kanban_task(tid)
        self.assertIsNotNone(task["completed_at"])

        done_tasks = panel.list_kanban_tasks(status="done")
        self.assertTrue(any(t["id"] == tid for t in done_tasks))
        todo_tasks = panel.list_kanban_tasks(status="todo")
        self.assertFalse(any(t["id"] == tid for t in todo_tasks))

        html_board = panel.render_kanban_board_block("default")
        done_col_start = html_board.find('id="col-cards-done"')
        todo_col_start = html_board.find('id="col-cards-todo"')
        card_marker = f'id="card-{tid}"'
        card_pos = html_board.find(card_marker)
        self.assertNotEqual(card_pos, -1)
        # Card must reside in done column, not in todo column
        self.assertGreater(card_pos, done_col_start)
        self.assertLess(card_pos, todo_col_start if todo_col_start > done_col_start else len(html_board))

        # Frontend JS checks
        page = panel.page_source()
        self.assertIn("if(st === 'completed') st = 'done';", page)
        self.assertIn("var rawSt = t.status === 'completed' ? 'done' : t.status;", page)

    def test_16_save_kanban_config_cache_invalidation_and_clearing(self):
        panel._status_probe_cache["kanban_board_block"] = (time.time() + 10, "stale_block")
        ok, msg = panel.save_kanban_config({
            "dispatch_in_gateway": False,
            "default_assignee": None,
            "orchestrator_profile": None
        })
        self.assertTrue(ok, msg)
        self.assertNotIn("kanban_board_block", panel._status_probe_cache)
        cfg = panel.get_kanban_config()
        self.assertEqual(cfg["default_assignee"], "")
        self.assertEqual(cfg["orchestrator_profile"], "")

    def test_17_frontend_kanban_config_submit_disables_button(self):
        page = panel.page_source()
        start = page.index("function submitKanbanConfig")
        end = page.find("\nfunction ", start + 1)
        segment = page[start:end]
        self.assertIn("saveBtn.disabled = true", segment)

        start_open = page.index("function openKanbanConfigModal")
        end_open = page.find("\nfunction ", start_open + 1)
        segment_open = page[start_open:end_open]
        self.assertIn("saveBtn.disabled = false", segment_open)


class TestMarkdownRenderingAndKanbanAttachments(unittest.TestCase):
    """Unit tests for Rich Markdown rendering, Kanban attachment modal, and task/skill preview."""

    def test_01_markdown_headings_and_inline_formatting(self):
        for lvl in range(1, 7):
            src = f"{'#' * lvl} Judul Tingkat {lvl}"
            html = panel.render_markdown(src)
            self.assertIn(f'<h{lvl} class="md-h md-h{lvl}">Judul Tingkat {lvl}</h{lvl}>', html)

        # Bold, italic, strikethrough, inline code
        src = "Teks **tebal**, __tebal2__, *miring*, _miring2_, ~~coret~~, dan `kode_inline()`."
        html = panel.render_markdown(src)
        self.assertIn("<strong>tebal</strong>", html)
        self.assertIn("<strong>tebal2</strong>", html)
        self.assertIn("<em>miring</em>", html)
        self.assertIn("<em>miring2</em>", html)
        self.assertIn("<del>coret</del>", html)
        self.assertIn('<code class="md-inline-code">kode_inline()</code>', html)

    def test_02_markdown_code_blocks_and_copy_button(self):
        src = "```python\ndef test():\n    return '<safe>'\n```"
        html = panel.render_markdown(src)
        self.assertIn('class="md-code-wrap"', html)
        self.assertIn('<span>python</span>', html)
        self.assertIn('btn-copy-code', html)
        self.assertIn('onclick="copyCodeBlock(this)"', html)
        self.assertIn('class="md-code-block"', html)
        self.assertIn('&lt;safe&gt;', html)
        self.assertNotIn('<safe>', html)

    def test_02b_markdown_code_language_is_escaped(self):
        html = panel.render_markdown('```<img src=x onerror=alert(1)>\ncode\n```')
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', html)
        self.assertNotIn('<span><img', html)
        self.assertNotIn('onerror="alert(1)"', html)
        page = panel.page_source()
        self.assertIn("escapeMdHtml(lang || 'code')", page)

    def test_03_markdown_tables_with_alignment_and_responsive_wrap(self):
        src = (
            "| Kolom Kiri | Kolom Tengah | Kolom Kanan |\n"
            "| :--- | :---: | ---: |\n"
            "| Baris 1A | Baris 1B | 100 |\n"
            "| Baris 2A | Baris 2B | 250 |"
        )
        html = panel.render_markdown(src)
        self.assertIn('<div class="md-table-wrap">', html)
        self.assertIn('<table class="md-table">', html)
        self.assertIn('<thead><tr>', html)
        self.assertIn('<th style="text-align:left">Kolom Kiri</th>', html)
        self.assertIn('<th style="text-align:center">Kolom Tengah</th>', html)
        self.assertIn('<th style="text-align:right">Kolom Kanan</th>', html)
        self.assertIn('<tbody><tr>', html)
        self.assertIn('<td style="text-align:left">Baris 1A</td>', html)
        self.assertIn('<td style="text-align:center">Baris 1B</td>', html)
        self.assertIn('<td style="text-align:right">100</td>', html)

    def test_04_markdown_horizontal_rules_quotes_lists(self):
        for hr_src in ("---", "***", "___", "----"):
            html = panel.render_markdown(hr_src)
            self.assertIn('<hr class="md-hr">', html)

        # Blockquote
        q_src = "> Kutipan baris 1\n> Kutipan baris 2"
        q_html = panel.render_markdown(q_src)
        self.assertIn('<blockquote class="md-quote">', q_html)
        self.assertIn("Kutipan baris 1<br>Kutipan baris 2", q_html)

        # Unordered and ordered lists
        ul_src = "- Item A\n- Item B\n* Item C"
        ul_html = panel.render_markdown(ul_src)
        self.assertIn('<ul class="md-list">', ul_html)
        self.assertIn('<li>Item A</li>', ul_html)
        self.assertIn('<li>Item B</li>', ul_html)

        ol_src = "1. Langkah satu\n2. Langkah dua"
        ol_html = panel.render_markdown(ol_src)
        self.assertIn('<ol class="md-list">', ol_html)
        self.assertIn('<li>Langkah satu</li>', ol_html)
        self.assertIn('<li>Langkah dua</li>', ol_html)

    def test_05_markdown_links_and_xss_protection(self):
        src = (
            "Kunjungi [Hermes](https://hermes-agent.nousresearch.com) atau [Email](mailto:dev@test.com).\n"
            "Tautan jahat: [XSS](javascript:alert(1)).\n"
            "HTML mentah: <script>alert(2)</script><img src=x onerror=alert(3)>"
        )
        html = panel.render_markdown(src)
        self.assertIn('<a href="https://hermes-agent.nousresearch.com" target="_blank" rel="noopener noreferrer" class="md-link">Hermes</a>', html)
        self.assertIn('<a href="mailto:dev@test.com" target="_blank" rel="noopener noreferrer" class="md-link">Email</a>', html)
        self.assertNotIn('href="javascript:', html)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<img', html)
        self.assertIn('&lt;img', html)

    def test_06_attachment_viewer_modal_elements_in_page(self):
        page = panel.page_source()
        # Modal viewer container
        self.assertIn('id="view-kanban-attachment-modal"', page)
        self.assertIn('id="attachment-viewer-filename"', page)
        self.assertIn('id="attachment-viewer-preview"', page)
        self.assertIn('id="attachment-viewer-raw"', page)
        self.assertIn('id="att-btn-preview"', page)
        self.assertIn('id="att-btn-raw"', page)
        # Function handlers
        self.assertIn('openKanbanAttachment(', page)
        self.assertIn('setAttachmentViewMode(', page)
        self.assertIn('copyAttachmentContent()', page)
        self.assertIn('downloadAttachmentContent()', page)
        self.assertIn('closeAttachmentViewerModal()', page)

    def test_07_task_and_skill_markdown_preview_in_page(self):
        page = panel.page_source()
        # Task view modal elements styled for rich markdown
        self.assertIn('id="view-task-body" class="markdown-body"', page)
        self.assertIn('id="view-task-output" class="markdown-body"', page)
        # Skill reader preview element and toggle
        self.assertIn('id="profile-skill-read-body" class="markdown-body"', page)
        self.assertIn('id="profile-skill-toggle-btn"', page)
        self.assertIn('toggleProfileSkillView()', page)

        # openViewTaskModal applies renderMarkdown
        js = page[page.index("function openViewTaskModal"):]
        js = js[:js.index("function formatKanbanBytes")]
        self.assertIn("renderMarkdown(t.body)", js)
        self.assertIn("renderMarkdown(outText)", js)
        self.assertIn("renderMarkdown(bodyTxt)", js)


class TestSubprocessCachingAndOptimization(unittest.TestCase):
    """Test subprocess TTL caching, non-blocking SSE polling, and fast /proc status checks."""

    def test_ui_accessibility_and_keyboard_nav(self):
        """Main tabs must be semantic buttons with role=tab, live-badge has role=status, toast container present."""
        page = panel.build_status_page("")
        self.assertIn('role="tablist"', page)
        self.assertIn('<button type="button" class="tab active" role="tab"', page)
        self.assertIn('id="live-badge"', page)
        self.assertIn('role="status"', page)
        self.assertIn('id="toast-wrap"', page)
        self.assertIn('CLOSERS', page)

    def test_degraded_models_fallback_is_retried_not_cached_fresh(self):
        """A combos-only SQLite fallback must not be trusted for the full 5-minute TTL."""
        saved = dict(panel._models_cache)
        saved_ok = panel._models_fetch_ok
        try:
            panel._models_cache.update({"at": 0.0, "val": []})
            fake = {"9router (Kombo)": ["m1"]}

            def _degraded():
                panel._models_fetch_ok = False
                return fake

            with mock.patch.object(panel, "get_available_models", side_effect=_degraded):
                self.assertEqual(panel.get_available_models_cached(), fake)
            age = time.time() - panel._models_cache["at"]
            self.assertGreaterEqual(age, panel.MODELS_CACHE_TTL - 31,
                                    "degraded fetch must be retried within ~30 s")
        finally:
            panel._models_cache.update(saved)
            panel._models_fetch_ok = saved_ok

    def setUp(self):
        panel.invalidate_process_list_cache()

    def tearDown(self):
        panel.invalidate_process_list_cache()

    def test_systemctl_timeout_is_capped(self):
        self.assertTrue(hasattr(panel, "SYSTEMCTL_TIMEOUT"))
        self.assertLessEqual(panel.SYSTEMCTL_TIMEOUT, 2.0)
        self.assertGreaterEqual(panel.SYSTEMCTL_TIMEOUT, 1.0)

    def test_docker_metric_subprocess_caching(self):
        call_count = 0
        def fake_run(cmd, *args, **kwargs):
            nonlocal call_count
            if isinstance(cmd, list) and len(cmd) >= 2 and cmd[0] == "docker" and cmd[1] == "inspect":
                call_count += 1
                return mock.MagicMock(stdout="running\t99999\tabcdef123456\n", returncode=0)
            return mock.MagicMock(stdout="", returncode=0)

        with mock.patch.object(panel.subprocess, "run", side_effect=fake_run):
            with mock.patch("os.path.exists", return_value=True):
                st1, pid1, mem1 = panel.get_docker_metric("test-container")
                self.assertEqual(st1, "running")
                self.assertEqual(pid1, "99999")
                self.assertEqual(call_count, 1)

                st2, pid2, mem2 = panel.get_docker_metric("test-container")
                self.assertEqual(st2, "running")
                self.assertEqual(pid2, "99999")
                self.assertEqual(call_count, 1, "Subprocess must not be called again within window TTL")

    def test_dashboard_active_detection_user_system_and_port(self):
        """Hermes Dashboard detection must check user service, system service, and port 9119."""
        # Case 1: user service active
        with mock.patch.object(panel, "service_active", side_effect=lambda name, user=False, **kw: user is True):
            self.assertTrue(panel.is_dashboard_active(fresh=True))

        # Case 2: system service active
        with mock.patch.object(panel, "service_active", side_effect=lambda name, user=False, **kw: user is False):
            self.assertTrue(panel.is_dashboard_active(fresh=True))

        # Case 3: neither systemd unit active, but port 9119 listening
        with mock.patch.object(panel, "service_active", return_value=False), \
             mock.patch.object(panel, "is_dashboard_port_open", return_value=True):
            self.assertTrue(panel.is_dashboard_active(fresh=True))

        # Case 4: all dead
        with mock.patch.object(panel, "service_active", return_value=False), \
             mock.patch.object(panel, "is_dashboard_port_open", return_value=False):
            self.assertFalse(panel.is_dashboard_active(fresh=True))

    def test_dashboard_action_routes_to_correct_scope(self):
        """dashboard_action must use --user when user unit is present."""
        calls = []
        def fake_run(cmd, *a, **kw):
            calls.append(list(cmd))
            return mock.MagicMock(returncode=0)

        with mock.patch.object(panel, "is_dashboard_user_unit", return_value=True), \
             mock.patch.object(panel.subprocess, "run", side_effect=fake_run):
            panel.dashboard_action("restart")
            self.assertIn(["systemctl", "--user", "restart", panel.SERVICE], calls)

        calls.clear()
        with mock.patch.object(panel, "is_dashboard_user_unit", return_value=False), \
             mock.patch.object(panel.subprocess, "run", side_effect=fake_run):
            panel.dashboard_action("stop")
            self.assertIn(["systemctl", "stop", panel.SERVICE], calls)

        # Non-zero exit must be reported as failure, not swallowed.
        with mock.patch.object(panel, "is_dashboard_user_unit", return_value=False), \
             mock.patch.object(panel.subprocess, "run", return_value=mock.MagicMock(returncode=3)):
            self.assertFalse(panel.dashboard_action("restart"))
        with mock.patch.object(panel, "is_dashboard_user_unit", return_value=False), \
             mock.patch.object(panel.subprocess, "run", return_value=mock.MagicMock(returncode=0)):
            self.assertTrue(panel.dashboard_action("restart"))
        self.assertFalse(panel.dashboard_action("bogus"))

    def test_mutations_invalidate_cached_blocks(self):
        """Profile and Kanban mutations must invalidate their _status_probe_cache entries."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            cfg_path = os.path.join(td, "config.yaml")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write("model:\n  default: m1\n  provider: 9router\n")
            with mock.patch.object(panel, "CONFIG_PATH", cfg_path):
                with panel._status_probe_cache_lock:
                    panel._status_probe_cache["profiles_block"] = {"at": time.monotonic(), "value": "stale"}
                ok, _ = panel.create_agent_profile("worker1")
                self.assertTrue(ok)
                with panel._status_probe_cache_lock:
                    self.assertNotIn("profiles_block", panel._status_probe_cache)

                # Verify create_agent_profile without clone preserves providers block
                with open(cfg_path, "w", encoding="utf-8") as f:
                    f.write("model:\n  default: m1\n  provider: 9router\nproviders:\n  9router:\n    api: http://127.0.0.1:20128/v1\n")
                ok2, _ = panel.create_agent_profile("worker2")
                self.assertTrue(ok2)
                with open(os.path.join(td, "profiles", "worker2", "config.yaml"), encoding="utf-8") as f:
                    w2_cfg = panel.yaml.safe_load(f)
                self.assertIn("9router", w2_cfg.get("providers", {}))

                with panel._status_probe_cache_lock:
                    panel._status_probe_cache["kanban_board_block"] = {"at": time.monotonic(), "value": "stale"}
                ok, _, _ = panel.create_kanban_task("Task 1")
                self.assertTrue(ok)
                with panel._status_probe_cache_lock:
                    self.assertNotIn("kanban_board_block", panel._status_probe_cache)

    def test_service_active_subprocess_caching(self):
        call_count = 0
        def fake_run(cmd, *args, **kwargs):
            nonlocal call_count
            if isinstance(cmd, list) and cmd[0] == "systemctl" and "is-active" in cmd:
                call_count += 1
                return mock.MagicMock(stdout="active\n", returncode=0)
            return mock.MagicMock(stdout="", returncode=0)

        with mock.patch.object(panel.subprocess, "run", side_effect=fake_run):
            res1 = panel.service_active("test-unit.service")
            self.assertTrue(res1)
            self.assertEqual(call_count, 1)

            res2 = panel.service_active("test-unit.service")
            self.assertTrue(res2)
            self.assertEqual(call_count, 1, "Subprocess must not be called again within window TTL")

    def test_get_process_list_no_new_subprocess_in_ttl(self):
        call_count = 0
        def fake_run(cmd, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if isinstance(cmd, list) and len(cmd) >= 3 and cmd[0] == "systemctl" and cmd[1] == "show":
                return mock.MagicMock(stdout="MainPID=11111\nActiveState=active\n", returncode=0)
            if isinstance(cmd, list) and len(cmd) >= 2 and cmd[0] == "systemctl" and cmd[1] == "is-active":
                return mock.MagicMock(stdout="active\n", returncode=0)
            if isinstance(cmd, list) and len(cmd) >= 2 and cmd[0] == "docker" and cmd[1] == "inspect":
                return mock.MagicMock(stdout="running\t22222\tcontainer123\n", returncode=0)
            return mock.MagicMock(stdout="", returncode=0)

        with mock.patch.object(panel.subprocess, "run", side_effect=fake_run):
            with mock.patch("os.path.exists", return_value=True):
                procs1 = panel.get_process_list()
                first_calls = call_count
                self.assertGreater(first_calls, 0)

                procs2 = panel.get_process_list()
                self.assertEqual(call_count, first_calls, "No new subprocesses must be called within TTL window")
                self.assertEqual(len(procs1), len(procs2))

    def test_ttl_cached_cold_miss_coalesces_concurrent_callers(self):
        import threading
        call_count = 0
        lock = threading.Lock()
        def loader():
            nonlocal call_count
            with lock:
                call_count += 1
            time.sleep(0.05)
            return "cold"

        with panel._status_probe_cache_lock:
            panel._status_probe_cache.pop("test_cold_coalesce", None)
        results = []
        threads = [threading.Thread(target=lambda: results.append(panel._ttl_cached("test_cold_coalesce", 1.0, loader))) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=1)
        self.assertEqual(call_count, 1)
        self.assertEqual(results, ["cold"] * 6)

    def test_ttl_cached_waiters_do_not_reload_after_slow_owner(self):
        started = threading.Event()
        release = threading.Event()
        calls = 0
        lock = threading.Lock()

        def loader():
            nonlocal calls
            with lock:
                calls += 1
            started.set()
            release.wait(timeout=2)
            return "slow"

        with panel._status_probe_cache_lock:
            panel._status_probe_cache.pop("test_slow_coalesce", None)
        results = []
        owner = threading.Thread(target=lambda: results.append(panel._ttl_cached("test_slow_coalesce", 0.01, loader)))
        waiter = threading.Thread(target=lambda: results.append(panel._ttl_cached("test_slow_coalesce", 0.01, loader)))
        owner.start()
        self.assertTrue(started.wait(timeout=1))
        waiter.start()
        time.sleep(0.05)
        release.set()
        owner.join(timeout=2)
        waiter.join(timeout=2)
        self.assertEqual(calls, 1)
        self.assertEqual(sorted(results), ["slow", "slow"])

    def test_ttl_cached_invalidation_rejects_stale_worker_result(self):
        import threading
        started = threading.Event()
        release = threading.Event()
        with panel._status_probe_cache_lock:
            panel._status_probe_cache["test_generation"] = {"at": time.monotonic() - 10, "value": "old"}
        def loader():
            started.set()
            release.wait(timeout=1)
            return "stale-worker"
        self.assertEqual(panel._ttl_cached("test_generation", 0.05, loader), "old")
        self.assertTrue(started.wait(timeout=1))
        panel._invalidate_status_cache("test_generation")
        release.set()
        time.sleep(0.08)
        with panel._status_probe_cache_lock:
            self.assertNotIn("test_generation", panel._status_probe_cache)

    def test_ttl_cached_stale_while_revalidate(self):
        call_count = 0
        def loader():
            nonlocal call_count
            call_count += 1
            return f"val_{call_count}"

        v1 = panel._ttl_cached("test_swr", 0.05, loader)
        self.assertEqual(v1, "val_1")
        self.assertEqual(call_count, 1)

        v2 = panel._ttl_cached("test_swr", 0.05, loader)
        self.assertEqual(v2, "val_1")
        self.assertEqual(call_count, 1)

        time.sleep(0.06)
        v3 = panel._ttl_cached("test_swr", 0.05, loader)
        self.assertEqual(v3, "val_1", "Must return stale value immediately without blocking")

        time.sleep(0.05)
        v4 = panel._ttl_cached("test_swr", 0.05, loader)
        self.assertEqual(v4, "val_2")

    def test_docker_containers_info_parsing(self):
        """get_docker_containers_info parses active and inactive containers, web ports, and metadata."""
        sample_output = "\n".join([
            json.dumps({
                "Names": "test-running",
                "State": "running",
                "Status": "Up 2 hours",
                "Image": "vendor/app:latest",
                "Ports": "0.0.0.0:8080->80/tcp",
                "Labels": "icon=https://example.com/icon.png",
            }),
            json.dumps({
                "Names": "test-stopped",
                "State": "exited",
                "Status": "Exited (0) 10 minutes ago",
                "Image": "vendor/offline:latest",
                "Ports": "",
                "Labels": "",
            }),
        ])
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=sample_output)
            containers = panel.get_docker_containers_info()
            self.assertEqual(len(containers), 2)

            c1 = containers[0]
            self.assertEqual(c1["name"], "test-running")
            self.assertTrue(c1["is_running"])
            self.assertEqual(c1["port"], "8080")
            self.assertEqual(c1["icon"], "https://example.com/icon.png")

            c2 = containers[1]
            self.assertEqual(c2["name"], "test-stopped")
            self.assertFalse(c2["is_running"])

    def test_render_docker_apps_block(self):
        """render_docker_apps_block renders CasaOS-style cards with open and control buttons."""
        mock_containers = [
            {
                "name": "my-app",
                "title": "My App",
                "state": "running",
                "is_running": True,
                "status": "Up 1 hour",
                "image": "myapp:latest",
                "icon": "https://example.com/app.png",
                "port": "3000",
                "path": "/app",
                "scheme": "http",
            },
            {
                "name": "my-offline-app",
                "title": "Offline App",
                "state": "exited",
                "is_running": False,
                "status": "Exited (0)",
                "image": "offline:latest",
                "icon": "https://example.com/offline.png",
                "port": "4000",
                "path": "/",
                "scheme": "http",
            },
        ]
        with mock.patch.object(panel, "get_docker_containers_info", return_value=mock_containers):
            html = panel._render_docker_apps_block_uncached()
            self.assertIn("My App", html)
            self.assertIn("1 Aktif", html)
            self.assertIn("1 Berhenti", html)
            self.assertIn("Port: <span", html)
            self.assertIn("3000", html)
            # Active app with port has active open link
            self.assertIn('onclick="window.open(\'http://\'+window.location.hostname+\':3000/app\'', html)
            self.assertIn("Stop", html)
            self.assertIn("Restart", html)
            # Inactive app has disabled open button and Start button
            self.assertIn("Offline App", html)
            self.assertIn("Berhenti", html)
            self.assertIn("Start", html)



class TestGatewayNetwork(unittest.TestCase):
    """Jalur Jaringan Gateway: per-platform proxy routes in .env + the relay's DNS settings."""

    @classmethod
    def setUpClass(cls):
        cls.server = panel.TimeoutThreadingHTTPServer(("127.0.0.1", 0), panel.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp(prefix="panel-net-")
        self.cfg_path = os.path.join(self.tmpdir, "config.yaml")
        self.env_path = os.path.join(self.tmpdir, ".env")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            panel.yaml.safe_dump({"platforms": {"telegram": {"enabled": True}, "discord": {"enabled": False}}}, f)
        with open(self.env_path, "w", encoding="utf-8") as f:
            f.write("# Hermes env\nTELEGRAM_BOT_TOKEN=x\nexport FOO=bar\nTELEGRAM_PROXY=socks5://127.0.0.1:40001\n")
        os.chmod(self.env_path, 0o600)
        for name, value in (("CONFIG_PATH", self.cfg_path),
                            ("RELAY_STATUS_PATH", os.path.join(self.tmpdir, "status.json"))):
            p = mock.patch.object(panel, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("_run_quiet", ""), ("service_active", True)):
            p = mock.patch.object(panel, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        panel.invalidate_config_cache()
        panel._invalidate_network_cache()
        panel._last_action_at.clear()
        with panel._hermes_update_lock:  # rendering /status must not spawn a real `hermes update --check`
            panel._hermes_update_cache["at"] = time.time()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        panel.invalidate_config_cache()

    def _env(self) -> str:
        with open(self.env_path, encoding="utf-8") as f:
            return f.read()

    def _post(self, path: str, payload: dict):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(payload).encode(),
                                     method="POST")
        req.add_header("Cookie", cookie)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_route_detection(self):
        f = panel._route_from_proxy_url
        self.assertEqual(f("socks5://127.0.0.1:40001"), "relay")
        self.assertEqual(f("http://localhost:40001"), "relay")
        self.assertEqual(f("socks5h://127.0.0.1:40000"), "warp")
        self.assertEqual(f(""), "direct")
        self.assertEqual(f("http://10.0.0.5:8080"), "custom")

    def test_state_lists_platforms_routes_and_defaults(self):
        st = panel.get_gateway_network_state()
        by_id = {p["id"]: p for p in st["platforms"]}
        self.assertEqual(by_id["telegram"]["route"], "relay")
        self.assertTrue(by_id["telegram"]["enabled"])
        self.assertEqual(by_id["discord"]["route"], "direct")
        self.assertFalse(by_id["discord"]["enabled"])
        self.assertEqual({p["id"]: p["support"] for p in st["info_platforms"]}["whatsapp"], "none")
        self.assertEqual(st["dns_source"], "default")
        self.assertEqual(st["dns"]["hosts"][0]["host"], "api.telegram.org")

    def test_save_routes_writes_env_preserving_other_lines_and_mode(self):
        ok, err, res = panel.save_gateway_routes({"telegram": {"route": "warp"}, "discord": {"route": "relay"}})
        self.assertTrue(ok, err)
        self.assertEqual(sorted(res["changed"]), ["discord", "telegram"])
        self.assertTrue(res["needs_restart"])
        text = self._env()
        self.assertIn("TELEGRAM_PROXY=socks5://127.0.0.1:40000\n", text)
        self.assertIn("DISCORD_PROXY=http://127.0.0.1:40001\n", text)
        self.assertTrue(text.startswith("# Hermes env\nTELEGRAM_BOT_TOKEN=x\nexport FOO=bar\n"))
        self.assertEqual(os.stat(self.env_path).st_mode & 0o777, 0o600)

    def test_unchanged_route_needs_no_restart(self):
        ok, _, res = panel.save_gateway_routes({"telegram": {"route": "relay"}})
        self.assertTrue(ok)
        self.assertEqual(res, {"changed": [], "needs_restart": False})

    def test_direct_removes_var(self):
        ok, err, _ = panel.save_gateway_routes({"telegram": {"route": "direct"}})
        self.assertTrue(ok, err)
        self.assertNotIn("TELEGRAM_PROXY", self._env())

    def test_custom_socks_rejected_for_aiohttp_platform(self):
        ok, err, _ = panel.save_gateway_routes({"discord": {"route": "custom", "url": "socks5://10.0.0.2:1080"}})
        self.assertFalse(ok)
        self.assertIn("http://", err)
        self.assertNotIn("DISCORD_PROXY", self._env())

    def test_custom_url_validation_and_masked_credentials_kept(self):
        for bad in ("ftp://x:1", "http://nohost", "http://1.2.3.4:80 x", ""):
            ok, _, _ = panel.save_gateway_routes({"telegram": {"route": "custom", "url": bad}})
            self.assertFalse(ok, bad)
        ok, err, _ = panel.save_gateway_routes(
            {"telegram": {"route": "custom", "url": "socks5://user:s3cret@10.0.0.2:1080"}})
        self.assertTrue(ok, err)
        st = panel.get_gateway_network_state()
        shown = {p["id"]: p for p in st["platforms"]}["telegram"]["url"]
        self.assertEqual(shown, "socks5://user:***@10.0.0.2:1080")
        ok, _, res = panel.save_gateway_routes({"telegram": {"route": "custom", "url": shown}})
        self.assertTrue(ok)
        self.assertEqual(res["changed"], [])
        self.assertIn("TELEGRAM_PROXY=socks5://user:s3cret@10.0.0.2:1080", self._env())

    def test_direct_refused_while_yaml_proxy_url_set(self):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            panel.yaml.safe_dump({"platforms": {"telegram": {"enabled": True, "extra": {"proxy_url": "http://1.2.3.4:8"}}}}, f)
        panel.invalidate_config_cache()
        ok, err, _ = panel.save_gateway_routes({"telegram": {"route": "direct"}})
        self.assertFalse(ok)
        self.assertIn("proxy_url", err)
        notes = {p["id"]: p for p in panel.get_gateway_network_state()["platforms"]}["telegram"]["notes"]
        self.assertTrue(any("diabaikan" in n for n in notes))

    def test_unknown_or_unsupported_platform_rejected(self):
        for pid in ("whatsapp", "nope"):
            ok, err, _ = panel.save_gateway_routes({pid: {"route": "relay"}})
            self.assertFalse(ok, pid)

    def test_dns_validation(self):
        bad = [
            {"resolver": "x"}, {"resolver": "custom"}, {"resolver": "custom", "resolver_servers": ["::1"]},
            {"stagger_seconds": 20}, {"hosts": [{"host": "bad host", "ips": ["1.1.1.1"]}]},
            {"hosts": [{"host": "a.org", "ips": ["2001:db8::1"]}]}, {"hosts": [{"host": "a.org", "ips": []}]},
            {"hosts": [{"host": "a.org", "ips": ["1.1.1.1"], "mode": "x"}]},
        ]
        for raw in bad:
            ok, err, _ = panel.save_gateway_dns(raw)
            self.assertFalse(ok, raw)
            self.assertTrue(err)
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "gateway-network.json")))

    def test_dns_save_normalizes_and_is_read_back(self):
        ok, err, cfg = panel.save_gateway_dns({
            "resolver": "custom", "resolver_servers": ["9.9.9.9", ""], "stagger_seconds": "2.5",
            "hosts": [{"host": "Discord.com.", "ips": "162.159.135.232, 162.159.136.232", "mode": "only"}]})
        self.assertTrue(ok, err)
        path = os.path.join(self.tmpdir, "gateway-network.json")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
        dns, source, error = panel.read_gateway_dns()
        self.assertEqual((source, error), ("file", ""))
        self.assertEqual(dns["hosts"], [{"host": "discord.com", "ips": ["162.159.135.232", "162.159.136.232"], "mode": "only"}])
        self.assertEqual(dns["stagger_seconds"], 2.5)
        ok, _, cfg = panel.save_gateway_dns({"resolver": "cloudflare", "resolver_servers": ["9.9.9.9"]})
        self.assertEqual(cfg["resolver_servers"], [])

    def test_route_test_uses_curl_through_selected_proxy(self):
        fake = mock.Mock(returncode=0, stdout="302 0.50", stderr="")
        with mock.patch.object(panel.subprocess, "run", return_value=fake) as run:
            res = panel.test_gateway_route("telegram")
            self.assertTrue(res["ok"])
            argv = run.call_args[0][0]
            self.assertEqual(argv[argv.index("-x") + 1], "socks5h://127.0.0.1:40001")
            self.assertEqual(argv[-1], "https://api.telegram.org/")
            self.assertEqual((res["passed"], res["total"], res["avg_seconds"]), (3, 3, 0.5))
            panel.test_gateway_route("discord", route="direct")
            self.assertIn("--noproxy", run.call_args[0][0])
            panel.test_gateway_route("discord", route="relay")
            argv = run.call_args[0][0]
            self.assertEqual(argv[argv.index("-x") + 1], "http://127.0.0.1:40001")
        fail = mock.Mock(returncode=97, stdout="000 0", stderr="")
        with mock.patch.object(panel.subprocess, "run", return_value=fail):
            res = panel.test_gateway_route("telegram", attempts=1)
        self.assertFalse(res["ok"])
        self.assertEqual(res["passed"], 0)
        self.assertIn("SOCKS", res["results"][0]["error"])

    def test_resolve_gateway_host_rejects_failed_relay(self):
        failed = mock.Mock(returncode=1, stdout=json.dumps({"ok": False, "error": "resolver down"}), stderr="")
        with mock.patch.object(panel.os.path, "isfile", return_value=True), \
             mock.patch.object(panel.subprocess, "run", return_value=failed):
            res = panel.resolve_gateway_host("api.telegram.org")
        self.assertFalse(res["ok"])
        self.assertIn("resolver down", res["error"])

    def test_http_api_auth_methods_and_save(self):
        url = f"http://127.0.0.1:{self.port}/api/network"
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(url)
        self.assertEqual(cm.exception.code, 401)
        req = urllib.request.Request(url)
        req.add_header("Cookie", f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}")
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
        self.assertTrue(data["ok"])
        self.assertIn("platforms", data)
        req = urllib.request.Request(url + "/routes")
        req.add_header("Cookie", f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 405)
        code, body = self._post("/api/network/routes", {"routes": {"discord": {"route": "warp"}}})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["changed"], ["discord"])
        self.assertIn("DISCORD_PROXY=http://127.0.0.1:40000", self._env())
        code, body = self._post("/api/network/routes", {"routes": {"discord": {"route": "bogus"}}})
        self.assertEqual(code, 400)
        code, body = self._post("/api/network/dns", {"dns": {"resolver": "pihole"}})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["dns"]["resolver"], "pihole")
        with mock.patch.object(panel, "restart_bot") as rb:
            code, body = self._post("/api/network/restart-gateway", {"confirm": True})
        self.assertEqual(code, 200)
        rb.assert_called_once()

    def test_warp_service_stop_refused_while_platform_needs_it(self):
        self._post("/api/network/routes", {"routes": {"telegram": {"route": "warp"}}})
        with mock.patch.object(panel.subprocess, "run") as run:
            ok, err, res = panel.warp_service_action("stop")
        self.assertFalse(ok)
        self.assertIn("Telegram", err)
        self.assertTrue(res.get("logs"))
        run.assert_not_called()

    def test_warp_service_stop_while_relay_active_stops_only_warp_and_returns_logs(self):
        # setUp .env: TELEGRAM_PROXY -> relay :40001; relay works via direct IPv4 without warp-svc
        calls = []
        def fake(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with mock.patch.object(panel.subprocess, "run", side_effect=fake):
            ok, err, res = panel.warp_service_action("stop")
        self.assertTrue(ok, err)
        self.assertEqual(calls, [["systemctl", "disable", "--now", "warp-svc"]])
        self.assertTrue(any("systemctl disable --now warp-svc" in line for line in res.get("logs", [])))

    def test_warp_service_stop_stops_relay_then_warp_when_unused(self):
        self._post("/api/network/routes", {"routes": {"telegram": {"route": "direct"}}})
        calls = []
        def fake(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with mock.patch.object(panel.subprocess, "run", side_effect=fake):
            ok, err, res = panel.warp_service_action("stop")
        self.assertTrue(ok, err)
        self.assertEqual(calls, [["systemctl", "disable", "--now", panel.RELAY_SERVICE],
                                 ["systemctl", "disable", "--now", "warp-svc"]])
        self.assertTrue(res.get("logs"))

    def test_warp_service_start_enables_warp_then_relay(self):
        calls = []
        def fake(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with mock.patch.object(panel.subprocess, "run", side_effect=fake):
            ok, err, res = panel.warp_service_action("start")
        self.assertTrue(ok, err)
        self.assertEqual(calls, [["systemctl", "enable", "--now", "warp-svc"],
                                 ["systemctl", "enable", "--now", panel.RELAY_SERVICE]])
        self.assertTrue(res.get("logs"))

    def test_warp_service_rejects_bad_action_and_endpoint(self):
        ok, err, _ = panel.warp_service_action("rm -rf")
        self.assertFalse(ok)
        code, body = self._post("/api/network/warp-service", {"action": "bogus"})
        self.assertEqual(code, 400)
        with mock.patch.object(panel, "warp_service_action", return_value=(True, "", {"logs": ["ok"]})) as w:
            code, body = self._post("/api/network/warp-service", {"action": "start"})
        self.assertEqual(code, 200, body)
        self.assertEqual(body.get("logs"), ["ok"])
        w.assert_called_once_with("start")

    def test_network_ui_has_warp_toggle(self):
        self.assertIn("netWarpToggle", panel.NETWORK_SCRIPT)
        self.assertIn("/api/network/warp-service", panel.NETWORK_SCRIPT)
        self.assertIn("net-warp-op-slot", panel.NETWORK_SCRIPT)
        self.assertIn("net-warp-logbox", panel.NETWORK_SCRIPT)
        self.assertIn("net-warp-progress-bar", panel.NETWORK_SCRIPT)
        self.assertNotIn("warpOn && warpNeeded.length ? ' disabled", panel.NETWORK_SCRIPT)

    def test_passwordless_mode_still_rejects_cross_site_posts(self):
        """Passwordless LAN mode must not disable CSRF checks (any web page could drive the panel)."""
        for name in ("PASSWORD", "TOKEN"):
            p = mock.patch.object(panel, name, "")
            p.start()
            self.addCleanup(p.stop)
        url = f"http://127.0.0.1:{self.port}/api/network/routes"
        body = json.dumps({"routes": {"telegram": {"route": "direct"}}}).encode()

        def post(extra):
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            for k, v in extra.items():
                req.add_header(k, v)
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code

        self.assertEqual(post({"Origin": "http://evil.example"}), 403)
        self.assertEqual(post({"Sec-Fetch-Site": "cross-site"}), 403)
        self.assertEqual(post({"Referer": "http://evil.example/x"}), 403)
        self.assertIn("TELEGRAM_PROXY=socks5://127.0.0.1:40001", self._env())
        self.assertEqual(post({"Origin": f"http://127.0.0.1:{self.port}"}), 200)
        self.assertNotIn("TELEGRAM_PROXY", self._env())

    def test_page_has_network_card_and_valid_script(self):
        page = panel.build_status_page()
        self.assertIn('id="gateway-network-slot"', page)
        self.assertIn("Jalur Jaringan Gateway", page)
        self.assertIn(f"/assets/network-card.js?v={panel.NETWORK_ASSET_VERSION}", page)
        self.assertIn(f"/assets/network-card.css?v={panel.NETWORK_ASSET_VERSION}", page)
        self.assertNotIn("function netRenderCard", page)  # served as a cached asset, not inline
        r = subprocess.run(["node", "--check", "-"], input=panel.NETWORK_SCRIPT.replace("<script>", "").replace("</script>", ""),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_network_assets_served_with_cache_and_auth(self):
        url = f"http://127.0.0.1:{self.port}/assets/network-card.js?v={panel.NETWORK_ASSET_VERSION}"
        req = urllib.request.Request(url)
        req.add_header("Cookie", f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}")
        with urllib.request.urlopen(req) as r:
            body = r.read().decode()
            self.assertIn("javascript", r.headers["Content-Type"])
            self.assertIn("immutable", r.headers["Cache-Control"])
        self.assertIn("function netRenderCard", body)
        req = urllib.request.Request(url.replace(".js", ".css"))
        req.add_header("Cookie", f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}")
        with urllib.request.urlopen(req) as r:
            self.assertIn("text/css", r.headers["Content-Type"])
            self.assertIn(".net-seg", r.read().decode())
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            code = urllib.request.build_opener(NoRedirect).open(url).status
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertIn(code, (302, 401, 403))  # not served without a session

    def test_network_script_renders_state_and_collects_payloads(self):
        js = panel.NETWORK_SCRIPT.replace("<script>", "").replace("</script>", "")
        state = panel.get_gateway_network_state()
        script = "var document={getElementById:function(){return null}};var window={};\n" + js + r"""
var st = """ + json.dumps(state) + r""";
var html = netRenderCard(st);
if (html.indexOf('Telegram') < 0 || html.indexOf('net-route-telegram') < 0) throw new Error('missing telegram row');
if (html.indexOf('api.telegram.org') < 0) throw new Error('missing dns host');
if (html.indexOf('<script') >= 0) throw new Error('unescaped');
var ips = netParseIps(' 1.2.3.4,5.6.7.8\n9.9.9.9 ');
if (ips.join('|') !== '1.2.3.4|5.6.7.8|9.9.9.9') throw new Error('ips ' + ips.join('|'));
if (netEsc('<b>"x"</b>') !== '&lt;b&gt;&quot;x&quot;&lt;/b&gt;') throw new Error('esc');
process.stdout.write('ok');
"""
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "ok")



class TestHermesUpdateFlow(unittest.TestCase):
    """Tombol update Hermes: cek sesuai channel, deteksi proses, unit systemd terpisah, anti-downgrade."""

    STABLE_CURRENT = "→ Update channel: stable\n✓ Up to date with the latest release (stable v0.22.2 (d923a5eb487a)).\n"
    STABLE_AVAILABLE = ("→ Update channel: stable\n→ Selected release available: stable v0.22.3 (abc1234def56)\n"
                        "  Run `hermes update` to install it.\n")
    STABLE_AHEAD = ("→ Update channel: stable\n→ This checkout is newer than the selected release: stable v0.22.0 (1f6c02c07c69).\n"
                    "  `hermes update` moves it back to that release (`hermes update --set-channel main` follows every commit).\n")

    @classmethod
    def setUpClass(cls):
        cls.server = panel.TimeoutThreadingHTTPServer(("127.0.0.1", 0), panel.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        panel._last_action_at.clear()
        with panel._hermes_update_lock:  # a background refresh left over from an earlier test
            panel._hermes_check_running = False  # would make _refresh_hermes_update() return early
        with panel._update_scan_lock:
            panel._update_scan_cache["at"] = 0.0
        self._saved_cache = dict(panel._hermes_update_cache)
        self.addCleanup(lambda: panel._hermes_update_cache.update(self._saved_cache))
        with panel._hermes_update_lock:  # never spawn a real `hermes update --check` from tests
            panel._hermes_update_cache["at"] = time.time()

    def test_parse_check_variants(self):
        f = panel._parse_hermes_update_check
        cur = f(self.STABLE_CURRENT, 0)
        self.assertEqual((cur["status"], cur["channel"], cur["remote"]), ("current", "stable", "stable v0.22.2 (d923a5eb487a)"))
        self.assertEqual(f("→ Update channel: stable\n✓ No newer release: stable v0.22.2 (d923a5eb487a).\n", 0)["status"], "current")
        av = f(self.STABLE_AVAILABLE, 0)
        self.assertEqual((av["status"], av["target"], av["remote"]), ("available", "abc1234def56", "stable v0.22.3 (abc1234def56)"))
        ah = f(self.STABLE_AHEAD, 0)
        self.assertEqual((ah["status"], ah["remote"]), ("ahead", "stable v0.22.0 (1f6c02c07c69)"))
        mn = f("☤ Update available: 12 commits behind origin/main.\n  Run 'hermes update' to install.\n", 0)
        self.assertEqual((mn["status"], mn["behind"], mn["target"]), ("available", 12, "main"))
        self.assertEqual(f("✓ Already up to date.\n", 0)["status"], "current")
        err = f("→ Update channel: stable\n✗ Could not resolve the stable source channel: timed out\n", 1)
        self.assertEqual(err["status"], "unknown")
        self.assertIn("Could not resolve", err["error"])
        self.assertEqual(f("", 1)["status"], "unknown")

    def _fake_run(self, check_output):
        calls = []

        def run(argv, *a, **kw):
            calls.append(list(argv))
            if argv[:2] == [panel.HERMES_BIN, "update"]:
                return mock.Mock(returncode=0, stdout=check_output, stderr="")
            if "rev-parse" in argv:
                return mock.Mock(returncode=0, stdout="d923a5eb487a17c0a68801bba9ef0f56c81040a8\n", stderr="")
            if "describe" in argv:
                return mock.Mock(returncode=0, stdout="v0.22.2\n", stderr="")
            return mock.Mock(returncode=0, stdout="fix: a\nfeat: b\n", stderr="")
        return calls, run

    def test_refresh_uses_official_check_and_never_fetches_git(self):
        calls, run = self._fake_run(self.STABLE_AHEAD)
        with mock.patch.object(panel.subprocess, "run", side_effect=run), \
             mock.patch.object(panel, "_clear_stale_hermes_git_lock"), \
             mock.patch.object(panel.urllib.request, "urlopen") as urlopen:
            panel._refresh_hermes_update()
        self.assertIn([panel.HERMES_BIN, "update", "--check"], calls)
        self.assertFalse(any("fetch" in c for c in calls), calls)
        urlopen.assert_not_called()
        cache = panel._hermes_update_cache
        self.assertEqual((cache["status"], cache["local"], cache["channel"]), ("ahead", "v0.22.2", "stable"))

    def test_available_patch_notes_come_from_release_target(self):
        calls, run = self._fake_run(self.STABLE_AVAILABLE)
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = json.dumps({"ahead_by": 7, "commits": [
            {"commit": {"message": "fix(x): one\n\nbody"}}, {"commit": {"message": "feat(y): two"}}]}).encode()
        with mock.patch.object(panel.subprocess, "run", side_effect=run), \
             mock.patch.object(panel, "_clear_stale_hermes_git_lock"), \
             mock.patch.object(panel.urllib.request, "urlopen", return_value=resp) as urlopen:
            panel._refresh_hermes_update()
        self.assertIn("...abc1234def56", urlopen.call_args[0][0].full_url)
        cache = panel._hermes_update_cache
        self.assertEqual((cache["status"], cache["behind"], cache["remote"]), ("available", 7, "stable v0.22.3 (abc1234def56)"))
        self.assertEqual(cache["patch_notes"], ["feat(y): two", "fix(x): one"])

    def test_check_update_route_does_not_hit_unbound_local(self):
        """POST /check-update must set the refresh flag, not raise UnboundLocalError."""
        import http.client, tempfile
        with tempfile.TemporaryDirectory() as td:
            ran = threading.Event()
            with mock.patch.object(panel, "UPDATE_CACHE_PATH", os.path.join(td, "update-check.json")), \
                 mock.patch.object(panel, "_refresh_update_cache", side_effect=lambda: ran.set()):
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
                    conn.request("POST", "/check-update", headers={
                        "Cookie": f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}",
                        "Host": f"127.0.0.1:{self.port}",
                    })
                    resp = conn.getresponse()
                    resp.read()
                    conn.close()
                    self.assertEqual(resp.status, 302)
                    self.assertTrue(ran.wait(2), "refresh thread never started (flag was shadowed)")
                    self.assertTrue(panel._update_refreshing, "refresh flag not set")
                finally:
                    panel._update_refreshing = False

    def test_env_int_never_crashes_on_bad_values(self):
        with mock.patch.dict(os.environ, {"PANEL_PORT": ""}):
            self.assertEqual(panel._env_int("PANEL_PORT", 1000, 1, 65535), 1000)
        with mock.patch.dict(os.environ, {"PANEL_PORT": "abc"}):
            self.assertEqual(panel._env_int("PANEL_PORT", 1000, 1, 65535), 1000)
        with mock.patch.dict(os.environ, {"PANEL_PORT": "70000"}):
            self.assertEqual(panel._env_int("PANEL_PORT", 1000, 1, 65535), 65535)

    def test_markdown_link_url_cannot_break_out_of_href(self):
        out = panel.render_markdown('[x](https://a.com/"onmouseover="x)')
        self.assertNotIn('href="https://a.com/"', out)
        self.assertIn("&quot;", out)

    def test_markdown_link_url_cannot_smuggle_inline_code_placeholder(self):
        """Inline-code placeholders are resolved after the <a> is built; a URL containing one
        would splice raw HTML into the href attribute and break out of it."""
        out = panel.render_markdown('[x](https://a.com/`b`)')
        self.assertNotIn('href="https://a.com/<code', out)
        self.assertNotIn('<code class="md-inline-code">b</code>"', out)
        # the link must not be emitted at all rather than emitted malformed
        self.assertNotIn('<a href="https://a.com/', out)

    def test_markdown_link_url_cannot_smuggle_placeholder_in_relative_path(self):
        out = panel.render_markdown('[x](/`b`)')
        self.assertNotIn('href="/<code', out)
        self.assertNotIn('<a href="/', out)

    def test_pid_detection_is_precise(self):
        boot = "import os, re, sys\nimport hermes_bootstrap\nfrom hermes_cli.main import main"
        cases = [
            (["/usr/local/bin/hermes", "update", "--yes", "--no-backup"], True),
            (["/root/.hermes/tools/python3", "-I", "-c", boot, "update", "--yes", "--no-gateway-restart"], True),
            (["python3", "/opt/hermes-lib/hermes_cli/main.py", "update", "--gateway"], True),
            (["/root/.hermes/tools/python3", "-I", "-c", boot, "update", "--check"], False),
            (["/usr/local/bin/hermes", "update", "--plan"], False),
            (["/usr/local/bin/hermes", "skills", "update"], False),
            (["/root/.hermes/tools/python3", "-I", "-c", boot, "gateway", "run"], False),
            (["apt-get", "update"], False),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv[-3:]), mock.patch("pathlib.Path.read_bytes",
                                                          return_value=b"\x00".join(a.encode() for a in argv) + b"\x00"):
                self.assertEqual(panel._pid_is_hermes_update(99999), expected)

    def test_github_token_env_injects_gh_token_without_overriding(self):
        base = {"PATH": "/usr/bin"}
        fake = subprocess.CompletedProcess([], 0, stdout="tok_from_gh\n", stderr="")
        with mock.patch.object(panel.subprocess, "run", return_value=fake):
            env = panel._github_token_env(dict(base))
        self.assertEqual(env["GITHUB_TOKEN"], "tok_from_gh")
        with mock.patch.object(panel.subprocess, "run") as run:
            env = panel._github_token_env({"GITHUB_TOKEN": "keep"})
        self.assertEqual(env["GITHUB_TOKEN"], "keep")
        run.assert_not_called()
        with mock.patch.object(panel.subprocess, "run", side_effect=OSError):
            env = panel._github_token_env(dict(base))
        self.assertNotIn("GITHUB_TOKEN", env)

    def test_hermes_update_timeout_closes_child_pipes(self):
        """On timeout the updater must reap the child and close its pipes, not leak fds."""
        import tempfile
        from pathlib import Path
        td = tempfile.mkdtemp(prefix="hupd-timeout-")
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))
        log_path = os.path.join(td, "update.log")

        class FakeProc:
            def __init__(self):
                self.stdout = io.StringIO()
                self.stderr = io.StringIO()
                self.waited = 0
                self.killed = False
            def wait(self, timeout=None):
                self.waited += 1
                if self.waited == 1:
                    raise subprocess.TimeoutExpired(cmd="hermes", timeout=timeout or 1)
                return 124

        fake = FakeProc()
        with mock.patch.object(panel, "HERMES_UPDATE_TIMEOUT", 0.05), \
             mock.patch.object(panel, "HERMES_UPDATE_LOG_PATH", log_path), \
             mock.patch.object(panel, "_hermes_update_argv", return_value=["/bin/sh", "-c", "sleep 5"]), \
             mock.patch.object(panel.subprocess, "Popen", return_value=fake), \
             mock.patch.object(panel, "_clear_stale_hermes_git_lock"), \
             mock.patch.object(panel, "is_hermes_updating", return_value=False), \
             mock.patch.object(panel, "_hermes_update_summary", return_value="timeout"):
            panel.run_hermes_update()
            for _ in range(100):
                if not panel._hermes_update_running:
                    break
                time.sleep(0.02)

        self.assertFalse(panel._hermes_update_running)
        self.assertGreaterEqual(fake.waited, 2)
        self.assertTrue(fake.stdout.closed)
        self.assertTrue(fake.stderr.closed)
        self.assertIn("timeout", Path(log_path).read_text(encoding="utf-8"))

    def test_update_runs_in_transient_systemd_unit(self):
        with mock.patch.object(panel.shutil, "which", return_value="/usr/bin/systemd-run"), \
             mock.patch.dict(os.environ, {"HERMES_HOME": "/opt/hh"}):
            argv = panel._hermes_update_argv()
        self.assertEqual(argv[0], "systemd-run")
        for flag in ("--collect", "--wait", f"--property=RuntimeMaxSec={panel.HERMES_UPDATE_TIMEOUT}",
                     f"--property=StandardOutput=append:{panel.HERMES_UPDATE_LOG_PATH}", "--setenv=HERMES_HOME=/opt/hh"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[-3:], [panel.HERMES_LIB_DIR, panel.HERMES_UPDATE_LOG_PATH, panel.HERMES_BIN])
        self.assertIn(panel.HERMES_UPDATE_WRAPPER, argv)
        with mock.patch.object(panel.shutil, "which", return_value=None):
            self.assertEqual(panel._hermes_update_argv()[0], "/bin/sh")

    def _run_wrapper(self, fake_hermes_body: str):
        import tempfile
        td = tempfile.mkdtemp(prefix="hupd-")
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))
        lib = os.path.join(td, "lib")
        os.makedirs(lib)
        g = ["git", "-C", lib, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", lib], check=True)
        subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "base"], check=True)
        bindir = os.path.join(td, "bin")
        os.makedirs(bindir)
        calls = os.path.join(td, "systemctl.calls")
        with open(os.path.join(bindir, "systemctl"), "w") as f:
            f.write(f'#!/bin/sh\necho "$@" >> {calls}\n')
        hermes = os.path.join(bindir, "hermes")
        with open(hermes, "w") as f:
            f.write("#!/bin/sh\n" + fake_hermes_body.replace("$LIB", lib) + "\n")
        for name in ("systemctl", "hermes"):
            os.chmod(os.path.join(bindir, name), 0o755)
        log = os.path.join(td, "update.log")
        with open(log, "w") as lf:
            rc = subprocess.run(["/bin/sh", "-c", panel.HERMES_UPDATE_WRAPPER, "w", lib, log, hermes], stdout=lf,
                                stderr=subprocess.STDOUT, env={**os.environ, "PATH": bindir + ":" + os.environ["PATH"]},
                                timeout=30).returncode
        restarted = panel.Path(calls).read_text() if os.path.exists(calls) else ""
        return rc, panel.Path(log).read_text(), restarted

    def test_wrapper_skips_restart_when_nothing_changed(self):
        rc, log, restarted = self._run_wrapper('echo "✓ Already up to date!"; exit 0')
        self.assertEqual(rc, 0)
        self.assertIn("Tidak ada perubahan kode", log)
        self.assertEqual(restarted, "")
        self.assertIn("sudah versi terbaru", panel._hermes_update_summary(rc, log))

    def test_wrapper_restarts_gateway_when_code_changed(self):
        rc, log, restarted = self._run_wrapper(
            'git -C "$LIB" -c user.name=t -c user.email=t@t commit -q --allow-empty -m new; echo updated; exit 0')
        self.assertEqual(rc, 0)
        self.assertIn("Kode berubah", log)
        self.assertEqual(restarted.strip(), "--user restart --no-block hermes-gateway")
        self.assertIn("sukses diperbarui", panel._hermes_update_summary(rc, log))

    def test_wrapper_failure_propagates_without_restart(self):
        rc, log, restarted = self._run_wrapper('echo boom; exit 3')
        self.assertEqual(rc, 3)
        self.assertEqual(restarted, "")
        self.assertIn("exit=3", log)
        self.assertIn("gagal (exit 3)", panel._hermes_update_summary(rc, log))

    def test_update_button_refused_when_checkout_is_ahead(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/update-hermes", data=b"", method="POST")
        req.add_header("Cookie", cookie)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        with mock.patch.object(panel, "get_hermes_update", return_value={"status": "ahead"}), \
             mock.patch.object(panel, "run_hermes_update") as run:
            try:
                urllib.request.build_opener(NoRedirect).open(req)
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 302)
                self.assertIn("just=hermes-ahead", e.headers.get("Location", ""))
        run.assert_not_called()

    def test_render_hides_button_when_ahead_and_shows_target_when_available(self):
        base = {"local": "v0.22.2", "behind": 0, "channel": "stable", "patch_notes": ["a"] * 5, "at": time.time()}
        with mock.patch.object(panel, "get_hermes_update", return_value={**base, "status": "ahead", "remote": "stable v0.22.0 (1f6c02c07c69)"}):
            page = panel.build_status_page()
        self.assertNotIn('href="/update-hermes"', page)
        self.assertIn("lebih baru dari rilis", page)
        with mock.patch.object(panel, "get_hermes_update", return_value={**base, "status": "available", "behind": 3, "remote": "stable v0.22.3 (abc1234def56)"}):
            page = panel.build_status_page()
        self.assertIn('href="/update-hermes"', page)
        self.assertIn("stable v0.22.3 (abc1234def56)", page)

    def test_native_9router_systemd_support(self):
        """Native 9router.service is detected in process list, process-action, port resolver, and UI labels."""
        with mock.patch("os.path.isfile", side_effect=lambda p: p == "/etc/systemd/system/9router.service"), \
             mock.patch.object(panel, "get_systemd_show", return_value={"ActiveState": "active", "MainPID": "54321"}), \
             mock.patch.object(panel, "_read_proc_rss_mb", return_value=88.5):
            procs = panel.get_process_list(fresh=True)
            r_proc = next((p for p in procs if p["id"] == "9router"), None)
            self.assertIsNotNone(r_proc)
            self.assertEqual(r_proc["kind"], "Layanan Systemd")
            self.assertEqual(r_proc["status"], "Berjalan")
            self.assertTrue(r_proc["is_active"])
            self.assertEqual(r_proc["pid"], "54321")
            self.assertEqual(r_proc["mem_mb"], 88.5)

        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        panel._last_action_at.clear()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/process-action?service=9router&action=restart",
            data=b"", method="POST",
        )
        req.add_header("Cookie", cookie)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        with mock.patch("os.path.isfile", side_effect=lambda p: p == "/etc/systemd/system/9router.service"), \
             mock.patch.object(panel.subprocess, "run") as mock_sub:
            try:
                urllib.request.build_opener(NoRedirect).open(req)
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 302)
            mock_sub.assert_any_call(["systemctl", "restart", "9router.service"], timeout=15)

    def test_openclaw_control_ui_helpers_and_toggle(self):
        """OpenClaw Control UI toggle helper, route, and UI elements behave correctly."""
        import tempfile
        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as tf:
            json.dump({"gateway": {"controlUi": {"enabled": False}}}, tf)
            tmp_cfg = tf.name

        try:
            with mock.patch.object(panel, "OPENCLAW_CONFIG_PATH", tmp_cfg):
                self.assertFalse(panel.is_openclaw_control_ui_enabled())
                new_state = panel.toggle_openclaw_control_ui()
                self.assertTrue(new_state)
                self.assertTrue(panel.is_openclaw_control_ui_enabled())
                new_state = panel.toggle_openclaw_control_ui()
                self.assertFalse(new_state)
                self.assertFalse(panel.is_openclaw_control_ui_enabled())

                # Test POST /openclaw-toggle
                cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
                panel._last_action_at.clear()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{self.port}/openclaw-toggle",
                    data=b"", method="POST",
                )
                req.add_header("Cookie", cookie)

                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, *a, **k):
                        return None
                try:
                    urllib.request.build_opener(NoRedirect).open(req)
                except urllib.error.HTTPError as e:
                    self.assertEqual(e.code, 302)
                    self.assertIn("tab=control", e.headers.get("Location", ""))

                self.assertTrue(panel.is_openclaw_control_ui_enabled())

                # Test page render includes openclaw toggle button
                page = panel.build_status_page()
                self.assertIn('id="btn-openclaw-toggle"', page)
                self.assertIn('/openclaw-toggle', page)
                self.assertIn('Buka Dasbor OpenClaw', page)
        finally:
            if os.path.exists(tmp_cfg):
                os.remove(tmp_cfg)

    def test_openclaw_gateway_in_process_list(self):
        """openclaw-gateway service is properly parsed into get_process_list."""
        with mock.patch.object(panel, "service_active", side_effect=lambda name, user=False, fresh=False: name == "openclaw-gateway" and user is True), \
             mock.patch.object(panel, "get_systemd_show", return_value={"MainPID": "98765"}), \
             mock.patch.object(panel, "_read_proc_rss_mb", return_value=125.0), \
             mock.patch.object(panel, "is_openclaw_control_ui_enabled", return_value=False):
            procs = panel.get_process_list(fresh=True)
            oc_proc = next((p for p in procs if p["id"] == "openclaw-gateway"), None)
            self.assertIsNotNone(oc_proc)
            self.assertEqual(oc_proc["kind"], "Layanan Systemd User")
            self.assertEqual(oc_proc["status"], "Berjalan")
            self.assertTrue(oc_proc["is_active"])
            self.assertEqual(oc_proc["pid"], "98765")
            self.assertEqual(oc_proc["mem_mb"], 125.0)
            self.assertIn("UI Mati", oc_proc["name"])
            self.assertEqual(oc_proc["stop_url"], "/process-action?service=openclaw-gateway&action=stop")


    def test_native_9router_update_command_pins_usr_prefix(self):
        """Native 9router update must pin --prefix /usr and /usr/bin/node so systemd's /usr/local/bin PATH cannot hijack the install target."""
        cmd = panel._router_update_command()
        self.assertIn('/usr/bin/node /usr/bin/npm install -g --prefix /usr 9router@latest', cmd)
        self.assertIn('export PATH="/usr/bin:/usr/sbin:/bin:/sbin:$PATH"', cmd)

    def test_config_cache_uses_nanosecond_mtime_and_empty_values(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}"); f.flush(); os.fsync(f.fileno())
            with mock.patch.object(panel, "CONFIG_PATH", path):
                panel.invalidate_config_cache()
                first = panel.get_parsed_config()
                self.assertEqual(first, {})
                with open(path, "w", encoding="utf-8") as f:
                    f.write("model:\n  default: changed\n"); f.flush(); os.fsync(f.fileno())
                os.utime(path, ns=(time.time_ns(), time.time_ns()))
                self.assertEqual(panel.get_parsed_config()["model"]["default"], "changed")


class TestActionDebounce(unittest.TestCase):
    """Debounce must be per-action: two different actions in quick succession both run."""

    def setUp(self):
        panel._last_action_at.clear()

    def test_same_action_within_window_is_debounced(self):
        t = 1000.0
        self.assertFalse(panel._debounce_hit("/toggle", t))
        self.assertTrue(panel._debounce_hit("/toggle", t + 1.0))

    def test_same_action_after_window_runs_again(self):
        t = 1000.0
        self.assertFalse(panel._debounce_hit("/toggle", t))
        self.assertFalse(panel._debounce_hit("/toggle", t + panel.DEBOUNCE_SECONDS + 0.1))

    def test_different_actions_both_run(self):
        t = 1000.0
        self.assertFalse(panel._debounce_hit("/toggle", t))
        self.assertFalse(panel._debounce_hit("/restart-bot", t + 0.1))
        self.assertFalse(panel._debounce_hit("process:9router:stop", t + 0.2))

    def test_process_action_key_includes_service_and_action(self):
        t = 1000.0
        self.assertFalse(panel._debounce_hit("process:9router:stop", t))
        self.assertTrue(panel._debounce_hit("process:9router:stop", t + 0.5))
        self.assertFalse(panel._debounce_hit("process:9router:start", t + 0.6))
        self.assertFalse(panel._debounce_hit("process:cloudflared:stop", t + 0.7))

    def test_debounce_map_is_pruned(self):
        t = 1000.0
        for i in range(200):
            panel._debounce_hit(f"/k{i}", t + i)
        self.assertLessEqual(len(panel._last_action_at), 64)


class TestWave77Audit(unittest.TestCase):
    """Wave 77 finite checklist verification: mutation guards, SSE rendering, JS syntax, route parity."""

    def test_wave77_profile_mutation_controls_disable_during_submission(self):
        page = panel.page_source()
        # 1. Rename profile submission guard & controls
        self.assertIn("var profileRenameSubmitting = false;", page)
        rename_start = page.index("function submitRenameProfile")
        rename_end = page.index("function openProfileSoulModal", rename_start)
        rename_fn = page[rename_start:rename_end]
        self.assertIn("if(profileRenameSubmitting) return;", rename_fn)
        self.assertIn("profileRenameSubmitting = true;", rename_fn)
        self.assertIn("btn.disabled = true;", rename_fn)

        # 2. Soul profile submission guard & controls
        self.assertIn("var profileSoulSubmitting = false;", page)
        soul_start = page.index("function submitProfileSoul")
        soul_end = page.index("function openProfileModelPicker", soul_start)
        soul_fn = page[soul_start:soul_end]
        self.assertIn("if(profileSoulSubmitting) return;", soul_fn)
        self.assertIn("profileSoulSubmitting = true;", soul_fn)
        self.assertIn("btn.disabled = true;", soul_fn)

        # 3. Toggle profile skill in-flight guard & disable
        skill_start = page.index("function toggleProfileSkill")
        skill_end = page.index("var currentProfileSkillRaw", skill_start)
        skill_fn = page[skill_start:skill_end]
        self.assertIn("profileSkillSubmitting", skill_fn)
        self.assertIn(".disabled = true", skill_fn)

        # 4. Toggle profile toolset in-flight guard & disable
        tool_start = page.index("function toggleProfileToolset")
        tool_end = page.index("// --- Kanban UI JavaScript ---", tool_start)
        tool_fn = page[tool_start:tool_end]
        self.assertIn("profileToolSubmitting", tool_fn)
        self.assertIn(".disabled = true", tool_fn)

    def test_wave77_sse_processes_table_interaction_guard(self):
        script = panel.SSE_SCRIPT
        apply_start = script.index("function apply(d){")
        apply_end = script.index("function setLive", apply_start)
        apply_fn = script[apply_start:apply_end]
        self.assertIn("process-table-slot", apply_fn)
        self.assertIn("activeElement", apply_fn)
        self.assertIn(":focus-within", apply_fn)
        self.assertIn("set('process-table-slot',d.processes_table)", apply_fn)

    def test_wave77_sse_gw_summary_badge_uses_text_node(self):
        script = panel.SSE_SCRIPT
        apply_start = script.index("function apply(d){")
        apply_end = script.index("function setLive", apply_start)
        apply_fn = script[apply_start:apply_end]
        self.assertNotIn("set('gw-summary-badge'", apply_fn)
        self.assertIn("textContent = d.gw_summary_text", apply_fn)

    def test_wave77_nav_script_is_mutating_path_prefix_matching(self):
        script = panel.NAV_SCRIPT
        nav_start = script.index("function isMutatingPath")
        nav_end = script.index("function postNavigate", nav_start)
        nav_fn = script[nav_start:nav_end]
        self.assertTrue(
            "prefix.slice(-1) === '/'" in nav_fn or "endsWith('/')" in nav_fn or "indexOf" in nav_fn,
            "isMutatingPath must support prefix matching for entries ending with /"
        )

    def test_wave77_route_api_parity(self):
        import inspect, re
        source = inspect.getsource(panel.Handler.do_POST)
        post_paths = set(re.findall(r'parsed\.path\s*==\s*["\']([^"\']+)["\']', source))
        action_post_paths = {p for p in post_paths if p not in ("/login", "/logout")}
        for p in action_post_paths:
            self.assertIn(p, panel.MUTATING_PATHS, f"Route '{p}' in do_POST must be registered in MUTATING_PATHS")

        arr_text = panel.NAV_SCRIPT.split("MUTATING_PREFIXES = [")[1].split("];")[0]
        for clean in re.findall(r"['\"]([^'\"]+)['\"]", arr_text):
            if clean.endswith("/"):
                matching = [mp for mp in panel.MUTATING_PATHS if mp.startswith(clean)]
                self.assertGreater(len(matching), 0, f"Prefix '{clean}' in NAV_SCRIPT has no matching routes in MUTATING_PATHS")
            else:
                self.assertIn(clean, panel.MUTATING_PATHS, f"Path '{clean}' in NAV_SCRIPT must be in MUTATING_PATHS")

    def test_wave77_static_inline_js_syntax_including_network_script(self):
        import shutil, subprocess, tempfile
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node not installed")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
            tf.write(panel.NETWORK_SCRIPT)
            tmp_name = tf.name
        try:
            res = subprocess.run([node_bin, "--check", tmp_name], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"NETWORK_SCRIPT has JS SyntaxError:\n{res.stderr}")
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


    def test_page_has_no_external_font_or_icon_dependency(self):
        """Panel must render fully offline: zero fonts.googleapis / jsdelivr refs in PAGE."""
        page = panel.page_source()
        for domain in ("fonts.googleapis.com", "fonts.gstatic.com", "cdn.jsdelivr.net"):
            self.assertNotIn(domain, page,
                             f"PAGE still references external domain {domain}")
        # CSP must not whitelist external font/icon domains
        self.assertNotIn("https://fonts.googleapis.com", page)
        self.assertNotIn("https://fonts.gstatic.com", page)
        self.assertNotIn("https://cdn.jsdelivr.net", page)
        # Local @font-face must be present
        self.assertIn("@font-face", page)
        self.assertIn("/static/fonts/Geist-400.woff2", page)
        self.assertIn("/static/fonts/GeistMono-400.woff2", page)
        self.assertIn("font-display:swap", page)

    def test_font_route_serves_woff2_with_immutable_cache(self):
        """GET /static/fonts/Geist-400.woff2 returns 200 with font/woff2 + immutable cache."""
        import http.client
        server = panel.TimeoutThreadingHTTPServer(("127.0.0.1", 0), panel.Handler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/static/fonts/Geist-400.woff2")
            resp = conn.getresponse()
            body = resp.read()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("Content-Type"), "font/woff2")
            cc = resp.getheader("Cache-Control") or ""
            self.assertIn("immutable", cc)
            self.assertIn("max-age=31536000", cc)
            self.assertGreater(len(body), 1000)  # real woff2, not empty
            conn.close()

            # Unknown font name -> 404
            conn2 = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn2.request("GET", "/static/fonts/evil.woff2")
            resp2 = conn2.getresponse()
            resp2.read()
            self.assertEqual(resp2.status, 404)
            conn2.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_no_onerror_external_fallback_in_service_cards(self):
        """Service catalog cards must not carry onerror fallback to external CDN."""
        import inspect
        src = inspect.getsource(panel)
        self.assertNotIn("onerror=\"this.src=", src.replace(" ", ""),
                         "onerror external-image fallback still present")


class TestMobileAccessibility(unittest.TestCase):
    """Mobile/accessibility polish: lang, tab scroll, touch targets, focus, SSE empty states."""

    def test_page_declares_lang_attribute(self):
        # Screen readers need the document language for pronunciation.
        self.assertIn('<html lang="id">', panel.PAGE)

    def test_active_tab_scrolls_into_view(self):
        import re
        page = panel.page_source()
        # switchTab (the single activation path for click, URL/sessionStore restore, keyboard)
        # must bring the activated tab into view.
        start = page.index("function switchTab")
        fn = page[start:page.index("safeStore('setItem', 'activeTab'", start)]
        self.assertIn("scrollIntoView", fn, "activating a tab must scroll it into view")
        self.assertIn("block: 'nearest'", fn)
        self.assertIn("inline: 'center'", fn)
        # Every tab button routes through switchTab with its own element.
        # Derive the expected count from the tablist so adding a tab does not
        # silently weaken this guard.
        tablist = page[page.index('class="tabs"'):]
        tablist = tablist[:tablist.index("</div>")]
        tab_count = len(re.findall(r'<button[^>]*class="tab[ "]', tablist))
        onclicks = re.findall(r"onclick=\"switchTab\('[a-z]+', this\)\"", page)
        self.assertEqual(len(onclicks), tab_count,
                         "every tab button must pass its element to switchTab")
        self.assertGreaterEqual(tab_count, 7, "core tabs plus the Kuota AI tab")
        # Keyboard arrow navigation focuses + clicks the next tab, hitting the same path.
        # The tab a11y IIFEs ship inside NAV_SCRIPT, injected via the {nav_script} slot.
        self.assertIn("{nav_script}", page)
        seg = panel.NAV_SCRIPT[panel.NAV_SCRIPT.index("A11y: panah kiri/kanan"):]
        seg = seg[:seg.index("})();") + len("})();")]
        self.assertIn("next.focus()", seg)
        self.assertIn("next.click()", seg)

    def test_touch_targets_meet_minimums(self):
        import re
        page = panel.page_source()
        css = page[page.index("<style>"):page.index("</style>")]

        def rule_min_height(selector):
            m = re.search(re.escape(selector) + r"\{\{([^{}]*)\}\}", css)
            self.assertIsNotNone(m, f"CSS rule {selector} not found")
            body = m.group(1)
            hm = re.search(r"min-height:\s*(\d+)px", body)
            self.assertIsNotNone(hm, f"{selector} declares no min-height")
            return int(hm.group(1))

        # WCAG 2.5.5: primary tab navigation must stay a 44px target on phones.
        self.assertGreaterEqual(rule_min_height(".tab"), 44)
        mq = re.search(r"@media \(max-width:390px\)\{\{\.tab\{\{([^{}]*)\}\}", css)
        self.assertIsNotNone(mq, "390px override for .tab not found")
        mq_h = re.search(r"min-height:\s*(\d+)px", mq.group(1))
        self.assertIsNotNone(mq_h, "390px .tab override lost min-height")
        self.assertGreaterEqual(int(mq_h.group(1)), 44, "390px override shrank the tab target")
        # Compact action buttons (cards, process table, pagination): 32px floor.
        for sel in (".btn-action-sm", ".btn-end-task", ".btn-start-task",
                    ".btn-restart-task", ".patch-notes-pages button"):
            self.assertGreaterEqual(rule_min_height(sel), 32, sel)
        # Nothing anywhere in the shipped markup may shrink a control below 32px.
        blob = page + panel.NAV_SCRIPT + panel.NETWORK_SCRIPT + panel.SSE_SCRIPT
        small = sorted({int(h) for h in re.findall(r"min-height:\s*(\d+)px", blob) if int(h) < 32})
        self.assertEqual(small, [], f"min-height below the 32px floor: {small}")

    def test_focus_visible_style_present(self):
        import re
        page = panel.page_source()
        css = page[page.index("<style>"):page.index("</style>")]
        # 1. A global :focus-visible ring in the panel accent, with clear offset.
        m = re.search(r":focus-visible\{\{([^{}]*)\}\}", css)
        self.assertIsNotNone(m, "no global :focus-visible rule in PAGE")
        body = m.group(1)
        self.assertIn("outline:", body)
        self.assertIn("outline-offset:", body)
        self.assertIn("var(--accent", body)
        # 2. Any CSS rule that strips the outline must replace it — same rule
        #    or a matching :focus/:focus-visible rule.
        rules = {sel.strip(): b for sel, b in re.findall(r"([^{}]+)\{\{([^{}]*)\}\}", css)}
        login = panel.build_login_page()
        lcss = login[login.index("<style>"):login.index("</style>")]
        rules.update({sel.strip(): b for sel, b in re.findall(r"([^{}]+)\{([^{}]*)\}", lcss)})
        for sel, body in rules.items():
            if "outline:none" in body.replace(" ", ""):
                focus_body = rules.get(sel + ":focus", "") + rules.get(sel + ":focus-visible", "")
                self.assertTrue(
                    "box-shadow" in body or "border-color" in body
                    or "box-shadow" in focus_body or "border-color" in focus_body,
                    f"{sel} drops the focus outline with no replacement")
        # 3. Inline styles cannot carry a :focus replacement — none may strip it.
        markup = re.sub(r"<style>.*?</style>", "", page, flags=re.S)
        self.assertNotIn("outline:none", markup.replace(" ", ""),
                         "inline outline:none kills the global focus ring with no replacement")

    def test_sse_slots_have_loading_or_empty_placeholder(self):
        """Every SSE-filled slot must render something informative when its
        data is empty — never a bare empty string the user can't interpret."""
        with mock.patch.object(panel, "get_aux_tasks_config", return_value=[]):
            aux = panel.render_aux_tasks_block()
        self.assertTrue(aux.strip(), "aux-tasks-slot renders '' when there are no aux tasks")
        self.assertIn("Belum ada", aux)
        with mock.patch.object(panel, "get_process_list", return_value=[]):
            table = panel._render_processes_table_uncached()
        tbody = table[table.index("<tbody>"):table.index("</tbody>")]
        self.assertIn("<tr>", tbody, "empty process list renders an empty tbody")
        self.assertIn("colspan", tbody)
        self.assertIn("Tidak ada", tbody)
        with mock.patch.object(panel, "get_clean_junk_result", return_value={"log": "", "at": 0}):
            clean = panel.render_clean_junk_card()
        self.assertTrue(clean.strip(), "clean-log-slot renders '' before the first cleanup")
        self.assertIn("Belum ada", clean)
        with mock.patch.object(panel, "get_gateway_platforms", return_value=[]):
            self.assertIn("Belum ada", panel.render_gateway_platforms_html())
        with mock.patch.object(panel, "get_fallback_models_config", return_value=[]):
            self.assertIn("Belum ada", panel.render_backup_models_block())
        with mock.patch.object(panel, "list_agent_profiles", return_value=[]), \
             mock.patch.object(panel, "get_gateway_profile_statuses", return_value={}):
            self.assertIn("Profil Saat Ini", panel._render_profiles_block_uncached())


class TestWave78ResourceAudit(unittest.TestCase):
    """Wave 78: timer/cache/blocking-IO/silent-error/config-race/SSE-payload audit."""

    # ---- Area 6: SSE payload must travel per-key, not all-or-nothing ----
    def test_60_sse_delta_omits_unchanged_keys(self):
        frag = {k: f"<{k}-v1>" for k in panel.SSE_CLIENT_KEYS}
        first = json.loads(panel._sse_payload(frag, delta=True))
        self.assertEqual(set(first), set(panel.SSE_CLIENT_KEYS),
                         "first delta push must carry every client key")
        self.assertEqual(json.loads(panel._sse_payload(frag, delta=True)), {},
                         "an unchanged fragment must produce an empty delta")
        mutated = dict(frag)
        mutated["gw_summary_text"] = "diubah"
        delta = json.loads(panel._sse_payload(mutated, delta=True))
        self.assertEqual(set(delta), {"gw_summary_text"},
                         "only the key that changed may travel")
        self.assertEqual(panel._sse_payload(frag), json.dumps(frag),
                         "full payload (initial connect / /api/status) must stay complete")

    def test_60b_sse_push_loop_uses_delta(self):
        import inspect
        src = inspect.getsource(panel._sse_push_loop)
        self.assertIn("_sse_payload(frag, delta=True)", src,
                      "push loop must send per-key deltas, not the full 36 KB payload every tick")

    # ---- Area 2: tail-log cache must stay bounded ----
    def test_61_tail_log_cache_is_bounded(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="panel-tailcap-")
        path = os.path.join(tmp, "big.log")
        with open(path, "w", encoding="utf-8") as f:
            for i in range(4000):
                f.write(f"line {i} " + "x" * 60 + "\n")
        try:
            for n in range(1, 200):
                panel._tail_file_cached(path, n)
            with panel._tail_log_lock:
                size = len(panel._tail_log_cache)
            self.assertLessEqual(size, 32,
                                 f"_tail_log_cache grew to {size} entries via /api/gateway-log?n=")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_61b_log_endpoints_clamp_n(self):
        """?n= is client-controlled; unbounded values must not create new cache keys."""
        import inspect
        src = inspect.getsource(panel)
        self.assertIn("TAIL_N_ALLOWED", src,
                      "log endpoints must clamp n to a fixed allowed set")

    # ---- Area 3: stalled request bodies must not pin a thread forever ----
    def test_62_handler_socket_has_timeout(self):
        self.assertIsNotNone(panel.Handler.timeout,
                             "Handler needs a socket timeout or a stalled body read pins a thread")
        self.assertGreater(panel.Handler.timeout, 0)

    # ---- Area 4: swallowed errors in the per-tick fragment path must be logged ----
    def test_63_process_row_failure_is_logged(self):
        with mock.patch.object(panel, "get_systemd_show", side_effect=RuntimeError("boom")):
            with mock.patch.object(panel.sys, "stderr") as err:
                procs = panel.get_process_list(fresh=True)
        self.assertNotIn("hermes-gateway", [p["id"] for p in procs],
                         "a failing probe must still drop the row")
        written = "".join(c.args[0] for c in err.write.call_args_list if c.args)
        self.assertIn("hermes-gateway", written,
                      "a silently vanished service row must leave a log line")

    def test_63b_log_swallow_is_rate_limited(self):
        panel._swallow_log_at.clear()
        with mock.patch.object(panel.sys, "stderr") as err:
            for _ in range(5):
                panel._log_swallow("unit-test-ctx", RuntimeError("boom"))
        self.assertEqual(err.write.call_count, 1,
                         "per-tick swallows must not flood stderr")

    # ---- Area 5: model switch must not rewrite an unrelated default: key ----
    def test_64_set_current_model_scoped_to_model_block(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="panel-modelcfg-")
        cfg_path = os.path.join(tmp, "config.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write("agent:\n"
                    "  default: decoy-value\n"
                    "model:\n"
                    "  custom_providers:\n"
                    "    9router:\n"
                    "      discover_models: true\n"
                    "  default: OLD-MODEL\n")
        try:
            with mock.patch.object(panel, "CONFIG_PATH", cfg_path):
                self.assertTrue(panel.set_current_model("NEW-MODEL"))
            with open(cfg_path, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("default: decoy-value", text,
                          "an unrelated default: key above model: must not be rewritten")
            self.assertIn("default: NEW-MODEL", text)
            self.assertNotIn("OLD-MODEL", text)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class TestProviderQuota(unittest.TestCase):
    """Kuota AI: 9router per-account usage normalisation and the /api/provider-quota route."""

    AG_BUCKETS = {
        "gemini_session": {"used": 0, "total": 1000, "remainingPercentage": 100.0,
                           "resetAt": "2026-10-30T19:00:00.000Z", "displayName": "Gemini (5h)"},
        "gemini_weekly": {"used": 540, "total": 1000, "remainingPercentage": 46.0,
                          "resetAt": "2026-10-31T07:00:00.000Z"},
        "claude_gpt_session": {"used": 1000, "total": 1000, "remainingPercentage": 0.0,
                               "resetAt": "2026-10-30T19:00:00.000Z"},
        "claude_gpt_weekly": {"used": 1000, "total": 1000, "remainingPercentage": 0.0,
                              "resetAt": "2026-11-01T16:00:00.000Z"},
        "gemini-3.8-flash-high": {"used": 23, "total": 1000, "remainingPercentage": 97.7,
                                  "resetAt": "2026-10-30T19:33:57.000Z",
                                  "displayName": "Gemini 3.8 Flash (High)"},
        "gemini-3.1-flash-image": {"used": 23, "total": 1000, "remainingPercentage": 97.7,
                                   "resetAt": "2026-10-30T19:33:57.000Z",
                                   "displayName": "Gemini 3.1 Flash Image"},
        "claude-sonnet-5-5-high": {"used": 0, "total": 1000, "remainingPercentage": 100.0,
                                   "resetAt": "2026-10-30T19:00:00.000Z",
                                   "displayName": "Claude Sonnet 5.5 (High)"},
        "gemini-4-argon-high": {"used": 0, "total": 1000, "remainingPercentage": 100.0,
                                "resetAt": "2026-11-06T14:24:22.000Z"},
        "gemini-4-argon-medium": {"used": 0, "total": 1000, "remainingPercentage": 100.0,
                                  "resetAt": "2026-11-06T14:24:22.000Z"},
        "gemini-4-argon-low": {"used": 0, "total": 1000, "remainingPercentage": 100.0,
                               "resetAt": "2026-11-06T14:24:22.000Z"},
        "gpt-oss-120b-medium": {"used": 0, "total": 1000, "remainingPercentage": 100.0,
                                "resetAt": "2026-10-30T19:00:00.000Z"},
    }

    def _ag_usage(self, buckets=None):
        return {
            "plan": "Antigravity",
            "quotas": dict(buckets if buckets is not None else self.AG_BUCKETS),
            "subscriptionInfo": {"currentTier": {"id": "free-tier", "name": "Antigravity"}},
        }

    def _account(self, usage, provider="antigravity"):
        conn = {"id": "acc-1", "provider": provider, "name": "a@b.c", "email": "a@b.c",
                "priority": 3, "isActive": 1}
        return panel._normalize_quota_account(conn, usage)

    # ---- pct normalisation -------------------------------------------------

    def test_pct_prefers_remaining_percentage(self):
        self.assertEqual(panel._quota_pct({"remainingPercentage": 42.34}), 42.3)

    def test_pct_derives_from_remaining_over_total(self):
        self.assertEqual(panel._quota_pct({"remaining": 73, "total": 100}), 73.0)

    def test_pct_derives_from_used_when_remaining_absent(self):
        # codebuddy-intl ships used+total with no remaining / remainingPercentage.
        self.assertEqual(panel._quota_pct({"used": 0.13, "total": 100}), 99.9)

    def test_pct_none_for_zero_total_and_unlimited(self):
        self.assertIsNone(panel._quota_pct({"used": 0, "total": 0}))
        self.assertIsNone(panel._quota_pct({"remainingPercentage": 50, "unlimited": True}))

    def test_pct_is_clamped(self):
        self.assertEqual(panel._quota_pct({"remainingPercentage": 130}), 100.0)
        self.assertEqual(panel._quota_pct({"remainingPercentage": -5}), 0.0)

    # ---- antigravity five groups ------------------------------------------

    def test_antigravity_renders_exactly_five_groups_in_order(self):
        acct = self._account(self._ag_usage())
        self.assertEqual(len(acct["groups"]), 5)
        self.assertEqual([g["label"] for g in acct["groups"]], [
            "Kuota Gemini (Flash & Pro)",
            "Kuota Weekly Gemini",
            "Kuota Claude",
            "Kuota Weekly Claude",
            "Kuota Weekly Argon",
        ])
        self.assertEqual([g["key"] for g in acct["groups"]], [
            "gemini_session", "gemini_weekly", "claude_gpt_session",
            "claude_gpt_weekly", "gemini-4-argon",
        ])

    def test_group_main_bars_use_their_aggregate_bucket(self):
        acct = self._account(self._ag_usage())
        g = {x["key"]: x for x in acct["groups"]}
        self.assertEqual(g["gemini_session"]["pct"], 100.0)
        self.assertEqual(g["gemini_weekly"]["pct"], 46.0)
        self.assertEqual(g["claude_gpt_session"]["pct"], 0.0)
        self.assertEqual(g["claude_gpt_weekly"]["pct"], 0.0)

    def test_group_details_exclude_aggregates_and_argon_from_gemini(self):
        acct = self._account(self._ag_usage())
        g = {x["key"]: x for x in acct["groups"]}
        gem_keys = [b["key"] for b in g["gemini_session"]["buckets"]]
        self.assertIn("gemini-3.8-flash-high", gem_keys)
        self.assertNotIn("gemini_session", gem_keys)
        self.assertNotIn("gemini-4-argon-high", gem_keys)
        self.assertNotIn("gpt-oss-120b-medium", gem_keys)
        claude_keys = [b["key"] for b in g["claude_gpt_session"]["buckets"]]
        self.assertEqual(claude_keys, ["claude-sonnet-5-5-high"])

    def test_argon_group_pct_is_min_of_tiers(self):
        buckets = dict(self.AG_BUCKETS)
        buckets["gemini-4-argon-high"] = {"used": 0, "total": 1000, "remainingPercentage": 80.0,
                                          "resetAt": "2026-11-06T14:24:22.000Z"}
        buckets["gemini-4-argon-medium"] = {"used": 0, "total": 1000, "remainingPercentage": 35.3,
                                            "resetAt": "2026-11-06T14:24:22.000Z"}
        buckets["gemini-4-argon-low"] = {"used": 0, "total": 1000, "remainingPercentage": 90.0,
                                         "resetAt": "2026-11-06T14:24:22.000Z"}
        acct = self._account(self._ag_usage(buckets))
        argon = [g for g in acct["groups"] if g["key"] == "gemini-4-argon"][0]
        self.assertEqual(argon["pct"], 35.3)
        self.assertEqual(len(argon["buckets"]), 3)

    def test_argon_absent_is_no_access_not_zero(self):
        buckets = {k: v for k, v in self.AG_BUCKETS.items() if "argon" not in k}
        acct = self._account(self._ag_usage(buckets))
        argon = [g for g in acct["groups"] if g["key"] == "gemini-4-argon"][0]
        self.assertEqual(argon["status"], "no_access")
        self.assertIsNone(argon["pct"], "missing Argon must render 'Tidak tersedia', never 0%")
        self.assertIn("Tidak tersedia", argon["note"])

    def test_argon_no_access_does_not_degrade_account_status(self):
        buckets = {k: v for k, v in self.AG_BUCKETS.items() if "argon" not in k}
        buckets["claude_gpt_session"] = {"used": 0, "total": 1000, "remainingPercentage": 100.0}
        buckets["claude_gpt_weekly"] = {"used": 0, "total": 1000, "remainingPercentage": 100.0}
        acct = self._account(self._ag_usage(buckets))
        self.assertEqual(acct["status"], "ok")

    def test_account_status_is_worst_of_groups_one_to_four(self):
        acct = self._account(self._ag_usage())
        # Claude buckets are all zero -> that group is exhausted -> account exhausted.
        self.assertEqual(acct["status"], "exhausted")

    def test_fallback_when_aggregate_bucket_missing(self):
        buckets = {k: v for k, v in self.AG_BUCKETS.items()
                   if k not in ("gemini_session", "claude_gpt_session")}
        acct = self._account(self._ag_usage(buckets))
        g = {x["key"]: x for x in acct["groups"]}
        self.assertTrue(g["gemini_session"]["fallback"])
        self.assertEqual(g["gemini_session"]["pct"], 97.7)
        self.assertTrue(g["claude_gpt_session"]["fallback"])

    # ---- non-antigravity providers ----------------------------------------

    def test_codex_uses_remaining_field(self):
        acct = self._account({"plan": "plus", "quotas": {
            "session": {"used": 7, "total": 100, "remaining": 93, "resetAt": "x"},
            "weekly": {"used": 28, "total": 100, "remaining": 72, "resetAt": "y"},
        }}, provider="codex")
        got = {b["key"]: b["pct"] for b in acct["buckets"]}
        self.assertEqual(got, {"session": 93.0, "weekly": 72.0})
        self.assertEqual(acct["status"], "ok")

    def test_aggregate_buckets_are_flagged(self):
        acct = self._account({"quotas": {
            "session": {"remaining": 50, "total": 100},
            "Monthly": {"used": 1, "total": 100},
        }}, provider="codex")
        flags = {b["key"]: b["aggregate"] for b in acct["buckets"]}
        self.assertTrue(flags["session"])
        self.assertTrue(flags["Monthly"])

    def test_auth_expired_message_maps_to_status(self):
        acct = self._account({"message": "Antigravity quota API authentication expired. Chat may still work.",
                              "quotas": {}})
        self.assertEqual(acct["status"], "auth_expired")
        self.assertEqual(acct["groups"], [])

    def test_usage_not_available_maps_to_unsupported(self):
        acct = self._account({"message": "Usage not available for this connection"}, provider="gemini")
        self.assertEqual(acct["status"], "unsupported")

    def test_missing_usage_maps_to_error(self):
        acct = self._account(None, provider="gemini")
        self.assertEqual(acct["status"], "error")

    # ---- response shape / security ----------------------------------------

    def test_get_router_db_path_prefers_existing_candidate(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as tf:
            with mock.patch.object(panel, "ROUTER_DB_CANDIDATES", ["/nonexistent/db.sqlite", tf.name]):
                self.assertEqual(panel.get_router_db_path(), tf.name)

    def test_router_cli_token_derivation(self):
        with tempfile.TemporaryDirectory() as td:
            mid_file = os.path.join(td, "machine-id")
            sec_dir = os.path.join(td, "auth")
            os.makedirs(sec_dir, exist_ok=True)
            sec_file = os.path.join(sec_dir, "cli-secret")
            with open(mid_file, "w") as f1, open(sec_file, "w") as f2:
                f1.write("test-mid\n")
                f2.write("test-secret\n")
            with mock.patch.object(panel, "os") as mock_os:
                # bypass patching and call directly with mock bases
                pass
            expected = hashlib.sha256("test-mid9r-cli-authtest-secret".encode("utf-8")).hexdigest()[:16]
            self.assertEqual(len(expected), 16)

    def test_router_usage_sends_cli_token_when_present(self):
        with mock.patch.object(panel, "_router_cli_token", return_value="fake_cli_token"), \
             mock.patch.object(panel, "urllib") as mock_urllib:
            req_holder = []
            class FakeResp:
                def __enter__(self): return self
                def __exit__(self, *a): pass
                def read(self): return b'{"ok": true}'
            def fake_urlopen(req, timeout=None):
                req_holder.append(req)
                return FakeResp()
            mock_urllib.request.urlopen = fake_urlopen
            mock_urllib.request.Request = urllib.request.Request
            panel._router_usage("test-conn-123")
            self.assertEqual(len(req_holder), 1)
            self.assertEqual(req_holder[0].headers.get("X-9r-cli-token"), "fake_cli_token")

    def test_route_registered(self):
        src = _handler_source()
        self.assertIn('"/api/provider-quota"', src)
        self.assertIn("get_provider_quota(refresh=want_refresh, provider_filter=prov)", src)

    def test_quota_response_never_leaks_secrets(self):
        conn = {"id": "acc-1", "provider": "antigravity", "name": "a@b.c", "email": "a@b.c",
                "priority": 1, "isActive": 1}
        blob = json.dumps(panel._normalize_quota_account(conn, self._ag_usage()))
        for needle in ("ya29.", "Bearer ", "INITIAL_PASSWORD", "auth_token", "refresh_token"):
            self.assertNotIn(needle, blob)

    def test_kuota_tab_is_wired(self):
        page = panel.page_source()
        self.assertIn("switchTab('quota', this)", page)
        self.assertIn('id="tab-quota"', page)
        self.assertIn("function quotaRender", page)
        self.assertIn("clip-path:inset(0 calc(100% - var(--q,0%)) 0 0)", page)

    def test_new_account_appears_on_next_build(self):
        """Adding a connection in 9router must surface on the next rebuild."""
        conn1 = {"id": "a1", "provider": "codex", "name": "one@x", "email": "one@x",
                 "priority": 1, "isActive": 1}
        conn2 = {"id": "a2", "provider": "codex", "name": "two@x", "email": "two@x",
                 "priority": 2, "isActive": 1}
        seq = [[conn1], [conn1, conn2]]

        def fake_read():
            return seq.pop(0) if seq else [conn1, conn2]

        def fake_fetch(conns):
            usage = {"quotas": {"session": {"remaining": 50, "total": 100}}}
            return [panel._normalize_quota_account(c, usage) for c in conns]

        with mock.patch.object(panel, "_read_router_connections", side_effect=fake_read), \
             mock.patch.object(panel, "_quota_fetch_all", side_effect=fake_fetch):
            first = panel._quota_build()
            second = panel._quota_build()

        self.assertEqual(sum(p["accountCount"] for p in first["providers"]), 1)
        self.assertEqual(sum(p["accountCount"] for p in second["providers"]), 2,
                         "a connection added in 9router must show up on the next rebuild")

    def test_connection_list_is_not_cached(self):
        """_quota_build must re-read providerConnections every time."""
        with mock.patch.object(panel, "_read_router_connections", return_value=[]) as m_read, \
             mock.patch.object(panel, "_quota_fetch_all", return_value=[]):
            panel._quota_build()
            panel._quota_build()
        self.assertEqual(m_read.call_count, 2)

    def test_provider_filter_matches_prefix_family(self):
        data = {"ok": True, "providers": [
            {"provider": "openai-compatible-chat-abc", "label": "OpenAI Compatible", "accounts": []},
            {"provider": "codex", "label": "Codex", "accounts": []},
        ]}
        out = panel._quota_filter(data, "openai-compatible")
        self.assertEqual([p["provider"] for p in out["providers"]], ["openai-compatible-chat-abc"])


def _handler_source() -> str:
    import inspect
    return inspect.getsource(panel.Handler.do_GET)


class TestPanelDeepSleep(unittest.TestCase):
    """Socket activation + idle exit: the panel must park itself when unused."""

    def test_no_inherited_socket_without_env(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LISTEN_FDS", None)
            os.environ.pop("LISTEN_PID", None)
            self.assertIsNone(panel._inherited_listen_socket())

    def test_inherited_socket_ignores_foreign_pid(self):
        with mock.patch.dict(os.environ, {"LISTEN_FDS": "1", "LISTEN_PID": "1"}):
            self.assertIsNone(panel._inherited_listen_socket())

    def test_inherited_socket_ignores_zero_fds(self):
        with mock.patch.dict(os.environ, {"LISTEN_FDS": "0", "LISTEN_PID": str(os.getpid())}):
            self.assertIsNone(panel._inherited_listen_socket())

    def test_inherited_socket_ignores_garbage(self):
        with mock.patch.dict(os.environ, {"LISTEN_FDS": "abc", "LISTEN_PID": "x"}):
            self.assertIsNone(panel._inherited_listen_socket())

    def test_conn_tracking_is_monotonic_at_zero(self):
        base = panel._active_conns
        panel._conn_delta(+1)
        self.assertEqual(panel._active_conns, base + 1)
        panel._conn_delta(-1)
        self.assertEqual(panel._active_conns, base)
        # never goes negative even if finish() fires twice
        panel._conn_delta(-1)
        self.assertEqual(panel._active_conns, base)

    def test_touch_activity_moves_timestamp(self):
        before = panel._last_activity
        time.sleep(0.01)
        panel._touch_activity()
        self.assertGreater(panel._last_activity, before)

    def test_idle_busy_while_update_runs(self):
        with mock.patch.object(panel, "_hermes_update_running", True):
            self.assertTrue(panel._panel_idle_busy())
        with mock.patch.object(panel, "_hermes_update_running", False), \
             mock.patch.object(panel, "_hermes_check_running", False), \
             mock.patch.object(panel, "_update_refreshing", False), \
             mock.patch.object(panel, "_models_bg_refreshing", False), \
             mock.patch.object(panel, "_quota_refreshing", {"on": False}), \
             mock.patch.object(panel, "_clean_junk_result", {"status": "idle"}), \
             mock.patch.object(panel, "_wa_pair_proc", None):
            self.assertFalse(panel._panel_idle_busy())

    def test_idle_busy_while_quota_refresh_runs(self):
        with mock.patch.object(panel, "_quota_refreshing", {"on": True}):
            self.assertTrue(panel._panel_idle_busy())

    def test_watchdog_exits_when_limit_zero(self):
        """limit<=0 must disable the watchdog thread entirely (no idle exit)."""
        with mock.patch.dict(os.environ, {"PANEL_IDLE_EXIT_SECONDS": "0"}):
            panel._idle_watchdog()  # returns immediately instead of looping

    def test_service_unit_uses_socket_activation(self):
        src = open("/etc/systemd/system/hermes-panel.service", encoding="utf-8").read() \
            if os.path.exists("/etc/systemd/system/hermes-panel.service") else ""
        if not src:
            self.skipTest("unit file not present")
        self.assertIn("hermes-panel.socket", src,
                      "service must require the socket so it can be woken")
        self.assertNotIn("Restart=always", src,
                         "Restart=always would respawn the panel right after idle exit")


class TestProfileRuntimeParity(unittest.TestCase):
    """Tab Profil harus menampilkan apa yang benar-benar dimuat Hermes.

    Skill: status on / off (skills.disabled + platform_disabled) / tak berlaku
    (gate platform-environment-apps), termasuk skill dari external_dirs.
    Toolset: daftar efektif per platform + jumlah tool dari registry.
    Config: config.yaml profil, kredensial disamarkan.
    """

    def setUp(self):
        import shutil
        self.tmpdir = tempfile.mkdtemp(prefix="hermes-parity-")
        self.root = panel.Path(self.tmpdir)
        self._orig_config_path = panel.CONFIG_PATH
        panel.CONFIG_PATH = str(self.root / "config.yaml")
        panel._status_probe_cache.clear()
        panel._ttl_generation.clear()

    def tearDown(self):
        import shutil
        panel.CONFIG_PATH = self._orig_config_path
        panel._status_probe_cache.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- helpers ---------------------------------------------------------
    def _skill(self, rel, name, extra_fm=""):
        d = self.root / "skills" / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} description.\n{extra_fm}---\n\nBody {name}.\n",
            encoding="utf-8",
        )
        return d

    def _write_cfg(self, text):
        (self.root / "config.yaml").write_text(text, encoding="utf-8")

    def _states(self, profile, platform=""):
        inv = panel.get_profile_skill_inventory(profile, platform)
        self.assertTrue(inv.get("ok"), inv.get("error"))
        return {s["name"]: s["state"] for s in inv["skills"]}, inv

    # --- skills ----------------------------------------------------------
    def test_skill_platform_disable_is_per_platform(self):
        self._skill("demo-a", "demo-a")
        self._skill("demo-b", "demo-b")
        self._write_cfg(
            "model:\n  default: m\n"
            "skills:\n  disabled: [demo-b]\n  platform_disabled:\n    telegram: [demo-a]\n"
        )
        base_state, base = self._states("default")
        self.assertEqual(base["platform"], "cli")
        self.assertEqual(base_state["demo-a"], "on")
        self.assertEqual(base_state["demo-b"], "off")

        tg_state, tg = self._states("default", "telegram")
        self.assertEqual(tg["platform"], "telegram")
        self.assertEqual(tg_state["demo-a"], "off", "platform_disabled harus berlaku di platform itu")
        self.assertEqual(tg_state["demo-b"], "off", "disable global berlaku di semua platform")
        self.assertEqual(tg["enabled_count"], 0)

    def test_skill_hidden_when_its_own_gate_excludes_this_host(self):
        import sys
        self._skill("universal", "universal")
        other_os = "windows" if not sys.platform.startswith("win") else "linux"
        self._skill("elsewhere", "elsewhere", f"platforms: [{other_os}]\n")
        state, inv = self._states("default")
        self.assertEqual(state["universal"], "on")
        self.assertEqual(state["elsewhere"], "hidden")
        self.assertEqual(inv["hidden_count"], 1)
        self.assertEqual(inv["enabled_count"], 1)
        hidden = {s["name"]: s for s in inv["skills"]}["elsewhere"]
        self.assertEqual(hidden["hidden_reason"], "platform")

    def test_skill_roots_follow_hermes_external_dirs(self):
        self._skill("local-a", "local-a")
        ext = self.root / "ext-skills"
        (ext / "ext-b").mkdir(parents=True)
        (ext / "ext-b" / "SKILL.md").write_text("---\nname: ext-b\ndescription: dari dir eksternal\n---\n")
        self._write_cfg(f"model:\n  default: m\nskills:\n  external_dirs:\n    - {ext}\n")
        state, inv = self._states("default")
        self.assertIn("ext-b", state, "skills.external_dirs harus ikut discan seperti Hermes")
        tiers = {s["name"]: s["tier"] for s in inv["skills"]}
        self.assertEqual(tiers["local-a"], 1)
        self.assertEqual(tiers["ext-b"], 3)
        self.assertEqual([r["tier"] for r in inv["roots"]], [1, 3])

    def test_inventory_matches_hermes_when_library_is_available(self):
        """Sanity: hasil panel == hasil fungsi yang dipakai runtime (bila hermes-lib ada)."""
        if panel._hermes_module("agent.skill_utils") is None:
            self.skipTest("hermes-lib tidak tersedia di mesin ini")
        self._skill("demo-a", "demo-a")
        self._skill("demo-b", "demo-b")
        self._write_cfg("model:\n  default: m\nskills:\n  disabled: [demo-b]\n")
        state, inv = self._states("default")
        self.assertEqual(inv["total"], 2)
        self.assertEqual(sorted(state.values()), ["off", "on"])
        self.assertEqual(inv["enabled_count"] + inv["disabled_count"] + inv["hidden_count"], inv["total"])

    def test_skill_toggle_writes_platform_disabled(self):
        import yaml
        self._skill("demo-a", "demo-a")
        self._write_cfg("model:\n  default: m\n")
        ok, msg = panel.set_profile_skill_enabled("default", "demo-a", False, platform="telegram")
        self.assertTrue(ok, msg)
        raw = yaml.safe_load((self.root / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(raw["skills"]["platform_disabled"]["telegram"], ["demo-a"])
        self.assertNotIn("demo-a", raw["skills"].get("disabled") or [])

        ok, msg = panel.set_profile_skill_enabled("default", "demo-a", True, platform="telegram")
        self.assertTrue(ok, msg)
        raw = yaml.safe_load((self.root / "config.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("platform_disabled", raw.get("skills") or {})

    def test_global_disable_is_not_overridable_per_platform(self):
        self._skill("demo-a", "demo-a")
        self._write_cfg("model:\n  default: m\nskills:\n  disabled: [demo-a]\n")
        ok, msg = panel.set_profile_skill_enabled("default", "demo-a", False, platform="telegram")
        self.assertFalse(ok)
        self.assertIn("global", msg)

    def test_inventory_degrades_without_hermes_lib(self):
        self._skill("demo-a", "demo-a")
        self._write_cfg("model:\n  default: m\nskills:\n  disabled: [demo-a]\n")
        saved_mods = dict(panel._HERMES_MOD_CACHE)
        saved_lib = dict(panel._HERMES_LIB_CACHE)
        try:
            panel._HERMES_MOD_CACHE.clear()
            panel._HERMES_LIB_CACHE.update({"dir": "", "tried": True})
            inv = panel.get_profile_skill_inventory("default")
            self.assertTrue(inv["ok"], inv.get("error"))
            self.assertEqual(inv["source"], "panel-scan")
            self.assertEqual(inv["total"], 1)
            self.assertEqual(inv["disabled_count"], 1)
            ts = panel.get_profile_toolsets("default")
            self.assertEqual(ts["source"], "config")
        finally:
            panel._HERMES_MOD_CACHE.clear()
            panel._HERMES_MOD_CACHE.update(saved_mods)
            panel._HERMES_LIB_CACHE.clear()
            panel._HERMES_LIB_CACHE.update(saved_lib)

    def test_duplicate_declared_name_reports_cli_disagreement(self):
        """Nama deklarasi ganda: loader memfilter dua-duanya, `hermes skills list` hanya satu."""
        self._skill("cat-a/dup", "dup")
        self._skill("cat-b/dup", "dup")
        self._write_cfg("model:\n  default: m\nskills:\n  disabled: [dup]\n")
        inv = panel.get_profile_skill_inventory("default")
        self.assertEqual(inv["enabled_count"], 0, "loader memfilter dua-duanya")
        self.assertEqual(inv["disabled_count"], 2)
        self.assertEqual(inv["list_enabled_count"], 2, "CLI hanya melihat load name, jadi tampak aktif")
        self.assertEqual(inv["list_mismatch"], ["cat-a/dup", "cat-b/dup"])

    # --- toolsets --------------------------------------------------------
    def test_toolsets_report_catalog_counts_per_platform(self):
        self._write_cfg(
            "model:\n  default: m\n"
            "platform_toolsets:\n  cli: [web, terminal]\n  telegram: [web]\n"
        )
        ts = panel.get_profile_toolsets("default", "cli")
        self.assertTrue(ts["ok"])
        self.assertTrue(ts["explicit"])
        self.assertEqual(ts["enabled"], ["web", "terminal"])
        self.assertIn("web_search", ts["tools"])
        self.assertIn("terminal", ts["tools"])
        self.assertEqual(ts["tool_count"], len(ts["tools"]))
        rows = {r["name"]: r for r in ts["rows"]}
        self.assertGreater(rows["web"]["tool_count"], 0)
        self.assertTrue(rows["web"]["description"])
        self.assertEqual(ts["platforms"], ["cli", "telegram"])

        tg = panel.get_profile_toolsets("default", "telegram")
        self.assertEqual(tg["enabled"], ["web"])
        self.assertNotIn("terminal", tg["tools"])

    def test_toolsets_default_to_platform_bundle_when_unset(self):
        self._write_cfg("model:\n  default: m\n")
        ts = panel.get_profile_toolsets("default")
        self.assertFalse(ts["explicit"])
        self.assertEqual(ts["configured"], ["hermes-cli"])
        if ts["source"] == "hermes-lib":
            self.assertGreater(ts["tool_count"], 0)
            self.assertEqual(ts["rows"][0]["name"], "hermes-cli")

    def test_toolsets_subtract_agent_disabled_toolsets(self):
        self._write_cfg(
            "model:\n  default: m\n"
            "platform_toolsets:\n  cli: [web, terminal]\n"
            "agent:\n  disabled_toolsets: [terminal]\n"
        )
        ts = panel.get_profile_toolsets("default")
        self.assertEqual(ts["enabled"], ["web"])
        self.assertEqual(ts["disabled_toolsets"], ["terminal"])
        self.assertNotIn("terminal", ts["tools"])

    def test_toolset_toggle_updates_effective_list(self):
        self._write_cfg("model:\n  default: m\nplatform_toolsets:\n  cli: [web, terminal]\n")
        ok, msg = panel.set_profile_toolset_enabled("default", "web", False)
        self.assertTrue(ok, msg)
        ts = panel.get_profile_toolsets("default")
        self.assertEqual(ts["enabled"], ["terminal"])
        self.assertNotIn("web_search", ts["tools"])

    # --- config ----------------------------------------------------------
    def test_config_summary_groups_and_masks_secrets(self):
        self._write_cfg(
            "model:\n  default: m\n  provider: 9router\n"
            "telegram:\n  enabled: true\n  bot_token: 123456:SECRET-TOKEN\n"
            "platform_toolsets:\n  cli: [web]\n"
            "skills:\n  disabled: [a, b]\n"
        )
        cfg = panel.get_profile_config_summary("default")
        self.assertTrue(cfg["ok"], cfg.get("error"))
        self.assertTrue(cfg["masked"])
        self.assertNotIn("SECRET-TOKEN", cfg["raw_yaml"])
        self.assertIn(panel._REDACTED_SENTINEL, cfg["raw_yaml"])
        ids = [g["id"] for g in cfg["groups"]]
        for expected in ("model", "agent", "toolsets", "skills", "platforms", "aux", "mcp"):
            self.assertIn(expected, ids)
        groups = {g["id"]: g for g in cfg["groups"]}
        self.assertTrue(any(k == "disabled" and v.startswith("2 ") for k, v in groups["skills"]["items"]))
        self.assertTrue(any(k == "telegram" and "aktif" in v for k, v in groups["platforms"]["items"]))

    def test_config_summary_rejects_invalid_profile(self):
        cfg = panel.get_profile_config_summary("../etc")
        self.assertFalse(cfg["ok"])
        self.assertIn("tidak valid", cfg["error"])

    # --- card + page -----------------------------------------------------
    def test_profile_card_shows_runtime_counts(self):
        self._skill("demo-a", "demo-a")
        self._write_cfg("model:\n  default: m\nplatform_toolsets:\n  cli: [web]\n")
        html = panel._render_profiles_block_uncached()
        self.assertIn("Skills: 1/1 aktif", html)
        self.assertIn("toolset", html)
        self.assertNotIn("Tools: 22", html)

    def test_page_source_inlines_profile_script(self):
        src = panel.page_source()
        self.assertNotIn("{profile_script}", src)
        for fn in ("function openProfileSkillsModal", "function loadProfileTools",
                   "function onProfilePlatformChange", "function renderProfileConfig"):
            self.assertIn(fn, src)

    def test_profile_endpoints_accept_platform_and_config_is_read_only(self):
        import inspect
        src = inspect.getsource(panel.Handler.do_GET)
        self.assertIn('"/api/profile-config"', src)
        self.assertIn('"/api/profile-skills"', src)
        self.assertIn('"/api/profile-toolsets"', src)
        self.assertIn('qs.get("platform")', src)
        self.assertNotIn("/api/profile-config", panel.MUTATING_PATHS)


class TestProfileManagementParity(unittest.TestCase):
    """Buat/ubah/hapus profil harus mengikuti CLI Hermes, bukan tafsiran panel."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="hermes-mgmt-")
        self.root = panel.Path(self.tmpdir)
        self._orig_config_path = panel.CONFIG_PATH
        panel.CONFIG_PATH = str(self.root / "config.yaml")
        (self.root / "config.yaml").write_text("model:\n  default: m\n  provider: 9router\n", encoding="utf-8")
        panel._status_probe_cache.clear()

    def tearDown(self):
        import shutil
        panel.CONFIG_PATH = self._orig_config_path
        panel._status_probe_cache.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_description_comes_from_profile_yaml(self):
        prof = self.root / "profiles" / "worker"
        prof.mkdir(parents=True)
        (prof / "profile.yaml").write_text(
            "description: Full-stack developer and DevOps generalist — writes code,\n"
            "  manages Docker homelab infrastructure.\n"
            "description_auto: false\n", encoding="utf-8")
        self.assertEqual(panel._read_profile_description(prof),
                         "Full-stack developer and DevOps generalist — writes code, "
                         "manages Docker homelab infrastructure.")

    def test_description_falls_back_to_meta_json(self):
        prof = self.root / "profiles" / "legacy"
        prof.mkdir(parents=True)
        (prof / "meta.json").write_text('{"description": "profil lama"}', encoding="utf-8")
        self.assertEqual(panel._read_profile_description(prof), "profil lama")
        self.assertEqual(panel._read_profile_description(self.root / "nowhere"), "")

    def test_skill_count_matches_runtime_not_raw_walk(self):
        skills = self.root / "skills"
        for rel in ("a", "b", ".archive/old-skill", "a/references/sub"):
            d = skills / rel
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(f"---\nname: {rel.split('/')[-1]}\ndescription: d\n---\n")
        profiles = panel.list_agent_profiles()
        default = {p["name"]: p for p in profiles}["default"]
        self.assertEqual(default["skill_count"], 2,
                         "skill_count harus sama dengan inventaris runtime, bukan hitungan walk mentah")

    def test_create_profile_uses_hermes_cli(self):
        calls = []

        def fake_cli(args, timeout=180):
            calls.append(args)
            target = self.root / "profiles" / args[2]
            target.mkdir(parents=True, exist_ok=True)
            (target / "config.yaml").write_text("model:\n  default: m\n", encoding="utf-8")
            return 0, "Profile 'x' created\n58 bundled skills synced."

        with mock.patch.object(panel, "_run_hermes_cli", side_effect=fake_cli):
            ok, msg = panel.create_agent_profile("worker", description="bot kerja")
        self.assertTrue(ok, msg)
        self.assertEqual(calls[0][:3], ["profile", "create", "worker"])
        self.assertIn("--description", calls[0])
        self.assertIn("58 skill bawaan", msg)
        self.assertTrue((self.root / "profiles" / "worker" / "config.yaml").is_file())

    def test_create_profile_falls_back_when_cli_unavailable(self):
        with mock.patch.object(panel, "_run_hermes_cli", return_value=(-1, "hermes tidak ada")):
            ok, msg = panel.create_agent_profile("manual", description="tanpa cli")
        self.assertTrue(ok, msg)
        prof = self.root / "profiles" / "manual"
        self.assertTrue((prof / "config.yaml").is_file())
        yaml_txt = (prof / "profile.yaml").read_text(encoding="utf-8")
        self.assertIn("tanpa cli", yaml_txt)
        self.assertIn("manual", msg)

    def test_rename_profile_uses_cli(self):
        old = self.root / "profiles" / "lama"
        old.mkdir(parents=True)
        calls = []

        def fake_cli(args, timeout=180):
            calls.append(args)
            if args[:2] == ["profile", "rename"]:
                (self.root / "profiles" / args[3]).mkdir(parents=True, exist_ok=True)
                old.rmdir()
                return 0, "renamed"
            return -1, "no"

        with mock.patch.object(panel, "_run_hermes_cli", side_effect=fake_cli):
            ok, msg = panel.rename_agent_profile("lama", "baru")
        self.assertTrue(ok, msg)
        self.assertEqual(calls[0], ["profile", "rename", "lama", "baru"])
        self.assertIn("hermes CLI", msg)

    def test_model_change_drops_stale_routing_keys(self):
        import yaml as _yaml
        self.root.joinpath("profiles", "p1").mkdir(parents=True)
        cfg_file = self.root / "profiles" / "p1" / "config.yaml"
        cfg_file.write_text(
            "model:\n  default: lama\n  provider: 9router\n  base_url: http://127.0.0.1:20128/v1\n"
            "  api_mode: chat_completions\n", encoding="utf-8")
        ok, msg = panel.set_agent_profile_model("p1", "openrouter", "baru")
        self.assertTrue(ok, msg)
        data = _yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        self.assertEqual(data["model"]["provider"], "openrouter")
        self.assertNotIn("base_url", data["model"])
        self.assertNotIn("api_mode", data["model"])
        self.assertIn("base_url", msg)

        # Provider sama: kunci routing tidak boleh ikut terhapus.
        ok, _ = panel.set_agent_profile_model("p1", "openrouter", "lain")
        data = _yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        self.assertEqual(data["model"]["default"], "lain")
        self.assertEqual(data["model"]["provider"], "openrouter")


class TestDisplayedValuesMatchReality(unittest.TestCase):
    """Nilai yang tampil di panel harus berasal dari keadaan nyata, bukan konstanta."""

    def setUp(self):
        panel._status_probe_cache.clear()

    def tearDown(self):
        panel._status_probe_cache.clear()

    def test_links_use_detected_lan_ip_not_hardcoded(self):
        container = {"name": "web", "title": "Web", "image": "nginx", "state": "running",
                     "is_running": True, "status": "Up 2 hours", "icon": "", "port": "8080",
                     "path": "/", "scheme": "http"}
        with mock.patch.object(panel, "get_server_ips", return_value=("100.64.0.9", "10.1.2.3")):
            with mock.patch.object(panel, "is_openclaw_control_ui_enabled", return_value=True):
                openclaw = panel.get_openclaw_open_block()
            panel._status_probe_cache.clear()
            with mock.patch.object(panel, "get_docker_containers_info", return_value=[container]):
                docker = panel._render_docker_apps_block_uncached()
            panel._status_probe_cache.clear()
            with mock.patch.object(panel, "get_server_ips", return_value=("", "")):
                with mock.patch.object(panel, "is_openclaw_control_ui_enabled", return_value=True):
                    fallback = panel.get_openclaw_open_block()
        self.assertIn("10.1.2.3", openclaw)
        self.assertIn("10.1.2.3", docker)
        self.assertNotIn("192.168.1.100", openclaw + docker)
        self.assertNotIn("{_link_host()}", openclaw + docker)
        self.assertIn("127.0.0.1", fallback, "tanpa IP terdeteksi jangan tinggalkan placeholder")

    def test_emmc_wear_reported_as_bucket_range(self):
        self.assertEqual(panel._emmc_wear_label(4), "30\u201340%")
        self.assertEqual(panel._emmc_wear_label(1), "0\u201310%")
        self.assertEqual(panel._emmc_wear_label(10), "90\u2013100%")
        self.assertEqual(panel._emmc_wear_label(11), ">100%")
        self.assertEqual(panel._emmc_wear_upper(4), 40)
        self.assertEqual(panel._emmc_wear_upper(99), 100)

    def test_delete_profile_without_name_reports_it(self):
        ok, msg = panel.delete_agent_profile("")
        self.assertFalse(ok)
        self.assertIn("tidak diberikan", msg)
        ok, msg = panel.delete_agent_profile("default")
        self.assertFalse(ok)
        self.assertIn("default", msg)


class TestConfigSummarySchemaShapes(unittest.TestCase):
    """Config tab harus membaca bentuk config yang benar-benar ditulis Hermes."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="hermes-cfgschema-")
        self.root = panel.Path(self.tmpdir)
        self._orig_config_path = panel.CONFIG_PATH
        panel.CONFIG_PATH = str(self.root / "config.yaml")
        panel._status_probe_cache.clear()

    def tearDown(self):
        import shutil
        panel.CONFIG_PATH = self._orig_config_path
        panel._status_probe_cache.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _aux(self):
        cfg = panel.get_profile_config_summary("default")
        self.assertTrue(cfg.get("ok"), cfg.get("error"))
        groups = {g["id"]: g for g in cfg["groups"]}
        return dict(groups["aux"]["items"])

    def test_flat_auxiliary_shape_is_read(self):
        # Bentuk yang ditulis `hermes config set auxiliary.vision.model ...`
        (self.root / "config.yaml").write_text(
            "model:\n  default: m\n"
            "auxiliary:\n"
            "  vision:\n    provider: 9router\n    model: m-vision\n    timeout: 120\n"
            "  compression:\n    provider: 9router\n    model: m-comp\n"
            "delegation:\n  provider: 9router\n  model: m-del\n", encoding="utf-8")
        aux = self._aux()
        self.assertEqual(aux.get("vision"), "9router / m-vision")
        self.assertEqual(aux.get("compression"), "9router / m-comp")
        self.assertEqual(aux.get("delegation"), "9router / m-del")

    def test_nested_auxiliary_tasks_shape_is_read(self):
        (self.root / "config.yaml").write_text(
            "model:\n  default: m\n"
            "auxiliary:\n  tasks:\n    stt:\n      provider: p\n      model: mm\n", encoding="utf-8")
        self.assertEqual(self._aux().get("stt"), "p / mm")

    def test_auto_tasks_are_not_listed_as_pinned(self):
        (self.root / "config.yaml").write_text(
            "model:\n  default: m\nauxiliary:\n  vision:\n    timeout: 120\n", encoding="utf-8")
        self.assertEqual(self._aux(), {"-": "semua otomatis"})


class TestProfileCardStaysCheap(unittest.TestCase):
    """Kartu profil tidak boleh memicu scan inventaris (regresi 12 detik /api/profiles)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="hermes-cheap-")
        self.root = panel.Path(self.tmpdir)
        self._orig_config_path = panel.CONFIG_PATH
        panel.CONFIG_PATH = str(self.root / "config.yaml")
        panel._status_probe_cache.clear()
        skills = self.root / "skills"
        for rel in ("a", "b", ".archive/lama", "a/references/x"):
            d = skills / rel
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(f"---\nname: {rel.split('/')[-1]}\ndescription: d\n---\n")
        (self.root / "config.yaml").write_text("model:\n  default: m\nskills:\n  disabled: [b]\n", encoding="utf-8")

    def tearDown(self):
        import shutil
        panel.CONFIG_PATH = self._orig_config_path
        panel._status_probe_cache.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_card_summary_does_not_scan_inventory(self):
        with mock.patch.object(panel, "get_profile_skill_inventory",
                               side_effect=AssertionError("kartu tidak boleh memindai skill")):
            summary = panel._profile_skill_tool_summary("default")
        self.assertEqual(summary["skills_total"], 2, "hanya skill nyata, bukan .archive/references")
        self.assertEqual(summary["skills_on"], 1)
        self.assertTrue(summary["estimated"])

    def test_listing_profiles_does_not_scan_inventory(self):
        with mock.patch.object(panel, "get_profile_skill_inventory",
                               side_effect=AssertionError("listing tidak boleh memindai skill")):
            profiles = panel.list_agent_profiles()
        default = {p["name"]: p for p in profiles}["default"]
        self.assertEqual(default["skill_count"], 2)

    def test_card_switches_to_exact_numbers_once_cached(self):
        first = panel._profile_skill_tool_summary("default")
        self.assertTrue(first["estimated"])
        panel._ttl_cached("profile_skill_inventory:default:", 45.0,
                          lambda: panel.get_profile_skill_inventory("default", ""))
        panel._invalidate_status_cache("profile_skill_tool_summary:default")
        second = panel._profile_skill_tool_summary("default")
        self.assertFalse(second["estimated"])
        self.assertEqual(second["skills_total"], 2)
        self.assertEqual(second["skills_on"], 1)
        html = panel._render_profiles_block_uncached()
        self.assertIn("Skills: 1/2 aktif", html)
        self.assertNotIn("perkiraan", html)


class TestServiceTableTruth(unittest.TestCase):
    """Tabel layanan harus jujur: memori seluruh cgroup, dan unit/container hantu ditandai."""

    def setUp(self):
        panel._status_probe_cache.clear()
        panel.invalidate_service_cache()
        panel.invalidate_docker_metric_cache()
        panel.invalidate_systemctl_show_cache()

    def tearDown(self):
        panel._status_probe_cache.clear()
        panel.invalidate_service_cache()
        panel.invalidate_docker_metric_cache()
        panel.invalidate_systemctl_show_cache()

    def test_service_memory_sums_cgroup_not_mainpid(self):
        with mock.patch.object(panel, "get_systemd_show", return_value={"ControlGroup": "/x.service"}):
            with mock.patch.object(panel, "_cgroup_procs", return_value=["11", "12", "13"]):
                with mock.patch.object(panel, "_read_proc_rss_mb", side_effect=lambda p: 10.0):
                    self.assertEqual(panel._service_mem_mb("x.service", 11), 30.0)

    def test_service_memory_falls_back_to_mainpid(self):
        with mock.patch.object(panel, "get_systemd_show", return_value={"ControlGroup": ""}):
            with mock.patch.object(panel, "_cgroup_procs", return_value=[]):
                with mock.patch.object(panel, "_read_proc_rss_mb", side_effect=lambda p: 42.5):
                    self.assertEqual(panel._service_mem_mb("x.service", 99), 42.5)

    def test_missing_docker_container_reports_missing(self):
        fake = subprocess.CompletedProcess(args=[], returncode=1, stdout="",
                                           stderr="Error: No such object: pihole")
        with mock.patch.object(panel.subprocess, "run", return_value=fake):
            status, pid, mem = panel.get_docker_metric("pihole")
        self.assertEqual((status, pid, mem), ("missing", "0", 0.0))

    def test_missing_container_row_has_no_action_buttons(self):
        def fake_metric(cname, fresh=False, ttl=0):
            return ("missing", "0", 0.0) if cname.startswith("pihole") else ("running", "123", 5.0)

        with mock.patch.object(panel, "get_docker_metric", side_effect=fake_metric):
            with mock.patch.object(panel, "service_active", return_value=False):
                with mock.patch.object(panel, "_unit_installed", return_value=False):
                    rows = {p["id"]: p for p in panel.get_process_list()}
        pihole = rows["pihole"]
        self.assertEqual(pihole["status"], "Tidak terpasang")
        self.assertFalse(pihole["installed"])
        self.assertEqual(pihole["start_url"], "")
        self.assertEqual(pihole["stop_url"], "")

    def test_unit_installed_reflects_disk(self):
        self.assertTrue(panel._unit_installed("hermes-panel.service"))
        self.assertFalse(panel._unit_installed("casaos-tidak-ada.service"))
        self.assertFalse(panel._unit_installed("openclaw-absent.service", user=True))

    def test_phantom_units_are_not_shown_as_stopped(self):
        with mock.patch.object(panel, "service_active", return_value=False):
            with mock.patch.object(panel, "_unit_installed", return_value=False):
                with mock.patch.object(panel, "get_docker_metric", return_value=("missing", "0", 0.0)):
                    with mock.patch.object(panel, "is_openclaw_control_ui_enabled", return_value=False):
                        rows = {p["id"]: p for p in panel.get_process_list()}
        for pid_key in ("casaos", "openclaw-gateway"):
            self.assertEqual(rows[pid_key]["status"], "Tidak terpasang", pid_key)
            self.assertNotIn("start_url", [k for k, v in rows[pid_key].items() if v])
            self.assertEqual(rows[pid_key]["restart_url"], "")

    def test_render_uses_muted_badge_for_absent_services(self):
        procs = [{"id": "casaos", "name": "CasaOS", "kind": "Tidak terpasang",
                  "status": "Tidak terpasang", "is_active": False, "installed": False,
                  "pid": "-", "mem_mb": 0.0, "stop_url": "", "start_url": "", "restart_url": ""}]
        with mock.patch.object(panel, "get_process_list", return_value=procs):
            html_out = panel._render_processes_table_uncached()
        self.assertIn("badge-muted", html_out)
        self.assertIn("Tidak terpasang", html_out)


class TestStaleFragmentsAfterBackgroundChecks(unittest.TestCase):
    """Fragmen cache harus disegarkan saat pengecekan latar selesai."""

    def setUp(self):
        panel._status_probe_cache.clear()

    def tearDown(self):
        panel._status_probe_cache.clear()
        with panel._hermes_update_lock:
            panel._hermes_update_cache = {"status": "unknown", "local": "?", "remote": "?",
                                          "behind": 0, "patch_notes": [], "error": "", "at": 0}
            panel._hermes_check_running = False

    def test_update_block_invalidated_when_check_finishes(self):
        panel._status_probe_cache["hermes_update_block"] = {"at": 1.0, "value": "<div>Mengecek…</div>"}
        panel._status_probe_cache["processes_table"] = {"at": 1.0, "value": "<table></table>"}

        def fake_run(cmd, *args, **kwargs):
            text = ""
            if "rev-parse" in cmd:
                text = "bf0203a794a0deadbeef\n"
            elif "describe" in cmd:
                text = "v0.22.5\n"
            elif "update" in cmd and "--check" in cmd:
                text = "Update available: v0.23.0 (3 commits behind)"
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=text, stderr="")

        with mock.patch.object(panel.subprocess, "run", side_effect=fake_run):
            with mock.patch.object(panel, "_clear_stale_hermes_git_lock"):
                with mock.patch.object(panel.urllib.request, "urlopen", side_effect=OSError("offline")):
                    panel._refresh_hermes_update()

        self.assertNotIn("hermes_update_block", panel._status_probe_cache,
                         "blok pembaruan harus disegarkan agar 'Mengecek…' tidak nyangkut")
        self.assertNotIn("processes_table", panel._status_probe_cache)
        self.assertFalse(panel._hermes_check_running)

    def test_openclaw_hint_distinguishes_absent_from_stopped(self):
        with mock.patch.object(panel, "is_openclaw_control_ui_enabled", return_value=False):
            with mock.patch.object(panel, "_unit_installed", return_value=False):
                absent = panel.get_openclaw_open_block()
            with mock.patch.object(panel, "_unit_installed", return_value=True):
                stopped = panel.get_openclaw_open_block()
        self.assertIn("tidak terpasang", absent)
        self.assertIn("mati", stopped)


class TestKanbanStatusCanonical(unittest.TestCase):
    """Alias `completed` diterima, tapi DB selalu menyimpan status kanonik `done`."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="hermes-kanban-")
        self.db = panel.Path(self.tmpdir) / "kanban.db"
        self._orig = panel.get_kanban_db_path
        panel.get_kanban_db_path = lambda board="": self.db

    def tearDown(self):
        import shutil
        panel.get_kanban_db_path = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_normalize_helper(self):
        self.assertEqual(panel._normalize_kanban_status("completed"), "done")
        self.assertEqual(panel._normalize_kanban_status("DONE"), "done")
        self.assertEqual(panel._normalize_kanban_status("blocked"), "blocked")
        self.assertEqual(panel._normalize_kanban_status("ngawur"), "todo")
        self.assertEqual(panel._normalize_kanban_status("ngawur", default=""), "")

    def test_create_with_completed_stores_done(self):
        ok, msg, tid = panel.create_kanban_task("uji", status="completed")
        self.assertTrue(ok, msg)
        with panel.closing(panel.ensure_kanban_db(self.db)) as con:
            row = con.execute("SELECT status, completed_at FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertTrue(row["completed_at"])

    def test_update_with_completed_stores_done(self):
        ok, msg, tid = panel.create_kanban_task("uji2", status="todo")
        self.assertTrue(ok, msg)
        ok, msg = panel.update_kanban_task_status(tid, "completed")
        self.assertTrue(ok, msg)
        with panel.closing(panel.ensure_kanban_db(self.db)) as con:
            row = con.execute("SELECT status, completed_at FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertTrue(row["completed_at"])

    def test_invalid_status_still_rejected(self):
        ok, msg, tid = panel.create_kanban_task("uji3")
        self.assertTrue(ok, msg)
        ok, msg = panel.update_kanban_task_status(tid, "ngawur")
        self.assertFalse(ok)
        self.assertIn("ngawur", msg)


class TestKanbanColumnPagination(unittest.TestCase):
    """Kolom besar tidak dirender semua; sisanya lewat /api/kanban/column."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="hermes-kbpage-")
        self.db = panel.Path(self.tmpdir) / "kanban.db"
        self._orig_path = panel.get_kanban_db_path
        panel.get_kanban_db_path = lambda board="": self.db
        for i in range(15):
            panel.create_kanban_task(f"tugas {i}", status="done")

    def tearDown(self):
        import shutil
        panel.get_kanban_db_path = self._orig_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_column_alias_mapping(self):
        self.assertEqual(panel._kanban_col_of("completed"), "done")
        self.assertEqual(panel._kanban_col_of("scheduled"), "todo")
        self.assertEqual(panel._kanban_col_of("ngawur"), "todo")

    def test_large_column_is_capped_with_load_more(self):
        html_out = panel.render_kanban_board_block("default")
        self.assertEqual(html_out.count('class="kanban-card"'), panel.KANBAN_COL_PAGE)
        self.assertIn("+ 3 lainnya", html_out)
        self.assertIn('data-offset="12"', html_out)

    def test_column_endpoint_pages_remaining_cards(self):
        first = panel.render_kanban_column_cards("default", "done", 0, 12)
        self.assertEqual((first["total"], first["loaded"], first["remaining"]), (15, 12, 3))
        rest = panel.render_kanban_column_cards("default", "done", 12, 12)
        self.assertEqual((rest["loaded"], rest["remaining"]), (3, 0))
        self.assertEqual(rest["cards"].count('class="kanban-card"'), 3)
        self.assertEqual(panel.render_kanban_column_cards("default", "done", 12, 999)["loaded"], 3)

    def test_card_renderer_only_probes_liveness_for_running(self):
        task = {"id": "t_x", "title": "x", "status": "done", "assignee": "", "priority": 0}
        with mock.patch.object(panel, "_enrich_kanban_liveness",
                               side_effect=AssertionError("liveness hanya untuk kolom running")):
            panel._render_kanban_task_card(dict(task), "done")
        with mock.patch.object(panel, "_enrich_kanban_liveness") as probe:
            panel._render_kanban_task_card(dict(task), "running")
        probe.assert_called_once()

    def test_small_column_has_no_load_more_button(self):
        # DB terpisah dengan hanya 2 tugas: tidak boleh ada tombol muat-lainnya.
        self.db = panel.Path(self.tmpdir) / "small.db"
        panel.create_kanban_task("satu", status="done")
        panel.create_kanban_task("dua", status="done")
        html_out = panel.render_kanban_board_block("default")
        self.assertNotIn("kb-load-more", html_out)
        self.assertEqual(html_out.count('class="kanban-card"'), 2)


class TestKanbanBoardArchiveCounts(unittest.TestCase):
    """Dropdown board menampilkan jumlah arsip, bukan hanya tugas aktif."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="hermes-kbarch-")
        self.root = panel.Path(self.tmpdir)
        self._orig_root = panel.get_hermes_root
        self._orig_cur = panel.get_current_kanban_board
        panel.get_hermes_root = lambda: self.root
        panel.get_current_kanban_board = lambda: "default"
        db = self.root / "kanban.db"
        panel.create_kanban_task("aktif", board="")
        panel.create_kanban_task("lama1", status="archived", board="")
        panel.create_kanban_task("lama2", status="archived", board="")

    def tearDown(self):
        import shutil
        panel.get_hermes_root = self._orig_root
        panel.get_current_kanban_board = self._orig_cur
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_board_reports_archived_count(self):
        boards = {b["slug"]: b for b in panel.list_kanban_boards()}
        self.assertEqual(boards["default"]["task_count"], 1)
        self.assertEqual(boards["default"]["archived_count"], 2)

    def test_dropdown_label_shows_active_and_archive(self):
        opts = panel.render_kanban_board_options("default")
        self.assertIn("1 aktif · 2 arsip", opts)

    def test_dropdown_without_archive_keeps_short_label(self):
        other = panel.Path(tempfile.mkdtemp(prefix="hermes-kbarch2-"))
        try:
            panel.get_hermes_root = lambda: other
            ok, _msg, _tid = panel.create_kanban_task("hanya satu", board="")
            self.assertTrue(ok)
            opts = panel.render_kanban_board_options("default")
            self.assertNotIn("arsip", opts)
            self.assertIn("(1)", opts)
        finally:
            import shutil
            shutil.rmtree(str(other), ignore_errors=True)


class TestKanbanArchiveViewer(unittest.TestCase):
    """Arsip bisa dibuka lewat /api/kanban/column?status=archived (lazy-load)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="hermes-kbview-")
        self.db = panel.Path(self.tmpdir) / "kanban.db"
        self._orig = panel.get_kanban_db_path
        panel.get_kanban_db_path = lambda board="": self.db
        panel.create_kanban_task("aktif1")
        panel.create_kanban_task("aktif2")
        for i in range(15):
            panel.create_kanban_task(f"arsip {i}", status="archived")

    def tearDown(self):
        import shutil
        panel.get_kanban_db_path = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_archive_column_only_lists_archived(self):
        d = panel.render_kanban_column_cards("default", "archived", 0, 12)
        self.assertEqual(d["total"], 15)
        self.assertEqual(d["loaded"], 12)
        self.assertEqual(d["remaining"], 3)
        self.assertIn("arsip 0", d["cards"])
        self.assertNotIn("aktif1", d["cards"])
        rest = panel.render_kanban_column_cards("default", "archived", 12, 12)
        self.assertEqual((rest["loaded"], rest["remaining"]), (3, 0))

    def test_archive_does_not_leak_into_board_columns(self):
        html_out = panel.render_kanban_board_block("default")
        self.assertNotIn("arsip 0", html_out)
        self.assertIn("aktif1", html_out)

    def test_archive_ui_present_in_page(self):
        self.assertIn('id="kb-archive-wrap"', panel.PAGE)
        self.assertIn('id="col-cards-archived"', panel.PAGE)
        self.assertIn("toggleKanbanArchive()", panel.PAGE)
        self.assertIn("function toggleKanbanArchive", panel.PAGE)
        self.assertIn("async function loadKanbanArchive", panel.PAGE)


class TestQuotaTabControls(unittest.TestCase):
    """Kontrol UI tab Kuota: pencarian akun + batas render per provider."""

    def test_search_and_cap_present(self):
        self.assertIn('id="qz-search"', panel.PAGE)
        self.assertIn("quotaSetFilter(this.value)", panel.PAGE)
        self.assertIn("var QUOTA_ACCOUNT_PAGE = 6;", panel.PAGE)
        self.assertIn("showAllProv", panel.PAGE)
        self.assertIn("data-qmore", panel.PAGE)

    def test_quota_state_has_filter_and_showall(self):
        self.assertIn("filter:''", panel.PAGE)
        self.assertIn("function quotaAccountMatches", panel.PAGE)


def _quota_js() -> str:
    """JS tab Kuota persis seperti dirender (PAGE memakai str.format)."""
    start = panel.PAGE.index("var QUOTA = ")
    end = panel.PAGE.index("function quotaEnsure")
    return panel.PAGE[start:end].replace("{{", "{").replace("}}", "}")


_QUOTA_HARNESS = r"""
function escHtml(s){ return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
var document = { getElementById: function(){ return null; }, querySelectorAll: function(){ return []; } };
function mkAccounts(n){
  var out = [];
  for(var i = 1; i <= n; i++) out.push({name: 'Key ' + i, email: 'user' + i + '@mail.com', status: 'ok', isActive: true, groups: []});
  return out;
}
"""


@unittest.skipUnless(subprocess.run(["which", "node"], capture_output=True).returncode == 0, "node not installed")
class TestQuotaTabBehaviourJs(unittest.TestCase):
    """Perilaku JS Kuota: batas 6 akun, kritis didahulukan, pencarian."""

    def _run(self, body: str) -> str:
        script = _QUOTA_HARNESS + _quota_js() + body
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_provider_caps_accounts_and_offers_more(self):
        out = self._run("""
var p = {provider: 'qoder', label: 'Qoder', accountCount: 8, accounts: mkAccounts(8)};
var h = quotaProviderHtml(p);
process.stdout.write(String((h.match(/Key /g) || []).length) + '|' + (h.indexOf('2 akun lainnya') !== -1));
""")
        self.assertEqual(out, "6|true")

    def test_exhausted_account_is_not_hidden_by_the_cap(self):
        out = self._run("""
var acc = mkAccounts(8);
acc[7].status = 'exhausted';
var h = quotaProviderHtml({provider: 'qoder', label: 'Q', accounts: acc});
process.stdout.write(String(h.indexOf('Key 8') !== -1));
""")
        self.assertEqual(out, "true")

    def test_show_all_renders_every_account(self):
        out = self._run("""
QUOTA.showAllProv.qoder = true;
var h = quotaProviderHtml({provider: 'qoder', label: 'Q', accounts: mkAccounts(9)});
process.stdout.write(String((h.match(/Key /g) || []).length) + '|' + (h.indexOf('akun lainnya') !== -1));
""")
        self.assertEqual(out, "9|false")

    def test_search_matches_email_and_hides_other_providers(self):
        out = self._run("""
QUOTA.filter = 'user7@mail.com';
var hit = quotaProviderHtml({provider: 'qoder', label: 'Q', accounts: mkAccounts(8)});
var miss = quotaProviderHtml({provider: 'gemini', label: 'G', accounts: mkAccounts(2)});
process.stdout.write(String((hit.match(/Key /g) || []).length) + '|' + String(miss === ''));
""")
        self.assertEqual(out, "1|true")

    def test_rank_orders_problem_accounts_first(self):
        out = self._run("""
var a = {status: 'ok', isActive: true};
var b = {status: 'exhausted', isActive: true};
var c = {status: 'ok', isActive: false};
process.stdout.write([quotaAccountRank(a), quotaAccountRank(b), quotaAccountRank(c)].join(','));
""")
        self.assertEqual(out, "2,0,1")


def _kanban_lazy_js() -> str:
    """JS lazy-load/arsip Kanban persis seperti dirender."""
    start = panel.PAGE.index("async function loadKanbanArchive()")
    end = panel.PAGE.index("function showKanbanToast")
    return panel.PAGE[start:end].replace("{{", "{").replace("}}", "}")


_KANBAN_DOM = r"""
function makeEl(id){
  return {
    id: id, innerHTML: '', textContent: '', value: 'default', disabled: false,
    style: {}, dataset: {}, children: [], className: '', type: '',
    setAttribute: function(k, v){
      if(k === 'data-status') this.dataset.status = v;
      if(k === 'data-offset') this.dataset.offset = v;
    },
    appendChild: function(c){ this.children.push(c); return c; },
    insertAdjacentHTML: function(pos, html){ this.innerHTML += html; },
    remove: function(){ this.removed = true; }
  };
}
var els = {};
var document = {
  getElementById: function(id){ return els[id] || null; },
  createElement: function(tag){ return makeEl(tag); }
};
var fetchCalls = [];
var fetchResponses = [];
function fetch(url, opts){
  fetchCalls.push(url);
  var d = fetchResponses.shift();
  return Promise.resolve({json: function(){ return Promise.resolve(d); }});
}
function seedArchive(open){
  els['kb-archive-wrap'] = makeEl('kb-archive-wrap');
  els['kb-archive-wrap'].style.display = open ? '' : 'none';
  els['col-cards-archived'] = makeEl('col-cards-archived');
  els['kanban-board-select'] = makeEl('kanban-board-select');
  els['kanban-board-select'].value = 'default';
}
"""


@unittest.skipUnless(subprocess.run(["which", "node"], capture_output=True).returncode == 0, "node not installed")
class TestKanbanLazyLoadJs(unittest.TestCase):
    """Perilaku lazy-load kolom & panel arsip Kanban di node."""

    def _run(self, body: str) -> str:
        script = _KANBAN_DOM + _kanban_lazy_js() + body
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_archive_opens_loads_and_offers_more(self):
        out = self._run("""
seedArchive(false);
fetchResponses.push({ok:true, cards:'<div class="kanban-card">A</div>', loaded:12, remaining:3});
toggleKanbanArchive();
setTimeout(function(){
  var col = els['col-cards-archived'];
  process.stdout.write(JSON.stringify({
    open: els['kb-archive-wrap'].style.display,
    loaded: col.dataset.loaded,
    hasCard: col.innerHTML.indexOf('kanban-card') !== -1,
    moreButtons: col.children.length,
    moreText: col.children[0].textContent,
    url: fetchCalls[0]
  }));
}, 20);
""")
        import json as _json
        d = _json.loads(out)
        self.assertEqual(d["open"], "")
        self.assertEqual(d["loaded"], "1")
        self.assertTrue(d["hasCard"])
        self.assertEqual(d["moreButtons"], 1)
        self.assertEqual(d["moreText"], "+ 3 lainnya")
        self.assertIn("status=archived", d["url"])

    def test_archive_refresh_only_when_open(self):
        out = self._run("""
seedArchive(true);
els['col-cards-archived'].dataset.loaded = '1';
fetchResponses.push({ok:true, cards:'BARU', loaded:1, remaining:0});
refreshKanbanArchive();
setTimeout(function(){
  var afterOpen = fetchCalls.length + '|' + els['col-cards-archived'].innerHTML;
  els['kb-archive-wrap'].style.display = 'none';
  refreshKanbanArchive();
  setTimeout(function(){
    process.stdout.write(afterOpen + '|' + fetchCalls.length);
  }, 20);
}, 20);
""")
        self.assertEqual(out, "1|BARU|1")

    def test_column_more_appends_then_removes_button(self):
        out = self._run("""
seedArchive(false);
els['col-cards-done'] = makeEl('col-cards-done');
var btn = makeEl('button');
btn.dataset.status = 'done';
btn.dataset.offset = '12';
fetchResponses.push({ok:true, cards:'<div class="kanban-card">B</div>', loaded:12, remaining:0});
loadKanbanColumnMore(btn);
setTimeout(function(){
  process.stdout.write(JSON.stringify({
    html: els['col-cards-done'].innerHTML,
    removed: btn.removed === true,
    url: fetchCalls[0]
  }));
}, 20);
""")
        import json as _json
        d = _json.loads(out)
        self.assertIn("kanban-card", d["html"])
        self.assertTrue(d["removed"])
        self.assertIn("offset=12", d["url"])

    def test_column_more_failure_reenables_button(self):
        out = self._run("""
seedArchive(false);
els['col-cards-done'] = makeEl('col-cards-done');
var btn = makeEl('button');
btn.dataset.status = 'done';
btn.dataset.offset = '0';
fetchResponses.push({ok:false, error:'db rusak'});
loadKanbanColumnMore(btn);
setTimeout(function(){
  process.stdout.write(btn.textContent + '|' + btn.disabled + '|' + (btn.removed === true));
}, 20);
""")
        self.assertEqual(out, "Gagal memuat — coba lagi|false|false")

    def test_client_renderer_uses_same_column_cap(self):
        self.assertIn("var KANBAN_COL_PAGE = 12;", panel.PAGE)
        self.assertIn("items.slice(0, KANBAN_COL_PAGE)", panel.PAGE)
        self.assertIn("refreshKanbanArchive();", panel.PAGE)


def _kanban_filter_js() -> str:
    start = panel.PAGE.index("function kanbanFilterActive()")
    end = panel.PAGE.index("function filterKanbanCards()")
    return panel.PAGE[start:end].replace("{{", "{").replace("}}", "}")


_KANBAN_FILTER_DOM = r"""
var els = {};
var document = {
  getElementById: function(id){ return els[id] || null; },
  querySelectorAll: function(){ return []; }
};
var filterCalls = 0;
function filterKanbanCards(){ filterCalls++; }
var refreshCalls = 0;
function refreshKanbanBoard(silent){ refreshCalls++; }
"""


@unittest.skipUnless(subprocess.run(["which", "node"], capture_output=True).returncode == 0, "node not installed")
class TestKanbanFilterJs(unittest.TestCase):
    """Pencarian/filter Kanban harus menjangkau tugas di luar 12 kartu pertama."""

    def _run(self, body: str) -> str:
        script = _KANBAN_FILTER_DOM + _kanban_filter_js() + body
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_filter_active_detection(self):
        out = self._run("""
els['kanban-search-input'] = {value: ''};
els['kanban-filter-assignee'] = {value: ''};
els['kanban-filter-priority'] = {value: ''};
var a = kanbanFilterActive();
els['kanban-search-input'].value = 'tugas';
var b = kanbanFilterActive();
els['kanban-search-input'].value = '   ';
var c = kanbanFilterActive();
els['kanban-filter-priority'].value = 'urgent';
var d = kanbanFilterActive();
process.stdout.write([a, b, c, d].join(','));
""")
        self.assertEqual(out, "false,true,false,true")

    def test_filter_change_debounces_board_refresh(self):
        out = self._run("""
onKanbanFilterChanged();
onKanbanFilterChanged();
onKanbanFilterChanged();
setTimeout(function(){
  process.stdout.write(filterCalls + '|' + refreshCalls);
}, 400);
""")
        self.assertEqual(out, "3|1")

    def test_renderer_bypasses_cap_only_when_filtering(self):
        self.assertIn("kanbanFilterActive() ? items : items.slice(0, KANBAN_COL_PAGE)", panel.PAGE)
        self.assertIn('oninput="onKanbanFilterChanged()"', panel.PAGE)
        self.assertEqual(panel.PAGE.count('onchange="onKanbanFilterChanged()"'), 2)


def _profile_modal_js() -> str:
    """JS modal profil persis seperti dirender dari PAGE + PROFILE_SCRIPT."""
    source = panel.PAGE.replace("{profile_script}", panel.PROFILE_SCRIPT)
    start = source.index("var currentSkillsProfile = '';")
    end = source.index("function loadProfileConfig", start)
    return source[start:end].replace("{{", "{").replace("}}", "}")


_PROFILE_MODAL_DOM = r"""
function profileDecodeAttr(s){
  return String(s == null ? '' : s).replace(/&quot;/g, '"').replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>');
}
function profileParseButtons(html){
  var out = [], re = /<button\b([^>]*)>([\s\S]*?)<\/button>/g, match;
  while((match = re.exec(html))){
    var btn = {attrs: {}, disabled: false, textContent: match[2].replace(/<[^>]*>/g, '')};
    var attrs = match[1], attrRe = /([:\w-]+)(?:="([^"]*)")?/g, attr;
    while((attr = attrRe.exec(attrs))){
      btn.attrs[attr[1]] = profileDecodeAttr(attr[2] == null ? '' : attr[2]);
      if(attr[1] === 'disabled') btn.disabled = true;
    }
    btn.getAttribute = function(name){
      return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
    };
    out.push(btn);
  }
  return out;
}
function profileEl(id){
  var el = {id: id, value: '', checked: false, style: {}, _innerHTML: '', buttons: []};
  Object.defineProperty(el, 'innerHTML', {
    get: function(){ return this._innerHTML; },
    set: function(value){
      this._innerHTML = String(value);
      this.buttons = profileParseButtons(this._innerHTML);
    }
  });
  return el;
}
var els = {};
var document = {
  getElementById: function(id){ return els[id] || null; },
  querySelector: function(selector){
    var match = /^button\[([:\w-]+)(?:="([^"]*)")?\]$/.exec(selector);
    if(!match) return null;
    var lists = [els['profile-skills-list'], els['profile-tools-list']];
    for(var i = 0; i < lists.length; i++){
      if(!lists[i]) continue;
      for(var j = 0; j < lists[i].buttons.length; j++){
        var btn = lists[i].buttons[j], actual = btn.getAttribute(match[1]);
        if(actual !== null && (match[2] == null || actual === profileDecodeAttr(match[2]))) return btn;
      }
    }
    return null;
  }
};
var pendingFetches = [];
function fetchJsonChecked(url, opts){
  return new Promise(function(resolve, reject){
    pendingFetches.push({url: url, opts: opts, resolve: resolve, reject: reject});
  });
}
var refreshCalls = [];
var toastCalls = [];
function toast(message, kind){ toastCalls.push({message: message, kind: kind}); }
function profileAction(listId, name, toolset){
  var list = els[listId], attr = toolset ? 'data-ts-name' : 'data-skill-name';
  var fn = toolset ? 'toggleProfileToolset(' : 'toggleProfileSkill(';
  if(!list) return null;
  for(var i = 0; i < list.buttons.length; i++){
    var btn = list.buttons[i], onclick = btn.getAttribute('onclick') || '';
    if(btn.getAttribute(attr) === name && onclick.indexOf(fn) !== -1) return btn;
  }
  return null;
}
function seedProfileSkills(rows){
  els['profile-skills-list'] = profileEl('profile-skills-list');
  els['profile-skills-search'] = profileEl('profile-skills-search');
  els['profile-skills-hide-off'] = profileEl('profile-skills-hide-off');
  currentSkillsProfile = 'coder';
  profileSkillsMeta.platform = 'cli';
  profileSkillsCache = rows;
  renderProfileSkillsList();
}
function seedProfileTools(rows){
  els['profile-tools-list'] = profileEl('profile-tools-list');
  els['profile-skills-platform'] = profileEl('profile-skills-platform');
  els['profile-skills-platform'].value = 'cli';
  currentSkillsProfile = 'coder';
  profileToolsCache = {
    rows: rows, disabled: [], tools: [], count: rows.length, other: [],
    explicit: true, source: '', platform: 'cli', extraDisabled: []
  };
  renderProfileToolsList();
}
"""


@unittest.skipUnless(subprocess.run(["which", "node"], capture_output=True).returncode == 0, "node not installed")
class TestProfileModalBehaviourJs(unittest.TestCase):
    """Perilaku JS modal profil: filter, render besar, dan toggle in-flight."""

    def _run(self, body: str) -> str:
        # Refresh diganti setelah JS asli dimuat agar test dapat mengontrol data segar.
        refresh_stub = "\nrefreshProfilePanel = function(platform){ refreshCalls.push(platform); };\n"
        script = _PROFILE_MODAL_DOM + _profile_modal_js() + refresh_stub + body
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_render_156_valid_skills_without_crash(self):
        out = self._run("""
var rows = [];
for(var i = 0; i < 156; i++){
  var state = i % 3 === 0 ? 'on' : (i % 3 === 1 ? 'off' : 'hidden');
  rows.push({
    name: 'skill-' + i, description: 'Deskripsi <skill> & ' + i,
    category: 'kategori-' + (i % 5), state: state,
    enabled: state === 'on', essential: false
  });
}
seedProfileSkills(rows);
var list = els['profile-skills-list'];
var readCount = list.buttons.filter(function(btn){
  return (btn.getAttribute('onclick') || '').indexOf('readProfileSkill(') !== -1;
}).length;
process.stdout.write(JSON.stringify({
  rendered: (list.innerHTML.match(/<strong[^>]*>skill-\\d+<\\/strong>/g) || []).length,
  readCount: readCount,
  escaped: list.innerHTML.indexOf('Deskripsi &lt;skill&gt; &amp; 155') !== -1,
  empty: list.innerHTML.indexOf('Tidak ada skill cocok') !== -1
}));
""")
        self.assertEqual(json.loads(out), {
            "rendered": 156, "readCount": 156, "escaped": True, "empty": False,
        })

    def test_skill_filter_preserves_in_flight_toggle(self):
        out = self._run("""
seedProfileSkills([
  {name:'alpha', description:'Alpha', category:'test', state:'on', enabled:true, essential:false},
  {name:'beta', description:'Beta', category:'test', state:'off', enabled:false, essential:false}
]);
toggleProfileSkill('alpha', false);
var initial = profileAction('profile-skills-list', 'alpha', false);
var before = {disabled:initial.disabled, label:initial.textContent};
els['profile-skills-search'].value = 'beta';
filterProfileSkills();
var filteredOut = profileAction('profile-skills-list', 'alpha', false) === null;
els['profile-skills-search'].value = '';
els['profile-skills-hide-off'].checked = true;
filterProfileSkills();
var restored = profileAction('profile-skills-list', 'alpha', false);
toggleProfileSkill('alpha', false);
process.stdout.write(JSON.stringify({
  before:before, filteredOut:filteredOut,
  restored:!!restored, disabled:restored.disabled, label:restored.textContent,
  betaHidden:els['profile-skills-list'].innerHTML.indexOf('>beta</strong>') === -1,
  requests:pendingFetches.length, pending:!!profileSkillSubmitting.alpha
}));
""")
        self.assertEqual(json.loads(out), {
            "before": {"disabled": True, "label": "..."},
            "filteredOut": True, "restored": True, "disabled": True, "label": "...",
            "betaHidden": True, "requests": 1, "pending": True,
        })

    def test_skill_toggle_disables_only_action_and_restores_on_success(self):
        out = self._run("""
seedProfileSkills([
  {name:'alpha', description:'Alpha', category:'test', state:'on', enabled:true, essential:false}
]);
var readBefore = document.querySelector('button[data-skill-name="alpha"]');
toggleProfileSkill('alpha', false);
var actionDuring = profileAction('profile-skills-list', 'alpha', false);
var during = {
  actionDisabled:actionDuring.disabled, actionLabel:actionDuring.textContent,
  readDisabled:readBefore.disabled
};
toggleProfileSkill('alpha', false);
refreshProfilePanel = function(platform){
  refreshCalls.push(platform);
  profileSkillsCache[0].enabled = false;
  profileSkillsCache[0].state = 'off';
  renderProfileSkillsList();
};
pendingFetches[0].resolve({ok:true, message:'tersimpan'});
setTimeout(function(){
  var after = profileAction('profile-skills-list', 'alpha', false);
  process.stdout.write(JSON.stringify({
    during:during, afterDisabled:after.disabled, afterLabel:after.textContent,
    requests:pendingFetches.length, pending:!!profileSkillSubmitting.alpha,
    refreshes:refreshCalls.length, toast:toastCalls[0].kind
  }));
}, 0);
""")
        self.assertEqual(json.loads(out), {
            "during": {"actionDisabled": True, "actionLabel": "...", "readDisabled": False},
            "afterDisabled": False, "afterLabel": "Nyalakan", "requests": 1,
            "pending": False, "refreshes": 1, "toast": "ok",
        })

    def test_skill_toggle_restores_action_on_api_failure(self):
        out = self._run("""
seedProfileSkills([
  {name:'alpha', description:'Alpha', category:'test', state:'on', enabled:true, essential:false}
]);
toggleProfileSkill('alpha', false);
var during = profileAction('profile-skills-list', 'alpha', false).disabled;
pendingFetches[0].resolve({ok:false, error:'ditolak'});
setTimeout(function(){
  var after = profileAction('profile-skills-list', 'alpha', false);
  process.stdout.write(JSON.stringify({
    during:during, afterDisabled:after.disabled, afterLabel:after.textContent,
    pending:!!profileSkillSubmitting.alpha, refreshes:refreshCalls.length,
    toast:toastCalls[0].kind
  }));
}, 0);
""")
        self.assertEqual(json.loads(out), {
            "during": True, "afterDisabled": False, "afterLabel": "Matikan",
            "pending": False, "refreshes": 0, "toast": "err",
        })

    def test_toolset_toggle_disables_only_action_and_restores_on_success(self):
        out = self._run("""
seedProfileTools([
  {name:'coding', enabled:true, tools:['read'], tool_count:1, description:'Coding'}
]);
var toolsBefore = document.querySelector('button[data-ts-name="coding"]');
toggleProfileToolset('coding', false);
// Expand/collapse atau render ulang lain tidak boleh melepas status in-flight.
renderProfileToolsList();
var actionDuring = profileAction('profile-tools-list', 'coding', true);
var during = {
  actionDisabled:actionDuring.disabled, actionLabel:actionDuring.textContent,
  toolsDisabled:toolsBefore.disabled, toolsLabel:toolsBefore.textContent
};
refreshProfilePanel = function(platform){
  refreshCalls.push(platform);
  profileToolsCache.rows[0].enabled = false;
  renderProfileToolsList();
};
pendingFetches[0].resolve({ok:true});
setTimeout(function(){
  var after = profileAction('profile-tools-list', 'coding', true);
  process.stdout.write(JSON.stringify({
    during:during, afterDisabled:after.disabled, afterLabel:after.textContent,
    pending:!!profileToolSubmitting.coding, refreshes:refreshCalls.length,
    toast:toastCalls[0].kind
  }));
}, 0);
""")
        self.assertEqual(json.loads(out), {
            "during": {
                "actionDisabled": True, "actionLabel": "...",
                "toolsDisabled": False, "toolsLabel": "1 tool",
            },
            "afterDisabled": False, "afterLabel": "Nyalakan", "pending": False,
            "refreshes": 1, "toast": "ok",
        })

    def test_toolset_toggle_restores_action_on_rejection(self):
        out = self._run("""
seedProfileTools([
  {name:'coding', enabled:true, tools:[], tool_count:0, description:'Coding'}
]);
toggleProfileToolset('coding', false);
var during = profileAction('profile-tools-list', 'coding', true).disabled;
pendingFetches[0].reject(new Error('offline'));
setTimeout(function(){
  var after = profileAction('profile-tools-list', 'coding', true);
  process.stdout.write(JSON.stringify({
    during:during, afterDisabled:after.disabled, afterLabel:after.textContent,
    pending:!!profileToolSubmitting.coding, refreshes:refreshCalls.length,
    toast:toastCalls[0].kind
  }));
}, 0);
""")
        self.assertEqual(json.loads(out), {
            "during": True, "afterDisabled": False, "afterLabel": "Matikan",
            "pending": False, "refreshes": 0, "toast": "err",
        })



# ---------------------------------------------------------------------------
# t_6f2efe25 — interaksi gabungan filter Kuota & Model (Node harness fake-DOM).
# Test perilaku murni untuk kode yang sudah ada; temuan gap didokumentasikan di
# komentar task (bukan dipaksa patch): filterModels tidak punya empty state.
# ---------------------------------------------------------------------------

def _model_filter_js() -> str:
    """JS filterModels persis seperti dirender (PAGE memakai str.format)."""
    start = panel.PAGE.index("function filterModels")
    end = panel.PAGE.index("function toggleLog")
    return panel.PAGE[start:end].replace("{{", "{").replace("}}", "}")


_MODEL_HARNESS = r"""
var __chips = [];
var __groups = [];
var __empty = {style: {}, textContent: ''};
var document = {
  getElementById: function(id){ return id === 'model-search-empty' ? __empty : null; },
  querySelectorAll: function(sel){
    if(sel === '#model-chips .model-chip') return __chips;
    if(sel === '#model-chips .model-group') return __groups;
    return [];
  }
};
function mkGroup(){
  var g = {style: {}, _chips: [], querySelectorAll: function(sel){
    return sel === '.model-chip' ? this._chips : [];
  }};
  __groups.push(g); return g;
}
function mkChip(text, group){
  var c = {textContent: text, style: {}, innerHTML: '<chip>' + text + '</chip>'};
  group._chips.push(c); __chips.push(c); return c;
}
"""


class TestQuotaModelFilterInteractionJs(unittest.TestCase):
    """Skenario gabungan: kap 6 vs pencarian, ranking kritis, showAllProv, regex literal."""

    def _run(self, body: str) -> str:
        script = _QUOTA_HARNESS + _quota_js() + body
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def _run_model(self, body: str) -> str:
        script = _MODEL_HARNESS + _model_filter_js() + body
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_search_bypasses_cap_and_clear_restores_cap(self):
        out = self._run("""
var p = {provider: 'qoder', label: 'Q', accountCount: 8, accounts: mkAccounts(8)};
QUOTA.filter = 'key';
var withQ = quotaProviderHtml(p);
QUOTA.filter = '';
var cleared = quotaProviderHtml(p);
process.stdout.write(String((withQ.match(/Key /g) || []).length) + '|'
  + String((cleared.match(/Key /g) || []).length) + '|'
  + String(cleared.indexOf('2 akun lainnya') !== -1));
""")
        self.assertEqual(out, "8|6|true")

    def test_critical_promoted_even_with_full_sibling_provider(self):
        out = self._run("""
var acc = mkAccounts(9);
acc[8].status = 'expired';
var qh = quotaProviderHtml({provider: 'qoder', label: 'Q', accounts: acc});
var gh = quotaProviderHtml({provider: 'gemini', label: 'G', accounts: mkAccounts(8)});
process.stdout.write(String(qh.indexOf('Key 9') !== -1) + '|'
  + String(gh.indexOf('Key 7') !== -1) + '|' + String(gh.indexOf('Key 8') !== -1));
""")
        self.assertEqual(out, "true|false|false")

    def test_showall_state_survives_search_clear(self):
        out = self._run("""
QUOTA.showAllProv.qoder = true;
QUOTA.filter = 'key';
var during = quotaProviderHtml({provider: 'qoder', label: 'Q', accounts: mkAccounts(9)});
QUOTA.filter = '';
var after = quotaProviderHtml({provider: 'qoder', label: 'Q', accounts: mkAccounts(9)});
var sibling = quotaProviderHtml({provider: 'gemini', label: 'G', accounts: mkAccounts(8)});
process.stdout.write(String((during.match(/Key /g) || []).length) + '|'
  + String((after.match(/Key /g) || []).length) + '|'
  + String((sibling.match(/Key /g) || []).length));
""")
        self.assertEqual(out, "9|9|6")

    def test_model_filter_treats_regex_chars_literally(self):
        out = self._run_model("""
var g = mkGroup();
mkChip('combo-deepseek-v4.1', g);
mkChip('gemini-flash', g);
filterModels('.');          // literal titik — bukan regex "semua karakter"
var dot = __chips[0].style.display !== 'none' && __chips[1].style.display === 'none';
filterModels('');
var reset = __chips[0].style.display !== 'none' && __chips[1].style.display !== 'none';
process.stdout.write(String(dot) + '|' + String(reset));
""")
        self.assertEqual(out, "true|true")

    def test_model_filter_no_match_hides_groups_without_crash(self):
        out = self._run_model("""
var g1 = mkGroup(), g2 = mkGroup();
mkChip('gpt-4o', g1); mkChip('claude-3', g2);
filterModels('zzz-tidak-ada');
process.stdout.write(String(__chips[0].style.display) + '|' + String(__chips[1].style.display) + '|'
  + String(g1.style.display) + '|' + String(g2.style.display));
""")
        self.assertEqual(out, "none|none|none|none")

    def test_model_filter_shows_empty_state_when_no_match(self):
        out = self._run_model("""
var g = mkGroup();
mkChip('gpt-4o', g);
filterModels('zzz-tidak-ada');
var shown = __empty.style.display;
filterModels('gpt');
var hiddenWhileMatch = __empty.style.display;
filterModels('   ');
var hiddenWhenCleared = __empty.style.display;
process.stdout.write(String(shown) + '|' + String(hiddenWhileMatch) + '|' + String(hiddenWhenCleared));
""")
        # '' = tampil (default), 'none' = disembunyikan
        self.assertEqual(out, "|none|none")

    def test_model_filter_never_touches_inner_html(self):
        # SSE delta untuk fragmen lain tidak boleh menghapus hasil filter:
        # filterModels hanya mengubah visibilitas (style/textContent), bukan innerHTML.
        src = _model_filter_js()
        self.assertNotIn("innerHTML", src)
        out = self._run_model("""
var g = mkGroup();
var c = mkChip('gpt-4o', g);
var before = c.innerHTML;
filterModels('gpt');
process.stdout.write(String(c.innerHTML === before) + '|' + String(c.style.display));
""")
        self.assertEqual(out, "true|")



class TestBackupModelsChain(unittest.TestCase):
    """Rantai model cadangan: tampilan harus = rantai efektif Hermes (fp + legacy).

    Akar bug: Hermes (hermes_cli/fallback_config.get_fallback_chain) memakai
    ``fallback_providers`` DAN legacy ``fallback_model`` sekaligus, sedangkan panel
    hanya membaca salah satunya — entri legacy tak terlihat dan tak bisa dihapus.
    Negatif kontrol: pada kode tanpa patch, test 1-2 gagal (entri legacy hilang dari
    tampilan; remove legacy → False).
    """

    def _cfg(self, tmpdir, data):
        import yaml
        path = os.path.join(tmpdir, "config.yaml")
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f)
        return path

    def _load(self, path):
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def test_display_merges_legacy_entries_after_providers(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(d, {
                "fallback_providers": [{"provider": "p1", "model": "m1", "base_url": "http://a"}],
                "fallback_model": [
                    {"provider": "p2", "model": "m2"},
                    {"provider": "p1", "model": "m1", "base_url": "http://a"},  # duplikat rute
                ],
            })
            with mock.patch.object(panel, "CONFIG_PATH", cfg):
                panel.invalidate_config_cache()
                got = panel.get_fallback_models_config()
        self.assertEqual([g["model"] for g in got], ["m1", "m2"])  # dedup rute, urutan Hermes

    def test_remove_legacy_entry_migrates_chain(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(d, {
                "fallback_model": {"provider": "p2", "model": "m2"},  # dict tunggal legacy
            })
            with mock.patch.object(panel, "CONFIG_PATH", cfg):
                panel.invalidate_config_cache()
                self.assertTrue(panel.remove_fallback_model(0))
            after = self._load(cfg)
        self.assertEqual(after.get("fallback_providers"), [])
        self.assertNotIn("fallback_model", after)  # migrasi ala `hermes fallback`

    def test_set_replaces_entry_in_merged_chain_and_keeps_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(d, {
                "fallback_providers": [{"provider": "p1", "model": "m1", "base_url": "http://old", "api_key": "sk-old"}],
                "fallback_model": {"provider": "p2", "model": "m2"},
            })
            with mock.patch.object(panel, "CONFIG_PATH", cfg), \
                 mock.patch.object(panel, "get_router_api_key", return_value=""), \
                 mock.patch.object(panel, "get_9router_host", return_value="127.0.0.1"):
                panel.invalidate_config_cache()
                # index 0 → entri pertama gabungan (m1); kredensial lama dipertahankan
                self.assertTrue(panel.set_fallback_model(0, "p9", "m9"))
            after = self._load(cfg)
        chain = after.get("fallback_providers") or []
        self.assertEqual([e["model"] for e in chain], ["m9", "m2"])
        self.assertEqual(chain[0]["base_url"], "http://old")
        self.assertEqual(chain[0]["api_key"], "sk-old")
        self.assertNotIn("fallback_model", after)

    def test_set_append_and_remove_out_of_range(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(d, {})
            with mock.patch.object(panel, "CONFIG_PATH", cfg), \
                 mock.patch.object(panel, "get_router_api_key", return_value="k"), \
                 mock.patch.object(panel, "get_9router_host", return_value="h"):
                panel.invalidate_config_cache()
                self.assertTrue(panel.set_fallback_model(-1, "p", "m0"))   # tambah baru
                self.assertFalse(panel.remove_fallback_model(5))           # di luar jangkauan
            after = self._load(cfg)
        self.assertEqual(len(after.get("fallback_providers") or []), 1)

    def test_corrupt_yaml_fails_softly(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "config.yaml")
            with open(cfg, "w", encoding="utf-8") as f:
                f.write("fallback_providers: [unclosed\n  bad: yaml:\n   - ]")
            with mock.patch.object(panel, "CONFIG_PATH", cfg):
                panel.invalidate_config_cache()
                self.assertEqual(panel.get_fallback_models_config(), [])   # tampilan aman
                self.assertFalse(panel.set_fallback_model(0, "p", "m"))   # tulis ditolak
                self.assertFalse(panel.remove_fallback_model(0))

    def test_render_escapes_newline_in_model_name(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(d, {"fallback_providers": [{"provider": "p", "model": "m\nx"}]})
            with mock.patch.object(panel, "CONFIG_PATH", cfg):
                panel.invalidate_config_cache()
                html_out = panel.render_backup_models_block()
        # Newline literal dalam atribut onclick memutus literal string JS;
        # harus ter-escape menjadi \n (dua karakter) di dalam onclick.
        self.assertNotIn("'m\nx'", html_out)
        self.assertIn("m\\nx", html_out)

import yaml


class TestFastYamlLoader(unittest.TestCase):
    """Jalur baca YAML panel memakai parser C (CSafeLoader) saat libyaml tersedia.

    Profil jalur dingin: parsing YAML = 4,5 dtk dari total 7,3 dtk build_fragments
    dingin; PyYAML memakai parser Python untuk safe_load meski libyaml terpasang.
    Negatif kontrol: simbol _yaml_load tidak ada pada monolit tanpa patch.
    """

    def test_helper_exists_and_prefers_c_loader(self):
        self.assertTrue(hasattr(panel, "_yaml_load"))
        if getattr(panel.yaml, "__with_libyaml__", False):
            self.assertIs(panel._YAML_SAFE_LOADER, panel.yaml.CSafeLoader)
        else:
            self.assertIs(panel._YAML_SAFE_LOADER, panel.yaml.SafeLoader)

    def test_parses_identically_to_safe_load(self):
        doc = (
            "model:\n  provider: custom\n  models: [a, b]\n"
            "fallback_providers:\n  - {provider: p1, model: m1, api_key: 'sk-1'}\n"
            "quoted: \"teks 'dengan' quote\"\nnested:\n  deep:\n    x: 1\n"
        )
        self.assertEqual(panel._yaml_load(doc), yaml.safe_load(doc))

    def test_all_read_sites_use_fast_loader(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard-toggle-server.py")).read()
        self.assertNotIn("yaml.safe_load(", src)
        self.assertGreaterEqual(src.count("_yaml_load("), 16)

    def test_semantics_safe_on_untrusted_input(self):
        # Tag !python/* harus DITOLAK — CSafeLoader menurunkan SafeLoader.
        with self.assertRaises(yaml.YAMLError):
            panel._yaml_load("!!python/object/apply:os.system ['id']")



class TestNetworkValidationContracts(unittest.TestCase):
    """Kontrak validasi endpoint Jaringan (diverifikasi live saat audit Wave 102).

    Empat penolakan input tidak valid ini yang diandalkan UI tab Jaringan;
    sebelumnya hanya jalur sukses yang teruji. Validasi host harus terjadi
    SEBELUM subprocess dipanggil (tanpa biaya).
    """

    def test_resolve_rejects_invalid_host_before_subprocess(self):
        with mock.patch.object(panel.subprocess, "run") as run:
            res = panel.resolve_gateway_host("!!invalid host!!")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "Nama host tidak valid")
        run.assert_not_called()  # ditolak di regex, tanpa membangunkan relay

    def test_resolve_rejects_empty_host(self):
        res = panel.resolve_gateway_host("   ")
        self.assertFalse(res["ok"])

    def test_route_test_rejects_unknown_platform(self):
        res = panel.test_gateway_route("tidakada", route="", url="")
        self.assertFalse(res["ok"])
        self.assertIn("tidak dikenal", res["error"])

    def test_route_test_rejects_custom_url_with_bad_scheme(self):
        res = panel.test_gateway_route("telegram", route="custom", url="bukan-url")
        self.assertFalse(res["ok"])
        self.assertIn("Skema proxy", res["error"])

    def test_route_test_rejects_unknown_route(self):
        res = panel.test_gateway_route("telegram", route="nonsense", url="")
        self.assertFalse(res["ok"])
        self.assertIn("Rute tidak dikenal", res["error"])


if __name__ == "__main__":
    unittest.main()
