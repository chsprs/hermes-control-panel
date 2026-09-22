import importlib
import json
import os
import subprocess
import sys
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
import importlib.util
spec = importlib.util.spec_from_file_location(
    "panel", os.path.join(REPO_ROOT, "dashboard-toggle-server.py")
)
panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(panel)


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

    def setUp(self):
        panel._last_action_at = 0.0
        panel._last_aux_model_at = 0.0
        panel._last_model_switch_at = 0.0
        with panel._router_update_lock:
            panel._router_updating = False

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

    # --- PR 2: Config write failures propagation ---
    def test_02_aux_model_write_failure_returns_500(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.TOKEN}"
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
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.TOKEN}"
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
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.TOKEN}"
        with mock.patch.object(panel, "reset_all_aux_tasks", return_value=False):
            code, _, _ = self._request(
                "/reset-aux",
                method="POST",
                headers={"Cookie": cookie},
            )
            self.assertEqual(code, 500)

    def test_05_remove_fallback_write_failure_returns_500(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.TOKEN}"
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
    def test_12_server_exits_without_token(self):
        script_path = os.path.join(REPO_ROOT, "dashboard-toggle-server.py")
        env = {k: v for k, v in os.environ.items() if k != "PANEL_TOKEN"}
        res = subprocess.run([sys.executable, script_path], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 2)
        self.assertIn("PANEL_TOKEN is required", res.stderr)

    def test_13_unauthenticated_request_returns_403(self):
        code, _, _ = self._request("/status")
        self.assertEqual(code, 403)

    def test_14_bootstrap_sets_cookie_and_redirects(self):
        code, headers, _ = self._request(f"/status?token={panel.TOKEN}")
        self.assertEqual(code, 302)
        set_cookie = headers.get("Set-Cookie", "")
        self.assertIn(panel.SESSION_COOKIE_NAME, set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertEqual(headers.get("Location"), "/status")

    def test_15_mutation_method_enforcement(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.TOKEN}"
        # 1. GET to mutating route without shortcut -> 405 Method Not Allowed
        code, _, _ = self._request("/restart-bot", method="GET", headers={"Cookie": cookie})
        self.assertEqual(code, 405)

        # 2. POST to mutating route -> allowed (302)
        code, headers, _ = self._request("/restart-bot", method="POST", headers={"Cookie": cookie})
        self.assertEqual(code, 302)

        # 3. GET shortcut with token (CasaOS compat) -> allowed (302)
        code, headers, _ = self._request(f"/toggle?token={panel.TOKEN}", method="GET")
        self.assertEqual(code, 302)

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


if __name__ == "__main__":
    unittest.main()
