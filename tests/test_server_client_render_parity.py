"""Stdlib-only regressions for server-fragment/client-render parity.

The assertions in this module intentionally use plain ``assert`` statements so
each check also has a small, self-contained failure message.  No browser, Node,
or third-party test package is required.
"""

from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import re
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PANEL_TOKEN", "render-parity-token")
os.environ.setdefault("PANEL_PASSWORD", "render-parity-password")
_SPEC = importlib.util.spec_from_file_location(
    "panel_render_parity", REPO_ROOT / "dashboard-toggle-server.py"
)
panel = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(panel)
panel._run_hermes_cli = lambda *args, **kwargs: (-1, "disabled in tests")


DIRECT_SSE_SLOTS = {
    "processes_table": "process-table-slot",
    "gateway_list_block": "gateway-list-slot",
    "profiles_block": "profiles-slot",
    "update_block": "update-content-slot",
}
LOG_SSE_SLOTS = {
    "log_card": "log-slot",
    "hermes_log_card": "hermes-log-slot",
    "clean_junk_card": "clean-log-slot",
    "gateway_log_card": "gateway-log-slot",
}


def _kanban_task(number, **overrides):
    task = {
        "id": f"t_{number}",
        "title": f"Task {number}",
        "status": "done",
        "assignee": "coder",
        "priority": 0,
        "live_state": "",
        "live_detail": "",
    }
    task.update(overrides)
    return task


