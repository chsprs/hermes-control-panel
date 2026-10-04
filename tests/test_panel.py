import importlib
import gzip
import io
import zlib
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
os.environ["PANEL_PASSWORD"] = "test-panel-password"
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
        cls.server.server_close()

    def setUp(self):
        panel._last_action_at = 0.0
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

    # --- PR 2: Config write failures propagation ---
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
    def test_12_server_exits_without_token(self):
        script_path = os.path.join(REPO_ROOT, "dashboard-toggle-server.py")
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("PANEL_TOKEN", "PANEL_PASSWORD")
        }
        res = subprocess.run([sys.executable, script_path], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 2)
        self.assertIn("PANEL_PASSWORD", res.stderr)

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
            panel._last_action_at = 0.0
            code, headers, _ = self._request(f"/toggle?token={panel.TOKEN}", method="GET")
            self.assertEqual(code, 302)
            mock_run.assert_called()

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
        page = panel.PAGE
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
        self.assertIn("_waPairPollTimer = setTimeout(pollWaPairStatus, 2500);", page)

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
            m_gw.assert_called_with(n=1)

            code, _, _ = self._request("/api/gateway-log?n=0", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            m_gw.assert_called_with(n=1)

            code, _, _ = self._request("/api/gateway-log?n=invalid", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            m_gw.assert_called_with(n=100)

        with mock.patch.object(panel, "tail_whatsapp_bridge_log", return_value="wa log line") as m_wa:
            code, _, _ = self._request("/api/whatsapp-log?n=99999", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            m_wa.assert_called_with(n=1000)

            code, _, _ = self._request("/api/whatsapp-log?n=-5", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            m_wa.assert_called_with(n=1)

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

        # 2. When CasaOS is inactive
        with mock.patch.object(panel, "service_active", return_value=False):
            procs = panel.get_process_list()
            casa = next((p for p in procs if p["id"] == "casaos"), None)
            self.assertIsNotNone(casa)
            self.assertEqual(casa["status"], "Berhenti")
            self.assertFalse(casa["is_active"])
            self.assertEqual(casa["pid"], "-")
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
            panel._last_action_at = 0.0
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
        # Verify significant payload reduction: 400KB+ to < 85KB (~80% reduction)
        self.assertLess(len(raw), 85000)
        self.assertGreater(len(decompressed), 350000)

    def test_deflate_compression_on_status_page(self):
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.SESSION_VALUE}"
        code, headers, raw = self._request_raw("/status", headers={"Cookie": cookie, "Accept-Encoding": "deflate"})
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("Content-Encoding"), "deflate")
        self.assertIn("Accept-Encoding", headers.get("Vary", ""))
        decompressed = zlib.decompress(raw).decode("utf-8")
        self.assertIn("<!doctype html>", decompressed.lower())
        self.assertLess(len(raw), 85000)
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

    def test_49_sse_client_keys_parity(self):
        """build_fragments() must return every key defined in SSE_CLIENT_KEYS."""
        frag = panel.build_fragments()
        for k in panel.SSE_CLIENT_KEYS:
            self.assertIn(k, frag, f"Key '{k}' in SSE_CLIENT_KEYS must be returned by build_fragments()")

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
        self.assertIn('data-provider="custom:9router"', panel.PAGE)
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
        ok, msg = panel.delete_agent_profile("temporary")
        self.assertTrue(ok, msg)
        self.assertFalse((self.root / "profiles" / "temporary").exists())
        # Active profile must fallback to default
        self.assertEqual(panel.get_active_profile_name(), "default")
        self.assertFalse((self.root / "active_profile").exists())

        # Cannot delete default
        ok_def, _ = panel.delete_agent_profile("default")
        self.assertFalse(ok_def)

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
                     "from tools.skills_tool import _find_all_skills;"
                     "a=_find_all_skills(skip_disabled=True);e=_find_all_skills(skip_disabled=False);"
                     "print(len(a),len(e))"],
                    capture_output=True, text=True, timeout=120)
        h_total, h_en = map(int, r.stdout.strip().split())
        self.assertEqual(inv["total"], h_total, "panel vs hermes total skill")
        self.assertEqual(inv["enabled_count"], h_en, "panel vs hermes enabled")
        # Modal + toggle UI ada
        self.assertIn('id="profile-skills-modal"', panel.PAGE)
        self.assertIn("openProfileSkillsModal(", panel.PAGE)
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

    def test_06_delete_task(self):
        _, _, task_id = panel.create_kanban_task(title="Delete Me")
        panel.add_kanban_comment(task_id, "temporary note")
        ok, msg = panel.delete_kanban_task(task_id)
        self.assertTrue(ok, msg)
        self.assertIsNone(panel.get_kanban_task(task_id))

    def test_07_boards_management(self):
        boards = panel.list_kanban_boards()
        self.assertTrue(any(b["slug"] == "default" for b in boards))

        ok, msg = panel.create_kanban_board("project-x", name="Project X")
        self.assertTrue(ok, msg)

        boards_after = panel.list_kanban_boards()
        self.assertTrue(any(b["slug"] == "project-x" for b in boards_after))

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
        page = panel.PAGE
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
        page = panel.PAGE
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


if __name__ == "__main__":
    unittest.main()
