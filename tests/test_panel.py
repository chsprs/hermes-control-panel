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
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.TOKEN}"

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
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.TOKEN}"
        code, _, body = self._request("/api/gateway-log?n=20", method="GET", headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        data = json.loads(body if isinstance(body, str) else body.decode("utf-8"))
        self.assertTrue(data.get("ok"))
        self.assertIn("log", data)


if __name__ == "__main__":
    unittest.main()