class TestServerClientRenderParity(unittest.TestCase):
    def _mock_fragment_dependencies(self, stack):
        """Isolate fragment rendering while keeping its real composition logic."""
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
            "render_gateway_platforms_html": (
                '<div class="update-hint">Belum ada gateway</div>'
            ),
            "render_gateway_log_card": "",
            "get_available_models_cached": {},
            "get_all_configured_providers": [],
            "get_router_update_result": {"status": "idle", "summary": ""},
            "tail_update_log": "",
            "get_hermes_update": {
                "status": "current",
                "local": "1",
                "remote": "1",
                "behind": 0,
                "channel": "",
            },
            "get_hermes_update_result": {
                "status": "idle",
                "running": False,
                "summary": "",
            },
            "tail_hermes_update_log": "",
            "get_cpu_percent": 1.0,
            "render_processes_table": (
                '<table><tbody><tr><td title="safe">'
                + ("batas-λ" * 1024)
                + "</td></tr></tbody></table>"
            ),
            "render_clean_junk_card": "",
            "render_profiles_block": "<div>Profil Saat Ini</div>",
            "get_reasoning_effort": "low",
            "get_rate_limited_providers": [],
            "get_update_status": "available",
            "get_cached_router_info": {
                "version": "1<installed>",
                "latest_version": "2&latest",
                "npm_latest": "",
                "npm_ahead": False,
            },
            "get_cached_router_version": "1",
            "get_router_version": "1",
            "get_router_image_date": "2026-01-01",
            "get_9router_patch_notes": [],
            "render_patch_notes_block": "",
            "get_hermes_patch_notes": [],
            "get_9router_public_url": "http://router.local",
            "get_open_block_active": "",
            "get_openclaw_open_block": "",
            "is_openclaw_control_ui_enabled": False,
            "render_aux_tasks_block": "",
            "render_backup_models_block": "",
            "render_docker_apps_block": "",
            "render_auth_setting_block": "",
            "render_kanban_dispatcher_badge": "",
        }
        for name, value in values.items():
            stack.enter_context(mock.patch.object(panel, name, return_value=value))
        stack.enter_context(mock.patch.object(panel, "_router_updating", False))
        stack.enter_context(mock.patch.object(panel.os.path, "isfile", return_value=True))
        stack.enter_context(mock.patch.object(panel.os.path, "exists", return_value=False))
        stack.enter_context(mock.patch.object(panel.os, "getloadavg", return_value=(1.0, 2.0, 3.0)))
        # Static cached blocks are irrelevant here. Shared SSE blocks above are
        # direct render mocks, so full and SSE-only renders remain comparable.
        stack.enter_context(mock.patch.object(panel, "_ttl_cached", side_effect=lambda *args: ""))

    def test_full_and_sse_fragments_have_identical_client_values(self):
        with ExitStack() as stack:
            self._mock_fragment_dependencies(stack)
            full = panel.build_fragments()
            sse = panel.build_fragments(sse_only=True)
            payload = json.loads(panel._sse_payload(full))

        for key in panel.SSE_CLIENT_KEYS:
            with self.subTest(fragment=key):
                assert key in full, f"full render omitted {key}"
                assert key in sse, f"SSE render omitted {key}"
                assert key in payload, f"client payload omitted {key}"
                assert full[key] == sse[key] == payload[key], (
                    f"full/SSE/client values diverged for {key}"
                )

        assert full["rate_limit_card"] == "", "empty rate-limit input must clear its static card"
        assert "model_chips" not in sse, "static model chips must not be rebuilt by SSE"
        assert "rate_limit_card" not in sse, "static rate-limit card must not be rebuilt by SSE"
        assert "v1&lt;installed&gt;" in full["update_block"]
        assert "v2&amp;latest" in full["update_block"]

        # The client must consume the exact server fragment at the matching slot;
        # there is no second HTML renderer for these direct SSE blocks.
        for key, slot in DIRECT_SSE_SLOTS.items():
            with self.subTest(fragment=key, slot=slot):
                pattern = rf"set\('{re.escape(slot)}',\s*d\.{key}\)"
                assert re.search(pattern, panel.SSE_SCRIPT), (
                    f"client does not assign d.{key} to #{slot}"
                )

    def test_sse_delta_preserves_empty_and_large_fragments(self):
        keys = tuple(DIRECT_SSE_SLOTS) + tuple(LOG_SSE_SLOTS)
        large = "<div>" + ("λ<&" * 4096) + "</div>"
        first = {key: f'{large}<span data-key="{key}"></span>' for key in keys}
        empty = {key: "" for key in keys}

        with panel._sse_last_keys_lock:
            previous = dict(panel._sse_last_keys)
            panel._sse_last_keys.clear()
        try:
            assert json.loads(panel._sse_payload(first, delta=True)) == first, (
                "large fragments changed during JSON transport"
            )
            assert json.loads(panel._sse_payload(empty, delta=True)) == empty, (
                "a transition to empty must be sent, not omitted as a delta"
            )
            assert json.loads(panel._sse_payload(empty, delta=True)) == {}, (
                "only an unchanged repeated empty fragment may be omitted"
            )
            assert json.loads(panel._sse_payload(empty)) == empty, (
                "full sync must retain explicit empty fields"
            )
        finally:
            with panel._sse_last_keys_lock:
                panel._sse_last_keys.clear()
                panel._sse_last_keys.update(previous)

        # Empty log fragments are meaningful: the client normalizes null/empty
        # to an empty string and updates the slot instead of retaining stale DOM.
        for key, slot in LOG_SSE_SLOTS.items():
            with self.subTest(fragment=key, slot=slot):
                guard = f"if(d.{key} !== undefined)"
                assert guard in panel.SSE_SCRIPT, f"missing explicit-empty guard for {key}"
                block = panel.SSE_SCRIPT.split(guard, 1)[1].split("    }", 1)[0]
                assert "|| ''" in block, f"{key} does not normalize empty/null"
                assert re.search(rf"stickySet\('{re.escape(slot)}',\s*\w+\)", block), (
                    f"{key} is not rendered into #{slot}"
                )

    def test_kanban_server_and_client_share_page_boundaries(self):
        match = re.search(r"\bvar KANBAN_COL_PAGE\s*=\s*(\d+);", panel.PAGE)
        assert match, "client Kanban page size constant is missing"
        client_limit = int(match.group(1))
        assert client_limit == panel.KANBAN_COL_PAGE, (
            "server and client Kanban page sizes diverged"
        )
        assert "kanbanFilterActive() ? items : items.slice(0, KANBAN_COL_PAGE)" in panel.PAGE, (
            "client must use the shared limit only when no filter is active"
        )

        for total in (0, 1, panel.KANBAN_COL_PAGE, panel.KANBAN_COL_PAGE + 1, 25):
            tasks = [_kanban_task(i) for i in range(total)]
            with self.subTest(tasks=total), mock.patch.object(
                panel, "list_kanban_tasks", return_value=tasks
            ):
                rendered = panel.render_kanban_board_block("parity")
                done = rendered.split(
                    '<div class="kanban-column" data-status="done">', 1
                )[1]
                visible = min(total, panel.KANBAN_COL_PAGE)
                assert done.count('class="kanban-card"') == visible, (
                    f"server rendered the wrong card count at boundary {total}"
                )
                badge = re.search(r'id="badge-count-done"[^>]*>(\d+)</span>', done)
                assert badge and int(badge.group(1)) == total, (
                    "column badge must retain the total, not only visible cards"
                )
                hidden = total - visible
                if hidden:
                    assert f'data-offset="{visible}" data-total="{total}"' in done
                    assert f"+ {hidden} lainnya" in done
                    more = panel.render_kanban_column_cards(
                        board="parity", status="done", offset=visible
                    )
                    assert more["loaded"] == min(hidden, panel.KANBAN_COL_PAGE)
                    assert more["remaining"] == max(0, hidden - panel.KANBAN_COL_PAGE)
                    assert more["cards"].count('class="kanban-card"') == more["loaded"]
                else:
                    assert 'class="btn kb-load-more"' not in done
                if total == 0:
                    assert "Kosong" in done, "empty column needs its client-compatible placeholder"

        empty_card = panel._render_kanban_task_card(
            _kanban_task(0, id="", title="", assignee="", live_detail=""), "done"
        )
        assert 'data-task-id=""' in empty_card
        assert 'data-title=""' in empty_card
        assert "None" not in empty_card
        assert ">@</span>" not in empty_card, "empty assignee must not create an empty @ badge"

    def test_process_table_empty_and_placeholder_fields(self):
        with mock.patch.object(panel, "get_process_list", return_value=[]):
            empty = panel._render_processes_table_uncached()
        assert "Tidak ada layanan terdeteksi." in empty
        assert '<td colspan="5"' in empty

        rows = [
            {
                "name": "",
                "kind": "",
                "status": "",
                "is_active": False,
                "installed": True,
                "pid": None,
                "mem_mb": None,
                "stop_url": "",
                "start_url": "",
                "restart_url": "",
            },
            {
                "name": "svc<&",
                "kind": "",
                "status": "Berjalan",
                "is_active": True,
                "installed": True,
                "pid": 0,
                "mem_mb": 0,
                "stop_url": "",
                "start_url": "",
                "restart_url": "",
            },
        ]
        with mock.patch.object(panel, "get_process_list", return_value=rows):
            rendered = panel._render_processes_table_uncached()
        body = rendered.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
        assert body.count("<tr>") == len(rows), "process renderer must not truncate rows"
        assert "None" not in rendered
        assert '<span class="td-val">0</span>' in rendered, "PID zero is data, not an empty field"
        assert '<span class="td-val">-</span>' in rendered
        assert "svc&lt;&amp;" in rendered
        assert 'class="btn-end-task"' not in rendered
        assert 'class="btn-start-task"' not in rendered
        assert 'class="btn-restart-task"' not in rendered

    def test_profiles_and_gateway_keep_nonempty_empty_state_contract(self):
        with mock.patch.object(panel, "list_agent_profiles", return_value=[]), \
             mock.patch.object(panel, "get_active_profile_name", return_value=""), \
             mock.patch.object(panel, "get_gateway_profile_statuses", return_value={}):
            empty = panel._render_profiles_block_uncached()
        assert "Profil Saat Ini" in empty
        assert 'class="card card-status profile-card"' not in empty
        assert "None" not in empty

        profile = {
            "name": "empty-fields",
            "is_active": False,
            "is_default": False,
            "model": "",
            "provider": "",
            "description": "",
            "skill_count": 0,
            "has_env": False,
            "has_soul": False,
        }
        with mock.patch.object(panel, "list_agent_profiles", return_value=[profile]), \
             mock.patch.object(panel, "get_active_profile_name", return_value="default"), \
             mock.patch.object(panel, "get_gateway_profile_statuses", return_value={}), \
             mock.patch.object(panel, "render_gateway_profile_badge", return_value=""), \
             mock.patch.object(panel, "_profile_skill_tool_summary", return_value={}):
            rendered = panel._render_profiles_block_uncached()
        assert "mengikuti default" in rendered
        assert "9router" in rendered
        assert "Skills: ?" in rendered
        assert "None" not in rendered

        with mock.patch.object(panel, "get_gateway_platforms", return_value=[]):
            gateway = panel.render_gateway_platforms_html()
        assert "Belum ada gateway" in gateway
        assert gateway.strip(), "gateway SSE slot must receive a visible empty-state placeholder"

    def test_model_chips_are_static_and_filter_handles_empty_query(self):
        with ExitStack() as stack:
            self._mock_fragment_dependencies(stack)
            full = panel.build_fragments()
            sse = panel.build_fragments(sse_only=True)
        assert "Belum ada daftar model" in full["model_chips"]
        assert "model_chips" not in sse
        assert "model_chips" not in panel.SSE_CLIENT_KEYS
        assert "d.model_chips" not in panel.SSE_SCRIPT

        filter_source = panel.PAGE.split("function filterModels(q)", 1)[1].split(
            "function toggleLog", 1
        )[0]
        assert "q = (q || '').trim().toLowerCase()" in filter_source
        assert "c.textContent.toLowerCase().indexOf(q)" in filter_source
        assert "c.style.display" in filter_source
        assert "innerHTML" not in filter_source, (
            "client filtering must not replace or truncate server-rendered chips"
        )

    def test_rate_limit_error_length_boundaries_and_empty_fields(self):
        rows = [
            {"provider": "", "name": "", "error_at": "", "error": ""},
            {"provider": "p180", "name": "n", "error_at": "t", "error": "x" * 180},
            {"provider": "p181", "name": "n", "error_at": "t", "error": "y" * 181},
        ]
        with ExitStack() as stack:
            self._mock_fragment_dependencies(stack)
            stack.enter_context(mock.patch.object(
                panel, "get_rate_limited_providers", return_value=rows
            ))
            card = panel.build_fragments()["rate_limit_card"]

        assert "UNKNOWN · unknown" in card
        assert "--:--:-- WIB" in card
        assert "None" not in card
        assert "x" * 180 in card
        assert "x" * 181 not in card
        assert "y" * 180 in card
        assert "y" * 181 not in card, "181-character errors must be clipped to 180"
        assert "rate_limit_card" not in panel.SSE_CLIENT_KEYS
        assert "d.rate_limit_card" not in panel.SSE_SCRIPT
        assert '<div id="rl-slot">{rate_limit_card}</div>' in panel.PAGE

    def test_update_and_hermes_log_tolerate_empty_fields(self):
        with ExitStack() as stack:
            self._mock_fragment_dependencies(stack)
            stack.enter_context(mock.patch.object(panel, "get_update_status", return_value="current"))
            stack.enter_context(mock.patch.object(panel, "get_cached_router_info", return_value={}))
            stack.enter_context(mock.patch.object(panel, "get_cached_router_version", return_value=""))
            stack.enter_context(mock.patch.object(panel, "get_hermes_update", return_value={
                "status": "unknown",
                "local": "",
                "remote": "",
                "behind": 0,
                "channel": "",
                "error": "",
            }))
            full = panel.build_fragments()
            sse = panel.build_fragments(sse_only=True)
            payload = json.loads(panel._sse_payload(full))

        assert "v?" in full["update_block"]
        assert "None" not in full["update_block"]
        assert full["update_block"] == sse["update_block"] == payload["update_block"]
        assert full["hermes_log_card"] == sse["hermes_log_card"] == ""
        assert payload["hermes_log_card"] == "", "empty Hermes log must survive serialization"
        assert '<div id="hermes-log-slot"></div>' in full["hermes_update_block"]
        assert "Gagal cek pembaruan Hermes" in full["hermes_update_block"]
        assert "None" not in full["hermes_update_block"]


if __name__ == "__main__":
    unittest.main()
