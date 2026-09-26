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
        cls.server.server_close()

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
        cookie = f"{panel.SESSION_COOKIE_NAME}={panel.TOKEN}"
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


if __name__ == "__main__":
    unittest.main()
