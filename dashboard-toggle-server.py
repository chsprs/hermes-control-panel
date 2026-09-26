#!/usr/bin/env python3
"""Tiny HTTP toggle + status panel for hermes-dashboard.service, for CasaOS
one-click shortcuts.

Pure stdlib (http.server) — no framework, minimal RAM footprint. Runs
`systemctl start/stop hermes-dashboard` via subprocess, and reads a few
other live signals (Telegram bot status, active AI model, 9router
reachability, RAM) for an at-a-glance panel. Every info lookup has a short
timeout so a stuck dependency never hangs the page.

Design note (Post/Redirect/Get, adapted for GET-only shortcuts): action
routes (/toggle, /on, /off) are NOT safe to reload — a browser refresh would
resubmit the action and flip state again unpredictably. So every action
route performs its systemctl call once, then issues an HTTP 302 redirect to
/status, which is pure read-only and safe to refresh forever.

Routes:
  GET /toggle?token=TOKEN         -> flips hermes-dashboard state, then redirects to /status
  GET /on?token=TOKEN             -> starts hermes-dashboard, then redirects to /status
  GET /off?token=TOKEN            -> stops hermes-dashboard, then redirects to /status
  GET /bot-toggle?token=TOKEN     -> flips hermes-gateway (Telegram bot) state
  GET /restart-bot?token=TOKEN    -> restarts hermes-gateway
  GET /switch-model?token=TOKEN&model=ID -> sets model.default in config.yaml
  GET /update-router?token=TOKEN  -> docker compose pull + up -d for 9router
  GET /check-update?token=TOKEN   -> force a fresh 9router update check
  GET /status?token=TOKEN         -> read-only status panel (safe to refresh)
  GET /api/status?token=TOKEN     -> JSON of all live fragments (panel auto-poll)
  GET /?token=TOKEN               -> alias for /status

Auto-refresh: the panel polls /api/status on an interval and swaps the dynamic
fragments in place (no full reload, no flicker), driving a top progress bar and
a loading spinner. All rendering stays server-side — /api/status returns the
same HTML fragments the initial page uses, so the client never re-implements it.
"""

import copy
import glob
import hmac
import html
import json
import os
from pathlib import Path
import re
import signal
import socket
import queue
import sqlite3
import tempfile
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import yaml
from datetime import datetime, timedelta, timezone
from http import cookies as http_cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, urlsplit

TOKEN = os.environ.get("PANEL_TOKEN", "").strip()
PORT = int(os.environ.get("PANEL_PORT", 9120))
SERVICE = os.environ.get("HERMES_DASHBOARD_SERVICE", "hermes-dashboard")
DEBOUNCE_SECONDS = 3.0
STARTUP_COUNTDOWN_SECONDS = 15  # systemd reports "active" almost instantly,
# but the actual web server takes ~10-15s to bind and answer.
ROUTER_MODELS_URL = "http://{host}:20128/v1/models"
ROUTER_VERSION_URL = "http://{host}:20128/api/version"
MODELS_TIMEOUT = 6.0  # /v1/models is heavier than the plain reachability
# ping — 9router has shown response times up to several seconds under load
# this session, so give it more room than the quick INFO_TIMEOUT checks.

ROUTER_HOST_OVERRIDE = os.environ.get("ROUTER_HOST", "").strip()
HERMES_DASHBOARD_URL = os.environ.get("HERMES_DASHBOARD_URL", "").strip()
CONFIG_PATH = os.environ.get("HERMES_CONFIG_PATH", "/root/.hermes/config.yaml")
SESSION_COOKIE_NAME = "hermes_panel_session"
# CasaOS one-click shortcuts may only mutate state via explicit GET when the
# URL itself carries a valid token; every other mutation requires POST.
MUTATING_PATHS = frozenset({
    "/toggle", "/on", "/off", "/restart-bot", "/bot-toggle",
    "/switch-model", "/update-router", "/check-update",
    "/check-hermes-update", "/update-hermes",
    "/clean-junk", "/fetch-models", "/reload-panel-config",
    "/set-aux-model", "/reset-aux",
    "/set-fallback-model", "/remove-fallback-model",
    "/process-action",
    "/save-gateway-platform", "/toggle-gateway-platform", "/remove-gateway-platform",
    "/api/gateway-config-preview",
    "/api/whatsapp/pair-start", "/api/whatsapp/pair-cancel", "/api/whatsapp/pair-apply",
})
LEGACY_GET_SHORTCUTS = frozenset({"/toggle", "/on", "/off"})
ROUTER_URL = "http://{host}:20128/"
INFO_TIMEOUT = 2.0  # seconds — every live check below is capped at this
ROUTER_DB_PATH = "/DATA/AppData/9router/db/data.sqlite"  # host-side path of
# the same file 9router itself reads at /app/data/db/data.sqlite — reading
# it directly avoids a docker exec round-trip.
ROUTER_IMAGE = "decolua/9router:latest"
ROUTER_COMPOSE_DIR = "/opt/AppData/9router"
# CasaOS keeps the real compose file under /var/lib/casaos/apps/<app>/ while
# /opt/AppData/9router only holds the panel's update.log/update-check.json.
# Hardcoding either path makes `docker compose -f <missing>` fail, the
# fallback `docker start` reuses the OLD image, and the panel reports a
# successful update that never took effect. Resolve the real file instead.
ROUTER_COMPOSE_CANDIDATES = [
    os.environ.get("ROUTER_COMPOSE_FILE", ""),
    "/var/lib/casaos/apps/9router/docker-compose.yml",
    "/opt/AppData/9router/docker-compose.yml",
    "/DATA/AppData/9router/docker-compose.yml",
]


def router_compose_file() -> str:
    """Path of the compose file that actually exists ('' when none do)."""
    for cand in ROUTER_COMPOSE_CANDIDATES:
        if cand and os.path.isfile(cand):
            return cand
    return ""


def router_compose_dir() -> str:
    """Directory holding the real compose file, else the log dir."""
    path = router_compose_file()
    return os.path.dirname(path) if path else ROUTER_COMPOSE_DIR


ROUTER_REMOTE_COMPOSE_DIR = os.environ.get("ROUTER_REMOTE_COMPOSE_DIR", "/opt/AppData/9router")
ROUTER_SSH_USER = os.environ.get("ROUTER_SSH_USER", "root")
ROUTER_SSH_KEY = os.environ.get("ROUTER_SSH_KEY", "/root/.ssh/hermes_9router_ed25519")
ROUTER_CONTAINER = "9router"  # CasaOS compose file sets container_name explicitly
RATE_LIMIT_RECENCY_MINUTES = 10  # only count an account's lastError as a
# live problem if it happened within this window — the DB keeps lastError
# around indefinitely, it doesn't clear on its own once the account recovers.

# --- 9router update detection (Docker Hub digest comparison) ---
DOCKERHUB_REPO = "decolua/9router"  # namespace/name on Docker Hub
DOCKERHUB_TAG = "latest"
UPDATE_CACHE_PATH = f"{ROUTER_COMPOSE_DIR}/update-check.json"
UPDATE_CACHE_TTL = 1800  # seconds (30 min) — how long a good check is trusted
# before a background re-check is triggered. The check itself runs off-thread
# so the page never blocks on Docker Hub; only "current"/"available" answers
# are cached, "unknown" always re-checks so a transient network blip recovers.
UPDATE_NET_TIMEOUT = 6.0  # per Docker Hub call (token + manifest HEAD)

# --- panel auto-refresh (SSE) ---
MODELS_CACHE_TTL = 300  # the model list barely changes; cache it so the SSE
# push thread doesn't hammer 9router's /v1/models endpoint every tick.
UPDATE_LOG_TAIL = 40  # lines of the remote docker-compose log shown live
UPDATE_TIMEOUT = 300  # hard cap for one remote pull/recreate operation
HERMES_BIN = "/usr/local/bin/hermes"
HERMES_UPDATE_LOG_PATH = "/root/.hermes/logs/update.log"
# `hermes update` waits for the gateway to drain (restart_after_turn_timeout + restart_drain_timeout,
# ~1995s here) on top of dependency install + UI build; killing it earlier leaves a half-done update.
HERMES_UPDATE_TIMEOUT = int(os.environ.get("HERMES_UPDATE_TIMEOUT", "3600"))

_last_action_lock = threading.Lock()
_last_action_at = 0.0
_last_model_switch_lock = threading.Lock()
_last_model_switch_at = 0.0
_last_aux_model_lock = threading.Lock()
_last_aux_model_at = 0.0
_update_refresh_lock = threading.Lock()
_update_refreshing = False
_models_cache = {"at": 0.0, "val": []}
_models_cache_lock = threading.Lock()
_router_update_lock = threading.Lock()
_router_updating = False
_router_update_result = {"status": "idle", "exit_code": None, "changed": None, "summary": "", "finished_at": 0.0}
_router_image_date_cache = {"at": 0, "val": "?"}
_router_image_date_lock = threading.Lock()
_router_uptime_cache = {"at": 0, "val": ""}
_router_uptime_lock = threading.Lock()

CLEAN_JUNK_JSON = "/root/.hermes/logs/clean-junk.json"
CLEAN_JUNK_LOG = "/root/.hermes/logs/clean-junk.log"
_clean_junk_result = {"status": "idle", "freed_bytes": 0, "freed_human": "0 B", "freed_mb": 0.0, "files_count": 0, "log": "", "at": 0.0}
_clean_junk_lock = threading.Lock()
_config_write_lock = threading.Lock()
_env_write_lock = threading.Lock()

# --- icons ---
# Inline SVG (stroke-based, Lucide-style), never emoji: emoji glyphs render
# inconsistently across OS/browser font stacks and can't be styled via CSS
# (color, size, stroke weight) the way the rest of the UI is. Every icon here
# is built from plain primitives (rect/circle/line/polyline/polygon) plus at
# most one simple arc, so there is nothing that can render as broken markup —
# worst case is a slightly imperfect curve, never a malformed page.
def _icon(inner: str, size: int = 18) -> str:
    return (f'<svg class="icon" width="{size}" height="{size}" viewBox="0 0 24 24" '
            f'fill="none" stroke="currentColor" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
            f'focusable="false">{inner}</svg>')


ICON_MONITOR = _icon(
    '<rect x="2" y="3" width="20" height="14" rx="2"/>'
    '<line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>',
    size=22,
)
ICON_POWER = _icon('<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/>')
ICON_REFRESH = _icon(
    '<path d="M21 12a9 9 0 0 1-15.5 6.3"/><path d="M3 12a9 9 0 0 1 15.5-6.3"/>'
    '<polyline points="21 3 21 9 15 9"/><polyline points="3 21 3 15 9 15"/>'
)
ICON_ARROW_UP_CIRCLE = _icon(
    '<circle cx="12" cy="12" r="10"/>'
    '<polyline points="16 12 12 8 8 12"/><line x1="12" y1="16" x2="12" y2="8"/>'
)
ICON_CHECK = _icon('<polyline points="20 6 9 17 4 12"/>', size=15)
ICON_ALERT_TRIANGLE = _icon(
    '<polygon points="12 3 22 20 2 20"/>'
    '<line x1="12" y1="9" x2="12" y2="14"/><line x1="12" y1="17" x2="12.01" y2="17"/>'
)
ICON_CLOCK = _icon('<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>')
ICON_PAUSE = _icon('<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>')
ICON_EXTERNAL_LINK = _icon(
    '<rect x="3" y="3" width="12" height="12" rx="1.5"/>'
    '<polyline points="9 3 21 3 21 15"/><line x1="9" y1="15" x2="21" y2="3"/>'
)
ICON_TERMINAL = _icon('<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>', size=16)
ICON_LAYERS = _icon(
    '<polygon points="12 2 2 7 12 12 22 7 12 2"/>'
    '<polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>',
    size=16,
)
ICON_ACTIVITY = _icon('<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>', size=16)


ICON_CPU = _icon('<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/>', size=16)
ICON_RAM = _icon('<path d="M2 2h20v20H2z"/><path d="M6 6h12v12H6z"/>', size=16)
ICON_DISK = _icon('<line x1="22" y1="12" x2="2" y2="12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><line x1="6" y1="16" x2="6.01" y2="16"/><line x1="10" y1="16" x2="10.01" y2="16"/>', size=16)
ICON_NETWORK = _icon('<rect x="16" y="16" width="6" height="6" rx="1"/><rect x="2" y="16" width="6" height="6" rx="1"/><rect x="9" y="2" width="6" height="6" rx="1"/><path d="M5 16v-3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v3"/><line x1="12" y1="12" x2="12" y2="8"/>', size=16)
ICON_GLOBE = _icon('<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>', size=16)
ICON_BOT = _icon('<rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><line x1="8" y1="16" x2="8.01" y2="16"/><line x1="16" y1="16" x2="16.01" y2="16"/>', size=16)
ICON_ROUTER = _icon('<rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6" y2="14"/><line x1="18" y1="6" x2="18" y2="14"/><line x1="6" y1="18" x2="6.01" y2="18"/><line x1="10" y1="18" x2="10.01" y2="18"/>', size=16)
ICON_HERMES = _icon('<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>', size=16)
ICON_TRASH = _icon('<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>', size=16)
ICON_SHIELD = _icon('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>', size=16)

ICON_HERMES_LOGO = '<img src="https://cdn.jsdelivr.net/gh/selfhst/icons/webp/hermes-agent-light.webp" width="28" height="28" style="vertical-align:middle;object-fit:contain;filter:drop-shadow(0 2px 4px rgba(0,0,0,0.4))" alt="Hermes Logo">'

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes Control Panel</title>
<meta name="theme-color" content="#07090e">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Ccircle cx='12' cy='12' r='10' fill='%232563eb'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist+Mono:wght@400;500;600&family=Geist:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{{
  color-scheme:dark;
  --bg:#07090e;
  --surface:rgba(22,27,38,0.72);
  --surface-solid:#141923;
  --surface-elevated:rgba(30,38,54,0.7);
  --surface-hover:rgba(42,52,74,0.8);
  --border:rgba(255,255,255,0.09);
  --border-subtle:rgba(255,255,255,0.05);
  --border-hover:rgba(255,255,255,0.18);
  --text:#f8fafc;
  --text-muted:#94a3b8;
  --text-dim:#64748b;
  --accent:#3b82f6;
  --accent-light:#60a5fa;
  --accent-dim:rgba(59,130,246,0.15);
  --accent-squircle:linear-gradient(135deg,#2563eb,#3b82f6);
  --success:#10b981;
  --success-dim:rgba(16,185,129,0.14);
  --warning:#f59e0b;
  --warning-dim:rgba(245,158,11,0.14);
  --danger:#ef4444;
  --danger-dim:rgba(239,68,68,0.14);
  --radius-xl:20px;
  --radius-lg:16px;
  --radius-md:12px;
  --radius-sm:8px;
  --ease:cubic-bezier(.16,1,.3,1);
  --font-sans:'Geist',-apple-system,BlinkMacSystemFont,sans-serif;
  --font-mono:'Geist Mono',monospace;
}}
html,body{{overflow-x:hidden;max-width:100vw;box-sizing:border-box}}
*{{box-sizing:border-box;margin:0;padding:0}}

/* Custom Scrollbars */
::-webkit-scrollbar{{width:7px;height:7px}}
::-webkit-scrollbar-track{{background:#07090e}}
::-webkit-scrollbar-thumb{{background:rgba(255,255,255,0.14);border-radius:4px}}
::-webkit-scrollbar-thumb:hover{{background:rgba(255,255,255,0.25)}}
*{{scrollbar-width:thin;scrollbar-color:rgba(255,255,255,0.14) #07090e}}

body{{font-family:var(--font-sans);background:var(--bg);color:var(--text);
min-height:100vh;display:flex;flex-direction:column;align-items:center;
padding:1.4rem .9rem;font-size:14px;line-height:1.5;
-webkit-font-smoothing:antialiased;
background-image:radial-gradient(circle at 50% 0%, rgba(59,130,246,0.08) 0%, transparent 55%),
radial-gradient(circle at 85% 30%, rgba(16,185,129,0.04) 0%, transparent 40%)}}
.icon{{display:inline-block;vertical-align:-3px;flex-shrink:0}}

/* Header & Brand */
.header{{display:flex;width:100%;max-width:1040px;align-items:center;justify-content:space-between;gap:.8rem;margin-bottom:1.3rem}}
.header-brand{{display:flex;align-items:center;gap:.75rem}}
.header-logo{{width:34px;height:34px;object-fit:contain;filter:drop-shadow(0 4px 14px rgba(59,130,246,0.35))}}
h1{{font-size:1.22rem;font-weight:600;letter-spacing:-.025em;color:var(--text)}}
.live-badge{{display:inline-flex;align-items:center;gap:.45rem;padding:.24rem .7rem;
border-radius:999px;font-size:.68rem;font-weight:600;font-family:var(--font-mono);
background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.25);color:var(--success);
letter-spacing:.04em;text-transform:uppercase}}
.live-badge .dot{{width:6px;height:6px;border-radius:50%;background:var(--success);
box-shadow:0 0 8px var(--success)}}
.live-badge.connected{{color:var(--success);background:rgba(16,185,129,0.1);border-color:rgba(16,185,129,0.25)}}
.live-badge.connected .dot{{background:var(--success)}}

/* Apple CC Segmented Nav */
.tabs{{display:flex;gap:.3rem;margin:0 auto 1.35rem;width:100%;max-width:520px;
background:rgba(20,25,35,0.75);
padding:.3rem;border-radius:var(--radius-xl);border:1px solid var(--border)}}
.tab{{flex:1 1 0;min-height:42px;display:flex;align-items:center;justify-content:center;
border-radius:12px;text-align:center;gap:.35rem;
font-weight:500;font-size:.82rem;cursor:pointer;border:none;white-space:nowrap;
background:transparent;color:var(--text-muted);transition:all .18s var(--ease)}}
.tab:hover{{color:var(--text);background:rgba(255,255,255,0.04)}}
.tab.active{{background:rgba(59,130,246,0.20);color:#fff;
border:1px solid rgba(96,165,250,0.65);box-shadow:0 3px 14px rgba(37,99,235,0.40), inset 0 1px 0 rgba(255,255,255,0.08)}}

/* Panels & Layout */
.content-wrapper{{display:block;width:100%;max-width:1040px;margin:0 auto}}
.tab-panel{{display:none;width:100%;margin:0 auto}}
.tab-panel.active{{display:block}}
#tab-status{{max-width:1040px}}
#tab-performance{{max-width:920px}}
#tab-control{{max-width:880px}}
#tab-auxiliary{{max-width:880px}}

/* Cards */
/* No backdrop-filter on scrolling content: phones re-blur every card on every scroll frame, and the
   backdrop here is a faint gradient, so the blur was invisible anyway. Overlays keep theirs. */
.card{{background:var(--surface);
border:1px solid var(--border);border-radius:var(--radius-xl);padding:1.35rem;width:100%;
box-shadow:0 8px 32px rgba(0,0,0,0.37);transition:border-color .2s var(--ease),box-shadow .2s var(--ease)}}
.card:hover{{border-color:var(--border-hover);box-shadow:0 12px 36px rgba(0,0,0,0.45)}}
.card+.card{{margin-top:1.15rem}}
.card-title{{font-size:.72rem;font-family:var(--font-mono);color:var(--text-muted);
text-transform:uppercase;letter-spacing:.09em;font-weight:600;margin-bottom:1rem;
display:flex;align-items:center;gap:.5rem}}

/* Bento Tile Grid */
.cc-grid{{display:grid;grid-template-columns:repeat(auto-fit, minmax(220px, 1fr));gap:.75rem}}
@media (max-width: 440px){{.cc-grid{{grid-template-columns:1fr}}}}
@media (max-width:390px){{.tab{{font-size:.74rem;min-height:38px;gap:.2rem}}.tabs{{gap:.2rem;padding:.25rem}}}}

.cc-tile{{background:rgba(255,255,255,0.025);border:1px solid var(--border-subtle);
border-radius:var(--radius-lg);padding:1rem;display:flex;flex-direction:column;
justify-content:space-between;min-height:92px;transition:all .18s var(--ease)}}
.cc-tile:hover{{background:rgba(255,255,255,0.045);border-color:var(--border)}}
.cc-tile-header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:.5rem}}
.cc-icon-box{{width:32px;height:32px;border-radius:10px;display:flex;align-items:center;
justify-content:center;color:#fff;background:rgba(255,255,255,0.06);border:1px solid var(--border)}}
.cc-icon-blue{{background:linear-gradient(135deg,#2563eb,#3b82f6);box-shadow:0 2px 10px rgba(37,99,235,0.35)}}
.cc-icon-green{{background:linear-gradient(135deg,#059669,#10b981);box-shadow:0 2px 10px rgba(16,185,129,0.35)}}
.cc-icon-purple{{background:linear-gradient(135deg,#7c3aed,#8b5cf6);box-shadow:0 2px 10px rgba(139,92,246,0.35)}}
.cc-icon-orange{{background:linear-gradient(135deg,#ea580c,#f97316);box-shadow:0 2px 10px rgba(234,88,12,0.35)}}
.cc-icon-gray{{background:rgba(255,255,255,0.08)}}

.cc-tile-label{{font-size:.74rem;color:var(--text-muted);font-weight:500;text-transform:uppercase;letter-spacing:.05em}}
.cc-tile-val{{font-size:1.02rem;font-weight:600;font-family:var(--font-mono);color:var(--text);
font-variant-numeric:tabular-nums;display:flex;align-items:center;gap:.35rem;overflow-wrap:anywhere;word-break:break-word;min-width:0}}
.cc-tile-sub{{font-size:.72rem;color:var(--text-dim);margin-top:.2rem}}

/* Row Metrics */
.row{{display:flex;justify-content:space-between;align-items:center;gap:.75rem;
padding:.65rem 0;border-bottom:1px solid var(--border-subtle);font-size:.85rem}}
.row:last-child{{border-bottom:none}}
.label{{color:var(--text-muted);font-size:.82rem;font-weight:400;display:inline-flex;align-items:center;gap:.4rem}}
.value{{font-weight:500;display:inline-flex;align-items:center;font-size:.84rem;font-family:var(--font-mono);
font-variant-numeric:tabular-nums}}
.dot{{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:.5rem;flex-shrink:0}}
.up{{color:var(--success)}} .down{{color:var(--danger)}} .warn{{color:var(--warning)}}
.dot.up{{background:var(--success);box-shadow:0 0 8px rgba(16,185,129,0.6)}}
.dot.down{{background:var(--danger)}} .dot.warn{{background:var(--warning)}}

/* Grid Layout */
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:0}}
.grid .row{{padding:.6rem 0}}
.grid .row:nth-child(odd){{padding-right:.9rem}}
.grid .row:nth-child(even){{padding-left:.9rem;border-left:1px solid var(--border-subtle)}}
@media(max-width:540px){{
  .grid{{grid-template-columns:1fr}}
  .grid .row{{grid-column:auto !important;padding-right:0 !important;padding-left:0 !important;border-left:none !important}}
}}

/* Buttons */
a.toggle,.btn,a.open{{display:inline-flex;align-items:center;justify-content:center;gap:.55rem;
text-align:center;padding:.65rem 1.05rem;min-height:44px;border-radius:var(--radius-md);
text-decoration:none;font-weight:500;font-size:.84rem;color:var(--text);
width:100%;background:rgba(255,255,255,0.035);border:1px solid var(--border);
transition:all .18s var(--ease);cursor:pointer}}
a.toggle:hover,.btn:hover,a.open:hover{{border-color:var(--border-hover);background:rgba(255,255,255,0.07);transform:translateY(-1px)}}
a.toggle:active,.btn:active,a.open:active{{transform:scale(.98)}}

a.toggle.btn-on{{background:rgba(16,185,129,0.12) !important;color:#34d399 !important;
border-color:rgba(16,185,129,0.3) !important;box-shadow:0 2px 10px rgba(16,185,129,0.1)}}
a.toggle.btn-off{{background:rgba(239,68,68,0.12) !important;color:#f87171 !important;
border-color:rgba(239,68,68,0.3) !important;box-shadow:0 2px 10px rgba(239,68,68,0.1)}}

a.open{{background:linear-gradient(135deg,#1d4ed8,#2563eb);color:#fff;border-color:rgba(255,255,255,0.1);
box-shadow:0 4px 14px rgba(37,99,235,0.3)}}
a.open:hover{{background:linear-gradient(135deg,#2563eb,#3b82f6);box-shadow:0 6px 18px rgba(37,99,235,0.45);color:#fff}}

.btn-row{{display:grid;grid-template-columns:1fr;gap:.65rem;width:100%}}
@media (min-width: 600px){{
  .btn-row{{grid-template-columns:repeat(auto-fit, minmax(200px, 1fr))}}
}}

/* Unified Compact Action Buttons */
.btn-action-sm{{
  width:auto;min-height:32px;padding:.32rem .75rem;font-size:.75rem;font-weight:500;
  border-radius:var(--radius-sm);border:1px solid var(--border);background:rgba(255,255,255,0.05);
  color:var(--text);cursor:pointer;white-space:nowrap;transition:all .15s ease;
  text-decoration:none;display:inline-flex;align-items:center;gap:.35rem;margin:0;
}}
.btn-action-sm:hover{{background:rgba(255,255,255,0.12);border-color:var(--border-hover);color:#fff}}
.btn-action-danger{{background:rgba(239,68,68,0.12);color:#fca5a5;border:1px solid rgba(239,68,68,0.3)}}
.btn-action-danger:hover{{background:rgba(239,68,68,0.25);border-color:var(--danger);color:#fff}}
.btn-action-primary{{background:rgba(59,130,246,0.18);color:var(--accent-light);border:1px solid rgba(59,130,246,0.4)}}
.btn-action-primary:hover{{background:rgba(59,130,246,0.32);border-color:var(--accent);color:#fff}}
.btn-action-sm.active{{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}}
.badge-danger{{background:var(--danger-dim);color:var(--danger);border:1px solid rgba(239,68,68,0.3);padding:.15rem .45rem;border-radius:4px;font-size:.68rem;font-weight:600}}

/* Gateway Platform Card Responsive Elements */
.gw-card-row{{display:flex;flex-direction:column;gap:.6rem;padding:.75rem .9rem;
background:rgba(255,255,255,0.02);border:1px solid var(--border);border-radius:var(--radius-sm);margin-bottom:.55rem;
transition:background .15s ease,border-color .15s ease}}
.gw-card-row:hover{{background:rgba(255,255,255,0.035);border-color:rgba(255,255,255,0.12)}}
.gw-card-top{{display:flex;align-items:center;justify-content:space-between;gap:.75rem;min-width:0;width:100%}}
.gw-card-left{{display:flex;align-items:center;gap:.75rem;min-width:0;flex:1}}
.gw-card-info{{min-width:0;flex:1}}
.gw-card-title-wrap{{display:flex;align-items:center;gap:.45rem;flex-wrap:wrap;line-height:1.2}}
.gw-card-title{{font-size:.85rem;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.gw-card-meta{{font-family:var(--font-mono);font-size:.72rem;color:var(--text-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:.15rem}}
.gw-card-badge-wrap{{flex-shrink:0;display:flex;align-items:center}}
.gw-card-bottom{{display:flex;align-items:center;justify-content:flex-end;gap:.45rem;padding-top:.45rem;border-top:1px solid rgba(255,255,255,0.05);flex-wrap:wrap;width:100%}}

/* Model Selector Chips & Groups */
.models-container{{display:flex;flex-direction:column;gap:1.1rem;width:100%}}
.model-group-title{{display:flex;align-items:center;gap:.4rem;font-size:.72rem;font-family:var(--font-mono);
font-weight:600;color:var(--text-dim);text-transform:uppercase;letter-spacing:.08em;margin-bottom:.55rem}}
.models-grid{{display:grid;grid-template-columns:repeat(auto-fill, minmax(210px, 1fr));gap:.55rem;width:100%}}

.model-chip{{
  padding:.55rem .75rem;min-height:42px;border-radius:var(--radius-md);font-size:.78rem;
  font-family:var(--font-mono);background:rgba(255,255,255,0.03);color:var(--text-muted);
  text-decoration:none;display:flex;align-items:center;justify-content:space-between;gap:.45rem;
  border:1px solid var(--border-subtle);transition:all .18s var(--ease);
  box-sizing:border-box;overflow:hidden;
}}
a.model-chip:hover{{
  background:rgba(255,255,255,0.07);color:var(--text);border-color:var(--border-hover);
  transform:translateY(-1px);box-shadow:0 3px 10px rgba(0,0,0,0.25);
}}
a.model-chip:active{{transform:scale(.98)}}
.model-chip.active{{
  background:rgba(59,130,246,0.16) !important;color:#bfdbfe !important;
  font-weight:600;border-color:rgba(96,165,250,0.55) !important;box-shadow:0 0 16px rgba(59,130,246,0.22);
}}
.model-chip-content{{
  display:flex;align-items:center;gap:.45rem;overflow:hidden;min-width:0;flex:1;
}}
.model-chip-name{{
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:500;
}}
.model-chip-badge{{
  flex-shrink:0;font-size:.65rem;font-weight:600;padding:.15rem .4rem;border-radius:4px;
  background:rgba(255,255,255,0.07);color:var(--text-dim);letter-spacing:.02em;
}}
.model-chip.active .model-chip-badge{{
  background:rgba(59,130,246,0.25);color:#bfdbfe;
}}
@media (max-width:480px){{
  .models-grid{{grid-template-columns:1fr;}}
  #gw-config-form-view div[style*="grid-template-columns"]{{grid-template-columns:1fr !important;}}
}}

/* Utilities & Modals */
.search-input{{width:100%;padding:.65rem .9rem;min-height:42px;border-radius:var(--radius-md);
font-family:var(--font-mono);font-size:.8rem;background:rgba(255,255,255,0.035);
border:1px solid var(--border);color:var(--text);outline:none;
transition:border-color .18s var(--ease);margin-bottom:.9rem}}
.search-input:focus{{border-color:var(--accent-light);box-shadow:0 0 0 2px rgba(59,130,246,0.25)}}
.search-input::placeholder{{color:var(--text-dim)}}
.logbox{{background:#030509;border:1px solid var(--border);border-radius:var(--radius-lg);
padding:.9rem;font-family:var(--font-mono);font-size:.76rem;color:#34d399;
white-space:pre-wrap;word-break:break-word;max-height:190px;overflow-y:auto;line-height:1.45}}
.update-hint{{font-size:.8rem;color:var(--text-muted);text-align:center;
background:rgba(255,255,255,0.03);border:1px solid var(--border);border-radius:var(--radius-md);
padding:.65rem .85rem;display:flex;align-items:center;justify-content:center;gap:.4rem}}
.update-hint.up{{border-color:rgba(16,185,129,0.35);background:rgba(16,185,129,0.06);color:var(--text)}}
.update-hint.down{{border-color:rgba(239,68,68,0.35);background:rgba(239,68,68,0.06);color:var(--danger)}}
.update-hint.warn{{border-color:rgba(245,158,11,0.35);background:rgba(245,158,11,0.06);color:var(--warn)}}
.clean-log-header{{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:0.45rem}}
@media (max-width: 640px){{
  .clean-log-header{{flex-direction:column;align-items:stretch}}
  .clean-log-header .btn-action-sm{{align-self:flex-end;width:auto}}
}}
.hint-pill{{font-size:.78rem;color:var(--text-muted);padding:.5rem .85rem;border-radius:var(--radius-md);
background:rgba(255,255,255,0.035);border:1px solid var(--border);display:inline-flex;
align-items:center;justify-content:center;width:100%;text-align:center}}
#pbar{{position:fixed;top:0;left:0;height:2px;width:0;background:var(--accent-light);z-index:100}}
#spin{{display:inline-block;width:12px;height:12px;border:2px solid var(--border);
border-top-color:var(--accent-light);border-radius:50%;vertical-align:-1px;
opacity:0;transition:opacity .2s;animation:spin .8s linear infinite}}
#spin.on{{opacity:1}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
#navloader{{position:fixed;inset:0;background:rgba(7,9,14,0.85);backdrop-filter:blur(10px);
display:flex;align-items:center;justify-content:center;flex-direction:column;gap:1rem;
opacity:0;pointer-events:none;transition:opacity .15s var(--ease);z-index:200}}
#navloader.show{{opacity:1;pointer-events:auto}}
#navloader .ring{{width:32px;height:32px;border:2px solid var(--border);
border-top-color:var(--accent-light);border-radius:50%;animation:spin .7s linear infinite}}
#navloader span{{color:var(--text-muted);font-size:.82rem;font-family:var(--font-mono)}}
/* Confirm modal */
#confirm-modal, #aux-picker-modal, #gw-config-modal, #wa-pair-modal{{position:fixed;inset:0;background:rgba(7,9,14,0.85);backdrop-filter:blur(8px);
display:none;align-items:center;justify-content:center;z-index:300;padding:1.5rem}}
#confirm-modal.show, #aux-picker-modal.show, #gw-config-modal.show, #wa-pair-modal.show{{display:flex}}
.confirm-box{{background:rgba(22,27,38,0.95);border:1px solid var(--border-hover);
border-radius:var(--radius-xl);padding:1.6rem 1.5rem;max-width:360px;width:100%;
box-shadow:0 12px 48px rgba(0,0,0,0.7)}}
.confirm-box h3{{font-size:1rem;font-weight:600;margin-bottom:.5rem}}
.confirm-box p{{font-size:.82rem;color:var(--text-muted);margin-bottom:1.3rem;line-height:1.5}}
.confirm-actions{{display:flex;gap:.6rem}}
.confirm-actions .btn{{flex:1;margin:0}}
.btn-danger{{background:rgba(239,68,68,0.18);color:#fca5a5;border:1px solid rgba(239,68,68,0.4)}}
.btn-danger:hover{{background:rgba(239,68,68,0.3);border-color:var(--danger);color:#fff}}

/* Tugas Tambahan */
.aux-header{{display:flex;justify-content:space-between;align-items:center;gap:.75rem;margin-bottom:.6rem;flex-wrap:wrap}}
.aux-desc{{font-size:.8rem;color:var(--text-muted);line-height:1.4;margin-bottom:1rem}}
.aux-task-row{{display:flex;align-items:center;justify-content:space-between;gap:.75rem;padding:.75rem .9rem;
background:rgba(255,255,255,0.02);border:1px solid var(--border);border-radius:var(--radius-sm);margin-bottom:.5rem;
transition:background .15s ease,border-color .15s ease}}
.aux-task-row:hover{{background:rgba(255,255,255,0.04);border-color:rgba(255,255,255,0.15)}}
.aux-task-info{{min-width:0;flex:1}}
.aux-task-title{{display:flex;align-items:baseline;gap:.5rem;flex-wrap:wrap;margin-bottom:.2rem}}
.aux-task-name{{font-size:.82rem;font-weight:600;color:var(--text)}}
.aux-task-hint{{font-size:.72rem;color:var(--text-dim)}}
.mono-sub{{font-family:var(--font-mono);font-size:.74rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.mono-sub.auto{{color:var(--text-dim)}}
.mono-sub.custom{{color:var(--success)}}
.btn-aux-change{{width:auto;min-height:32px;padding:.32rem .75rem;font-size:.75rem;font-weight:500;
border-radius:var(--radius-sm);border:1px solid var(--border);background:rgba(255,255,255,0.05);
color:var(--text);cursor:pointer;white-space:nowrap;transition:all .15s ease;margin:0}}
.btn-aux-change:hover{{background:rgba(255,255,255,0.12);border-color:var(--border-hover);color:#fff}}
.aux-model-opt{{display:flex;align-items:center;justify-content:space-between;gap:.5rem;padding:.6rem .8rem;
background:rgba(255,255,255,0.03);border:1px solid var(--border);border-radius:var(--radius-sm);
cursor:pointer;text-decoration:none;transition:all .15s ease}}
.aux-model-opt:hover{{background:rgba(255,255,255,0.08);border-color:var(--border-hover);transform:translateY(-1px)}}
.aux-model-opt-name{{font-family:var(--font-mono);font-size:.78rem;font-weight:600;color:var(--text)}}
.aux-model-opt-sub{{font-size:.7rem;color:var(--text-dim)}}

/* Process Table Elements */
.task-table-wrap{{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:var(--radius-sm);border:1px solid var(--border);background:rgba(255,255,255,0.015);margin-bottom:.5rem}}
.task-table{{width:100%;border-collapse:collapse;font-size:.78rem;text-align:left}}
.task-table th{{background:rgba(255,255,255,0.04);color:var(--text-muted);font-size:.68rem;text-transform:uppercase;letter-spacing:.05em;padding:.65rem .85rem;border-bottom:1px solid var(--border);white-space:nowrap;font-family:var(--font-mono)}}
.task-table td{{padding:.75rem .85rem;border-bottom:1px solid rgba(255,255,255,0.04);vertical-align:middle}}
.task-table tr:hover td{{background:rgba(255,255,255,0.03)}}
.task-table tr:last-child td{{border-bottom:none}}
.task-name-cell{{display:flex;align-items:center;gap:.45rem;font-weight:500}}
.badge{{display:inline-block;padding:.15rem .45rem;border-radius:4px;font-size:.68rem;font-weight:600;background:rgba(255,255,255,0.05);color:var(--text-dim);border:1px solid rgba(255,255,255,0.08)}}
.badge-up{{background:var(--success-dim);color:var(--success);border:1px solid rgba(16,185,129,0.3);padding:.15rem .45rem;border-radius:4px;font-size:.68rem;font-weight:600}}
.badge-down{{background:var(--danger-dim);color:var(--danger);border:1px solid rgba(239,68,68,0.3);padding:.15rem .45rem;border-radius:4px;font-size:.68rem;font-weight:600}}
.badge-warn{{background:var(--warning-dim);color:var(--warning);border:1px solid rgba(245,158,11,0.3);padding:.15rem .45rem;border-radius:4px;font-size:.68rem;font-weight:600}}
.badge-muted{{background:rgba(255,255,255,0.05);color:var(--text-dim);border:1px solid rgba(255,255,255,0.08);padding:.15rem .45rem;border-radius:4px;font-size:.68rem;font-weight:600}}
.btn-end-task{{padding:.28rem .6rem;font-size:.72rem;border-radius:var(--radius-sm);border:1px solid rgba(239,68,68,0.35);background:rgba(239,68,68,0.12);color:#fca5a5;text-decoration:none;display:inline-block;cursor:pointer;font-weight:500;transition:all .15s ease}}
.btn-end-task:hover{{background:rgba(239,68,68,0.28);border-color:var(--danger);color:#fff}}
.btn-restart-task{{padding:.28rem .6rem;font-size:.72rem;border-radius:var(--radius-sm);border:1px solid var(--border);background:rgba(255,255,255,0.06);color:var(--text);text-decoration:none;display:inline-block;cursor:pointer;margin-left:.3rem;transition:all .15s ease}}
.btn-restart-task:hover{{background:rgba(255,255,255,0.14);color:#fff}}
.btn-start-task{{padding:.28rem .6rem;font-size:.72rem;border-radius:var(--radius-sm);border:1px solid rgba(16,185,129,0.35);background:rgba(16,185,129,0.12);color:#6ee7b7;text-decoration:none;display:inline-block;cursor:pointer;font-weight:500}}
.btn-start-task:hover{{background:rgba(16,185,129,0.28);color:#fff}}

.task-table .td-label{{display:none}}
/* Mobile: process table becomes stacked cards (no horizontal scroll) */
@media (max-width:640px){{
  .task-table thead{{display:none}}
  .task-table,.task-table tbody,.task-table tr,.task-table td{{display:block;width:100%}}
  .task-table tr{{border:1px solid var(--border);border-radius:var(--radius-md);margin-bottom:.6rem;padding:.5rem .7rem;background:rgba(255,255,255,0.02)}}
  .task-table tr:hover td{{background:transparent}}
  .task-table td{{border:none!important;padding:.28rem 0!important;text-align:left!important}}
  .task-table td[data-c="aksi"]{{display:flex;gap:.45rem;flex-wrap:wrap;justify-content:flex-start;padding-top:.45rem!important}}
  .task-table .td-label{{display:inline-block;font-size:.68rem;color:var(--text-dim);font-family:var(--font-mono);text-transform:uppercase;letter-spacing:.05em;margin-right:.5rem;min-width:52px}}
  .task-table td[data-c="pid"],.task-table td[data-c="mem"]{{display:flex;align-items:baseline;justify-content:flex-start;text-align:left!important}}
  .task-table td[data-c="pid"] .td-val,.task-table td[data-c="mem"] .td-val{{margin-left:auto;font-variant-numeric:tabular-nums}}
  .task-table td[data-c="status"]{{display:flex;align-items:center;gap:.5rem}}
}}

/* Performance Sparkline Cards */
.perf-tile{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-xl);padding:1.35rem;margin-bottom:1.15rem;box-shadow:0 6px 24px rgba(0,0,0,0.3)}}
.perf-header{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:.5rem}}
.perf-title{{font-size:.84rem;font-weight:600;color:var(--text);display:flex;align-items:center;gap:.45rem}}
.perf-sub{{font-size:.74rem;color:var(--text-muted);margin-top:.2rem}}
.perf-big-val{{font-size:1.6rem;font-weight:700;font-family:var(--font-mono);color:var(--accent-light);letter-spacing:-.02em}}
.sparkline-box{{width:100%;height:85px;background:rgba(0,0,0,0.32);border-radius:var(--radius-md);border:1px solid rgba(255,255,255,0.06);overflow:hidden;position:relative;margin-top:.75rem}}
.sparkline-svg{{width:100%;height:100%;display:block}}
.spark-poly{{fill:none;stroke:var(--accent-light);stroke-width:2;vector-effect:non-scaling-stroke}}
.spark-fill{{fill:rgba(96,165,250,0.12);stroke:none}}
.spark-poly.ram{{stroke:#c084fc}}
.spark-fill.ram{{fill:rgba(192,132,252,0.12)}}

/* 2-column layout for Performance Storage & Network on Desktop */
.perf-grid-2col{{display:grid;grid-template-columns:1fr;gap:1.15rem}}
@media (min-width: 768px){{
  .perf-grid-2col{{grid-template-columns:1fr 1fr}}
  .perf-grid-2col .card{{margin-top:0 !important;margin-bottom:0 !important}}
}}

/* Patch Notes */
.patch-notes-box{{margin-top:.75rem;padding:.75rem .85rem;background:rgba(0,0,0,0.22);border:1px solid rgba(255,255,255,0.07);border-radius:var(--radius-sm);font-size:.78rem;line-height:1.45;text-align:left}}
.patch-notes-title{{font-weight:600;color:var(--text);margin-bottom:.4rem;display:flex;align-items:center;gap:.35rem;font-size:.76rem;letter-spacing:.02em}}
.patch-notes-list{{margin:0;padding-left:1.15rem;color:var(--text-muted)}}
.patch-notes-list li{{margin-bottom:.3rem;word-break:break-word}}
.patch-notes-list li:last-child{{margin-bottom:0}}
.patch-notes-pages{{display:flex;align-items:center;justify-content:flex-end;gap:.45rem;margin-top:.65rem}}
.patch-notes-pages button{{width:auto;min-height:30px;padding:.25rem .6rem;font-size:.7rem}}
.patch-notes-page-label{{min-width:5.5rem;text-align:center;font:600 .68rem var(--font-mono);color:var(--text-dim)}}

/* Responsive Desktop Overrides */
@media (min-width: 768px) {{
  body {{ padding: 2.2rem 2.2rem 3rem; }}
  #tab-control a.toggle,
  #tab-control a.open,
  #tab-control .card-warn a.toggle {{
    min-height: 42px !important;
    padding: .5rem 1rem !important;
    font-size: .82rem !important;
  }}
}}

@media (min-width: 1024px) {{
  body {{ padding: 2.4rem 2.5rem 3.5rem; }}
  .header {{ max-width:1040px; margin:0 auto 1.5rem; }}
  .tabs {{ max-width:520px; margin:0 auto 1.5rem; }}
  .card, .perf-tile {{ padding:1.4rem; }}
  .models-grid {{ grid-template-columns:repeat(auto-fill, minmax(240px, 1fr)); }}
}}

@media (min-width: 1440px) {{
  body {{ padding-left:4rem; padding-right:4rem; }}
  .header, .content-wrapper, #tab-status {{ max-width:1120px; }}
}}
</style></head><body>
<div id="pbar"></div>
<div id="navloader"><div class="ring"></div><span id="nav-label">Memproses…</span></div>
<div id="confirm-modal">
  <div class="confirm-box">
    <h3 id="confirm-title">Konfirmasi</h3>
    <p id="confirm-msg"></p>
    <div class="confirm-actions">
      <button type="button" class="btn" id="confirm-cancel">Batal</button>
      <button type="button" class="btn btn-danger" id="confirm-ok">Lanjutkan</button>
    </div>
  </div>
</div>
<div id="aux-picker-modal">
  <div class="confirm-box" style="max-width:540px;width:92%;max-height:85vh;display:flex;flex-direction:column;padding:1.25rem">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.75rem">
      <h3 id="aux-picker-title" style="margin:0;font-size:1.05rem">Pilih Model</h3>
      <button type="button" class="btn" style="width:auto;padding:0.25rem 0.6rem;font-size:0.85rem;line-height:1;margin:0" onclick="closeAuxPicker()">✕</button>
    </div>
    <input type="text" id="aux-model-search" class="search-input" placeholder="Cari model… (saring)" oninput="filterAuxPicker(this.value)" style="margin-bottom:0.8rem">
    <div id="aux-picker-list" style="overflow-y:auto;flex:1;max-height:55vh;display:flex;flex-direction:column;gap:0.45rem;padding-right:2px">
    </div>
  </div>
</div>
<div id="gw-config-modal">
  <div class="confirm-box" style="max-width:580px;width:94%;max-height:88vh;display:flex;flex-direction:column;padding:1.4rem;overflow-y:auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.75rem">
      <h3 id="gw-config-title" style="margin:0;font-size:1.05rem">Konfigurasi Gateway</h3>
      <button type="button" class="btn" style="width:auto;padding:0.25rem 0.6rem;font-size:0.85rem;line-height:1;margin:0" onclick="closeGwConfig()">✕</button>
    </div>

    <div id="gw-platform-select-wrap" style="margin-bottom:0.75rem;display:none">
      <label style="font-size:0.75rem;color:var(--text-muted);display:block;margin-bottom:0.25rem">Pilih Platform Gateway Hermes Resmi</label>
      <select id="gw-platform-catalog-select" class="search-input" onchange="selectCatalogPlatform(this.value)" style="margin-bottom:0.45rem;background:rgba(255,255,255,0.06);color:var(--text);border:1px solid var(--border);border-radius:var(--radius-sm);cursor:pointer;font-size:0.8rem">
        <option value="">-- Pilih dari 21 Platform Resmi Hermes --</option>
        <optgroup label="Platform Populer">
          <option value="telegram">Telegram Bot</option>
          <option value="discord">Discord Bot</option>
          <option value="webhook">HTTP Webhook Listener</option>
          <option value="whatsapp">WhatsApp Multi-Device Bridge</option>
          <option value="slack">Slack Bot (Socket Mode)</option>
          <option value="line">LINE Messaging API</option>
        </optgroup>
        <optgroup label="Kolaborasi &amp; Terdesentralisasi">
          <option value="matrix">Matrix (Synapse/Dendrite)</option>
          <option value="mattermost">Mattermost Bot</option>
          <option value="irc">IRC (Internet Relay Chat)</option>
        </optgroup>
        <optgroup label="Enterprise &amp; Workspace">
          <option value="teams">Microsoft Teams (Azure Bot)</option>
          <option value="feishu">Feishu / Lark</option>
          <option value="google_chat">Google Chat</option>
          <option value="dingtalk">DingTalk</option>
          <option value="wecom">WeCom / WeChat Work</option>
        </optgroup>
        <optgroup label="Privasi &amp; Keamanan">
          <option value="signal">Signal Messenger (signal-cli)</option>
          <option value="simplex">SimpleX Chat</option>
        </optgroup>
        <optgroup label="Otomasi, Notifikasi &amp; Lainnya">
          <option value="ntfy">ntfy.sh Push Notifications</option>
          <option value="email">Email Gateway (IMAP/SMTP)</option>
          <option value="homeassistant">Home Assistant</option>
          <option value="sms">SMS Gateway (Twilio)</option>
          <option value="bluebubbles">BlueBubbles (iMessage)</option>
        </optgroup>
        <optgroup label="Kustom">
          <option value="custom">Kustom (Ketik Nama Platform Sendiri)</option>
        </optgroup>
      </select>
      <input type="text" id="gw-platform-input" class="search-input" placeholder="Identifier platform (contoh: telegram, discord...)" style="margin-bottom:0.45rem;font-size:0.78rem">
      <div style="display:flex;gap:4px;flex-wrap:wrap">
        <span class="model-chip" style="font-size:0.7rem;padding:0.15rem 0.45rem;cursor:pointer" onclick="selectCatalogPlatform('telegram')">Telegram</span>
        <span class="model-chip" style="font-size:0.7rem;padding:0.15rem 0.45rem;cursor:pointer" onclick="selectCatalogPlatform('discord')">Discord</span>
        <span class="model-chip" style="font-size:0.7rem;padding:0.15rem 0.45rem;cursor:pointer" onclick="selectCatalogPlatform('webhook')">Webhook</span>
        <span class="model-chip" style="font-size:0.7rem;padding:0.15rem 0.45rem;cursor:pointer" onclick="selectCatalogPlatform('whatsapp')">WhatsApp</span>
        <span class="model-chip" style="font-size:0.7rem;padding:0.15rem 0.45rem;cursor:pointer" onclick="selectCatalogPlatform('slack')">Slack</span>
        <span class="model-chip" style="font-size:0.7rem;padding:0.15rem 0.45rem;cursor:pointer" onclick="selectCatalogPlatform('matrix')">Matrix</span>
        <span class="model-chip" style="font-size:0.7rem;padding:0.15rem 0.45rem;cursor:pointer" onclick="selectCatalogPlatform('signal')">Signal</span>
        <span class="model-chip" style="font-size:0.7rem;padding:0.15rem 0.45rem;cursor:pointer" onclick="selectCatalogPlatform('mattermost')">Mattermost</span>
        <span class="model-chip" style="font-size:0.7rem;padding:0.15rem 0.45rem;cursor:pointer" onclick="selectCatalogPlatform('teams')">Teams</span>
        <span class="model-chip" style="font-size:0.7rem;padding:0.15rem 0.45rem;cursor:pointer" onclick="selectCatalogPlatform('ntfy')">ntfy</span>
        <span class="model-chip" style="font-size:0.7rem;padding:0.15rem 0.45rem;cursor:pointer" onclick="selectCatalogPlatform('email')">Email</span>
      </div>
    </div>

    <!-- Template & Mode Bar (Universal toolbar visible in both Form UI and YAML modes) -->
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.6rem;flex-wrap:wrap;gap:0.4rem;background:rgba(255,255,255,0.02);padding:0.4rem 0.6rem;border-radius:var(--radius-sm);border:1px solid var(--border)">
      <div style="display:flex;align-items:center;gap:0.4rem;flex-wrap:wrap">
        <span style="font-size:0.72rem;color:var(--text-muted);font-weight:600">📋 Template:</span>
        <select id="gw-template-picker" onchange="applyGwSelectedTemplate(this.value)" class="search-input" style="width:auto;margin:0;padding:0.2rem 0.5rem;font-size:0.72rem;background:rgba(255,255,255,0.06);color:var(--accent-light);border:1px solid rgba(59,130,246,0.3);border-radius:var(--radius-sm);cursor:pointer">
          <option value="">-- Muat Template Gateway --</option>
          <optgroup label="Populer">
            <option value="telegram">Telegram Bot</option>
            <option value="discord">Discord Bot</option>
            <option value="webhook">HTTP Webhook</option>
            <option value="whatsapp">WhatsApp Bridge</option>
            <option value="slack">Slack Bot</option>
            <option value="line">LINE Messaging</option>
          </optgroup>
          <optgroup label="Kolaborasi &amp; Chat">
            <option value="matrix">Matrix</option>
            <option value="mattermost">Mattermost</option>
            <option value="irc">IRC</option>
          </optgroup>
          <optgroup label="Enterprise">
            <option value="teams">Microsoft Teams</option>
            <option value="feishu">Feishu / Lark</option>
            <option value="google_chat">Google Chat</option>
            <option value="dingtalk">DingTalk</option>
            <option value="wecom">WeCom</option>
          </optgroup>
          <optgroup label="Privasi &amp; Notifikasi">
            <option value="signal">Signal</option>
            <option value="simplex">SimpleX</option>
            <option value="ntfy">ntfy</option>
            <option value="email">Email Gateway</option>
            <option value="homeassistant">Home Assistant</option>
            <option value="sms">SMS Gateway</option>
            <option value="bluebubbles">BlueBubbles</option>
          </optgroup>
        </select>
      </div>
      <div style="display:flex;align-items:center;gap:0.4rem;flex-wrap:wrap">
        <div style="display:flex;gap:0.25rem;background:rgba(255,255,255,0.04);padding:2px;border-radius:var(--radius-sm);border:1px solid var(--border)">
          <button type="button" class="btn-action-sm active" id="gw-btn-mode-ui" onclick="switchGwConfigMode('ui')" style="min-height:26px;font-size:0.72rem;padding:0.2rem 0.5rem">🎛 Form Setting (Full UI)</button>
          <button type="button" class="btn-action-sm" id="gw-btn-mode-yaml" onclick="switchGwConfigMode('yaml')" style="min-height:26px;font-size:0.72rem;padding:0.2rem 0.5rem">📝 Raw YAML (Manual)</button>
        </div>
        <label style="font-size:0.72rem;display:inline-flex;align-items:center;gap:0.3rem;cursor:pointer;background:rgba(255,255,255,0.04);padding:0.2rem 0.45rem;border-radius:var(--radius-sm);border:1px solid var(--border);margin:0">
          <input type="checkbox" id="gw-config-enabled-chk" checked style="accent-color:var(--accent)">
          <span style="font-weight:600">Aktifkan</span>
        </label>
      </div>
    </div>

    <!-- FORM UI VIEW (No coding, visual settings) -->
    <div id="gw-config-form-view" style="display:flex;flex-direction:column;gap:0.6rem;max-height:52vh;overflow-y:auto;padding-right:4px">
      <!-- WhatsApp Specific Banner / Quick Pair -->
      <div id="gw-form-wa-banner" style="display:none;background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.3);border-radius:var(--radius-sm);padding:0.6rem 0.8rem;align-items:center;justify-content:space-between;gap:0.6rem;flex-wrap:wrap">
        <div>
          <div style="font-weight:600;font-size:0.8rem;color:#10b981;display:flex;align-items:center;gap:5px">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"></path></svg>
            Jembatan WhatsApp Baileys
          </div>
          <div style="font-size:0.72rem;color:var(--text-muted);margin-top:2px">Tautkan sesi WhatsApp web menggunakan kamera HP tanpa token API.</div>
        </div>
        <button type="button" class="btn btn-action-sm btn-action-primary" style="background:rgba(16,185,129,0.2);color:#10b981;border-color:rgba(16,185,129,0.4)" onclick="closeGwConfig();openWaPairModal();">📱 Buka Pairing QR</button>
      </div>

      <!-- Mode & Port (WhatsApp only) -->
      <div id="gw-form-wa-fields" style="display:none;grid-template-columns:1fr 1fr;gap:0.5rem">
        <div style="display:flex;flex-direction:column;gap:3px">
          <label style="font-size:0.72rem;color:var(--text-muted);font-weight:600">Mode Operasi</label>
          <select id="gw-f-wa-mode" class="search-input" style="margin:0;font-size:0.75rem;padding:0.35rem 0.5rem">
            <option value="bot">Bot Dedicated (Akun Bot Terpisah)</option>
            <option value="self-chat">Self-Chat (Akun Pribadi/Catatan Sendiri)</option>
          </select>
        </div>
        <div style="display:flex;flex-direction:column;gap:3px">
          <label style="font-size:0.72rem;color:var(--text-muted);font-weight:600">Port Jembatan Bridge</label>
          <input type="number" id="gw-f-wa-port" class="search-input" value="3000" style="margin:0;font-size:0.75rem;padding:0.35rem 0.5rem">
        </div>
      </div>

      <!-- Platform-Specific Connection & Credentials Section -->
      <div id="gw-form-conn-box" style="background:rgba(255,255,255,0.02);border:1px solid var(--border);border-radius:var(--radius-sm);padding:0.6rem;display:flex;flex-direction:column;gap:0.45rem">
        <div style="font-size:0.75rem;font-weight:600;color:var(--accent-light);display:flex;align-items:center;justify-content:space-between">
          <span>🔑 Kredensial &amp; Koneksi Platform (<span id="gw-form-conn-title">Telegram</span>)</span>
          <span style="font-size:0.68rem;color:var(--text-dim);font-weight:normal">* Sesuai spesifikasi resmi Hermes</span>
        </div>
        <div id="gw-form-conn-fields" style="display:grid;grid-template-columns:1fr 1fr;gap:0.5rem"></div>
      </div>

      <!-- DM Policy & Allowlist -->
      <div style="background:rgba(255,255,255,0.02);border:1px solid var(--border);border-radius:var(--radius-sm);padding:0.6rem;display:flex;flex-direction:column;gap:0.45rem">
        <div style="font-size:0.75rem;font-weight:600;color:var(--accent-light)">🔒 Hak Akses Obrolan Pribadi (DM)</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.5rem">
          <div style="display:flex;flex-direction:column;gap:3px">
            <label style="font-size:0.7rem;color:var(--text-dim)">Kebijakan DM (<code>dm_policy</code>)</label>
            <select id="gw-f-dm-policy" class="search-input" style="margin:0;font-size:0.75rem;padding:0.3rem 0.5rem">
              <option value="">default Hermes (pairing)</option>
              <option value="open">open (Siapa saja boleh chat)</option>
              <option value="allowlist">allowlist (Hanya nomor/user terdaftar)</option>
              <option value="pairing">pairing (Wajib kode verifikasi)</option>
              <option value="disabled">disabled (Nonaktifkan chat pribadi)</option>
            </select>
          </div>
          <div style="display:flex;flex-direction:column;gap:3px">
            <label style="font-size:0.7rem;color:var(--text-dim)">Penyampaian Notifikasi</label>
            <select id="gw-f-notice-del" class="search-input" style="margin:0;font-size:0.75rem;padding:0.3rem 0.5rem">
              <option value="">default Hermes</option>
              <option value="public">public (Tampilkan di obrolan)</option>
              <option value="private">private (Kirim khusus ke admin)</option>
            </select>
          </div>
        </div>
        <div style="display:flex;flex-direction:column;gap:3px">
          <label style="font-size:0.7rem;color:var(--text-dim)">Nomor / User yang Diizinkan (<code>allow_from</code>)</label>
          <input type="text" id="gw-f-allow-from" class="search-input" placeholder="contoh: 6283197961899, 6282258948478" style="margin:0;font-size:0.75rem;padding:0.35rem 0.5rem">
          <div style="font-size:0.68rem;color:var(--text-dim)">* Pisahkan beberapa nomor/ID dengan koma. Untuk WhatsApp gunakan kode negara (contoh 62).</div>
        </div>
        <div style="display:flex;flex-direction:column;gap:3px">
          <label style="font-size:0.7rem;color:var(--text-dim)">Nomor Admin Penuh (<code>allow_admin_from</code>)</label>
          <input type="text" id="gw-f-allow-admin" class="search-input" placeholder="contoh: 6283197961899" style="margin:0;font-size:0.75rem;padding:0.35rem 0.5rem">
          <div style="font-size:0.68rem;color:var(--text-dim)">* Admin memiliki akses eksekusi perintah sistem dan modifikasi agent.</div>
        </div>
      </div>

      <!-- Group Settings -->
      <div style="background:rgba(255,255,255,0.02);border:1px solid var(--border);border-radius:var(--radius-sm);padding:0.6rem;display:flex;flex-direction:column;gap:0.45rem">
        <div style="font-size:0.75rem;font-weight:600;color:var(--accent-light)">👥 Pengaturan Grup (Group Chat)</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.5rem">
          <div style="display:flex;flex-direction:column;gap:3px">
            <label style="font-size:0.7rem;color:var(--text-dim)">Kebijakan Grup (<code>group_policy</code>)</label>
            <select id="gw-f-group-policy" class="search-input" style="margin:0;font-size:0.75rem;padding:0.3rem 0.5rem">
              <option value="">default Hermes (pairing)</option>
              <option value="open">open (Aktif di semua grup)</option>
              <option value="allowlist">allowlist (Hanya grup terdaftar)</option>
              <option value="pairing">pairing (Wajib kode verifikasi)</option>
              <option value="disabled">disabled (Abaikan pesan grup)</option>
            </select>
          </div>
          <div style="display:flex;flex-direction:column;gap:3px">
            <label style="font-size:0.7rem;color:var(--text-dim)">Grup Diizinkan (<code>group_allow_from</code>)</label>
            <input type="text" id="gw-f-group-allow" class="search-input" placeholder="contoh: 12036302...@g.us" style="margin:0;font-size:0.75rem;padding:0.35rem 0.5rem">
          </div>
        </div>
      </div>

      <!-- Toggles & Interaction -->
      <div style="background:rgba(255,255,255,0.02);border:1px solid var(--border);border-radius:var(--radius-sm);padding:0.6rem;display:flex;flex-direction:column;gap:0.4rem">
        <div style="font-size:0.75rem;font-weight:600;color:var(--accent-light)">⚙ Perilaku Pesan &amp; Respon</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.4rem">
          <label style="font-size:0.72rem;display:inline-flex;align-items:center;gap:0.4rem;cursor:pointer">
            <input type="checkbox" id="gw-f-req-mention" style="accent-color:var(--accent)">
            <span>Wajib mention / tag bot</span>
          </label>
          <label style="font-size:0.72rem;display:inline-flex;align-items:center;gap:0.4rem;cursor:pointer">
            <input type="checkbox" id="gw-f-reply-thread" style="accent-color:var(--accent)">
            <span>Balas dalam thread / quote</span>
          </label>
          <label style="font-size:0.72rem;display:inline-flex;align-items:center;gap:0.4rem;cursor:pointer">
            <input type="checkbox" id="gw-f-read-receipts" style="accent-color:var(--accent)">
            <span>Kirim centang biru (read)</span>
          </label>
        </div>
      </div>
    </div>

    <!-- RAW YAML VIEW (Advanced / Manual) -->
    <div id="gw-config-yaml-view" style="display:none;flex-direction:column;gap:0.4rem">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:0.2rem;flex-wrap:wrap;gap:0.4rem">
        <label style="font-size:0.75rem;color:var(--text-muted);font-weight:600">Editor YAML (<code>platforms.&lt;nama&gt;</code>)</label>
      </div>

      <textarea id="gw-config-yaml" spellcheck="false" oninput="_yamlEditedByUser=true" style="width:100%;height:190px;max-height:30vh;background:rgba(0,0,0,0.4);border:1px solid var(--border);border-radius:var(--radius-sm);color:#e2e8f0;font-family:var(--font-mono);font-size:0.78rem;padding:0.65rem;line-height:1.45;resize:vertical;outline:none;box-sizing:border-box" placeholder="enabled: true..."></textarea>
    </div>

    <!-- Dynamic syntax guide visible in both Form UI and YAML modes -->
    <details id="gw-config-guide-details" open style="margin-top:0.4rem;background:rgba(255,255,255,0.02);border:1px solid var(--border);border-radius:var(--radius-sm);padding:0.4rem 0.6rem;font-size:0.71rem">
      <summary id="gw-config-guide-title" style="cursor:pointer;color:var(--accent-light);font-weight:600;user-select:none">💡 Panduan Kunci &amp; Format Platform</summary>
      <div id="gw-config-guide-content" style="margin-top:0.35rem;color:var(--text-muted);line-height:1.5;display:flex;flex-direction:column;gap:0.25rem">
      </div>
    </details>

    <div id="gw-config-error" style="display:none;color:var(--danger);font-size:0.75rem;margin-top:0.4rem;padding:0.35rem 0.5rem;background:var(--danger-dim);border-radius:var(--radius-sm);border:1px solid rgba(239,68,68,0.3)"></div>

    <div style="margin-top:0.75rem;display:flex;align-items:center;justify-content:space-between;gap:0.5rem;flex-wrap:wrap">
      <label style="font-size:0.72rem;color:var(--text-dim);display:inline-flex;align-items:center;gap:0.35rem;cursor:pointer">
        <input type="checkbox" id="gw-config-restart-chk" checked style="accent-color:var(--accent)">
        <span>Mulai ulang gateway setelah simpan</span>
      </label>
      <div style="display:flex;gap:0.5rem">
        <button type="button" class="btn" style="width:auto;margin:0" onclick="closeGwConfig()">Batal</button>
        <button type="button" class="btn btn-action-primary" id="btn-save-gw-config" style="width:auto;margin:0;font-weight:600" onclick="saveGwConfig()">Simpan</button>
      </div>
    </div>
  </div>
</div>
<div id="wa-pair-modal">
  <div class="confirm-box" style="max-width:540px;width:95%;max-height:90vh;display:flex;flex-direction:column;padding:1.4rem;overflow-y:auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.75rem">
      <div style="display:flex;align-items:center;gap:8px">
        <div class="cc-icon-box bg-emerald" style="width:28px;height:28px;border-radius:8px">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>
        </div>
        <h3 style="margin:0;font-size:1.05rem">Pairing WhatsApp QR Code</h3>
      </div>
      <button type="button" class="btn" style="width:auto;padding:0.25rem 0.6rem;font-size:0.85rem;line-height:1;margin:0" onclick="closeWaPairModal()">✕</button>
    </div>

    <div id="wa-pair-status-bar" style="display:flex;align-items:center;justify-content:space-between;padding:0.45rem 0.75rem;background:rgba(255,255,255,0.03);border:1px solid var(--border);border-radius:6px;font-size:0.78rem;margin-bottom:0.75rem">
      <span id="wa-pair-status-text">Status: Siap untuk pairing</span>
      <span class="badge" id="wa-pair-status-badge">Idle</span>
    </div>

    <!-- QR Code display area -->
    <div id="wa-pair-qr-area" style="text-align:center;padding:0.75rem;background:rgba(0,0,0,0.25);border-radius:8px;border:1px solid var(--border);min-height:230px;display:flex;flex-direction:column;align-items:center;justify-content:center;margin-bottom:0.75rem">
      <div id="wa-pair-qr-container" style="display:flex;justify-content:center;align-items:center">
        <div style="color:var(--text-dim);font-size:0.82rem;padding:2rem 1rem">
          Tekan tombol <strong>"Mulai Pairing QR"</strong> di bawah untuk menginisialisasi jembatan Baileys dan membuat QR code.
        </div>
      </div>
      <div id="wa-pair-hint" style="font-size:0.74rem;color:var(--text-dim);margin-top:0.6rem;line-height:1.4">
        📱 Buka WhatsApp di HP → Menu (titik tiga) / Pengaturan → <strong>Perangkat Tertaut</strong> → <strong>Tautkan Perangkat</strong>.
      </div>
    </div>

    <!-- Live Pairing Logs -->
    <div style="display:flex;flex-direction:column;gap:4px;margin-bottom:0.75rem">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-size:0.74rem;font-weight:600;color:var(--text-dim)">Log Aktivitas Pairing (Live)</span>
        <span id="wa-pair-timer" style="font-size:0.72rem;color:var(--text-dim);font-family:var(--font-mono)"></span>
      </div>
      <div class="logbox" id="wa-pair-logbox" style="height:110px;max-height:130px;font-size:0.72rem;background:#0d1117">(siap memulai)</div>
    </div>

    <!-- Modal Actions -->
    <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;flex-wrap:wrap">
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <button type="button" class="btn btn-action-sm btn-action-primary" id="btn-start-wa-pair" onclick="startWaPair(false)">Mulai Pairing QR</button>
        <button type="button" class="btn btn-action-sm" id="btn-reset-wa-pair" onclick="startWaPair(true)" style="display:none">Pair Ulang (Hapus Sesi)</button>
        <button type="button" class="btn btn-action-sm btn-action-danger" id="btn-cancel-wa-pair" onclick="cancelWaPair()" style="display:none">Batalkan</button>
      </div>
      <div style="display:flex;gap:6px">
        <button type="button" class="btn btn-action-sm btn-action-primary" id="btn-apply-wa-pair" onclick="applyWaPair()" style="display:none;background:#10b981;border-color:#10b981">Aktifkan &amp; Restart Gateway</button>
        <button type="button" class="btn btn-action-sm" onclick="closeWaPairModal()">Tutup</button>
      </div>
    </div>
  </div>
</div>
<div class="header">
  <div class="header-brand">
    <img src="https://cdn.jsdelivr.net/gh/selfhst/icons/webp/hermes-agent-light.webp" class="header-logo" alt="Hermes Logo">
    <h1>Hermes Control Panel</h1>
    <span id="spin"></span>
  </div>
  <div class="live-badge"><span class="dot"></span>Live</div>
</div>

<div class="tabs">
  <div class="tab active" onclick="switchTab('status', this)">{icon_activity} Proses</div>
  <div class="tab" onclick="switchTab('performance', this)">{icon_cpu} Performa</div>
  <div class="tab" onclick="switchTab('control', this)">{icon_layers} Layanan</div>
  <div class="tab" onclick="switchTab('auxiliary', this)">{icon_bot} Tugas AI</div>
</div>

<div class="content-wrapper">
<!-- STATUS TAB -->
<div class="tab-panel active" id="tab-status">
  <!-- Process Table -->
  <div class="card card-status" style="padding:1.1rem;margin-bottom:1.25rem">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.4rem;margin-bottom:.8rem">
      <div class="card-title" style="margin-bottom:0">{icon_activity} Daftar Proses & Layanan Sistem</div>
      <span style="font-size:0.72rem;color:var(--text-dim);font-family:var(--font-mono)">Manajer Layanan Linux</span>
    </div>
    <div id="process-table-slot">
      {processes_table}
    </div>
  </div>

  <!-- Apple CC Highlights (Bento 4-Tile Grid) -->
  <div class="card card-status" style="padding:1.1rem;margin-bottom:1.25rem">
    <div class="card-title" style="margin-bottom:.8rem">{icon_monitor} Ringkasan Gateway & Model AI</div>
    <div class="cc-grid">
      <div class="cc-tile">
        <div class="cc-tile-header">
          <span class="cc-tile-label">Hermes Gateway</span>
          <div class="cc-icon-box cc-icon-blue">{icon_bot}</div>
        </div>
        <div class="cc-tile-val" id="cell-bot">{cell_bot}</div>
        <div class="cc-tile-sub" id="cell-gw">{cell_gw}</div>
        <div class="cc-tile-sub" id="cell-gw-platforms" style="margin-top:.25rem;font-size:.72rem">{cell_gw_platforms}</div>
      </div>
      <div class="cc-tile">
        <div class="cc-tile-header">
          <span class="cc-tile-label">9router AI</span>
          <div class="cc-icon-box cc-icon-purple">{icon_router}</div>
        </div>
        <div class="cc-tile-val" id="cell-router">{cell_router}</div>
        <div class="cc-tile-sub" id="cell-dash">{cell_dash}</div>
      </div>
      <div class="cc-tile">
        <div class="cc-tile-header">
          <span class="cc-tile-label">Model AI Aktif</span>
          <div class="cc-icon-box cc-icon-green">{icon_hermes}</div>
        </div>
        <div class="cc-tile-val" id="cell-model" style="font-size:.95rem">{cell_model}</div>
        <div class="cc-tile-sub" id="cell-providers">{cell_providers}</div>
      </div>
    </div>
  </div>

  <div class="card card-info">
    <div class="card-title">{icon_activity} Metrik & Sensor STB</div>
    <div class="cc-grid" style="margin-bottom:1rem">
      <div class="cc-tile">
        <div class="cc-tile-header">
          <span class="cc-tile-label">RAM / ZRAM</span>
          <div class="cc-icon-box cc-icon-blue">{icon_ram}</div>
        </div>
        <div class="cc-tile-val" id="cell-ram">{cell_ram}</div>
        <div class="cc-tile-sub">ZRAM: <span id="cell-zram">{cell_zram}</span></div>
      </div>
      <div class="cc-tile">
        <div class="cc-tile-header">
          <span class="cc-tile-label">SoC & eMMC</span>
          <div class="cc-icon-box cc-icon-orange">{icon_cpu}</div>
        </div>
        <div class="cc-tile-val" id="cell-temp">{cell_temp}</div>
        <div class="cc-tile-sub">eMMC: <span id="cell-emmc">{cell_emmc}</span></div>
      </div>
    </div>

    <div class="grid">
      <div class="row"><span class="label">{icon_globe} Internet</span>
        <span id="cell-internet">{cell_internet}</span></div>
      <div class="row"><span class="label">{icon_network} IP LAN</span>
        <span id="cell-lan">{cell_lan}</span></div>
      <div class="row"><span class="label">{icon_network} Tailscale</span>
        <span id="cell-ts">{cell_ts}</span></div>
      <div class="row"><span class="label">{icon_clock} Masa Aktif</span>
        <span id="cell-uptime">{cell_uptime}</span></div>
      <div class="row" style="grid-column: span 2; padding-right: 0"><span class="label">{icon_disk} Penyimpanan</span>
        <span id="cell-disk">{cell_disk}</span></div>
      <div class="row" style="display:none"><span class="label">Hermes CLI</span>
        <span id="cell-hermes">{cell_hermes}</span></div>
    </div>
  </div>

  <div class="card card-info">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.75rem">
      <div class="card-title" style="margin-bottom:0">{icon_layers} Model 9router</div>
    </div>
    <input type="text" id="model-search" class="search-input" placeholder="Cari model… (saring)" oninput="filterModels(this.value)">
    <div id="model-chips">{model_chips}</div>
  </div>

  <div id="rl-slot">{rate_limit_card}</div>
</div>

<!-- PERFORMANCE TAB -->
<div class="tab-panel" id="tab-performance">
  <div class="perf-tile">
    <div class="perf-header">
      <div>
        <div class="perf-title">{icon_cpu} CPU · Amlogic S905X3</div>
        <div class="perf-sub">4 Inti @ 1.9GHz · Suhu: <span id="perf-temp">{cell_temp}</span> · Beban: <span id="perf-load">{cell_load}</span></div>
      </div>
      <div class="perf-big-val" id="perf-cpu-val">{cpu_pct}%</div>
    </div>
    <div class="sparkline-box">
      <svg class="sparkline-svg" id="cpu-sparkline" viewBox="0 0 300 80" preserveAspectRatio="none">
        <line x1="0" y1="20" x2="300" y2="20" stroke="rgba(255,255,255,0.06)" stroke-dasharray="2,2"/>
        <line x1="0" y1="40" x2="300" y2="40" stroke="rgba(255,255,255,0.06)" stroke-dasharray="2,2"/>
        <line x1="0" y1="60" x2="300" y2="60" stroke="rgba(255,255,255,0.06)" stroke-dasharray="2,2"/>
        <polygon class="spark-fill" points="0,80 300,80"/>
        <polyline class="spark-poly" points="0,40 300,40"/>
      </svg>
    </div>
  </div>

  <div class="perf-tile">
    <div class="perf-header">
      <div>
        <div class="perf-title">{icon_ram} Memori · DDR4 + ZRAM</div>
        <div class="perf-sub">Penggunaan: <span id="perf-ram-sub">{cell_ram}</span> · ZRAM: <span id="cell-zram-perf">{cell_zram}</span></div>
      </div>
      <div class="perf-big-val" id="perf-ram-val" style="color:#c084fc">{ram_pct}%</div>
    </div>
    <div class="sparkline-box">
      <svg class="sparkline-svg" id="ram-sparkline" viewBox="0 0 300 80" preserveAspectRatio="none">
        <line x1="0" y1="20" x2="300" y2="20" stroke="rgba(255,255,255,0.06)" stroke-dasharray="2,2"/>
        <line x1="0" y1="40" x2="300" y2="40" stroke="rgba(255,255,255,0.06)" stroke-dasharray="2,2"/>
        <line x1="0" y1="60" x2="300" y2="60" stroke="rgba(255,255,255,0.06)" stroke-dasharray="2,2"/>
        <polygon class="spark-fill ram" points="0,80 300,80"/>
        <polyline class="spark-poly ram" points="0,40 300,40"/>
      </svg>
    </div>
  </div>

  <div class="perf-grid-2col">
    <div class="card card-info">
      <div class="card-title">{icon_disk} Penyimpanan & Keausan</div>
      <div class="grid">
        <div class="row"><span class="label">{icon_disk} Ruang</span>
          <span id="cell-disk-perf">{cell_disk}</span></div>
        <div class="row"><span class="label">{icon_shield} Status eMMC</span>
          <span id="cell-emmc-perf">{cell_emmc}</span></div>
      </div>
    </div>

    <div class="card card-info">
      <div class="card-title">{icon_network} Jaringan & Masa Aktif</div>
      <div class="grid">
        <div class="row"><span class="label">{icon_globe} Internet</span>
          <span id="cell-internet-perf">{cell_internet}</span></div>
        <div class="row"><span class="label">{icon_network} IP LAN</span>
          <span id="cell-lan-perf">{cell_lan}</span></div>
        <div class="row"><span class="label">{icon_network} Tailscale</span>
          <span id="cell-ts-perf">{cell_ts}</span></div>
        <div class="row"><span class="label">{icon_clock} Masa Aktif</span>
          <span id="cell-uptime-perf">{cell_uptime}</span></div>
      </div>
    </div>
  </div>
</div>

<!-- CONTROL TAB -->
<div class="tab-panel" id="tab-control">
  {countdown_block}
  <div class="card card-info">
    <div class="card-title">Tautan Cepat</div>
    <div class="btn-row" id="quick-links-slot">
      {open_block}
      {router_open_block}
    </div>
  </div>
  <div class="card card-control">
    <div class="card-title">Dasbor & Gateway</div>
    <div class="btn-row" id="dash-bot-btns-slot">
      <a class="toggle {dash_toggle_class}" id="btn-dash-toggle" href="/toggle">{icon_power}{toggle_label}</a>
      <a class="toggle {bot_toggle_class}" id="btn-bot-toggle" href="/bot-toggle">{icon_power}{bot_toggle_label}</a>
      <a class="toggle restart" href="/restart-bot">{icon_refresh}Mulai Ulang Gateway</a>
      <a class="toggle restart" href="/clean-junk">{icon_trash}Bersihkan Sampah</a>
    </div>
    <div id="clean-log-slot">{clean_junk_card}</div>
    <div id="clean-log-show" style="display:none;margin-top:.6rem">
      <button type="button" class="btn btn-action-sm" onclick="toggleLog('cleanLogDismissed','clean-log-show')">Tampilkan Log Pembersihan</button>
    </div>
  </div>
  <div class="card card-info" style="margin-bottom:1.25rem">
    <div class="aux-header">
      <div class="card-title" style="margin-bottom:0">{icon_bot} Platform Gateway Perpesanan</div>
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
        <span class="badge {gw_summary_badge_class}" id="gw-summary-badge">{gw_summary_text}</span>
        <button type="button" class="btn-action-sm" onclick="openGwConfig('', 'Platform Baru')">+ Tambah Gateway</button>
      </div>
    </div>
    <div class="aux-desc">
      Daftar platform komunikasi yang terkonfigurasi di <code>config.yaml</code>. Klik <strong>Atur</strong> untuk menyesuaikan parameter YAML, token, webhook port, atau channel secara kustom.
    </div>
    <div id="gateway-list-slot">
      {gateway_list_block}
    </div>
    <div id="gateway-log-slot">
      {gateway_log_card}
    </div>
    <div id="gateway-log-show" style="display:none;margin-top:.6rem">
      <button type="button" class="btn btn-action-sm" onclick="toggleLog('gatewayLogDismissed','gateway-log-show')">Tampilkan Log Gateway</button>
    </div>
  </div>
  <div class="card card-info" style="margin-bottom:1.25rem">
    <div class="aux-header">
      <div class="card-title" style="margin-bottom:0">{icon_shield} Model Cadangan</div>
      <button type="button" class="btn btn-action-sm" onclick="openFallbackPicker(-1, 'Cadangan Baru')">
        + Tambah Cadangan
      </button>
    </div>
    <div class="aux-desc">
      Model cadangan otomatis dipakai saat model utama gagal atau kena batas (HTTP 429/500). Urutan jalan dari atas ke bawah.
    </div>
    <div id="backup-models-slot">
      {backup_models_block}
    </div>
  </div>
  <div class="card card-warn" id="update-slot">
    <div id="update-content-slot">{update_block}</div>
    <div id="log-slot">{log_card}</div>
    <div id="log-show-wrap" style="display:none;margin-top:.6rem">
      <button type="button" class="btn" style="width:auto;padding:0.35rem 0.8rem;font-size:0.75rem;margin:0"
        onclick="toggleLog('logDismissed','log-show-wrap')">Tampilkan Log Pembaruan 9router</button>
    </div>
  </div>
  <div class="card card-warn" id="hermes-update-slot">{hermes_update_block}</div>
  <div id="hermes-log-show" style="display:none;margin-top:.6rem">
    <button type="button" class="btn" style="width:auto;padding:0.35rem 0.8rem;font-size:0.75rem;margin:0"
      onclick="toggleLog('hermesLogDismissed','hermes-log-show')">Tampilkan Log Hermes</button>
  </div>
</div>

<!-- TAB TUGAS -->
<div class="tab-panel" id="tab-auxiliary">
  <div class="card card-status" style="padding:1.25rem">
    <div class="aux-header">
      <div class="card-title" style="margin-bottom:0">{icon_cpu} Tugas Tambahan</div>
      <a class="toggle restart" style="width:auto;min-height:34px;padding:0.35rem 0.8rem;font-size:0.75rem;margin:0" href="/reset-aux">
        {icon_refresh}Kembalikan ke Otomatis
      </a>
    </div>
    <div class="aux-desc">
      Kelola model tugas sampingan (vision, kompresi, delegasi, dsb.). Pilihan <code>auto</code> menggunakan model utama. Tentukan model khusus untuk hemat biaya atau kemampuan spesifik.
    </div>
    <div id="aux-tasks-slot">
      {aux_tasks_block}
    </div>
  </div>
</div>
</div> <!-- .content-wrapper -->

{nav_script}
{script}
<script>
var AVAILABLE_MODELS = {available_models_json};
var currentAuxTask = '';
var currentFallbackIndex = null;

function ensureAvailableModels(callback){{
  var hasAny = false;
  if(typeof AVAILABLE_MODELS === 'object' && AVAILABLE_MODELS !== null){{
    for(var k in AVAILABLE_MODELS){{
      if(AVAILABLE_MODELS[k] && AVAILABLE_MODELS[k].length > 0){{ hasAny = true; break; }}
    }}
  }}
  if(hasAny){{
    if(callback) callback();
    return;
  }}
  var container = document.getElementById('aux-picker-list');
  if(container) container.innerHTML = '<div style="padding:1.5rem;color:var(--text-dim);font-size:0.82rem;text-align:center">Memuat daftar model 9router…</div>';
  fetch('/api/models', {{headers: {{'Accept': 'application/json'}}}})
    .then(function(r){{ return r.json(); }})
    .then(function(data){{
      if(typeof data === 'object' && data !== null){{
        AVAILABLE_MODELS = data;
      }}
      if(callback) callback();
    }})
    .catch(function(){{
      if(callback) callback();
    }});
}}

function openAuxPicker(taskKey, taskLabel){{
  currentAuxTask = taskKey;
  currentFallbackIndex = null;
  var title = document.getElementById('aux-picker-title');
  if(title) title.textContent = 'Pilih Model Tugas: ' + taskLabel;
  var input = document.getElementById('aux-model-search');
  if(input) input.value = '';
  var modal = document.getElementById('aux-picker-modal');
  if(modal) modal.classList.add('show');
  ensureAvailableModels(function(){{
    renderAuxPickerItems('');
  }});
}}

function openFallbackPicker(index, label){{
  currentFallbackIndex = index;
  currentAuxTask = '';
  var title = document.getElementById('aux-picker-title');
  if(title) title.textContent = (index === -1 ? 'Tambah Model Cadangan' : 'Ganti Model Cadangan: ' + label);
  var input = document.getElementById('aux-model-search');
  if(input) input.value = '';
  var modal = document.getElementById('aux-picker-modal');
  if(modal) modal.classList.add('show');
  ensureAvailableModels(function(){{
    renderFallbackPickerItems('');
  }});
}}

function closeAuxPicker(){{
  var modal = document.getElementById('aux-picker-modal');
  if(modal) modal.classList.remove('show');
  currentAuxTask = '';
  currentFallbackIndex = null;
}}

function filterAuxPicker(q){{
  if(currentFallbackIndex !== null){{
    renderFallbackPickerItems(q.toLowerCase());
  }} else {{
    renderAuxPickerItems(q.toLowerCase());
  }}
}}

function renderFallbackPickerItems(q){{
  var container = document.getElementById('aux-picker-list');
  if(!container) return;
  var out = '';
  if(typeof AVAILABLE_MODELS === 'object' && AVAILABLE_MODELS !== null){{
    for(var groupName in AVAILABLE_MODELS){{
      var list = AVAILABLE_MODELS[groupName] || [];
      var matched = list.filter(function(m){{ return !q || m.toLowerCase().indexOf(q) !== -1; }});
      if(matched.length > 0){{
        out += '<div style="font-size:0.7rem;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;color:var(--text-dim);margin-top:0.4rem;padding:0 0.2rem">' + escGw(groupName) + '</div>';
        for(var i = 0; i < matched.length; i++){{
          var mId = matched[i];
          var safeId = escGw(mId);
          var isFree = mId.toLowerCase().indexOf('free') !== -1;
          var badge = isFree ? '<span class="model-chip-badge">GRATIS</span>' : '<span class="model-chip-badge" style="background:rgba(255,255,255,0.06);color:var(--text-dim)">9ROUTER</span>';
          out += '<a class="aux-model-opt" href="javascript:void(0)" data-provider="custom:9router" data-model="' + safeId + '" onclick="selectFallbackModel(this.getAttribute(\\'data-provider\\'), this.getAttribute(\\'data-model\\'))">' +
                   '<div style="min-width:0;flex:1">' +
                     '<div class="aux-model-opt-name" style="overflow:hidden;text-overflow:ellipsis">' + safeId + '</div>' +
                     '<div class="aux-model-opt-sub">custom:9router</div>' +
                   '</div>' +
                   badge +
                 '</a>';
        }}
      }}
    }}
  }}
  container.innerHTML = out;
}}

function selectFallbackModel(provider, model){{
  if(currentFallbackIndex === null) return;
  var idx = currentFallbackIndex;
  closeAuxPicker();
  
  var pbar = document.getElementById('pbar');
  if(pbar) pbar.style.width = '50%';
  var spin = document.getElementById('spin');
  if(spin) spin.classList.add('on');

  var body = new URLSearchParams({{
    index: idx,
    provider: provider,
    model: model,
    ajax: '1'
  }});

  fetch('/set-fallback-model', {{
    method: 'POST',
    headers: {{'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'}},
    body: body.toString()
  }})
    .then(function(r){{ return r.json(); }})
    .then(function(res){{
      if(pbar){{ pbar.style.width = '100%'; setTimeout(function(){{ pbar.style.width = '0'; }}, 300); }}
      if(spin) spin.classList.remove('on');
      if(res && res.ok && res.html){{
        var slot = document.getElementById('backup-models-slot');
        if(slot) slot.innerHTML = res.html;
      }} else if(res && !res.ok){{
        alert('Gagal menyimpan model backup: ' + (res.reason || 'unknown error'));
      }}
    }})
    .catch(function(err){{
      if(pbar) pbar.style.width = '0';
      if(spin) spin.classList.remove('on');
      alert('Gagal mengganti model backup: ' + err);
    }});
}}

function renderAuxPickerItems(q){{
  var container = document.getElementById('aux-picker-list');
  if(!container) return;
  var out = '';
  if(!q || 'otomatis (pakai model utama)'.indexOf(q) !== -1 || 'model utama'.indexOf(q) !== -1){{
    out += '<a class="aux-model-opt" href="javascript:void(0)" onclick="selectAuxModel(\\'auto\\', \\'\\')">' +
           '<div>' +
             '<div class="aux-model-opt-name" style="color:var(--text)">Otomatis (Pakai Model Utama)</div>' +
             '<div class="aux-model-opt-sub">Ikuti model obrolan utama Hermes</div>' +
           '</div>' +
           '<span class="model-chip-badge">AUTO</span>' +
         '</a>';
  }}
  if(typeof AVAILABLE_MODELS === 'object' && AVAILABLE_MODELS !== null){{
    for(var groupName in AVAILABLE_MODELS){{
      var list = AVAILABLE_MODELS[groupName] || [];
      var matched = list.filter(function(m){{ return !q || m.toLowerCase().indexOf(q) !== -1; }});
      if(matched.length > 0){{
        out += '<div style="font-size:0.7rem;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;color:var(--text-dim);margin-top:0.4rem;padding:0 0.2rem">' + escGw(groupName) + '</div>';
        for(var i = 0; i < matched.length; i++){{
          var mId = matched[i];
          var safeId = escGw(mId);
          var isFree = mId.toLowerCase().indexOf('free') !== -1;
          var badge = isFree ? '<span class="model-chip-badge">GRATIS</span>' : '<span class="model-chip-badge" style="background:rgba(255,255,255,0.06);color:var(--text-dim)">9ROUTER</span>';
          out += '<a class="aux-model-opt" href="javascript:void(0)" data-provider="custom:9router" data-model="' + safeId + '" onclick="selectAuxModel(this.getAttribute(\\'data-provider\\'), this.getAttribute(\\'data-model\\'))">' +
                   '<div style="min-width:0;flex:1">' +
                     '<div class="aux-model-opt-name" style="overflow:hidden;text-overflow:ellipsis">' + safeId + '</div>' +
                     '<div class="aux-model-opt-sub">custom:9router</div>' +
                   '</div>' +
                   badge +
                 '</a>';
        }}
      }}
    }}
  }}
  container.innerHTML = out;
}}

function selectAuxModel(provider, model){{
  if(!currentAuxTask) return;
  var task = currentAuxTask;
  closeAuxPicker();
  
  var row = document.querySelector('.aux-task-row[data-task="' + task + '"]');
  var valEl = row ? row.querySelector('.mono-sub') : null;
  var prevText = valEl ? valEl.textContent : '';
  var prevClass = valEl ? valEl.className : '';
  if(valEl){{
    valEl.textContent = 'Menyimpan…';
    valEl.className = 'mono-sub custom';
  }}
  var pbar = document.getElementById('pbar');
  if(pbar) pbar.style.width = '50%';
  var spin = document.getElementById('spin');
  if(spin) spin.classList.add('on');

  var body = new URLSearchParams({{
    task: task,
    provider: provider,
    model: model,
    ajax: '1'
  }});

  fetch('/set-aux-model', {{
    method: 'POST',
    headers: {{'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'}},
    body: body.toString()
  }})
    .then(function(r){{ return r.json(); }})
    .then(function(res){{
      if(pbar){{ pbar.style.width = '100%'; setTimeout(function(){{ pbar.style.width = '0'; }}, 300); }}
      if(spin) spin.classList.remove('on');
      if(res && res.ok){{
        if(res.html){{
          var slot = document.getElementById('aux-tasks-slot');
          if(slot) slot.innerHTML = res.html;
        }} else if(valEl){{
          var isAuto = (provider === 'auto' || !provider) && !model;
          valEl.textContent = isAuto ? 'otomatis (pakai model utama)' : (provider && model ? provider + ' · ' + model : (model || provider));
          valEl.className = isAuto ? 'mono-sub auto' : 'mono-sub custom';
        }}
      }} else {{
        if(valEl){{ valEl.textContent = prevText; valEl.className = prevClass; }}
        alert('Gagal menyimpan auxiliary: ' + (res && res.reason ? res.reason : 'unknown error'));
      }}
    }})
    .catch(function(err){{
      if(pbar) pbar.style.width = '0';
      if(spin) spin.classList.remove('on');
      if(valEl){{ valEl.textContent = prevText; valEl.className = prevClass; }}
      alert('Gagal mengganti model auxiliary: ' + err);
    }});
}}

function switchTab(name, tab){{
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  var p = document.getElementById('tab-'+name);
  if(p) p.classList.add('active');
  if(tab) tab.classList.add('active');
  safeStore('setItem', 'activeTab', name);
  scrollAllLogsToBottom();
  restorePatchPages();
  syncGwLogTabUI();
}}

// Tab restore from URL or safeStore
var activeTabFromUrl = "{active_tab}";
if(activeTabFromUrl){{
  var btn = document.querySelector('.tab[onclick*="' + activeTabFromUrl + '"]');
  if(btn) switchTab(activeTabFromUrl, btn);
}} else {{
  var saved = safeStore('getItem', 'activeTab');
  if(saved && ['status','performance','control','auxiliary'].indexOf(saved) !== -1){{
    var btn = document.querySelector('.tab[onclick*="' + saved + '"]');
    if(btn) switchTab(saved, btn);
  }}
}}
restorePatchPages();
syncGwLogTabUI();

var currentGwPlatform = '';
var isNewGwPlatform = false;
var currentGwMode = 'ui';
var _gwPreviewGen = 0;
var _yamlEditedByUser = false;

function switchGwConfigMode(mode){{
  currentGwMode = mode;
  var btnUi = document.getElementById('gw-btn-mode-ui');
  var btnYaml = document.getElementById('gw-btn-mode-yaml');
  var formView = document.getElementById('gw-config-form-view');
  var yamlView = document.getElementById('gw-config-yaml-view');
  var yamlEl = document.getElementById('gw-config-yaml');

  if(mode === 'ui'){{
    if(btnUi) btnUi.classList.add('active');
    if(btnYaml) btnYaml.classList.remove('active');
    if(formView) formView.style.display = 'flex';
    if(yamlView) yamlView.style.display = 'none';
    if(_yamlEditedByUser && yamlEl && yamlEl.value.trim()){{
      var plat = currentGwPlatform || gwActivePlatform();
      populateGwFormFromYaml(yamlEl.value, plat);
      _yamlEditedByUser = false;
    }}
  }} else {{
    if(btnYaml) btnYaml.classList.add('active');
    if(btnUi) btnUi.classList.remove('active');
    if(yamlView) yamlView.style.display = 'flex';
    if(formView) formView.style.display = 'none';
    if(yamlEl){{
      // Apply the form's patch onto the full YAML server-side, so keys the form doesn't
      // know about (home_channel, extra, ...) stay in the editor.
      var errEl = document.getElementById('gw-config-error');
      var plat = gwActivePlatform();
      var reqGen = ++_gwPreviewGen;
      yamlEl.readOnly = true;
      fetch('/api/gateway-config-preview', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json', 'Accept': 'application/json' }},
        body: JSON.stringify({{ platform: plat, base_yaml: yamlEl.value, yaml: serializeGwFormToYaml(plat) }})
      }})
      .then(function(r){{ return r.json(); }})
      .then(function(res){{
        if(yamlEl) yamlEl.readOnly = false;
        if(reqGen !== _gwPreviewGen || currentGwMode !== 'yaml') return;
        if(res.ok && !_yamlEditedByUser) yamlEl.value = res.yaml;
        else if(!res.ok && errEl){{ errEl.textContent = res.error || 'Gagal menyusun YAML'; errEl.style.display = 'block'; }}
      }})
      .catch(function(){{
        if(yamlEl) yamlEl.readOnly = false;
      }});
    }}
  }}
}}

function gwActivePlatform(){{
  if(!isNewGwPlatform) return currentGwPlatform;
  var inputEl = document.getElementById('gw-platform-input');
  return inputEl ? inputEl.value.trim().toLowerCase() : '';
}}

// Form UI sends a PATCH over the editor's full YAML (base_yaml): only fields that exist in the
// config or that the user changed. Untouched defaults are never written, so opening "Setting" and
// saving can't silently change access policy (e.g. turn dm_policy into 'open').
var GW_FORM_KEYS_COMMON = ['dm_policy', 'allow_from', 'allow_admin_from', 'group_policy', 'group_allow_from', 'require_mention', 'reply_in_thread', 'notice_delivery'];
var GW_FORM_KEYS_WA = ['mode', 'dm_policy', 'allow_from', 'allow_admin_from', 'group_policy', 'group_allow_from',
  'require_mention', 'reply_in_thread', 'send_read_receipts', 'notice_delivery', 'bridge_port'];
var GW_FORM_LIST_KEYS = ['allow_from', 'allow_admin_from', 'group_allow_from', 'allowed_chats', 'allowed_channels', 'allowed_users', 'allowed_rooms', 'allowed_groups', 'watch_domains', 'watch_entities'];
var gwFormInitial = {{}};
var gwFormPresent = {{}};

var GW_PLATFORM_FIELDS = {{
  telegram: [
    {{ key: 'token', label: 'Bot Token (@BotFather)', type: 'password', placeholder: '123456:ABC-DEF... (kosongkan jika di .env)', hint: 'Token bot resmi dari @BotFather.' }},
    {{ key: 'allowed_chats', label: 'Allowed Chats (Whitelist)', type: 'list', placeholder: '1992783463, ...', hint: 'ID user atau grup Telegram (pisahkan koma).' }},
    {{ key: 'reply_to_mode', label: 'Mode Balasan Quote', type: 'select', options: [['first', 'first (Quote balasan chunk pertama)'], ['all', 'all (Quote tiap chunk)'], ['off', 'off (Kirim tanpa quote)']], default: 'first' }},
    {{ key: 'reactions', label: 'Emoji Reaction', type: 'checkbox', hint: 'Izinkan bot memberi emoji reaction.', default: true }},
    {{ key: 'typing_indicator', label: 'Indikator Typing', type: 'checkbox', hint: 'Tampilkan typing status saat berpikir.', default: true }},
    {{ key: 'gateway_restart_notification', label: 'Notifikasi Restart', type: 'checkbox', hint: 'Kirim notifikasi ke chat utama saat restart.', default: true }}
  ],
  discord: [
    {{ key: 'token', label: 'Bot Token Discord', type: 'password', placeholder: 'Discord Bot Token', hint: 'Bot token dari Discord Developer Portal.' }},
    {{ key: 'allowed_channels', label: 'Allowed Channels', type: 'list', placeholder: '123456789, ...', hint: 'ID channel Discord yang diizinkan.' }},
    {{ key: 'allowed_users', label: 'Allowed Users', type: 'list', placeholder: '987654321, ...', hint: 'ID user Discord yang diizinkan.' }},
    {{ key: 'reply_to_mode', label: 'Mode Balasan', type: 'select', options: [['first', 'first'], ['all', 'all'], ['off', 'off']], default: 'first' }},
    {{ key: 'slash_commands', label: 'Slash Commands', type: 'checkbox', hint: 'Daftarkan slash commands bot Discord.', default: true }}
  ],
  webhook: [
    {{ key: 'host', label: 'Bind Host', type: 'text', placeholder: '127.0.0.1', default: '127.0.0.1', hint: '127.0.0.1 lokal atau 0.0.0.0 publik.' }},
    {{ key: 'port', label: 'Listen Port', type: 'number', placeholder: '8644', default: 8644, hint: 'Port listener HTTP di server.' }},
    {{ key: 'path', label: 'Endpoint Path', type: 'text', placeholder: '/webhook', default: '/webhook' }},
    {{ key: 'secret', label: 'Secret Token (Bearer Auth)', type: 'password', placeholder: 'token-rahasia', hint: 'Otentikasi header Authorization: Bearer <secret>' }}
  ],
  whatsapp: [
    {{ key: 'mode', label: 'Mode Operasi', type: 'select', options: [['bot', 'Bot Dedicated (Akun Bot Terpisah)'], ['self-chat', 'Self-Chat (Akun Pribadi/Catatan Sendiri)']], default: 'bot' }},
    {{ key: 'bridge_port', label: 'Port Jembatan Bridge', type: 'number', placeholder: '3000', default: 3000, hint: 'Port server Baileys bridge di STB.' }}
  ],
  slack: [
    {{ key: 'token', label: 'Bot User OAuth Token (token)', type: 'password', placeholder: 'xoxb-...', hint: 'OAuth token bot Slack.' }},
    {{ key: 'app_token', label: 'App-Level Token (app_token)', type: 'password', placeholder: 'xapp-...', hint: 'Socket Mode connections:write token.' }},
    {{ key: 'allowed_channels', label: 'Allowed Channels', type: 'list', placeholder: 'C12345678, ...', hint: 'ID channel Slack yang diizinkan.' }},
    {{ key: 'reactions', label: 'Emoji Reaction', type: 'checkbox', hint: 'Beri emoji reaction di pesan.', default: true }}
  ],
  matrix: [
    {{ key: 'homeserver', label: 'Homeserver URL', type: 'text', placeholder: 'https://matrix.org', default: 'https://matrix.org' }},
    {{ key: 'user_id', label: 'Matrix User ID', type: 'text', placeholder: '@bot:matrix.org' }},
    {{ key: 'access_token', label: 'Access Token', type: 'password', placeholder: 'syt_...' }},
    {{ key: 'allowed_rooms', label: 'Allowed Rooms', type: 'list', placeholder: '!roomid:matrix.org' }},
    {{ key: 'allowed_users', label: 'Allowed Users', type: 'list', placeholder: '@user:matrix.org' }}
  ],
  mattermost: [
    {{ key: 'url', label: 'Server URL', type: 'text', placeholder: 'https://mattermost.example.com' }},
    {{ key: 'token', label: 'Bot Access Token', type: 'password', placeholder: 'token' }},
    {{ key: 'allowed_channels', label: 'Allowed Channels', type: 'list', placeholder: 'channel-id' }},
    {{ key: 'reply_mode', label: 'Mode Balasan', type: 'select', options: [['off', 'off (Flat)'], ['thread', 'thread (Nested)']], default: 'off' }}
  ],
  signal: [
    {{ key: 'phone_number', label: 'Nomor Signal (E.164)', type: 'text', placeholder: '+628...' }},
    {{ key: 'http_host', label: 'Host API signal-cli', type: 'text', placeholder: '127.0.0.1', default: '127.0.0.1' }},
    {{ key: 'http_port', label: 'Port API signal-cli', type: 'number', placeholder: '8080', default: 8080 }},
    {{ key: 'allowed_users', label: 'Allowed Users', type: 'list', placeholder: '+628...' }}
  ],
  teams: [
    {{ key: 'app_id', label: 'Microsoft App ID', type: 'text', placeholder: 'AZURE_BOT_APP_ID' }},
    {{ key: 'app_password', label: 'App Password (Secret)', type: 'password', placeholder: 'Client Secret' }},
    {{ key: 'tenant_id', label: 'Tenant ID (Opsional)', type: 'text', placeholder: 'AZURE_TENANT_ID' }},
    {{ key: 'port', label: 'Webhook Listen Port', type: 'number', placeholder: '3978', default: 3978 }}
  ],
  feishu: [
    {{ key: 'app_id', label: 'Feishu App ID', type: 'text', placeholder: 'cli_...' }},
    {{ key: 'app_secret', label: 'Feishu App Secret', type: 'password', placeholder: 'app secret' }},
    {{ key: 'domain', label: 'Domain Layanan', type: 'select', options: [['feishu', 'feishu (China)'], ['lark', 'lark (Internasional)']], default: 'feishu' }},
    {{ key: 'allowed_users', label: 'Allowed Users', type: 'list', placeholder: 'ou_...' }}
  ],
  google_chat: [
    {{ key: 'service_account_json', label: 'Service Account JSON Path', type: 'text', placeholder: 'credentials.json', default: 'credentials.json' }},
    {{ key: 'project_id', label: 'GCP Project ID', type: 'text', placeholder: 'gcp-project-id' }},
    {{ key: 'http_events_url', label: 'HTTP Events URL', type: 'text', placeholder: 'https://...' }}
  ],
  dingtalk: [
    {{ key: 'client_id', label: 'Client ID (App Key)', type: 'text', placeholder: 'Client ID' }},
    {{ key: 'client_secret', label: 'Client Secret (App Secret)', type: 'password', placeholder: 'Client Secret' }},
    {{ key: 'robot_code', label: 'Robot Code (Opsional)', type: 'text', placeholder: 'Robot Code' }},
    {{ key: 'allowed_users', label: 'Allowed Users', type: 'list', placeholder: 'staff_id' }}
  ],
  wecom: [
    {{ key: 'corp_id', label: 'WeCom Corp ID', type: 'text', placeholder: 'YOUR_CORP_ID' }},
    {{ key: 'corp_secret', label: 'Application Secret', type: 'password', placeholder: 'YOUR_CORP_SECRET' }},
    {{ key: 'allow_from', label: 'Allowed Members', type: 'list', placeholder: 'user_id' }},
    {{ key: 'group_allow_from', label: 'Allowed Groups', type: 'list', placeholder: 'group_id' }}
  ],
  line: [
    {{ key: 'channel_secret', label: 'Channel Secret', type: 'password', placeholder: 'Channel Secret' }},
    {{ key: 'channel_access_token', label: 'Channel Access Token', type: 'password', placeholder: 'Access Token' }},
    {{ key: 'port', label: 'Webhook Port', type: 'number', placeholder: '8646', default: 8646 }},
    {{ key: 'allowed_users', label: 'Allowed Users', type: 'list', placeholder: 'U1234...' }},
    {{ key: 'allowed_groups', label: 'Allowed Groups', type: 'list', placeholder: 'C1234...' }}
  ],
  ntfy: [
    {{ key: 'server', label: 'Server URL', type: 'text', placeholder: 'https://ntfy.sh', default: 'https://ntfy.sh' }},
    {{ key: 'topic', label: 'Topic Notifikasi', type: 'text', placeholder: 'hermes-alerts', default: 'hermes-alerts' }},
    {{ key: 'token', label: 'Bearer Token (Opsional)', type: 'password', placeholder: 'Token' }},
    {{ key: 'publish_topic', label: 'Publish Topic (Opsional)', type: 'text', placeholder: 'Topic balasan' }},
    {{ key: 'markdown', label: 'Format Markdown', type: 'checkbox', hint: 'Kirim header X-Markdown: true', default: false }}
  ],
  email: [
    {{ key: 'address', label: 'Alamat Email Bot', type: 'text', placeholder: 'bot@example.com' }},
    {{ key: 'password', label: 'Password / App Password', type: 'password', placeholder: 'App Password' }},
    {{ key: 'smtp_host', label: 'SMTP Host', type: 'text', placeholder: 'smtp.gmail.com', default: 'smtp.gmail.com' }},
    {{ key: 'smtp_port', label: 'SMTP Port', type: 'number', placeholder: '587', default: 587 }},
    {{ key: 'imap_host', label: 'IMAP Host', type: 'text', placeholder: 'imap.gmail.com', default: 'imap.gmail.com' }},
    {{ key: 'imap_port', label: 'IMAP Port', type: 'number', placeholder: '993', default: 993 }}
  ],
  homeassistant: [
    {{ key: 'url', label: 'Home Assistant URL', type: 'text', placeholder: 'http://homeassistant.local:8123', default: 'http://homeassistant.local:8123' }},
    {{ key: 'token', label: 'Long-Lived Access Token', type: 'password', placeholder: 'Access Token' }},
    {{ key: 'cooldown_seconds', label: 'Cooldown (Detik)', type: 'number', placeholder: '3', default: 3 }}
  ],
  simplex: [
    {{ key: 'ws_url', label: 'SimpleX WebSocket URL', type: 'text', placeholder: 'ws://127.0.0.1:5225', default: 'ws://127.0.0.1:5225' }},
    {{ key: 'auto_accept', label: 'Terima Kontak Otomatis', type: 'checkbox', hint: 'Otomatis terima permintaan kontak baru.', default: true }}
  ],
  sms: [
    {{ key: 'account_sid', label: 'Twilio Account SID', type: 'text', placeholder: 'AC_...' }},
    {{ key: 'auth_token', label: 'Twilio Auth Token', type: 'password', placeholder: 'Auth Token' }},
    {{ key: 'phone_number', label: 'Nomor Pengirim Twilio', type: 'text', placeholder: '+1...' }},
    {{ key: 'allowed_users', label: 'Allowed Users', type: 'list', placeholder: '+628...' }}
  ],
  irc: [
    {{ key: 'server', label: 'Server IRC', type: 'text', placeholder: 'irc.libera.chat', default: 'irc.libera.chat' }},
    {{ key: 'port', label: 'Port IRC', type: 'number', placeholder: '6697', default: 6697 }},
    {{ key: 'nickname', label: 'Nickname Bot', type: 'text', placeholder: 'hermes_bot', default: 'hermes_bot' }},
    {{ key: 'channel', label: 'Channel Utama', type: 'text', placeholder: '#hermes', default: '#hermes' }},
    {{ key: 'use_tls', label: 'Gunakan SSL/TLS', type: 'checkbox', hint: 'Koneksi aman ke server IRC.', default: true }}
  ],
  bluebubbles: [
    {{ key: 'server_url', label: 'Server URL BlueBubbles', type: 'text', placeholder: 'http://127.0.0.1:1234', default: 'http://127.0.0.1:1234' }},
    {{ key: 'password', label: 'Password Akses API', type: 'password', placeholder: 'Password' }}
  ]
}};

function escGw(s){{
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}}

function renderGwPlatformFields(plat, vals){{
  var container = document.getElementById('gw-form-conn-fields');
  var section = document.getElementById('gw-form-conn-box');
  var titleEl = document.getElementById('gw-form-conn-title');
  if(!container) return;
  if(titleEl) titleEl.textContent = GW_PLATFORM_NAMES[plat] || (plat ? plat.toUpperCase() : 'Platform');

  if(plat === 'whatsapp'){{
    if(section) section.style.display = 'none';
    return;
  }}
  if(section) section.style.display = 'flex';

  var fields = GW_PLATFORM_FIELDS[plat] || [];
  if(!fields.length){{
    container.innerHTML = '<div style="grid-column:1/-1;font-size:0.72rem;color:var(--text-muted);font-style:italic">Gunakan tab Raw YAML untuk konfigurasi kustom platform ini.</div>';
    return;
  }}

  var html = '';
  fields.forEach(function(f){{
    var elId = 'gw-f-plat-' + f.key;
    var rawVal = (vals && vals[f.key] !== undefined) ? vals[f.key] : (f.default !== undefined ? f.default : '');
    var valStr = Array.isArray(rawVal) ? rawVal.join(', ') : (rawVal === null ? '' : String(rawVal));
    var isChk = (f.type === 'checkbox');
    var isChecked = isChk ? (rawVal === true || rawVal === 'true') : false;

    if(isChk){{
      html += '<label style="font-size:0.72rem;display:inline-flex;align-items:center;gap:0.4rem;cursor:pointer;padding-top:0.4rem">' +
        '<input type="checkbox" id="' + elId + '" style="accent-color:var(--accent)"' + (isChecked ? ' checked' : '') + '>' +
        '<span>' + escGw(f.label) + '</span>' +
        '</label>';
    }} else if(f.type === 'select'){{
      html += '<div style="display:flex;flex-direction:column;gap:3px">' +
        '<label style="font-size:0.7rem;color:var(--text-dim);font-weight:600">' + escGw(f.label) + '</label>' +
        '<select id="' + elId + '" class="search-input" style="margin:0;font-size:0.75rem;padding:0.3rem 0.5rem">';
      (f.options || []).forEach(function(opt){{
        var optVal = opt[0];
        var optLabel = opt[1];
        var sel = (valStr === optVal) ? ' selected' : '';
        html += '<option value="' + escGw(optVal) + '"' + sel + '>' + escGw(optLabel) + '</option>';
      }});
      html += '</select></div>';
    }} else {{
      var inType = (f.type === 'password') ? 'password' : (f.type === 'number' ? 'number' : 'text');
      var ph = f.placeholder ? ' placeholder="' + escGw(f.placeholder) + '"' : '';
      html += '<div style="display:flex;flex-direction:column;gap:3px">' +
        '<label style="font-size:0.7rem;color:var(--text-dim);font-weight:600">' + escGw(f.label) + ' (<code>' + escGw(f.key) + '</code>)</label>' +
        '<input type="' + inType + '" id="' + elId + '" class="search-input" value="' + escGw(valStr) + '"' + ph + ' style="margin:0;font-size:0.75rem;padding:0.35rem 0.5rem">';
      if(f.hint){{
        html += '<div style="font-size:0.68rem;color:var(--text-dim)">' + escGw(f.hint) + '</div>';
      }}
      html += '</div>';
    }}
  }});
  container.innerHTML = html;
}}

function gwFormFieldValues(platform){{
  var plat = platform || currentGwPlatform || gwActivePlatform();
  function val(id){{ var e = document.getElementById(id); return e ? String(e.value || '').trim() : ''; }}
  function chk(id){{ var e = document.getElementById(id); return !!(e && e.checked); }}
  function list(id){{ return val(id).split(',').map(function(s){{ return s.trim(); }}).filter(Boolean); }}
  var port = parseInt(val('gw-f-wa-port'), 10);
  var res = {{
    mode: val('gw-f-wa-mode'),
    dm_policy: val('gw-f-dm-policy'),
    allow_from: list('gw-f-allow-from'),
    allow_admin_from: list('gw-f-allow-admin'),
    group_policy: val('gw-f-group-policy'),
    group_allow_from: list('gw-f-group-allow'),
    require_mention: chk('gw-f-req-mention'),
    reply_in_thread: chk('gw-f-reply-thread'),
    send_read_receipts: chk('gw-f-read-receipts'),
    notice_delivery: val('gw-f-notice-del'),
    bridge_port: isNaN(port) ? 3000 : port
  }};

  var fields = GW_PLATFORM_FIELDS[plat] || [];
  fields.forEach(function(f){{
    var elId = 'gw-f-plat-' + f.key;
    var el = document.getElementById(elId);
    if(!el) return;
    if(f.type === 'checkbox'){{
      res[f.key] = el.checked;
    }} else if(f.type === 'number'){{
      var n = parseInt(el.value, 10);
      res[f.key] = isNaN(n) ? (f.default !== undefined ? f.default : 0) : n;
    }} else if(f.type === 'list'){{
      res[f.key] = String(el.value || '').split(',').map(function(s){{ return s.trim(); }}).filter(Boolean);
    }} else {{
      res[f.key] = String(el.value || '').trim();
    }}
  }});
  return res;
}}

function gwUnquote(s){{ return String(s).trim().replace(/^['"]|['"]$/g, ''); }}

function populateGwFormFromYaml(yamlText, platform){{
  var plat = platform || currentGwPlatform || gwActivePlatform();
  var d = {{
    enabled: true,
    mode: 'bot',
    dm_policy: '',
    allow_from: [],
    allow_admin_from: [],
    group_policy: '',
    group_allow_from: [],
    require_mention: false,
    reply_in_thread: false,
    send_read_receipts: false,
    notice_delivery: '',
    bridge_port: 3000
  }};
  var present = {{}};
  var currentList = null;
  var parent = '';
  var lines = (yamlText || '').split(String.fromCharCode(10)).map(function(s){{ return s.replace(new RegExp(String.fromCharCode(13), 'g'), ''); }});
  for(var i=0; i<lines.length; i++){{
    var line = lines[i];
    var trimmed = line.trim();
    if(!trimmed || trimmed.startsWith('#')) continue;
    if(trimmed.startsWith('- ')){{
      if(currentList) currentList.push(gwUnquote(trimmed.substring(2)));
      continue;
    }}
    var colonIdx = trimmed.indexOf(':');
    if(colonIdx <= 0) continue;
    var k = trimmed.substring(0, colonIdx).trim();
    var v = gwUnquote(trimmed.substring(colonIdx + 1).replace(/ #.*$/, ''));
    currentList = null;
    var indented = line.charAt(0) === ' ' || line.charCodeAt(0) === 9;
    if(indented){{
      // Nested mapping (voice_fx.enabled, home_channel.chat_id, ...) is not a form field,
      // except the WhatsApp bridge port which lives under extra.
      if(parent === 'extra' && k === 'bridge_port'){{ d.bridge_port = parseInt(v, 10) || 3000; present.bridge_port = true; }}
      continue;
    }}
    parent = k;
    if(GW_FORM_LIST_KEYS.indexOf(k) >= 0){{
      present[k] = true;
      if(v.charAt(0) === '[' && v.charAt(v.length - 1) === ']'){{
        d[k] = v.slice(1, -1).split(',').map(gwUnquote).filter(Boolean);
      }} else if(v) {{
        d[k] = [v];
      }} else {{
        d[k] = [];
        currentList = d[k];
      }}
      continue;
    }}
    function gwParseBool(val){{
      var s = String(val).trim().toLowerCase();
      return s === 'true' || s === 'yes' || s === 'on' || s === '1';
    }}
    if(k === 'enabled') d.enabled = gwParseBool(v);
    else if(k === 'bridge_port'){{ d.bridge_port = parseInt(v, 10) || 3000; present.bridge_port = true; }}
    else {{
      present[k] = true;
      d[k] = (v === 'true' || v === 'false') ? gwParseBool(v) : (v === 'null' || v === '~' ? '' : v);
    }}
  }}

  var chkEnabled = document.getElementById('gw-config-enabled-chk');
  if(chkEnabled) chkEnabled.checked = d.enabled;
  var selMode = document.getElementById('gw-f-wa-mode');
  if(selMode) selMode.value = d.mode;
  var selDm = document.getElementById('gw-f-dm-policy');
  if(selDm) selDm.value = d.dm_policy;
  var inpAllow = document.getElementById('gw-f-allow-from');
  if(inpAllow) inpAllow.value = (d.allow_from || []).join(', ');
  var inpAdmin = document.getElementById('gw-f-allow-admin');
  if(inpAdmin) inpAdmin.value = (d.allow_admin_from || []).join(', ');
  var selGp = document.getElementById('gw-f-group-policy');
  if(selGp) selGp.value = d.group_policy;
  var inpGAllow = document.getElementById('gw-f-group-allow');
  if(inpGAllow) inpGAllow.value = (d.group_allow_from || []).join(', ');
  var chkReq = document.getElementById('gw-f-req-mention');
  if(chkReq) chkReq.checked = !!d.require_mention;
  var chkTh = document.getElementById('gw-f-reply-thread');
  if(chkTh) chkTh.checked = !!d.reply_in_thread;
  var chkRr = document.getElementById('gw-f-read-receipts');
  if(chkRr) chkRr.checked = !!d.send_read_receipts;
  var selNot = document.getElementById('gw-f-notice-del');
  if(selNot) selNot.value = (d.notice_delivery === 'public' || d.notice_delivery === 'private') ? d.notice_delivery : '';
  var inpPort = document.getElementById('gw-f-wa-port');
  if(inpPort) inpPort.value = d.bridge_port || 3000;

  var waBanner = document.getElementById('gw-form-wa-banner');
  var waFields = document.getElementById('gw-form-wa-fields');
  var isWa = (plat === 'whatsapp');
  if(waBanner) waBanner.style.display = isWa ? 'flex' : 'none';
  if(waFields) waFields.style.display = isWa ? 'grid' : 'none';

  renderGwPlatformFields(plat, d);

  gwFormPresent = present;
  gwFormInitial = gwFormFieldValues(plat);
}}

function serializeGwFormToYaml(platform){{
  var plat = platform || currentGwPlatform || gwActivePlatform();
  var lines = [];
  var chkEnabled = document.getElementById('gw-config-enabled-chk');
  var enabled = chkEnabled ? chkEnabled.checked : true;
  lines.push('enabled: ' + (enabled ? 'true' : 'false'));

  var cur = gwFormFieldValues(plat);
  var fields = GW_PLATFORM_FIELDS[plat] || [];

  fields.forEach(function(f){{
    var k = f.key;
    if(plat === 'whatsapp' && (k === 'mode' || k === 'bridge_port')) return;
    var v = cur[k];
    if(v === undefined) return;
    var changed = JSON.stringify(v) !== JSON.stringify(gwFormInitial[k]);
    if(!gwFormPresent[k] && !changed) return;
    if(Array.isArray(v)){{
      if(!v.length){{ lines.push(k + ': []'); return; }}
      lines.push(k + ':');
      v.forEach(function(it){{ lines.push('  - ' + (it.indexOf("'") >= 0 ? '"' + it + '"' : "'" + it + "'")); }});
    }} else if(v === '' || v === null){{
      lines.push(k + ': null');
    }} else if(typeof v === 'boolean'){{
      lines.push(k + ': ' + (v ? 'true' : 'false'));
    }} else if(typeof v === 'number'){{
      lines.push(k + ': ' + v);
    }} else {{
      lines.push(k + ': ' + (String(v).indexOf("'") >= 0 ? '"' + v + '"' : "'" + v + "'"));
    }}
  }});

  if(plat === 'whatsapp'){{
    var bp = cur.bridge_port;
    var bpChanged = bp !== gwFormInitial.bridge_port;
    if(gwFormPresent.bridge_port || bpChanged){{
      lines.push('extra:');
      lines.push('  bridge_port: ' + bp);
    }}
    var wm = cur.mode;
    var wmChanged = wm !== gwFormInitial.mode;
    if(gwFormPresent.mode || wmChanged){{
      lines.push('mode: ' + wm);
    }}
  }}

  GW_FORM_KEYS_COMMON.forEach(function(k){{
    var v = cur[k];
    var changed = JSON.stringify(v) !== JSON.stringify(gwFormInitial[k]);
    if(!gwFormPresent[k] && !changed) return;
    if(Array.isArray(v)){{
      if(!v.length){{ lines.push(k + ': []'); return; }}
      lines.push(k + ':');
      v.forEach(function(it){{ lines.push('  - ' + (it.indexOf("'") >= 0 ? '"' + it + '"' : "'" + it + "'")); }});
    }} else if(v === ''){{
      lines.push(k + ': null');
    }} else if(typeof v === 'boolean'){{
      lines.push(k + ': ' + (v ? 'true' : 'false'));
    }} else {{
      lines.push(k + ': ' + v);
    }}
  }});

  return lines.join(String.fromCharCode(10)) + String.fromCharCode(10);
}}


var GW_PLATFORM_NAMES = {{
  telegram: "Telegram Bot",
  discord: "Discord Bot",
  webhook: "HTTP Webhook",
  whatsapp: "WhatsApp",
  slack: "Slack Bot",
  matrix: "Matrix",
  mattermost: "Mattermost",
  signal: "Signal Messenger",
  teams: "Microsoft Teams",
  feishu: "Feishu / Lark",
  google_chat: "Google Chat",
  dingtalk: "DingTalk",
  wecom: "WeCom",
  line: "LINE Messaging",
  ntfy: "ntfy Push",
  email: "Email Gateway",
  homeassistant: "Home Assistant",
  simplex: "SimpleX Chat",
  sms: "SMS (Twilio)",
  irc: "IRC",
  bluebubbles: "BlueBubbles"
}};

var GW_PLATFORM_GUIDES = {{
  telegram: [
    {{ key: "enabled: true / false", desc: "Status aktif gateway Telegram saat runtime Hermes berjalan." }},
    {{ key: "token: '123456:ABC...'", desc: "Bot token dari @BotFather. Dikosongkan jika sudah di .env (TELEGRAM_BOT_TOKEN)." }},
    {{ key: "allowed_chats: 'ID1, ID2'", desc: "Whitelist chat ID (user/grup). Bot hanya merespons ID di daftar ini (pisahkan koma)." }},
    {{ key: "reactions: true / false", desc: "Izinkan bot memberikan emoji reaction pada pesan masuk Telegram." }},
    {{ key: "reply_to_mode: 'first' | 'all' | 'off'", desc: "Mode quote balasan: 'first' (hanya chunk pertama), 'all' (semua), atau 'off'." }},
    {{ key: "typing_indicator: true / false", desc: "Tampilkan indikator 'sedang mengetik...' di Telegram saat AI memproses respons." }},
    {{ key: "gateway_restart_notification: true", desc: "Kirim pesan notifikasi otomatis saat bot/gateway berhasil restart." }},
    {{ key: "home_channel:", desc: "Kanal default notifikasi & interaksi (chat_id, name, platform, thread_id)." }},
    {{ key: "proxy_url: 'http://...'", desc: "URL proxy HTTP atau SOCKS5 jika akses Telegram diblokir ISP." }},
    {{ key: "fallback_ips: 'IP1, IP2'", desc: "Daftar IP direct failover jika DNS ISP gagal meresolusi api.telegram.org." }}
  ],
  discord: [
    {{ key: "enabled: true / false", desc: "Status aktif bot Discord." }},
    {{ key: "token: 'DISCORD_BOT_TOKEN'", desc: "Bot token resmi dari Discord Developer Portal." }},
    {{ key: "require_mention: true / false", desc: "Jika true, bot hanya merespons saat di-mention (@bot) di dalam channel (default: true)." }},
    {{ key: "allowed_channels: 'ID1, ID2'", desc: "Whitelist Channel ID (pisahkan koma) agar bot hanya memantau channel tertentu." }},
    {{ key: "allowed_users: 'ID1, ID2'", desc: "Whitelist User ID yang memiliki izin berinteraksi dengan bot." }},
    {{ key: "slash_commands: true / false", desc: "Otomatis daftarkan Discord slash commands (/help, dll.)." }},
    {{ key: "reply_to_mode: 'first' | 'all' | 'off'", desc: "Mode kutip reply pesan balasan bot." }},
    {{ key: "home_channel:", desc: "Kanal utama notifikasi dan broadcast (chat_id, name, platform)." }}
  ],
  webhook: [
    {{ key: "enabled: true / false", desc: "Status aktif HTTP Webhook listener." }},
    {{ key: "port: 8644", desc: "Port listener HTTP server lokal di STB (default: 8644)." }},
    {{ key: "host: '127.0.0.1'", desc: "Bind host: '127.0.0.1' (lokal) atau '0.0.0.0' (publik)." }},
    {{ key: "path: '/webhook'", desc: "URL route path untuk menerima payload JSON (default: /webhook)." }},
    {{ key: "secret: 'token-rahasia'", desc: "Token otentikasi header Bearer / Authorization untuk validasi request." }}
  ],
  whatsapp: [
    {{ key: "enabled: true / false", desc: "Status aktif bridge WhatsApp." }},
    {{ key: "bridge_port: 3000", desc: "Port server lokal bridge Node.js Baileys (default: 3000)." }},
    {{ key: "dm_policy: 'pairing' | 'open' | 'allowlist' | 'disabled'", desc: "Kebijakan penerimaan direct message WhatsApp (default: pairing)." }},
    {{ key: "group_policy: 'pairing' | 'open' | 'allowlist' | 'disabled'", desc: "Kebijakan respons di dalam grup WhatsApp." }},
    {{ key: "allow_from: '628...'", desc: "Whitelist nomor WhatsApp pengirim yang diizinkan (format internasional tanpa '+')." }},
    {{ key: "group_allow_from: '...@g.us'", desc: "Whitelist ID grup WhatsApp yang diizinkan." }},
    {{ key: "send_read_receipts: false / true", desc: "Kirim tanda centang biru baca (read receipts) saat pesan diproses." }},
    {{ key: "require_mention: false / true", desc: "Wajib tag @bot sebelum merespons di dalam obrolan grup." }}
  ],
  slack: [
    {{ key: "enabled: true / false", desc: "Status aktif Slack Bot." }},
    {{ key: "token: 'xoxb-...'", desc: "Bot User OAuth Token dari Slack App Management." }},
    {{ key: "app_token: 'xapp-...'", desc: "App-Level Token untuk WebSocket Socket Mode (wajib scope: connections:write)." }},
    {{ key: "require_mention: true / false", desc: "Wajib mention bot di channel sebelum menjawab (default: true)." }},
    {{ key: "allowed_channels: 'C...'", desc: "Whitelist Channel ID Slack yang diizinkan (pisahkan koma)." }},
    {{ key: "reactions: true / false", desc: "Izinkan bot memberikan emoji reaction pada pesan masuk." }}
  ],
  matrix: [
    {{ key: "enabled: true / false", desc: "Status aktif Matrix bot adapter." }},
    {{ key: "homeserver: 'https://matrix.org'", desc: "URL homeserver Matrix (Synapse / Dendrite / Conduit)." }},
    {{ key: "user_id: '@bot:matrix.org'", desc: "Matrix User ID akun bot." }},
    {{ key: "access_token: 'syt_...'", desc: "Access token akun Matrix bot." }},
    {{ key: "require_mention: true / false", desc: "Wajib mention @bot di dalam room (default: true)." }},
    {{ key: "allowed_rooms: '!room:matrix.org'", desc: "Whitelist Room ID Matrix yang diizinkan." }},
    {{ key: "allowed_users: '@user:matrix.org'", desc: "Whitelist User ID yang diizinkan berinteraksi." }}
  ],
  mattermost: [
    {{ key: "enabled: true / false", desc: "Status aktif Mattermost bot adapter." }},
    {{ key: "url: 'https://mattermost.domain.com'", desc: "URL instance server Mattermost." }},
    {{ key: "token: 'bot-token'", desc: "Personal Access Token atau Bot Token Mattermost." }},
    {{ key: "require_mention: true / false", desc: "Wajib mention @bot di channel publik (default: true)." }},
    {{ key: "allowed_channels: 'channel-id'", desc: "Whitelist Channel ID Mattermost yang dipantau bot." }},
    {{ key: "reply_mode: 'thread' | 'off'", desc: "Mode balasan: 'thread' (nested) atau 'off' (flat timeline)." }}
  ],
  signal: [
    {{ key: "enabled: true / false", desc: "Status aktif Signal Messenger adapter." }},
    {{ key: "phone_number: '+628...'", desc: "Nomor telepon akun Signal terdaftar di signal-cli (format E.164)." }},
    {{ key: "http_host: '127.0.0.1'", desc: "Host REST API service signal-cli-rest-api." }},
    {{ key: "http_port: 8080", desc: "Port REST API service signal-cli-rest-api (default: 8080)." }},
    {{ key: "allowed_users: '+628...'", desc: "Whitelist nomor telepon atau UUID pengirim yang diizinkan." }}
  ],
  teams: [
    {{ key: "enabled: true / false", desc: "Status aktif Microsoft Teams bot adapter." }},
    {{ key: "app_id: 'UUID'", desc: "Microsoft App ID dari Azure Bot Framework registration." }},
    {{ key: "app_password: 'PASSWORD'", desc: "Client Secret / App Password Azure Bot." }},
    {{ key: "tenant_id: 'UUID'", desc: "Azure AD Tenant ID tempat aplikasi bot terdaftar." }},
    {{ key: "port: 3978", desc: "Port listener webhook Bot Framework lokal (default: 3978)." }},
    {{ key: "require_mention: false / true", desc: "Hanya jawab jika bot di-mention di channel Teams." }}
  ],
  feishu: [
    {{ key: "enabled: true / false", desc: "Status aktif Feishu / Lark bot adapter." }},
    {{ key: "app_id: 'cli_...'", desc: "App ID resmi dari Feishu Open Platform." }},
    {{ key: "app_secret: '...'", desc: "App Secret resmi dari Feishu Open Platform." }},
    {{ key: "domain: 'feishu' | 'lark'", desc: "'feishu' (wilayah China) atau 'lark' (Internasional)." }},
    {{ key: "require_mention: true / false", desc: "Wajib mention @bot di obrolan grup (default: true)." }},
    {{ key: "allowed_users: 'ou_...'", desc: "Whitelist Open ID / User ID Feishu yang diizinkan." }}
  ],
  google_chat: [
    {{ key: "enabled: true / false", desc: "Status aktif Google Chat adapter." }},
    {{ key: "service_account_json: 'creds.json'", desc: "Path berkas kredensial Service Account JSON Google Cloud." }},
    {{ key: "project_id: 'gcp-id'", desc: "GCP Project ID untuk inbound mode Pub/Sub subscription." }},
    {{ key: "http_events_url: 'https://...'", desc: "URL endpoint jika menggunakan mode HTTP Event Callback." }},
    {{ key: "allowed_users: 'email'", desc: "Whitelist email pengguna Google Workspace yang diizinkan." }}
  ],
  dingtalk: [
    {{ key: "enabled: true / false", desc: "Status aktif DingTalk bot adapter." }},
    {{ key: "client_id: '...'", desc: "DingTalk App Key (Client ID)." }},
    {{ key: "client_secret: '...'", desc: "DingTalk App Secret (Client Secret)." }},
    {{ key: "require_mention: true / false", desc: "Hanya respons jika di-mention @bot di dalam grup." }},
    {{ key: "allowed_users: 'staff_id'", desc: "Whitelist sender/staff ID DingTalk (* = semua pengguna)." }}
  ],
  wecom: [
    {{ key: "enabled: true / false", desc: "Status aktif WeCom / Enterprise WeChat." }},
    {{ key: "corp_id: '...'", desc: "Enterprise Corp ID WeCom resmi." }},
    {{ key: "corp_secret: '...'", desc: "Application Secret resmi WeCom." }},
    {{ key: "allow_from: 'user_id'", desc: "Whitelist member/user ID WeCom yang diizinkan." }},
    {{ key: "group_allow_from: 'group_id'", desc: "Whitelist group ID WeCom yang diizinkan." }}
  ],
  line: [
    {{ key: "enabled: true / false", desc: "Status aktif LINE Messaging API adapter." }},
    {{ key: "channel_secret: '...'", desc: "LINE Channel Secret untuk verifikasi HMAC signature." }},
    {{ key: "channel_access_token: '...'", desc: "LINE Long-lived Channel Access Token." }},
    {{ key: "port: 8646", desc: "Port webhook listener lokal di STB (default: 8646)." }},
    {{ key: "allowed_users: 'U...'", desc: "Whitelist LINE user ID (awalan 'U') yang diizinkan." }},
    {{ key: "allowed_groups: 'C...'", desc: "Whitelist LINE group ID (awalan 'C') yang diizinkan." }}
  ],
  ntfy: [
    {{ key: "enabled: true / false", desc: "Status aktif ntfy push notification adapter." }},
    {{ key: "topic: 'hermes-alerts'", desc: "Nama topic ntfy untuk subscribe / kirim pesan." }},
    {{ key: "server: 'https://ntfy.sh'", desc: "URL server ntfy (default: https://ntfy.sh atau server mandiri)." }},
    {{ key: "token: 'tk_...'", desc: "Bearer auth token jika topic ntfy dilindungi kata sandi." }},
    {{ key: "publish_topic: 'hermes-out'", desc: "Nama topic balasan (default: sama dengan topic asal)." }},
    {{ key: "markdown: false / true", desc: "Kirim header X-Markdown: true pada balasan notifikasi." }}
  ],
  email: [
    {{ key: "enabled: true / false", desc: "Status aktif Email Gateway (IMAP + SMTP)." }},
    {{ key: "address: 'bot@example.com'", desc: "Alamat email resmi akun bot." }},
    {{ key: "password: '...'", desc: "Password akun email atau Google App Password (2FA)." }},
    {{ key: "smtp_host: 'smtp.gmail.com'", desc: "Host server SMTP untuk mengirim balasan email." }},
    {{ key: "smtp_port: 587", desc: "Port SMTP server (587 STARTTLS atau 465 SSL)." }},
    {{ key: "imap_host: 'imap.gmail.com'", desc: "Host server IMAP untuk menerima/polling email masuk." }},
    {{ key: "imap_port: 993", desc: "Port IMAP server (SSL port 993)." }},
    {{ key: "allowed_users: 'boss@example.com'", desc: "Whitelist alamat email pengirim yang diizinkan." }}
  ],
  homeassistant: [
    {{ key: "enabled: true / false", desc: "Status aktif integrasi Home Assistant." }},
    {{ key: "url: 'http://homeassistant.local:8123'", desc: "URL instance server Home Assistant lokal." }},
    {{ key: "token: 'LONG_LIVED_TOKEN'", desc: "Long-Lived Access Token yang digenerate di profil user Home Assistant." }},
    {{ key: "watch_domains: ['light', 'switch']", desc: "Filter domain entity HA yang dipantau perubahannya." }},
    {{ key: "cooldown_seconds: 3", desc: "Jeda detik antar-event sebelum memicu prompt AI ulang." }}
  ],
  simplex: [
    {{ key: "enabled: true / false", desc: "Status aktif SimpleX Chat adapter." }},
    {{ key: "ws_url: 'ws://127.0.0.1:5225'", desc: "WebSocket URL daemon simplex-chat lokal." }},
    {{ key: "auto_accept: true / false", desc: "Otomatis menerima permintaan pertemanan/kontak baru masuk." }},
    {{ key: "group_allowed: '*'", desc: "Whitelist ID grup atau '*' untuk mengizinkan seluruh grup SimpleX." }}
  ],
  sms: [
    {{ key: "enabled: true / false", desc: "Status aktif SMS Gateway via Twilio." }},
    {{ key: "account_sid: 'AC...'", desc: "Twilio Account SID resmi." }},
    {{ key: "auth_token: '...'", desc: "Twilio Auth Token resmi." }},
    {{ key: "phone_number: '+1...'", desc: "Nomor telepon aktif Twilio pengirim (format E.164)." }},
    {{ key: "allowed_users: '+...'", desc: "Whitelist nomor telepon penerima yang diizinkan." }}
  ],
  irc: [
    {{ key: "enabled: true / false", desc: "Status aktif IRC adapter." }},
    {{ key: "server: 'irc.libera.chat'", desc: "Hostname server IRC yang dituju." }},
    {{ key: "port: 6697", desc: "Port koneksi IRC (6697 SSL atau 6667 plain)." }},
    {{ key: "nickname: 'hermes_bot'", desc: "Nickname bot di server IRC." }},
    {{ key: "channel: '#hermes'", desc: "Nama channel utama yang di-join otomatis oleh bot." }},
    {{ key: "use_tls: true / false", desc: "Gunakan enkripsi SSL/TLS pada koneksi socket IRC." }},
    {{ key: "server_password: '...'", desc: "Password server IRC (opsional)." }},
    {{ key: "nickserv_password: '...'", desc: "Password NickServ untuk identifikasi otomatis saat connect." }}
  ],
  bluebubbles: [
    {{ key: "enabled: true / false", desc: "Status aktif BlueBubbles (iMessage) adapter." }},
    {{ key: "server_url: 'http://127.0.0.1:1234'", desc: "URL endpoint server BlueBubbles macOS." }},
    {{ key: "password: '...'", desc: "Password otentikasi API BlueBubbles server." }}
  ]
}};

function escGw(s){{
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}}

function updateGwGuide(plat){{
  var titleEl = document.getElementById('gw-config-guide-title');
  var contentEl = document.getElementById('gw-config-guide-content');
  if(!contentEl) return;

  var normPlat = (plat || '').toLowerCase().trim();
  var name = GW_PLATFORM_NAMES[normPlat] || (normPlat ? normPlat.toUpperCase() : 'Platform');
  if(titleEl) titleEl.textContent = '💡 Panduan Kunci: ' + name;

  var guides = GW_PLATFORM_GUIDES[normPlat];
  if(!guides || !guides.length){{
    guides = [
      {{ key: "enabled: true / false", desc: "Status aktif platform gateway saat runtime Hermes berjalan." }},
      {{ key: "token: '...'", desc: "Token autentikasi platform (jika didukung adapter)." }},
      {{ key: "home_channel:", desc: "Kanal default notifikasi & interaksi (chat_id, name, platform)." }},
      {{ key: "extra:", desc: "Parameter kustom tambahan untuk adapter atau plugin ini." }}
    ];
  }}

  var html = '';
  for(var i = 0; i < guides.length; i++){{
    var g = guides[i];
    html += '<div style="margin-bottom:0.28rem">• <code style="color:var(--accent-light);font-weight:600;font-size:0.75rem">' +
            escGw(g.key) + '</code> — <span style="color:#cbd5e1">' + escGw(g.desc) + '</span></div>';
  }}
  contentEl.innerHTML = html;
}}

var GW_TEMPLATES = {{
  telegram: [
    "# Konfigurasi Platform Telegram (python-telegram-bot)",
    "enabled: true",
    "# token: '123456:ABC-DEF'               # Bot token @BotFather (opsional jika TELEGRAM_BOT_TOKEN di .env)",
    "# allowed_chats: '1992783463'           # Whitelist chat ID user/grup (pisahkan koma)",
    "reactions: true                         # Izinkan bot memberi emoji reaction pada pesan",
    "reply_to_mode: 'first'                  # 'first' (quote balasan chunk pertama), 'all', atau 'off'",
    "typing_indicator: true                  # Tampilkan status 'sedang mengetik...' saat AI berpikir",
    "gateway_restart_notification: true      # Kirim notifikasi ke kanal utama saat gateway restart",
    "home_channel:",
    "  chat_id: '1992783463'",
    "  name: vitooo",
    "  platform: telegram"
  ].join(String.fromCharCode(10)),
  discord: [
    "# Konfigurasi Platform Discord (discord.py)",
    "enabled: true",
    "token: 'YOUR_DISCORD_BOT_TOKEN'         # Bot token dari Discord Developer Portal",
    "require_mention: true                   # Hanya respons jika bot di-tag @bot di channel",
    "# allowed_channels: '123456789'         # Whitelist channel ID server Discord",
    "# allowed_users: '987654321'            # Whitelist user ID Discord",
    "slash_commands: true                    # Daftarkan Discord slash commands (/help, dll.)",
    "reply_to_mode: 'first'                  # 'first', 'all', atau 'off'",
    "home_channel:",
    "  chat_id: '123456789'",
    "  name: general",
    "  platform: discord"
  ].join(String.fromCharCode(10)),
  webhook: [
    "# Konfigurasi HTTP Webhook Listener (AIOHTTP Server)",
    "enabled: true",
    "port: 8644                              # Port listener HTTP lokal di STB",
    "host: '127.0.0.1'                       # Bind host ('127.0.0.1' lokal atau '0.0.0.0' publik)",
    "path: '/webhook'                        # URL route endpoint penerima webhook",
    "# secret: 'token-rahasia'               # Token otentikasi header Bearer / Authorization"
  ].join(String.fromCharCode(10)),
  whatsapp: [
    "# Konfigurasi WhatsApp Bridge (Baileys Node.js HTTP Bridge)",
    "enabled: true",
    "bridge_port: 3000                       # Port lokal server bridge WhatsApp",
    "dm_policy: 'pairing'                    # 'pairing' (kode pairing), 'open', 'allowlist', 'disabled'",
    "group_policy: 'pairing'                 # 'pairing', 'open', 'allowlist', 'disabled'",
    "# allow_from: '6281234567890'           # Whitelist nomor WhatsApp yang diizinkan",
    "# group_allow_from: '12036304@g.us'     # Whitelist grup WhatsApp",
    "send_read_receipts: false               # Kirim tanda centang biru baca (read receipts)",
    "require_mention: false                  # Hanya respons grup jika di-tag @bot"
  ].join(String.fromCharCode(10)),
  slack: [
    "# Konfigurasi Platform Slack (slack-bolt Socket Mode)",
    "enabled: true",
    "token: 'xoxb-your-bot-token'            # Bot User OAuth Token",
    "app_token: 'xapp-your-app-token'        # App-Level Token (scope: connections:write)",
    "require_mention: true                   # Hanya respons jika bot di-mention di channel",
    "# allowed_channels: 'C12345678'         # Whitelist channel ID Slack",
    "reactions: true                         # Izinkan bot memberi emoji reaction"
  ].join(String.fromCharCode(10)),
  matrix: [
    "# Konfigurasi Platform Matrix (mautrix SDK)",
    "enabled: true",
    "homeserver: 'https://matrix.org'        # URL homeserver Matrix (Synapse/Dendrite)",
    "user_id: '@bot:matrix.org'              # Matrix User ID akun bot",
    "access_token: 'syt_your_token'          # Access token Matrix akun bot",
    "require_mention: true                   # Hanya respons jika di-mention di room",
    "# allowed_rooms: '!roomid:matrix.org'   # Whitelist room ID Matrix",
    "# allowed_users: '@user:matrix.org'     # Whitelist user ID Matrix"
  ].join(String.fromCharCode(10)),
  mattermost: [
    "# Konfigurasi Platform Mattermost (v4 REST + WebSocket)",
    "enabled: true",
    "url: 'https://mattermost.example.com'   # URL instance server Mattermost",
    "token: 'your-bot-access-token'          # Personal Access Token atau Bot Token",
    "require_mention: true                   # Wajib @mention di channel publik",
    "# allowed_channels: 'channel-id'        # Whitelist channel ID",
    "reply_mode: 'off'                       # 'thread' (nested reply) atau 'off' (flat timeline)"
  ].join(String.fromCharCode(10)),
  signal: [
    "# Konfigurasi Platform Signal (signal-cli-rest-api)",
    "enabled: true",
    "phone_number: '+6281234567890'          # Nomor akun Signal terdaftar (format E.164)",
    "http_host: '127.0.0.1'                  # Host service signal-cli API",
    "http_port: 8080                         # Port service signal-cli API",
    "# allowed_users: '+6289876543210'       # Whitelist nomor/UUID pengirim"
  ].join(String.fromCharCode(10)),
  teams: [
    "# Konfigurasi Platform Microsoft Teams (Azure Bot Framework)",
    "enabled: true",
    "app_id: 'AZURE_BOT_APP_ID'              # Microsoft App ID dari Azure Portal",
    "app_password: 'AZURE_BOT_APP_PASSWORD'  # Client Secret dari Azure Portal",
    "# tenant_id: 'AZURE_TENANT_ID'          # Azure AD Tenant ID",
    "port: 3978                              # Webhook listen port (Bot Framework default 3978)",
    "require_mention: false                  # Hanya respons jika di-mention @bot di grup"
  ].join(String.fromCharCode(10)),
  feishu: [
    "# Konfigurasi Platform Feishu / Lark (lark-oapi SDK)",
    "enabled: true",
    "app_id: 'cli_your_app_id'               # Feishu / Lark App ID",
    "app_secret: 'your_app_secret'           # Feishu / Lark App Secret",
    "domain: 'feishu'                        # 'feishu' (China) atau 'lark' (Internasional)",
    "require_mention: true                   # Hanya respons jika di-mention @bot di grup",
    "# allowed_users: 'ou_your_user_id'      # Whitelist user ID"
  ].join(String.fromCharCode(10)),
  google_chat: [
    "# Konfigurasi Google Chat (Google Chat REST API)",
    "enabled: true",
    "service_account_json: 'credentials.json' # Path ke Service Account JSON key GCP",
    "# project_id: 'gcp-project-id'          # GCP Project ID untuk Pub/Sub",
    "# http_events_url: 'https://...'        # Endpoint jika mode HTTP callback",
    "# allowed_users: 'user@example.com'     # Whitelist email user"
  ].join(String.fromCharCode(10)),
  dingtalk: [
    "# Konfigurasi Platform DingTalk (dingtalk-stream SDK)",
    "enabled: true",
    "client_id: 'DINGTALK_CLIENT_ID'         # DingTalk App Key (Client ID)",
    "client_secret: 'DINGTALK_CLIENT_SECRET' # DingTalk App Secret (Client Secret)",
    "require_mention: true                   # Hanya respons jika di-mention di grup",
    "# allowed_users: 'staff_id'             # Whitelist sender ID (* = semua)"
  ].join(String.fromCharCode(10)),
  wecom: [
    "# Konfigurasi WeCom / WeChat Work (Smart Robot / Callback)",
    "enabled: true",
    "corp_id: 'YOUR_CORP_ID'                 # Enterprise Corp ID WeCom",
    "corp_secret: 'YOUR_CORP_SECRET'         # Application Secret WeCom",
    "# allow_from: 'user_id'                 # Whitelist member ID WeCom",
    "# group_allow_from: 'group_id'          # Whitelist group ID WeCom"
  ].join(String.fromCharCode(10)),
  line: [
    "# Konfigurasi LINE Messaging API (LINE Developers Webhook)",
    "enabled: true",
    "channel_secret: 'LINE_CHANNEL_SECRET'   # LINE Channel Secret untuk verifikasi HMAC",
    "channel_access_token: 'LONG_LIVED_TOKEN'# LINE Long-lived Channel Access Token",
    "port: 8646                              # Port webhook listener local di STB",
    "# allowed_users: 'U1234567890...'       # Whitelist LINE user ID (awalan U)",
    "# allowed_groups: 'C1234567890...'      # Whitelist LINE group ID (awalan C)"
  ].join(String.fromCharCode(10)),
  ntfy: [
    "# Konfigurasi ntfy Push Notifications (ntfy.sh / Self-Hosted)",
    "enabled: true",
    "topic: 'hermes-alerts'                  # Topic ntfy untuk subscribe / kirim pesan",
    "server: 'https://ntfy.sh'               # Server ntfy (atau server self-hosted milikmu)",
    "# token: 'tk_your_bearer_token'         # Bearer token jika topic diproteksi password",
    "# publish_topic: 'hermes-out'           # Topic balasan (default: sama dengan topic)",
    "markdown: false                         # Kirim header X-Markdown: true"
  ].join(String.fromCharCode(10)),
  email: [
    "# Konfigurasi Email Gateway (IMAP Inbound + SMTP Outbound)",
    "enabled: true",
    "address: 'bot@example.com'              # Alamat email akun bot",
    "password: 'your-app-password'           # Password akun atau Google App Password",
    "smtp_host: 'smtp.gmail.com'             # Host server SMTP pengirim",
    "smtp_port: 587                          # Port SMTP (587 STARTTLS atau 465 SSL)",
    "imap_host: 'imap.gmail.com'             # Host server IMAP penerima",
    "imap_port: 993                          # Port IMAP (993 SSL)",
    "# allowed_users: 'boss@example.com'     # Whitelist alamat email pengirim"
  ].join(String.fromCharCode(10)),
  homeassistant: [
    "# Konfigurasi Home Assistant (WebSocket + Persistent Notification)",
    "enabled: true",
    "url: 'http://homeassistant.local:8123'  # URL instance server Home Assistant",
    "token: 'YOUR_LONG_LIVED_ACCESS_TOKEN'   # Long-Lived Access Token dari profil user HA",
    "# watch_domains: ['light', 'switch']    # Filter domain entity yang diawasi",
    "# watch_entities: ['sensor.temp']       # Filter ID entity tertentu",
    "cooldown_seconds: 3                     # Jeda detik sebelum trigger event ulang"
  ].join(String.fromCharCode(10)),
  simplex: [
    "# Konfigurasi SimpleX Chat (WebSocket ke simplex-chat daemon)",
    "enabled: true",
    "ws_url: 'ws://127.0.0.1:5225'           # WebSocket URL daemon simplex-chat",
    "auto_accept: true                       # Otomatis terima permintaan kontak baru",
    "# group_allowed: '*'                    # Whitelist ID grup atau '*' untuk semua grup"
  ].join(String.fromCharCode(10)),
  sms: [
    "# Konfigurasi SMS Gateway (Twilio REST API)",
    "enabled: true",
    "account_sid: 'AC_YOUR_TWILIO_SID'       # Twilio Account SID",
    "auth_token: 'YOUR_TWILIO_AUTH_TOKEN'    # Twilio Auth Token",
    "phone_number: '+1234567890'             # Nomor telepon Twilio pengirim (format E.164)",
    "# allowed_users: '+628123456789'        # Whitelist nomor telepon penerima"
  ].join(String.fromCharCode(10)),
  irc: [
    "# Konfigurasi IRC (Internet Relay Chat asyncio)",
    "enabled: true",
    "server: 'irc.libera.chat'               # Hostname server IRC",
    "port: 6697                              # Port IRC (6697 SSL atau 6667 plain)",
    "nickname: 'hermes_bot'                  # Nickname bot di server",
    "channel: '#hermes'                      # Channel utama (pisahkan koma untuk banyak)",
    "use_tls: true                           # Gunakan koneksi terenkripsi SSL/TLS",
    "# server_password: 'secret'             # Password server (perintah PASS opsional)",
    "# nickserv_password: 'secret'           # Password NickServ untuk identifikasi otomatis"
  ].join(String.fromCharCode(10)),
  bluebubbles: [
    "# Konfigurasi BlueBubbles (iMessage via BlueBubbles Server)",
    "enabled: true",
    "server_url: 'http://127.0.0.1:1234'     # URL endpoint server BlueBubbles macOS",
    "password: 'your-server-password'        # Password akses API BlueBubbles"
  ].join(String.fromCharCode(10))
}};

function applyGwSelectedTemplate(key){{
  if(!key || !GW_TEMPLATES[key]) return;
  var yamlEl = document.getElementById('gw-config-yaml');
  if(!yamlEl) return;
  var prevTpl = GW_TEMPLATES[currentGwPlatform] || '';
  var isDirty = _yamlEditedByUser && yamlEl.value.trim() && yamlEl.value.trim() !== prevTpl.trim();
  if(isDirty && !confirm('Muat template contoh? Teks konfigurasi saat ini akan diganti dengan template pilihan.')){{
    var tp = document.getElementById('gw-template-picker'); if(tp) tp.value = '';
    return;
  }}
  var plat = key;
  if(isNewGwPlatform){{
    var inputEl = document.getElementById('gw-platform-input');
    var selectEl = document.getElementById('gw-platform-catalog-select');
    var titleEl = document.getElementById('gw-config-title');
    currentGwPlatform = plat;
    if(inputEl) inputEl.value = plat;
    if(selectEl) selectEl.value = plat;
    var name = GW_PLATFORM_NAMES[plat] || plat.toUpperCase();
    if(titleEl) titleEl.textContent = 'Tambah Gateway: ' + name;
  }}
  yamlEl.value = GW_TEMPLATES[key];
  _yamlEditedByUser = false;
  updateGwGuide(plat);
  populateGwFormFromYaml(GW_TEMPLATES[key], plat);
  var tp = document.getElementById('gw-template-picker'); if(tp) tp.value = '';
}}

function selectCatalogPlatform(plat){{
  var inputEl = document.getElementById('gw-platform-input');
  var yamlEl = document.getElementById('gw-config-yaml');
  var titleEl = document.getElementById('gw-config-title');
  var selectEl = document.getElementById('gw-platform-catalog-select');

  if(!plat){{
    if(inputEl) inputEl.value = '';
    return;
  }}
  if(plat === currentGwPlatform) return;
  var prevTpl = GW_TEMPLATES[currentGwPlatform] || '';
  var isFormDirty = false;
  try {{
    var cur = gwFormFieldValues();
    for (var k in cur) {{
      if (JSON.stringify(cur[k]) !== JSON.stringify(gwFormInitial[k])) {{
        isFormDirty = true;
        break;
      }}
    }}
  }} catch(e) {{}}
  var isYamlDirty = _yamlEditedByUser && yamlEl && yamlEl.value.trim() && yamlEl.value.trim() !== prevTpl.trim();
  var isDirty = (currentGwMode === 'ui') ? isFormDirty : isYamlDirty;
  if(isDirty && !confirm('Ganti platform? Perubahan yang belum disimpan akan hilang.')){{
    if(selectEl) selectEl.value = currentGwPlatform || '';
    return;
  }}
  currentGwPlatform = plat;
  _yamlEditedByUser = false;

  if(plat === 'custom'){{
    if(inputEl){{ inputEl.value = ''; inputEl.focus(); }}
    if(titleEl) titleEl.textContent = 'Tambah Gateway Kustom';
    if(yamlEl && !yamlEl.value.trim()){{ yamlEl.value = 'enabled: true' + String.fromCharCode(10); }}
    return;
  }}
  if(selectEl && selectEl.value !== plat) selectEl.value = plat;
  if(inputEl) inputEl.value = plat;

  var name = GW_PLATFORM_NAMES[plat] || plat.toUpperCase();
  if(titleEl) titleEl.textContent = 'Tambah Gateway: ' + name;
  updateGwGuide(plat);

  if(GW_TEMPLATES[plat]){{
    if(yamlEl){{
      yamlEl.value = GW_TEMPLATES[plat];
      populateGwFormFromYaml(yamlEl.value, plat);
    }}
  }} else {{
    fetch('/api/gateway-config?platform=' + encodeURIComponent(plat))
      .then(function(r){{ return r.json(); }})
      .then(function(d){{
        if(d.ok && yamlEl && currentGwPlatform === plat){{
          yamlEl.value = d.yaml || ('enabled: true' + String.fromCharCode(10));
          populateGwFormFromYaml(yamlEl.value, plat);
        }}
      }});
  }}
}}

function openGwConfig(platform, title){{
  currentGwPlatform = platform || '';
  isNewGwPlatform = !platform;
  var modal = document.getElementById('gw-config-modal');
  var titleEl = document.getElementById('gw-config-title');
  var selectWrap = document.getElementById('gw-platform-select-wrap');
  var inputEl = document.getElementById('gw-platform-input');
  var catalogSelect = document.getElementById('gw-platform-catalog-select');
  var yamlEl = document.getElementById('gw-config-yaml');
  var enabledChk = document.getElementById('gw-config-enabled-chk');
  var errEl = document.getElementById('gw-config-error');
  var saveBtn = document.getElementById('btn-save-gw-config');

  if(errEl){{ errEl.style.display = 'none'; errEl.textContent = ''; }}
  if(titleEl) titleEl.textContent = title ? 'Konfigurasi: ' + title : 'Tambah Platform Gateway';
  if(saveBtn){{ saveBtn.textContent = 'Simpan'; saveBtn.disabled = false; }}
  _yamlEditedByUser = false;
  if(yamlEl) yamlEl.value = '';

  switchGwConfigMode('ui');
  if(isNewGwPlatform){{
    currentGwPlatform = 'telegram';
    if(selectWrap) selectWrap.style.display = 'block';
    if(catalogSelect) catalogSelect.value = 'telegram';
    if(inputEl){{ inputEl.value = 'telegram'; inputEl.disabled = false; }}
    if(titleEl) titleEl.textContent = 'Tambah Gateway: Telegram Bot';
    if(yamlEl) yamlEl.value = GW_TEMPLATES['telegram'] || ('enabled: true' + String.fromCharCode(10));
    if(enabledChk) enabledChk.checked = true;
    updateGwGuide('telegram');
    populateGwFormFromYaml(yamlEl.value, 'telegram');
    if(modal) modal.classList.add('show');
  }} else {{
    if(selectWrap) selectWrap.style.display = 'none';
    if(yamlEl) yamlEl.value = 'Memuat konfigurasi…';
    if(saveBtn) saveBtn.disabled = true;
    updateGwGuide(platform);
    if(modal) modal.classList.add('show');

    fetch('/api/gateway-config?platform=' + encodeURIComponent(platform))
      .then(function(r){{ return r.json(); }})
      .then(function(d){{
        if(saveBtn) saveBtn.disabled = false;
        if(d.ok){{
          var yText = d.yaml || ('enabled: true' + String.fromCharCode(10));
          if(yamlEl) yamlEl.value = yText;
          if(enabledChk) enabledChk.checked = !!d.enabled;
          populateGwFormFromYaml(yText, platform);
        }} else {{
          if(errEl){{ errEl.textContent = d.error || 'Gagal memuat konfigurasi'; errEl.style.display = 'block'; }}
        }}
      }})
      .catch(function(e){{
        if(saveBtn) saveBtn.disabled = false;
        if(errEl){{ errEl.textContent = 'Error koneksi: ' + e; errEl.style.display = 'block'; }}
      }});
  }}
}}

function closeGwConfig(){{
  var modal = document.getElementById('gw-config-modal');
  if(modal) modal.classList.remove('show');
  currentGwPlatform = '';
  isNewGwPlatform = false;
  _yamlEditedByUser = false;
}}

function saveGwConfig(){{
  var inputEl = document.getElementById('gw-platform-input');
  var yamlEl = document.getElementById('gw-config-yaml');
  var enabledChk = document.getElementById('gw-config-enabled-chk');
  var restartChk = document.getElementById('gw-config-restart-chk');
  var errEl = document.getElementById('gw-config-error');
  var saveBtn = document.getElementById('btn-save-gw-config');

  var plat = isNewGwPlatform ? (inputEl ? inputEl.value.trim().toLowerCase() : '') : currentGwPlatform;
  if(!plat){{
    if(errEl){{ errEl.textContent = 'Pilih atau ketik nama platform.'; errEl.style.display = 'block'; }}
    return;
  }}

  var isEnabled = enabledChk ? enabledChk.checked : true;
  var isUi = (currentGwMode === 'ui');
  var yamlContent = isUi ? serializeGwFormToYaml(plat) : (yamlEl ? yamlEl.value : '');
  var restartGw = restartChk ? restartChk.checked : true;

  if(saveBtn){{ saveBtn.textContent = 'Menyimpan…'; saveBtn.disabled = true; }}
  if(errEl) errEl.style.display = 'none';

  var payload = {{
    platform: plat,
    yaml: yamlContent,
    enabled: isEnabled,
    restart_gw: restartGw,
    merge: isUi
  }};
  if(isUi && yamlEl) payload.base_yaml = yamlEl.value;

  fetch('/save-gateway-platform', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json', 'Accept': 'application/json' }},
    body: JSON.stringify(payload)
  }})
  .then(function(r){{ return r.json(); }})
  .then(function(res){{
    if(res.ok){{
      closeGwConfig();
      if(res.html){{
        var slot = document.getElementById('gateway-list-slot');
        if(slot) slot.innerHTML = res.html;
      }}
    }} else {{
      if(saveBtn){{ saveBtn.textContent = 'Simpan'; saveBtn.disabled = false; }}
      if(errEl){{ errEl.textContent = res.error || 'Gagal menyimpan konfigurasi'; errEl.style.display = 'block'; }}
    }}
  }})
  .catch(function(err){{
    if(saveBtn){{ saveBtn.textContent = 'Simpan'; saveBtn.disabled = false; }}
    if(errEl){{ errEl.textContent = 'Kesalahan jaringan: ' + err; errEl.style.display = 'block'; }}
  }});
}}

function toggleGwPlatform(plat, enable){{
  fetch('/toggle-gateway-platform', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json', 'Accept': 'application/json' }},
    body: JSON.stringify({{ platform: plat, enabled: enable, restart_gw: true }})
  }})
  .then(function(r){{ return r.json(); }})
  .then(function(res){{
    if(res.ok && res.html){{
      var slot = document.getElementById('gateway-list-slot');
      if(slot) slot.innerHTML = res.html;
    }}
  }});
}}

function deleteGwPlatform(plat, name){{
  if(!confirm('Hapus konfigurasi platform ' + (name || plat) + ' dari config.yaml?')) return;
  fetch('/remove-gateway-platform', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json', 'Accept': 'application/json' }},
    body: JSON.stringify({{ platform: plat, restart_gw: true }})
  }})
  .then(function(r){{ return r.json(); }})
  .then(function(res){{
    if(res.ok && res.html){{
      var slot = document.getElementById('gateway-list-slot');
      if(slot) slot.innerHTML = res.html;
    }}
  }});
}}

function switchGwLogTab(tab){{
  safeStore('setItem', 'gw_log_active_tab', tab);
  var boxGw = document.getElementById('gateway-logbox');
  var boxWa = document.getElementById('whatsapp-logbox');
  var btnGw = document.getElementById('tab-log-gw');
  var btnWa = document.getElementById('tab-log-wa');
  if(tab === 'wa'){{
    if(boxGw) boxGw.style.display = 'none';
    if(boxWa){{
      boxWa.style.display = 'block';
      boxWa.scrollTop = boxWa.scrollHeight;
    }}
    if(btnGw) btnGw.classList.remove('active');
    if(btnWa) btnWa.classList.add('active');
  }} else {{
    if(boxWa) boxWa.style.display = 'none';
    if(boxGw){{
      boxGw.style.display = 'block';
      boxGw.scrollTop = boxGw.scrollHeight;
    }}
    if(btnWa) btnWa.classList.remove('active');
    if(btnGw) btnGw.classList.add('active');
  }}
}}

function syncGwLogTabUI(){{
  var active = safeStore('getItem', 'gw_log_active_tab') || 'gw';
  switchGwLogTab(active);
}}

var _waPairPollTimer = null;
var _waPairTimerInterval = null;
var _waPairExpiresAt = null;
var _waPairCancelled = false;
var _waPairActionGen = 0;
var _lastWaPairStatus = '';
function openWaPairModal(){{
  _waPairCancelled = false;
  _lastWaPairStatus = '';
  var m = document.getElementById('wa-pair-modal');
  if(m){{
    m.classList.add('show');
    m.style.display = 'flex';
  }}
  var qrBox = document.getElementById('wa-pair-qr-container');
  if(qrBox) qrBox.innerHTML = '';
  var timerEl = document.getElementById('wa-pair-timer');
  if(timerEl) timerEl.textContent = '';
  fetch('/api/whatsapp/pair-status')
    .then(function(r){{ return r.json(); }})
    .then(function(d){{
      var m = document.getElementById('wa-pair-modal');
      var isOpen = m && (m.classList.contains('show') || m.style.display !== 'none');
      if(!isOpen || _waPairCancelled) return;
      setWaPairStatusUI(d);
      if(d.status === 'starting' || d.status === 'waiting_scan'){{
        pollWaPairStatus();
      }}
    }})
    .catch(function(err){{
      setWaPairStatusUI({{status: 'error', error: 'Gagal memuat status WhatsApp: ' + err}});
    }});
}}

function closeWaPairModal(skipCancel){{
  _waPairCancelled = true;
  var m = document.getElementById('wa-pair-modal');
  if(m){{
    m.classList.remove('show');
    m.style.display = 'none';
  }}
  if(_waPairPollTimer){{
    clearTimeout(_waPairPollTimer);
    _waPairPollTimer = null;
  }}
  if(_waPairTimerInterval){{
    clearInterval(_waPairTimerInterval);
    _waPairTimerInterval = null;
  }}
  var qrBox = document.getElementById('wa-pair-qr-container');
  if(qrBox) qrBox.innerHTML = '';
  var timerEl = document.getElementById('wa-pair-timer');
  if(timerEl) timerEl.textContent = '';
  var isPairingActive = (_lastWaPairStatus === 'waiting_scan' || _lastWaPairStatus === 'starting');
  if(!skipCancel && isPairingActive){{
    fetch('/api/whatsapp/pair-cancel', {{method: 'POST'}}).catch(function(){{}});
  }}
}}

function setWaPairStatusUI(data){{
  _lastWaPairStatus = data.status || '';
  var bText = document.getElementById('wa-pair-status-text');
  var badge = document.getElementById('wa-pair-status-badge');
  var qrBox = document.getElementById('wa-pair-qr-container');
  var logBox = document.getElementById('wa-pair-logbox');
  var timerEl = document.getElementById('wa-pair-timer');
  var btnStart = document.getElementById('btn-start-wa-pair');
  var btnReset = document.getElementById('btn-reset-wa-pair');
  var btnCancel = document.getElementById('btn-cancel-wa-pair');
  var btnApply = document.getElementById('btn-apply-wa-pair');

  if(timerEl){{
    if(data.status === 'waiting_scan' && data.expires_at){{
      _waPairExpiresAt = data.expires_at;
      var updateTimer = function(){{
        if(!_waPairExpiresAt || !timerEl) return;
        var rem = Math.max(0, Math.round(_waPairExpiresAt - (Date.now()/1000)));
        timerEl.textContent = rem > 0 ? ('⏳ ' + rem + 's') : '';
        if(rem <= 0){{
          var qb = document.getElementById('wa-pair-qr-container');
          if(qb) qb.innerHTML = '<div style="color:var(--warning);padding:2rem 1rem;text-align:center"><div style="font-size:2rem;margin-bottom:0.5rem">⏳</div><div style="font-weight:600">QR Code Kedaluwarsa</div><div style="font-size:0.75rem;color:var(--text-dim);margin-top:0.4rem">Menunggu refresh QR dari server…</div></div>';
          if(_waPairTimerInterval){{
            clearInterval(_waPairTimerInterval);
            _waPairTimerInterval = null;
          }}
        }}
      }};
      updateTimer();
      if(!_waPairTimerInterval){{
        _waPairTimerInterval = setInterval(updateTimer, 1000);
      }}
    }} else {{
      _waPairExpiresAt = null;
      if(_waPairTimerInterval){{
        clearInterval(_waPairTimerInterval);
        _waPairTimerInterval = null;
      }}
      timerEl.textContent = '';
    }}
  }}

  if(logBox && data.logs && data.logs.length > 0){{
    logBox.textContent = data.logs.join(String.fromCharCode(10));
    logBox.scrollTop = logBox.scrollHeight;
  }}

  if(data.status === 'waiting_scan'){{
    if(bText) bText.textContent = 'Menunggu scan dari WhatsApp di HP...';
    if(badge){{ badge.className = 'badge badge-warn'; badge.textContent = 'Scan QR'; }}
    if(qrBox && data.qr_svg){{
      qrBox.innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;gap:8px">' +
        data.qr_svg +
        '<div style="font-size:0.75rem;color:var(--accent-light);font-weight:500;display:flex;align-items:center;gap:6px">' +
        '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#10b981"></span> QR Aktif (Auto-refresh)</div></div>';
    }}
    if(btnStart) btnStart.style.display = 'none';
    if(btnCancel) btnCancel.style.display = 'inline-block';
    if(btnReset) btnReset.style.display = 'inline-block';
    if(btnApply) btnApply.style.display = 'none';
  }} else if(data.status === 'starting'){{
    if(bText) bText.textContent = 'Memulai bridge WhatsApp...';
    if(badge){{ badge.className = 'badge badge-warn'; badge.textContent = 'Starting'; }}
    if(qrBox){{
      qrBox.innerHTML = '<div style="padding:2.2rem 1rem;text-align:center;color:var(--text-muted)"><div class="spinner" style="margin:0 auto 0.75rem auto;width:24px;height:24px;border:2px solid rgba(255,255,255,0.1);border-top-color:var(--accent);border-radius:50%;animation:spin 0.8s linear infinite"></div><div style="font-weight:600;color:var(--text);font-size:0.85rem">Menghubungkan ke WhatsApp Bridge...</div><div style="font-size:0.75rem;margin-top:0.3rem">Menyiapkan socket Baileys &amp; QR code</div></div>';
    }}
    if(btnStart) btnStart.style.display = 'none';
    if(btnCancel) btnCancel.style.display = 'inline-block';
    if(btnReset) btnReset.style.display = 'none';
    if(btnApply) btnApply.style.display = 'none';
  }} else if(data.status === 'connected'){{
    var rawUName = (data.user && (data.user.name || data.user.id)) || 'Akun WhatsApp';
    var uName = escGw(rawUName);
    if(bText) bText.textContent = 'Terhubung sebagai: ' + rawUName;
    if(badge){{ badge.className = 'badge badge-up'; badge.textContent = 'Terhubung'; }}
    if(qrBox){{
      qrBox.innerHTML = '<div style="color:#10b981;padding:2rem 1rem;text-align:center"><div style="font-size:3rem;line-height:1;margin-bottom:0.5rem">✓</div><div style="font-weight:700;font-size:1.1rem">WhatsApp Berhasil Tertaut!</div><div style="font-size:0.82rem;color:var(--text);margin-top:0.4rem">Akun: <strong>' + uName + '</strong></div><div style="font-size:0.75rem;color:var(--text-dim);margin-top:0.3rem">Kredensial tersimpan di sesi lokal server.</div></div>';
    }}
    if(btnStart) btnStart.style.display = 'none';
    if(btnCancel) btnCancel.style.display = 'none';
    if(btnReset) btnReset.style.display = 'inline-block';
    if(btnApply) btnApply.style.display = 'inline-block';
  }} else if(data.status === 'error'){{
    var rawErr = data.error || 'Terjadi kesalahan';
    var err = escGw(rawErr);
    if(bText) bText.textContent = 'Status: ' + rawErr;
    if(badge){{ badge.className = 'badge badge-down'; badge.textContent = 'Gagal'; }}
    if(qrBox){{
      qrBox.innerHTML = '<div style="color:var(--danger);padding:2rem 1rem;text-align:center"><div style="font-size:2rem;margin-bottom:0.5rem">⚠</div><div style="font-weight:600">' + err + '</div><div style="font-size:0.75rem;color:var(--text-dim);margin-top:0.4rem">Tekan tombol Coba Lagi untuk membuat sesi pairing baru.</div></div>';
    }}
    if(btnStart){{ btnStart.textContent = 'Coba Lagi (Generate QR)'; btnStart.style.display = 'inline-block'; }}
    if(btnCancel) btnCancel.style.display = 'none';
    if(btnReset) btnReset.style.display = 'none';
    if(btnApply) btnApply.style.display = 'none';
  }} else {{
    if(bText) bText.textContent = 'Status: Siap untuk pairing';
    if(badge){{ badge.className = 'badge'; badge.textContent = 'Idle'; }}
    if(qrBox){{
      qrBox.innerHTML = '<div style="color:var(--text-dim);font-size:0.82rem;padding:2rem 1rem">Tekan tombol <strong>"Mulai Pairing QR"</strong> di bawah untuk menginisialisasi jembatan Baileys dan membuat QR code.</div>';
    }}
    if(btnStart){{ btnStart.textContent = 'Mulai Pairing QR'; btnStart.style.display = 'inline-block'; }}
    if(btnCancel) btnCancel.style.display = 'none';
    if(btnReset) btnReset.style.display = 'none';
    if(btnApply) btnApply.style.display = 'none';
  }}
}}

var _waPairPolling = false;
function pollWaPairStatus(){{
  if(_waPairPollTimer){{ clearTimeout(_waPairPollTimer); _waPairPollTimer = null; }}
  if(_waPairPolling) return;
  _waPairPolling = true;
  fetch('/api/whatsapp/pair-status')
    .then(function(r){{ return r.json(); }})
    .then(function(d){{
      _waPairPolling = false;
      var m = document.getElementById('wa-pair-modal');
      var isOpen = m && (m.classList.contains('show') || m.style.display !== 'none');
      if(!isOpen || _waPairCancelled) return;
      setWaPairStatusUI(d);
      if(d.status === 'starting' || d.status === 'waiting_scan'){{
        _waPairPollTimer = setTimeout(pollWaPairStatus, 1500);
      }}
    }})
    .catch(function(){{
      _waPairPolling = false;
      var m = document.getElementById('wa-pair-modal');
      var isOpen = m && (m.classList.contains('show') || m.style.display !== 'none');
      if(isOpen && !_waPairCancelled){{
        _waPairPollTimer = setTimeout(pollWaPairStatus, 2500);
      }}
    }});
}}

function startWaPair(clearSession){{
  _waPairCancelled = false;
  var actGen = ++_waPairActionGen;
  var bText = document.getElementById('wa-pair-status-text');
  var badge = document.getElementById('wa-pair-status-badge');
  var qrBox = document.getElementById('wa-pair-qr-container');
  var btnStart = document.getElementById('btn-start-wa-pair');
  var btnCancel = document.getElementById('btn-cancel-wa-pair');
  if(bText) bText.textContent = 'Menyiapkan jembatan WhatsApp...';
  if(badge){{ badge.className = 'badge badge-warn'; badge.textContent = 'Starting'; }}
  if(btnStart) btnStart.style.display = 'none';
  if(btnCancel) btnCancel.style.display = 'inline-block';
  if(qrBox){{
    qrBox.innerHTML = '<div style="padding:2.2rem 1rem;text-align:center;color:var(--text-muted)"><div class="spinner" style="margin:0 auto 0.75rem auto;width:24px;height:24px;border:2px solid rgba(255,255,255,0.1);border-top-color:var(--accent);border-radius:50%;animation:spin 0.8s linear infinite"></div><div style="font-weight:600;color:var(--text);font-size:0.85rem">Menghubungkan ke WhatsApp Bridge...</div><div style="font-size:0.75rem;margin-top:0.3rem">Menyiapkan socket Baileys &amp; QR code</div></div>';
  }}
  fetch('/api/whatsapp/pair-start', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json', 'Accept': 'application/json'}},
    body: JSON.stringify({{force_new: clearSession !== false}})
  }}).then(function(r){{ return r.json(); }})
    .then(function(d){{
      setWaPairStatusUI(d);
      pollWaPairStatus();
    }})
    .catch(function(){{
      pollWaPairStatus();
    }});
}}

function cancelWaPair(){{
  _waPairCancelled = true;
  var actGen = ++_waPairActionGen;
  if(_waPairPollTimer){{
    clearTimeout(_waPairPollTimer);
    _waPairPollTimer = null;
  }}
  if(_waPairTimerInterval){{
    clearInterval(_waPairTimerInterval);
    _waPairTimerInterval = null;
  }}
  fetch('/api/whatsapp/pair-cancel', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json', 'Accept': 'application/json'}},
    body: JSON.stringify({{}})
  }}).then(function(r){{ return r.json(); }})
    .then(function(d){{
      if(actGen !== _waPairActionGen) return;
      setWaPairStatusUI(d);
      var qrBox = document.getElementById('wa-pair-qr-container');
      if(qrBox){{
        qrBox.innerHTML = '<div style="color:var(--text-dim);font-size:0.82rem;padding:2rem 1rem">Pairing dibatalkan. Tekan <strong>"Mulai Pairing QR"</strong> untuk memulai kembali.</div>';
      }}
    }});
}}

function applyWaPair(){{
  var btn = document.getElementById('btn-apply-wa-pair');
  if(btn){{ btn.disabled = true; btn.textContent = 'Mengaktifkan...'; }}
  fetch('/api/whatsapp/pair-apply', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json', 'Accept': 'application/json'}},
    body: JSON.stringify({{restart_gw: true}})
  }}).then(function(r){{ return r.json(); }})
    .then(function(d){{
      if(d && d.ok){{
        closeWaPairModal(true);
        window.location.reload();
      }} else {{
        if(btn){{ btn.disabled = false; btn.textContent = 'Aktifkan & Mulai Ulang Gateway'; }}
        alert((d && d.message) || 'Gagal menerapkan pairing WhatsApp');
      }}
    }}).catch(function(err){{
      if(btn){{ btn.disabled = false; btn.textContent = 'Aktifkan & Mulai Ulang Gateway'; }}
      alert('Kesalahan jaringan: ' + err);
    }});
}}

function scrollAllLogsToBottom(){{
  var doScroll = function(){{
    document.querySelectorAll('.logbox').forEach(function(b){{
      b.scrollTop = b.scrollHeight;
    }});
  }};
  doScroll();
  setTimeout(doScroll, 40);
  setTimeout(doScroll, 180);
}}
function patchPage(boxId, page){{
  var box = document.getElementById(boxId);
  if(!box) return;
  var items = box.querySelectorAll('[data-patch-page]');
  var total = 1;
  for(var i=0;i<items.length;i++) total = Math.max(total, parseInt(items[i].getAttribute('data-patch-page') || '1', 10));
  page = Math.max(1, Math.min(total, page));
  for(var j=0;j<items.length;j++) items[j].style.display = items[j].getAttribute('data-patch-page') === String(page) ? '' : 'none';
  var label = box.querySelector('.patch-notes-page-label');
  if(label) label.textContent = page + ' / ' + total;
  var prev = box.querySelector('.patchPrev');
  var next = box.querySelector('.patchNext');
  if(prev) prev.disabled = page <= 1;
  if(next) next.disabled = page >= total;
  box.setAttribute('data-patch-current', String(page));
  safeStore('setItem', 'patch_page_' + boxId, String(page));
}}
function restorePatchPages(){{
  var boxes = document.querySelectorAll('.patch-notes-box');
  for(var i=0; i<boxes.length; i++){{
    var box = boxes[i];
    var saved = safeStore('getItem', 'patch_page_' + box.id);
    if(saved){{
      patchPage(box.id, parseInt(saved, 10));
    }}
  }}
}}
function filterModels(q){{
  q = q.toLowerCase();
  var chips = document.querySelectorAll('#model-chips .model-chip');
  chips.forEach(function(c){{
    c.style.display = c.textContent.toLowerCase().indexOf(q) !== -1 ? '' : 'none';
  }});
  var groups = document.querySelectorAll('#model-chips .model-group');
  groups.forEach(function(g){{
    var any = g.querySelectorAll('.model-chip:not([style*="none"])').length > 0;
    g.style.display = any ? '' : 'none';
  }});
}}
function toggleLog(flag, wrapId){{
  safeStore('removeItem', flag);
  var w = document.getElementById(wrapId);
  if(w) w.style.display = 'none';
  window._forceBottom = true;
  if(flag === 'logDismissed' && window._lastRouterLogCard){{
    var rls = document.getElementById('log-slot');
    if(rls) rls.innerHTML = window._lastRouterLogCard;
  }}
  if(flag === 'hermesLogDismissed' && window._lastHermesLogCard){{
    var hls = document.getElementById('hermes-log-slot');
    if(hls) hls.innerHTML = window._lastHermesLogCard;
  }}
  if(flag === 'cleanLogDismissed' && window._lastCleanJunkCard){{
    var cls = document.getElementById('clean-log-slot');
    if(cls) cls.innerHTML = window._lastCleanJunkCard;
  }}
  if(flag === 'gatewayLogDismissed' && window._lastGatewayLogCard){{
    var gls = document.getElementById('gateway-log-slot');
    if(gls){{
      gls.innerHTML = window._lastGatewayLogCard;
      syncGwLogTabUI();
    }}
  }}
}}
function syncLogUI(){{
  var routerDismissed = safeStore('getItem','logDismissed');
  var rw = document.getElementById('log-show-wrap');
  var rc = document.getElementById('router-log-card');
  var hasRouter = !!(rc || window._hasRouterLog || window._lastRouterLogCard);
  if(rw) rw.style.display = (routerDismissed && hasRouter) ? '' : 'none';
  if(routerDismissed && rc){{
    window._lastRouterLogCard = rc.outerHTML;
    window._hasRouterLog = true;
    rc.remove();
  }}
  var hermesDismissed = safeStore('getItem','hermesLogDismissed');
  var hw = document.getElementById('hermes-log-show');
  var hc = document.getElementById('hermes-log-card');
  var hasHermes = !!(hc || window._hasHermesLog || window._lastHermesLogCard);
  if(hw) hw.style.display = (hermesDismissed && hasHermes) ? '' : 'none';
  if(hermesDismissed && hc){{
    window._lastHermesLogCard = hc.outerHTML;
    window._hasHermesLog = true;
    hc.remove();
  }}
  var cleanDismissed = safeStore('getItem','cleanLogDismissed');
  var cw = document.getElementById('clean-log-show');
  var cc = document.getElementById('clean-log-card');
  var hasClean = !!(cc || window._hasCleanLog || window._lastCleanJunkCard);
  if(cw) cw.style.display = (cleanDismissed && hasClean) ? '' : 'none';
  if(cleanDismissed && cc){{
    window._lastCleanJunkCard = cc.outerHTML;
    window._hasCleanLog = true;
    cc.remove();
  }}
  var gatewayDismissed = safeStore('getItem','gatewayLogDismissed');
  var gwShow = document.getElementById('gateway-log-show');
  var gc = document.getElementById('gateway-log-card');
  var hasGateway = !!(gc || window._hasGatewayLog || window._lastGatewayLogCard);
  if(gwShow) gwShow.style.display = (gatewayDismissed && hasGateway) ? '' : 'none';
  if(gatewayDismissed && gc){{
    window._lastGatewayLogCard = gc.outerHTML;
    window._hasGatewayLog = true;
    gc.remove();
  }}
}}
if(document.getElementById('router-log-card')){{
  window._hasRouterLog = true;
  var initRc = document.getElementById('router-log-card');
  if(initRc) window._lastRouterLogCard = initRc.outerHTML;
}}
if(document.getElementById('hermes-log-card')){{
  window._hasHermesLog = true;
  var initHc = document.getElementById('hermes-log-card');
  if(initHc) window._lastHermesLogCard = initHc.outerHTML;
}}
if(document.getElementById('clean-log-card')){{
  window._hasCleanLog = true;
  var initClc = document.getElementById('clean-log-card');
  if(initClc) window._lastCleanJunkCard = initClc.outerHTML;
}}
if(document.getElementById('gateway-log-card')){{
  window._hasGatewayLog = true;
  var initGwc = document.getElementById('gateway-log-card');
  if(initGwc) window._lastGatewayLogCard = initGwc.outerHTML;
}}
syncLogUI();
// Initial load: scroll all existing log boxes to the bottom once
document.addEventListener('DOMContentLoaded', scrollAllLogsToBottom);
setTimeout(scrollAllLogsToBottom, 100);
setTimeout(scrollAllLogsToBottom, 600);
</script>
</body></html>"""

def get_open_block_active():
    if HERMES_DASHBOARD_URL:
        target = html.escape(HERMES_DASHBOARD_URL, quote=True)
        return (
            f'<a class="open" href="{target}" target="_blank" rel="noopener">'
            f'{ICON_EXTERNAL_LINK}Buka Dasbor Hermes</a>'
        )

    return (
        f'<a class="open" href="#" target="_blank" rel="noopener" '
        f'onclick="window.open(window.location.protocol+\'//\'+'
        f'window.location.hostname+\':9119\',\'_blank\',\'noopener\');'
        f'return false;">'
        f'{ICON_EXTERNAL_LINK}Buka Dasbor Hermes</a>'
    )

OPEN_BLOCK_INACTIVE = '<div class="update-hint warn" style="margin:0">Dasbor Hermes mati — nyalakan dulu untuk membukanya</div>'

_status_probe_cache = {}
_status_probe_cache_lock = threading.Lock()


def _ttl_cached(key: str, ttl: float, loader):
    """Return a short-lived cached probe result without holding the lock during I/O."""
    now = time.monotonic()

    with _status_probe_cache_lock:
        entry = _status_probe_cache.get(key)
        if entry and now - entry["at"] < ttl:
            return entry["value"]

    value = loader()

    with _status_probe_cache_lock:
        _status_probe_cache[key] = {
            "at": time.monotonic(),
            "value": value,
        }

    return value


def _invalidate_status_cache(*keys: str) -> None:
    with _status_probe_cache_lock:
        if keys:
            for key in keys:
                _status_probe_cache.pop(key, None)
        else:
            _status_probe_cache.clear()

def _probe_gateway_info() -> str:
    """Gateway service status: state, RSS memory, uptime."""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "show", "hermes-gateway",
             "--property=ActiveState,MainPID,ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=INFO_TIMEOUT,
            env={**os.environ, "XDG_RUNTIME_DIR": "/run/user/0"},
        )
        if r.returncode != 0:
            return "?"
        props = {}
        for line in r.stdout.strip().split("\n"):
            if "=" in line:
                k, _, v = line.partition("=")
                props[k] = v
        pid = props.get("MainPID", "?")
        # Read RSS from /proc/<pid>/status instead of systemd MemoryCurrent
        # (which reports virtual memory, not physical)
        mem_mb = 0
        if pid and pid != "0":
            try:
                with open(f"/proc/{pid}/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            mem_mb = int(line.split()[1]) / 1024  # kB -> MB
                            break
            except Exception:
                pass
        # uptime from ActiveEnterTimestamp
        ts_str = props.get("ActiveEnterTimestamp", "")
        uptime_part = ""
        if ts_str:
            try:
                parts = ts_str.split()
                if len(parts) >= 3:
                    from datetime import datetime
                    dt_str = f"{parts[1]} {parts[2]}"
                    start = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                    delta = datetime.now() - start
                    d, rem = divmod(int(delta.total_seconds()), 86400)
                    hours, rem = divmod(rem, 3600)
                    mins, _ = divmod(rem, 60)
                    if d > 0:
                        uptime_part = f" · {d} hari {hours} jam"
                    else:
                        uptime_part = f" · {hours} jam {mins} mnt"
            except Exception:
                pass
        return f"PID {pid} · {mem_mb:.0f}MB{uptime_part}"
    except Exception:
        return "?"


def get_gateway_info() -> str:
    return _ttl_cached("gateway_info", 2.0, _probe_gateway_info)


HERMES_GATEWAY_STATE_PATH = os.environ.get(
    "HERMES_GATEWAY_STATE_PATH",
    "/opt/AppData/hermes-native/hermes-data/gateway_state.json",
)


def _platform_icon(platform: str) -> str:
    p = platform.lower()
    if p == "telegram":
        return _icon('<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>', size=16)
    if p == "webhook":
        return _icon('<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>', size=16)
    if p == "discord":
        return _icon('<rect x="2" y="6" width="20" height="12" rx="6"/><circle cx="8" cy="12" r="1.5"/><circle cx="16" cy="12" r="1.5"/>', size=16)
    if p == "whatsapp":
        return _icon('<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>', size=16)
    if p == "slack":
        return _icon('<line x1="4" y1="9" x2="20" y2="9"/><line x1="4" y1="15" x2="20" y2="15"/><line x1="10" y1="3" x2="8" y2="21"/><line x1="16" y1="3" x2="14" y2="21"/>', size=16)
    if p == "matrix":
        return _icon('<polyline points="4 7 4 4 20 4 20 7"/><line x1="9" y1="20" x2="15" y2="20"/><line x1="12" y1="4" x2="12" y2="20"/>', size=16)
    if p in ("teams", "feishu", "google_chat", "wecom", "dingtalk"):
        return _icon('<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>', size=16)
    if p in ("signal", "simplex"):
        return _icon('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>', size=16)
    if p == "email":
        return _icon('<path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/>', size=16)
    if p == "ntfy":
        return _icon('<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>', size=16)
    if p == "homeassistant":
        return _icon('<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>', size=16)
    return ICON_BOT


def _platform_icon_box_class(platform: str) -> str:
    p = platform.lower()
    if p in ("telegram", "signal", "mattermost", "homeassistant"):
        return "cc-icon-blue"
    if p in ("webhook", "ntfy", "line"):
        return "cc-icon-orange"
    if p in ("discord", "slack", "teams", "feishu"):
        return "cc-icon-purple"
    if p in ("whatsapp", "matrix", "email", "google_chat"):
        return "cc-icon-green"
    return "cc-icon-blue"


def _probe_gateway_platforms() -> list[dict]:
    """Inspect messaging platforms configured in config.yaml and their live runtime status."""
    cfg = get_parsed_config()
    cfg_platforms = cfg.get("platforms")
    if not isinstance(cfg_platforms, dict):
        cfg_platforms = {}

    candidate_platforms = []
    for p in cfg_platforms.keys():
        if p not in candidate_platforms:
            candidate_platforms.append(p)

    for k in sorted(LEGACY_GATEWAY_ROOT_KEYS):
        if k not in candidate_platforms and k in cfg and isinstance(cfg[k], dict):
            if _to_bool(cfg[k].get("enabled")) or cfg[k].get("token") or cfg[k].get("bot_token"):
                candidate_platforms.append(k)

    state = {}
    if os.path.exists(HERMES_GATEWAY_STATE_PATH):
        try:
            with open(HERMES_GATEWAY_STATE_PATH, encoding="utf-8") as f:
                state = json.load(f) or {}
        except Exception:
            state = {}

    gw_active = service_active("hermes-gateway", user=True)
    rt_platforms = state.get("platforms") if isinstance(state.get("platforms"), dict) else {}

    results = []
    for p in candidate_platforms:
        p_cfg = _effective_platform_block(cfg, p)

        enabled = _to_bool(p_cfg.get("enabled", False))
        rt = rt_platforms.get(p) if isinstance(rt_platforms.get(p), dict) else {}

        rt_state = str(rt.get("state") or "").strip().lower()
        err_code = rt.get("error_code")
        err_msg = rt.get("error_message")
        needs_attention = bool(rt.get("needs_attention", False))

        is_error = bool(err_code or err_msg or rt_state in ("error", "failed", "fatal", "degraded") or needs_attention)
        error_detail = ""
        if err_msg:
            error_detail = str(err_msg).strip()
        elif err_code:
            error_detail = f"Kode error: {err_code}"
        elif rt_state in ("error", "failed", "fatal", "degraded"):
            error_detail = f"Status error: {rt_state}"

        meta_parts = []
        home = p_cfg.get("home_channel") if isinstance(p_cfg.get("home_channel"), dict) else {}
        if home:
            h_name = home.get("name")
            h_id = home.get("chat_id")
            if h_name and h_id:
                meta_parts.append(f"Home: {h_name} ({h_id})")
            elif h_name or h_id:
                meta_parts.append(f"Chat: {h_name or h_id}")
        if rt.get("listener_base"):
            meta_parts.append(f"Listener: {rt.get('listener_base')}")
        if enabled and gw_active and rt.get("writer_pid"):
            meta_parts.append(f"PID {rt.get('writer_pid')}")

        if not gw_active:
            status_key = "gateway_down"
            status_label = "Gateway Berhenti"
            badge_class = "badge-down"
        elif not enabled:
            status_key = "disabled"
            status_label = "Nonaktif"
            badge_class = "badge-muted"
        elif is_error:
            status_key = "error"
            status_label = "Error"
            badge_class = "badge-down"
        elif rt_state in ("connected", "running", "ok", "ready"):
            status_key = "connected"
            status_label = "Terhubung"
            badge_class = "badge-up"
        elif rt_state in ("connecting", "retrying"):
            status_key = "connecting"
            status_label = "Menghubungkan…"
            badge_class = "badge-warn"
        else:
            status_key = "disconnected"
            status_label = "Terputus"
            badge_class = "badge-down"

        labels_map = {
            "telegram": "Telegram Bot",
            "webhook": "HTTP Webhook",
            "discord": "Discord Bot",
            "whatsapp": "WhatsApp",
            "slack": "Slack Bot",
            "matrix": "Matrix",
            "mattermost": "Mattermost",
            "signal": "Signal Messenger",
            "teams": "Microsoft Teams",
            "feishu": "Feishu / Lark",
            "google_chat": "Google Chat",
            "dingtalk": "DingTalk",
            "wecom": "WeCom",
            "line": "LINE Messaging",
            "ntfy": "ntfy Push",
            "email": "Email Gateway",
            "homeassistant": "Home Assistant",
            "simplex": "SimpleX Chat",
            "sms": "SMS (Twilio)",
            "irc": "IRC",
            "bluebubbles": "BlueBubbles",
        }
        display_name = labels_map.get(p, p.capitalize())

        metadata_text = " · ".join(meta_parts) if meta_parts else ("Nonaktif di konfigurasi" if not enabled else "Menunggu inisialisasi…")

        results.append({
            "platform": p,
            "display_name": display_name,
            "enabled": enabled,
            "status_key": status_key,
            "status_label": status_label,
            "badge_class": badge_class,
            "is_error": is_error,
            "error_detail": error_detail,
            "metadata": metadata_text,
        })

    results.sort(key=lambda x: (not x["enabled"], x["display_name"]))
    return results


def get_gateway_platforms() -> list[dict]:
    return _ttl_cached("gateway_platforms", 3.0, _probe_gateway_platforms)


def get_gateway_platforms_summary() -> tuple[str, str, str]:
    """Return (badge_text, badge_class, compact_bento_text) for gateway platforms."""
    platforms = get_gateway_platforms()
    enabled = [p for p in platforms if p["enabled"]]
    if not enabled:
        return "0 Gateway", "badge-muted", "Tidak ada gateway aktif"

    connected_count = sum(1 for p in enabled if p["status_key"] == "connected")
    error_count = sum(1 for p in enabled if p["is_error"])

    if error_count > 0:
        badge_text = f"{error_count} Error · {connected_count}/{len(enabled)} Konek"
        badge_class = "badge-down"
    elif connected_count == len(enabled):
        badge_text = f"{connected_count} Terhubung"
        badge_class = "badge-up"
    else:
        badge_text = f"{connected_count}/{len(enabled)} Terhubung"
        badge_class = "badge-warn"

    compact_items = []
    for p in enabled:
        pname = p["platform"].capitalize()
        if p["is_error"]:
            compact_items.append(f'{pname}: <span class="down">⚠ Error</span>')
        elif p["status_key"] == "connected":
            compact_items.append(f'{pname}: <span class="up">Terhubung</span>')
        elif p["status_key"] == "connecting":
            compact_items.append(f'{pname}: <span class="warn">Konek…</span>')
        else:
            compact_items.append(f'{pname}: <span class="down">{html.escape(p["status_label"])}</span>')

    compact_bento = " · ".join(compact_items)
    return badge_text, badge_class, compact_bento


def render_gateway_platforms_html() -> str:
    """Render list of gateway platforms for Tab Layanan."""
    platforms = get_gateway_platforms()
    if not platforms:
        return (
            '<div class="update-hint" style="margin:0;font-size:0.75rem">'
            'Belum ada gateway yang dikonfigurasi pada config.yaml.'
            '</div>'
        )

    rows = []
    for p in platforms:
        name = html.escape(p["display_name"])
        meta = html.escape(p["metadata"])
        b_cls = p["badge_class"]
        b_label = html.escape(p["status_label"])
        icon_svg = _platform_icon(p["platform"])
        box_cls = _platform_icon_box_class(p["platform"])

        cfg_badge = (
            '<span class="badge" style="display:inline-flex;align-items:center;background:rgba(59,130,246,0.12);color:var(--accent-light);'
            'border:1px solid rgba(59,130,246,0.25);font-size:.62rem;padding:.08rem .35rem;border-radius:4px;white-space:nowrap;line-height:1.2">Config Aktif</span>'
            if p["enabled"] else
            '<span class="badge badge-muted" style="display:inline-flex;align-items:center;font-size:.62rem;padding:.08rem .35rem;border-radius:4px;white-space:nowrap;line-height:1.2">Config Nonaktif</span>'
        )

        err_div = ""
        if p["is_error"] and p["error_detail"]:
            err_msg = html.escape(redact_sensitive_tokens(p["error_detail"]))
            err_div = (
                f'<div style="color:var(--danger);font-size:.7rem;margin-top:.25rem;'
                f'display:flex;align-items:center;gap:.3rem">'
                f'<span>⚠ {err_msg}</span></div>'
            )

        safe_p = html.escape(p["platform"].replace(chr(92), chr(92)*2).replace(chr(39), chr(92)+chr(39)), quote=True)
        safe_name = html.escape(p["display_name"].replace(chr(92), chr(92)*2).replace(chr(39), chr(92)+chr(39)), quote=True)
        pair_btn = (
            f'<button type="button" class="btn-action-sm btn-action-primary" '
            f'style="background:rgba(16,185,129,0.15);color:#10b981;border-color:rgba(16,185,129,0.3)" '
            f'onclick="openWaPairModal()">Pairing QR</button>'
            if p["platform"] == "whatsapp" else ""
        )
        toggle_btn = (
            f'<button type="button" class="btn-action-sm" onclick="toggleGwPlatform(\'{safe_p}\', false)">Matikan</button>'
            if p["enabled"] else
            f'<button type="button" class="btn-action-sm btn-action-primary" onclick="toggleGwPlatform(\'{safe_p}\', true)">Nyalakan</button>'
        )
        del_btn = f'<button type="button" class="btn-action-sm btn-action-danger" onclick="deleteGwPlatform(\'{safe_p}\', \'{safe_name}\')">Hapus</button>'
        edit_btn = f'<button type="button" class="btn-action-sm" onclick="openGwConfig(\'{safe_p}\', \'{safe_name}\')">⚙ Setting</button>'

        rows.append(
            f'<div class="gw-card-row">'
            f'  <div class="gw-card-top">'
            f'    <div class="gw-card-left">'
            f'      <div class="cc-icon-box {box_cls}" style="width:34px;height:34px;min-width:34px;border-radius:10px">'
            f'        {icon_svg}'
            f'      </div>'
            f'      <div class="gw-card-info">'
            f'        <div class="gw-card-title-wrap">'
            f'          <span class="gw-card-title">{name}</span>'
            f'          {cfg_badge}'
            f'        </div>'
            f'        <div class="gw-card-meta">{meta}</div>'
            f'        {err_div}'
            f'      </div>'
            f'    </div>'
            f'    <div class="gw-card-badge-wrap">'
            f'      <span class="badge {b_cls}" style="display:inline-flex;align-items:center;white-space:nowrap">{b_label}</span>'
            f'    </div>'
            f'  </div>'
            f'  <div class="gw-card-bottom">'
            f'    {toggle_btn}'
            f'    {pair_btn}'
            f'    {edit_btn}'
            f'    {del_btn}'
            f'  </div>'
            f'</div>'
        )

    return "".join(rows)


GATEWAY_LOG_PATHS = [
    "/root/.hermes/logs/gateway.log",
    "/opt/AppData/hermes-native/hermes-data/logs/gateway.log",
]
WHATSAPP_LOG_PATHS = [
    "/root/.hermes/whatsapp/bridge.log",
    "/opt/AppData/hermes-native/hermes-data/whatsapp/bridge.log",
]


def tail_whatsapp_bridge_log(n: int = 60) -> str:
    for p in WHATSAPP_LOG_PATHS:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            try:
                with open(p, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - 40000))
                    raw = f.read().decode("utf-8", errors="replace")
                    lines = raw.splitlines()
                    return "\n".join(lines[-n:]) if lines else ""
            except Exception:
                pass
    return ""


def qr_to_svg(qr_text: str) -> str:
    """Generate compact SVG from QR text using node and qrcode-terminal vendor library."""
    if not qr_text:
        return ""
    node_code = """
    try {
      const QRCode = require('/opt/AppData/hermes-native/hermes-lib/scripts/whatsapp-bridge/node_modules/qrcode-terminal/vendor/QRCode');
      const QRErrorCorrectLevel = require('/opt/AppData/hermes-native/hermes-lib/scripts/whatsapp-bridge/node_modules/qrcode-terminal/vendor/QRCode/QRErrorCorrectLevel');
      const qr = new QRCode(-1, QRErrorCorrectLevel.L);
      qr.addData(process.argv[1]);
      qr.make();
      const count = qr.getModuleCount();
      let d = '';
      for (let r = 0; r < count; r++) {
        for (let c = 0; c < count; c++) {
          if (qr.isDark(r, c)) {
            d += `M${c + 4},${r + 4}h1v1h-1z`;
          }
        }
      }
      const size = count + 8;
      process.stdout.write(`<svg viewBox="0 0 ${size} ${size}" xmlns="http://www.w3.org/2000/svg" shape-rendering="crispEdges" style="width:230px;height:230px;max-width:85vw;max-height:85vw;background:#ffffff;padding:10px;border-radius:12px;box-shadow:0 4px 18px rgba(0,0,0,0.35);display:block;margin:0 auto;"><rect width="100%" height="100%" fill="#ffffff"/><path d="${d}" fill="#111827"/></svg>`);
    } catch (e) {
      process.stderr.write(String(e));
    }
    """
    try:
        r = subprocess.run(["node", "-e", node_code, qr_text], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.startswith("<svg"):
            return r.stdout
    except Exception:
        pass
    return ""


_wa_pair_lock = threading.Lock()
_wa_pair_proc = None
_wa_pair_state = {
    "status": "idle",
    "qr_raw": "",
    "qr_svg": "",
    "user": None,
    "error": None,
    "logs": [],
    "started_at": 0,
    "expires_at": 0,
}


def _append_wa_pair_log(msg: str):
    with _wa_pair_lock:
        ts = datetime.now().strftime("%H:%M:%S")
        _wa_pair_state["logs"].append(f"[{ts}] {redact_sensitive_tokens(msg)}")
        if len(_wa_pair_state["logs"]) > 80:
            _wa_pair_state["logs"] = _wa_pair_state["logs"][-80:]


def get_wa_pair_status() -> dict:
    with _wa_pair_lock:
        global _wa_pair_proc
        if _wa_pair_proc is not None and _wa_pair_proc.poll() is not None:
            code = _wa_pair_proc.poll()
            if _wa_pair_state["status"] not in ("connected", "error", "cancelled"):
                creds_p = Path("/root/.hermes/whatsapp/session/creds.json")
                if creds_p.exists():
                    try:
                        with open(creds_p, "r", encoding="utf-8") as f:
                            cdata = json.load(f)
                            if cdata.get("registered") or cdata.get("me"):
                                _wa_pair_state["status"] = "connected"
                                _wa_pair_state["user"] = cdata.get("me")
                    except Exception:
                        pass
                if _wa_pair_state["status"] != "connected":
                    _wa_pair_state["status"] = "idle" if code == 0 else "error"
                    if code != 0 and not _wa_pair_state.get("error"):
                        _wa_pair_state["error"] = f"Proses bridge keluar dengan kode {code}"
            _wa_pair_proc = None
        elif _wa_pair_proc is None and _wa_pair_state["status"] == "idle":
            creds_p = Path("/root/.hermes/whatsapp/session/creds.json")
            if creds_p.exists():
                try:
                    with open(creds_p, "r", encoding="utf-8") as f:
                        cdata = json.load(f)
                        if cdata.get("registered") or cdata.get("me"):
                            _wa_pair_state["status"] = "connected"
                            _wa_pair_state["user"] = cdata.get("me")
                except Exception:
                    pass
        return dict(_wa_pair_state)


def _wa_pair_watcher(proc, session_dir: Path):
    global _wa_pair_proc
    try:
        for line in proc.stdout or ():
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                _append_wa_pair_log(raw)
                continue

            evt = payload.get("event")
            if evt == "started":
                _append_wa_pair_log(f"Bridge aktif. Session: {session_dir.name}")
            elif evt == "qr":
                qr_str = str(payload.get("qr") or "").strip()
                svg = qr_to_svg(qr_str)
                with _wa_pair_lock:
                    _wa_pair_state["status"] = "waiting_scan"
                    _wa_pair_state["qr_raw"] = qr_str
                    _wa_pair_state["qr_svg"] = svg
                    _wa_pair_state["expires_at"] = time.time() + 60
                _append_wa_pair_log("QR Code dibuat. Silakan scan dengan WhatsApp di HP.")
            elif evt == "connected":
                user = payload.get("user") or {}
                with _wa_pair_lock:
                    _wa_pair_state["status"] = "connected"
                    _wa_pair_state["user"] = user
                    _wa_pair_state["qr_svg"] = ""
                acc_name = user.get("name") or user.get("id") or "WhatsApp User"
                _append_wa_pair_log(f"WhatsApp berhasil terhubung! Akun: {acc_name}")
            elif evt == "error":
                err = str(payload.get("error") or "Unknown error")
                with _wa_pair_lock:
                    _wa_pair_state["status"] = "error"
                    _wa_pair_state["error"] = err
                _append_wa_pair_log(f"Error: {err}")
            elif evt == "disconnected":
                reason = payload.get("reason")
                _append_wa_pair_log(f"Koneksi terputus (reason: {reason}).")
    except Exception as ex:
        _append_wa_pair_log(f"Exception watcher: {ex}")
    finally:
        with _wa_pair_lock:
            if _wa_pair_proc is proc:
                _wa_pair_proc = None
        if proc.poll() is None:
            _reap_proc_async(proc)


def _reap_proc_async(proc) -> None:
    """Terminate and reap a subprocess in a daemon thread to avoid blocking HTTP request threads."""
    if proc is None:
        return
    def _reaper():
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:
                pass
    threading.Thread(target=_reaper, daemon=True).start()


def start_wa_pair(clear_session: bool = True) -> tuple[bool, str]:
    global _wa_pair_proc
    with _wa_pair_lock:
        if _wa_pair_proc is not None and _wa_pair_proc.poll() is None:
            if _wa_pair_state.get("status") == "waiting_scan" and not clear_session:
                return True, "Pairing sudah berjalan."
            _reap_proc_async(_wa_pair_proc)
            _wa_pair_proc = None

        session_dir = Path("/root/.hermes/whatsapp/session")
        session_dir.mkdir(parents=True, exist_ok=True)

        creds_file = session_dir / "creds.json"
        is_registered = False
        if creds_file.exists():
            try:
                with open(creds_file, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                    is_registered = bool(cdata.get("registered"))
            except Exception:
                is_registered = False

        if clear_session or not is_registered:
            import shutil
            for item in session_dir.glob("*"):
                try:
                    if item.is_file():
                        item.unlink()
                    elif item.is_dir():
                        shutil.rmtree(item)
                except Exception:
                    pass

        bridge_script = Path("/opt/AppData/hermes-native/hermes-lib/scripts/whatsapp-bridge/bridge.js")
        if not bridge_script.exists():
            return False, f"Script bridge.js tidak ditemukan di {bridge_script}"

        env = os.environ.copy()
        env["WHATSAPP_MODE"] = "bot"
        existing_dm = _read_hermes_env().get("WHATSAPP_DM_POLICY") or "pairing"
        env["WHATSAPP_DM_POLICY"] = existing_dm

        cmd = [
            "node",
            str(bridge_script),
            "--pair-only",
            "--pair-json",
            "--session",
            str(session_dir)
        ]
        try:
            _wa_pair_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                cwd=str(bridge_script.parent),
            )
        except Exception as ex:
            return False, f"Gagal menjalankan node bridge: {ex}"

        _wa_pair_state["status"] = "starting"
        _wa_pair_state["qr_raw"] = ""
        _wa_pair_state["qr_svg"] = ""
        _wa_pair_state["user"] = None
        _wa_pair_state["error"] = None
        _wa_pair_state["logs"] = []
        _wa_pair_state["started_at"] = time.time()

    _append_wa_pair_log("Memulai proses pairing WhatsApp Baileys...")
    t = threading.Thread(target=_wa_pair_watcher, args=(_wa_pair_proc, session_dir), daemon=True)
    t.start()
    return True, "Pairing dimulai."


def cancel_wa_pair() -> None:
    global _wa_pair_proc
    with _wa_pair_lock:
        if _wa_pair_proc is not None:
            _reap_proc_async(_wa_pair_proc)
            _wa_pair_proc = None
        _wa_pair_state["status"] = "cancelled"
        _wa_pair_state["qr_raw"] = ""
        _wa_pair_state["qr_svg"] = ""
        _wa_pair_state["error"] = None
    _append_wa_pair_log("Proses pairing dibatalkan oleh pengguna.")


def apply_wa_pair(restart_gw: bool = True) -> tuple[bool, str]:
    global _wa_pair_proc
    with _wa_pair_lock:
        if _wa_pair_proc is not None:
            _reap_proc_async(_wa_pair_proc)
            _wa_pair_proc = None
    ok, err = toggle_gateway_platform_config("whatsapp", True)
    if not ok:
        return False, f"Gagal mengaktifkan WhatsApp di config: {err}"
    if restart_gw:
        restart_bot()
    return True, "WhatsApp berhasil diaktifkan dan gateway direstart!"


def redact_sensitive_tokens(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'\b\d{8,12}:[A-Za-z0-9_-]{25,}\b', '[REDACTED_TOKEN]', text)
    text = re.sub(r'\b[A-Za-z0-9_-]{24,32}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}\b', '[REDACTED_DISCORD_TOKEN]', text)
    text = re.sub(r'\bxox[baprs]-[A-Za-z0-9-]+\b', '[REDACTED_SLACK_TOKEN]', text)
    text = re.sub(r'(bearer\s+)[A-Za-z0-9_.-]{16,}', r'\1[REDACTED]', text, flags=re.IGNORECASE)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{20,}\b', '[REDACTED_KEY]', text)
    text = re.sub(r'\bAIza[0-9A-Za-z_-]{30,40}\b', '[REDACTED_KEY]', text)
    text = re.sub(r'\bgh[pousr]_[A-Za-z0-9_]{36,}\b', '[REDACTED_TOKEN]', text)
    return text


def tail_gateway_log(n: int = 60) -> str:
    for p in GATEWAY_LOG_PATHS:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            try:
                with open(p, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - 40000))
                    raw = f.read().decode("utf-8", errors="replace")
                    lines = raw.splitlines()
                    return "\n".join(lines[-n:]) if lines else ""
            except Exception:
                pass
    try:
        r = subprocess.run(
            ["journalctl", "_SYSTEMD_USER_UNIT=hermes-gateway.service", f"-n{n}", "--no-pager"],
            capture_output=True, text=True, timeout=2
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def render_gateway_log_card(n: int = 60) -> str:
    log_text = tail_gateway_log(n=n)
    log_text = redact_sensitive_tokens(log_text)
    gw_active = service_active("hermes-gateway", user=True)
    badge = f'<span class="up">{ICON_CHECK}Aktif (Live)</span>' if gw_active else f'<span class="down">{ICON_ALERT_TRIANGLE}Mati</span>'
    body = html.escape(log_text) if log_text.strip() else "(belum ada catatan log aktivitas gateway)"

    wa_log = tail_whatsapp_bridge_log(n=n)
    wa_log = redact_sensitive_tokens(wa_log)
    wa_body = html.escape(wa_log) if wa_log.strip() else "(belum ada catatan log aktivitas bridge whatsapp)"

    return (
        f'<div id="gateway-log-card" style="margin-top:0.85rem;border-top:1px solid var(--border);padding-top:0.75rem">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:0.45rem">'
        f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
        f'<div style="font-size:0.8rem;font-weight:600;color:var(--text);display:flex;align-items:center;gap:6px">{ICON_TERMINAL} Log Gateway Hermes {badge}</div>'
        f'<div style="display:inline-flex;gap:3px;background:rgba(255,255,255,0.05);padding:2px;border-radius:6px;border:1px solid var(--border)">'
        f'<button type="button" class="btn-action-sm active" id="tab-log-gw" onclick="switchGwLogTab(\'gw\')" style="padding:0.15rem 0.5rem;font-size:0.7rem;margin:0">Gateway Core</button>'
        f'<button type="button" class="btn-action-sm" id="tab-log-wa" onclick="switchGwLogTab(\'wa\')" style="padding:0.15rem 0.5rem;font-size:0.7rem;margin:0">WhatsApp Bridge</button>'
        f'</div>'
        f'</div>'
        f'<div style="display:flex;gap:4px;align-items:center">'
        f'<button type="button" class="btn" style="width:auto;padding:0.2rem 0.6rem;font-size:0.72rem;margin:0" '
        f"onclick=\"var bg=document.getElementById('gateway-logbox'), bw=document.getElementById('whatsapp-logbox'); if(bg&&bg.style.display!=='none')bg.scrollTop=bg.scrollHeight; if(bw&&bw.style.display!=='none')bw.scrollTop=bw.scrollHeight;\">"
        f'Ke Log Terbaru</button>'
        f'<button type="button" class="btn" style="width:auto;padding:0.2rem 0.6rem;font-size:0.72rem;margin:0" '
        f"onclick=\"safeStore('setItem','gatewayLogDismissed','1');var c=document.getElementById('gateway-log-card');if(c)c.remove();if(window.syncLogUI)syncLogUI()\">"
        f'Sembunyikan Log</button>'
        f'</div>'
        f'</div>'
        f'<div class="logbox" id="gateway-logbox" style="max-height:260px">{body}</div>'
        f'<div class="logbox" id="whatsapp-logbox" style="max-height:260px;display:none">{wa_body}</div>'
        f'</div>'
    )


# Hermes' gateway loader (gateway/config_loader.py::platform_section) reads a platform from both
# ``platforms.<name>`` and a root-level ``<name>:`` block; the root block wins for every adapter key.
# The panel therefore edits the merged view and folds it into ``platforms.<name>`` on write, so what
# the editor shows is exactly what the gateway loads and no root-only setting is dropped.
# Hermes only reads these from ``platforms.<name>``, so a root copy never overrides them.
_NESTED_ONLY_PLATFORM_KEYS = frozenset({"token", "api_key", "home_channel", "reply_to_mode"})

# gateway/run.py::_OWN_POLICY_OPEN_ENV — platforms whose 'open' policy aborts gateway startup unless
# an allow-all flag is set. (dm policy env, group policy env, allow-all env)
_OPEN_POLICY_GUARD = {
    "whatsapp": ("WHATSAPP_DM_POLICY", "WHATSAPP_GROUP_POLICY", "WHATSAPP_ALLOW_ALL_USERS"),
    "wecom": ("WECOM_DM_POLICY", "WECOM_GROUP_POLICY", "WECOM_ALLOW_ALL_USERS"),
    "weixin": ("WEIXIN_DM_POLICY", "WEIXIN_GROUP_POLICY", "WEIXIN_ALLOW_ALL_USERS"),
    "yuanbao": ("YUANBAO_DM_POLICY", "YUANBAO_GROUP_POLICY", "YUANBAO_ALLOW_ALL_USERS"),
    "qqbot": (None, None, "QQ_ALLOW_ALL_USERS"),
}
_TRUTHY = {"true", "1", "yes", "on"}


def _effective_platform_block(cfg: dict, platform: str) -> dict:
    """``platforms.<platform>`` overlaid with the root ``<platform>:`` block, as Hermes merges them."""
    cfg_platforms = cfg.get("platforms") if isinstance(cfg.get("platforms"), dict) else {}
    nested = cfg_platforms.get(platform) if isinstance(cfg_platforms.get(platform), dict) else {}
    root = cfg.get(platform) if isinstance(cfg.get(platform), dict) else {}
    merged = copy.deepcopy(nested)
    for key, value in root.items():
        if key in _NESTED_ONLY_PLATFORM_KEYS and key in merged:
            continue
        if key == "extra" and isinstance(value, dict) and isinstance(merged.get("extra"), dict):
            merged["extra"] = {**merged["extra"], **copy.deepcopy(value)}
        else:
            merged[key] = copy.deepcopy(value)
    if platform == "whatsapp":
        _overlay_whatsapp_env(merged)
    return merged


LEGACY_GATEWAY_ROOT_KEYS = {
    "telegram",
    "discord",
    "slack",
    "whatsapp",
    "webhook",
    "mattermost",
    "matrix",
    "signal",
    "feishu",
    "teams",
    "google_chat",
    "dingtalk",
    "wecom",
    "line",
    "ntfy",
    "email",
    "homeassistant",
    "simplex",
    "sms",
    "irc",
    "bluebubbles",
    "weixin",
    "yuanbao",
    "qqbot",
    "whatsapp_cloud",
}


def _set_platform_block(cfg: dict, platform: str, block: dict) -> None:
    """Make ``platforms.<platform>`` the single source of truth for this platform."""
    if not isinstance(cfg.get("platforms"), dict):
        cfg["platforms"] = {}
    cfg["platforms"][platform] = block
    if platform in LEGACY_GATEWAY_ROOT_KEYS and isinstance(cfg.get(platform), dict):
        del cfg[platform]


def _merge_platform_patch(base: dict, patch: dict, platform: str = "") -> dict:
    """Apply a Form UI patch: ``null`` removes a key, ``extra`` is merged one level deep."""
    merged = copy.deepcopy(base)
    wa_keys = {k for k, _, _ in _WHATSAPP_ENV_KEYS} if platform == "whatsapp" else set()
    for key, value in patch.items():
        if value is None:
            if key in wa_keys:
                merged[key] = None
            else:
                merged.pop(key, None)
        elif key == "extra" and isinstance(value, dict) and isinstance(merged.get("extra"), dict):
            for extra_key, extra_value in value.items():
                if extra_value is None:
                    merged["extra"].pop(extra_key, None)
                else:
                    merged["extra"][extra_key] = extra_value
        else:
            merged[key] = value
    return merged


def _read_hermes_env() -> dict:
    env = {}
    try:
        lines = (Path(CONFIG_PATH).parent / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return env
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _open_policy_violation(cfg: dict, platform: str, block: dict) -> str:
    """Mirror gateway/run.py::_own_policy_open_startup_violation for one platform block."""
    guard = _OPEN_POLICY_GUARD.get(platform)
    if not guard or not _to_bool(block.get("enabled")):
        return ""
    dm_env, group_env, allow_all_env = guard
    env = _read_hermes_env()
    extra = block.get("extra") if isinstance(block.get("extra"), dict) else {}
    dm_policy = str(block.get("dm_policy") or extra.get("dm_policy") or (env.get(dm_env) if dm_env else "") or "pairing").strip().lower()
    group_policy = str(block.get("group_policy") or extra.get("group_policy") or (env.get(group_env) if group_env else "") or "pairing").strip().lower()
    if dm_policy != "open" and group_policy != "open":
        return ""
    gateway_section = cfg.get("gateway") if isinstance(cfg.get("gateway"), dict) else {}
    opt_ins = (
        env.get("GATEWAY_ALLOW_ALL_USERS"),
        env.get(allow_all_env),
        block.get("allow_all_users"),
        extra.get("allow_all_users"),
        gateway_section.get("allow_all_users"),
        cfg.get("allow_all_users"),
    )
    if any(str(v).strip().lower() in _TRUTHY for v in opt_ins if v is not None):
        return ""
    return (
        f"Kebijakan 'open' pada {platform} ditolak: Hermes Gateway akan menolak start "
        f"(\"open policy without allow-all opt-in\"). Pakai 'allowlist' + allow_from, atau set "
        f"{allow_all_env}=true di .env jika memang semua orang boleh chat."
    )


def _read_config_for_write() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError("Format config.yaml tidak valid (bukan dictionary root).")
    return cfg


def _write_config_atomic(cfg: dict | str) -> None:
    """Atomic config.yaml write that keeps the file mode (Hermes keeps it 0600: it holds API keys)."""
    with _config_write_lock:
        try:
            mode = os.stat(CONFIG_PATH).st_mode & 0o777
        except OSError:
            mode = 0o600
        dirname = os.path.dirname(os.path.abspath(CONFIG_PATH))
        fd, tmp_path = tempfile.mkstemp(dir=dirname, prefix=".config.yaml.tmp.")
        try:
            os.chmod(tmp_path, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                if isinstance(cfg, str):
                    f.write(cfg)
                else:
                    yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
            os.replace(tmp_path, CONFIG_PATH)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        invalidate_config_cache()
        _invalidate_status_cache("gateway_platforms")


def get_gateway_platform_config(plat: str) -> dict:
    """Retrieve the effective YAML config (platforms.<plat> + root <plat>: block) for a platform."""
    cfg = get_parsed_config()
    cfg_platforms = cfg.get("platforms")
    if not isinstance(cfg_platforms, dict):
        cfg_platforms = {}

    known_templates = {
        "telegram": {"enabled": True, "reactions": True, "reply_to_mode": "first", "typing_indicator": True, "gateway_restart_notification": True, "home_channel": {"name": "vitooo", "chat_id": "1992783463", "platform": "telegram"}},
        "discord": {"enabled": True, "token": "", "require_mention": True, "slash_commands": True, "reply_to_mode": "first"},
        "webhook": {"enabled": True, "port": 8644, "host": "127.0.0.1", "path": "/webhook"},
        "whatsapp": {"enabled": True, "bridge_port": 3000, "dm_policy": "pairing", "group_policy": "pairing", "send_read_receipts": False},
        "slack": {"enabled": True, "token": "", "app_token": "", "require_mention": True, "reactions": True},
        "matrix": {"enabled": True, "homeserver": "https://matrix.org", "user_id": "@bot:matrix.org", "access_token": "", "require_mention": True},
        "mattermost": {"enabled": True, "url": "https://mattermost.example.com", "token": "", "require_mention": True, "reply_mode": "off"},
        "signal": {"enabled": True, "phone_number": "", "http_host": "127.0.0.1", "http_port": 8080},
        "teams": {"enabled": True, "app_id": "", "app_password": "", "port": 3978, "require_mention": False},
        "feishu": {"enabled": True, "app_id": "", "app_secret": "", "domain": "feishu", "require_mention": True},
        "google_chat": {"enabled": True, "service_account_json": "credentials.json"},
        "dingtalk": {"enabled": True, "client_id": "", "client_secret": "", "require_mention": True},
        "wecom": {"enabled": True, "corp_id": "", "corp_secret": ""},
        "line": {"enabled": True, "channel_secret": "", "channel_access_token": "", "port": 8646},
        "ntfy": {"enabled": True, "topic": "hermes-alerts", "server": "https://ntfy.sh", "markdown": False},
        "email": {"enabled": True, "address": "bot@example.com", "password": "", "smtp_host": "smtp.gmail.com", "smtp_port": 587, "imap_host": "imap.gmail.com", "imap_port": 993},
        "homeassistant": {"enabled": True, "url": "http://homeassistant.local:8123", "token": "", "cooldown_seconds": 3},
        "simplex": {"enabled": True, "ws_url": "ws://127.0.0.1:5225", "auto_accept": True},
        "sms": {"enabled": True, "account_sid": "", "auth_token": "", "phone_number": ""},
        "irc": {"enabled": True, "server": "irc.libera.chat", "port": 6697, "nickname": "hermes_bot", "channel": "#hermes", "use_tls": True},
        "bluebubbles": {"enabled": True, "server_url": "http://127.0.0.1:1234", "password": ""},
    }

    if plat and (isinstance(cfg_platforms.get(plat), dict) or isinstance(cfg.get(plat), dict)):
        plat_data = _effective_platform_block(cfg, plat)
        yaml_text = yaml.safe_dump(plat_data, default_flow_style=False, sort_keys=False, allow_unicode=True)
        return {
            "ok": True,
            "platform": plat,
            "enabled": _to_bool(plat_data.get("enabled", False)),
            "yaml": yaml_text,
            "is_new": False,
        }
    elif plat in known_templates:
        yaml_text = yaml.safe_dump(known_templates[plat], default_flow_style=False, sort_keys=False, allow_unicode=True)
        return {
            "ok": True,
            "platform": plat,
            "enabled": True,
            "yaml": yaml_text,
            "is_new": True,
        }
    else:
        yaml_text = "enabled: true\n"
        return {
            "ok": True,
            "platform": plat or "",
            "enabled": True,
            "yaml": yaml_text,
            "is_new": True,
        }


def _sync_env_platform_flag(platform: str, enabled: bool, extra_vars: dict = None,
                            remove_vars: list = None) -> None:
    """Set <PLATFORM>_ENABLED (+ extra_vars) and drop remove_vars in Hermes' .env.

    Raises on I/O errors: callers write .env before config.yaml, so a failure must abort the
    save rather than drop keys from config that never reached .env.
    """
    with _env_write_lock:
        env_file = Path(CONFIG_PATH).parent / ".env"
        if not env_file.exists():
            if platform != "whatsapp":
                return
            env_file.touch(mode=0o600)

        def _strip_export(s: str) -> str:
            st = s.strip()
            if st.startswith("export "):
                return st[7:].lstrip()
            return st

        drop_prefixes = tuple(f"{name}=" for name in (remove_vars or ()))
        lines = [line for line in env_file.read_text(encoding="utf-8").splitlines()
                 if not (drop_prefixes and _strip_export(line).startswith(drop_prefixes))]

        prefix = f"{platform.upper()}_ENABLED="
        found = False
        new_lines = []
        for line in lines:
            stripped = _strip_export(line)
            if stripped.startswith(prefix):
                exp = "export " if line.strip().startswith("export ") else ""
                new_lines.append(f"{exp}{prefix}{'true' if enabled else 'false'}")
                found = True
            else:
                new_lines.append(line)
        if not found and platform == "whatsapp":
            new_lines.append(f"{prefix}{'true' if enabled else 'false'}")

        for k, v in (extra_vars or {}).items():
            k_clean = str(k).replace("\r", "").replace("\n", "").strip()
            v_clean = str(v).replace("\r", "").replace("\n", "").strip()
            k_prefix = f"{k_clean}="
            for idx, line in enumerate(new_lines):
                if _strip_export(line).startswith(k_prefix):
                    exp = "export " if line.strip().startswith("export ") else ""
                    new_lines[idx] = f"{exp}{k_clean}={v_clean}"
                    break
            else:
                new_lines.append(f"{k_clean}={v_clean}")

        try:
            mode = os.stat(env_file).st_mode & 0o777
        except OSError:
            mode = 0o600
        dirname = os.path.dirname(os.path.abspath(env_file))
        fd, tmp_path = tempfile.mkstemp(dir=dirname, prefix=".env.tmp.")
        try:
            os.chmod(tmp_path, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(new_lines) + "\n")
            os.replace(tmp_path, env_file)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


# Hermes' dashboard Channels page stores these WhatsApp settings in .env, while the adapter prefers a
# config.yaml value when one exists (``mode`` is read from .env only). Keeping them in config would
# silently shadow every Channels edit, so the panel shows and writes them in .env only.
# (config key, .env var, is_list)
_WHATSAPP_ENV_KEYS = (
    ("allow_from", "WHATSAPP_ALLOWED_USERS", True),
    ("dm_policy", "WHATSAPP_DM_POLICY", False),
    ("mode", "WHATSAPP_MODE", False),
)


def _overlay_whatsapp_env(block: dict) -> None:
    """Show what the WhatsApp adapter uses: config value if present, else the .env value."""
    env = _read_hermes_env()
    for key, env_name, is_list in _WHATSAPP_ENV_KEYS:
        value = env.get(env_name, "").strip()
        if not value or (key in block and key != "mode"):
            continue
        block[key] = [x.strip() for x in value.split(",") if x.strip()] if is_list else value


def _sync_whatsapp_env(block: dict) -> None:
    """Move the Channels-managed keys out of ``block`` into .env (unset/empty ones are removed)."""
    existing_env = _read_hermes_env()
    set_vars, remove_vars = {}, []
    for key, env_name, is_list in _WHATSAPP_ENV_KEYS:
        if key not in block:
            if env_name in existing_env:
                remove_vars.append(env_name)
            continue
        value = block.pop(key, None)
        if is_list and isinstance(value, list):
            value = ",".join(str(x).strip() for x in value if str(x).strip())
        if value is None or str(value).strip() == "":
            remove_vars.append(env_name)
        else:
            set_vars[env_name] = str(value).strip()
    _sync_env_platform_flag("whatsapp", bool(block.get("enabled", True)), set_vars, remove_vars)


def _parse_platform_yaml(yaml_str: str | None, label: str = "YAML") -> tuple[dict | None, str]:
    try:
        parsed = yaml.safe_load(yaml_str) if (yaml_str or "").strip() else {}
    except Exception as e:
        return None, f"Sintaks {label} tidak valid: {e}"
    if parsed is None:
        return {}, ""
    if not isinstance(parsed, dict):
        return None, f"Format {label} harus berupa mapping/dictionary (key: value)."
    return parsed, ""


def _resolve_platform_block(cfg: dict, platform: str, yaml_str: str, merge: bool,
                            base_yaml: str | None) -> tuple[dict | None, str]:
    """Raw YAML mode replaces the block; Form UI mode (``merge``) patches the editor's base YAML
    (or, without one, the effective block on disk)."""
    parsed, err = _parse_platform_yaml(yaml_str)
    if parsed is None:
        return None, err
    if not merge:
        return parsed, ""
    if base_yaml is None:
        base = _effective_platform_block(cfg, platform)
    else:
        base, err = _parse_platform_yaml(base_yaml, "YAML dasar")
        if base is None:
            return None, err
    return _merge_platform_patch(base, parsed, platform=platform), ""


def preview_gateway_platform_config(platform: str, yaml_str: str, base_yaml: str | None = None) -> tuple[bool, str]:
    """Merged YAML for a Form UI patch without writing (Form → Raw YAML view switch)."""
    platform = platform.strip().lower()
    try:
        cfg = _read_config_for_write()
    except Exception as e:
        return False, f"Gagal membaca config.yaml: {e}"
    block, err = _resolve_platform_block(cfg, platform, yaml_str, True, base_yaml)
    if block is None:
        return False, err
    return True, yaml.safe_dump(block, default_flow_style=False, sort_keys=False, allow_unicode=True)


def save_gateway_platform_config(platform: str, yaml_str: str, enabled_override: bool | None = None,
                                 merge: bool = False, base_yaml: str | None = None) -> tuple[bool, str]:
    """Validate and atomically write platforms.<platform> (folding any root <platform>: block)."""
    platform = platform.strip().lower()
    if not platform or platform == "platforms":
        return False, "Nama platform tidak boleh kosong."

    if not re.match(r"^[a-z0-9_-]+$", platform):
        return False, "Nama platform hanya boleh berisi huruf kecil, angka, garis bawah (_), dan tanda hubung (-)."

    try:
        cfg = _read_config_for_write()
    except Exception as e:
        return False, f"Gagal membaca config.yaml: {e}"

    block, err = _resolve_platform_block(cfg, platform, yaml_str, merge, base_yaml)
    if block is None:
        return False, err

    if enabled_override is not None:
        block["enabled"] = _to_bool(enabled_override)
    elif "enabled" not in block:
        block["enabled"] = True

    violation = _open_policy_violation(cfg, platform, block)
    if violation:
        return False, violation

    try:
        if platform == "whatsapp":
            _sync_whatsapp_env(block)  # .env first: a failure here leaves config.yaml untouched
        _set_platform_block(cfg, platform, block)
        _write_config_atomic(cfg)
        return True, ""
    except Exception as e:
        return False, f"Gagal menyimpan ke config.yaml: {e}"


def toggle_gateway_platform_config(platform: str, enabled: bool) -> tuple[bool, str]:
    """Atomically toggle enabled state of a platform in config.yaml."""
    platform = platform.strip().lower()
    if not platform or platform == "platforms":
        return False, "Nama platform tidak valid."
    try:
        cfg = _read_config_for_write()
        block = _effective_platform_block(cfg, platform)
        block["enabled"] = enabled
        violation = _open_policy_violation(cfg, platform, block)
        if violation:
            return False, violation
        if platform == "whatsapp":
            _sync_whatsapp_env(block)  # .env first: a failure here leaves config.yaml untouched
        _set_platform_block(cfg, platform, block)
        _write_config_atomic(cfg)
        return True, ""
    except Exception as e:
        return False, f"Gagal mengubah status: {e}"


def remove_gateway_platform_config(platform: str) -> tuple[bool, str]:
    """Atomically remove a platform (platforms.<name> and any root <name>: block) from config.yaml."""
    platform = platform.strip().lower()
    if not platform or platform == "platforms" or not re.match(r"^[a-z0-9_-]+$", platform):
        return False, "Nama platform tidak valid."
    try:
        cfg = _read_config_for_write()
        platforms = cfg.get("platforms") if isinstance(cfg.get("platforms"), dict) else {}
        found = False
        if platform in platforms:
            del platforms[platform]
            found = True
        if platform in LEGACY_GATEWAY_ROOT_KEYS and isinstance(cfg.get(platform), dict):
            del cfg[platform]
            found = True
        if not found:
            return False, f"Platform '{platform}' tidak ditemukan di config.yaml."
        if platform == "whatsapp":
            _sync_env_platform_flag("whatsapp", False, remove_vars=["WHATSAPP_ENABLED", "WHATSAPP_MODE", "WHATSAPP_DM_POLICY", "WHATSAPP_ALLOWED_USERS"])
        _write_config_atomic(cfg)
        return True, ""
    except Exception as e:
        return False, f"Gagal menghapus platform: {e}"


def get_9router_host() -> str:
    """Auto-detect 9router host IP. Explicit ROUTER_HOST first, then local Docker, then discovery."""
    global _9router_host_cache, _9router_host_at
    if ROUTER_HOST_OVERRIDE:
        with _9router_host_lock:
            _9router_host_cache = ROUTER_HOST_OVERRIDE
            _9router_host_at = time.time()
        return ROUTER_HOST_OVERRIDE

    with _9router_host_lock:
        if _9router_host_cache and (time.time() - _9router_host_at) < 300:  # 5 min cache
            return _9router_host_cache

    # 1. Check local Docker / Compose first: if compose file or container exists locally, it's local!
    if router_compose_file():
        with _9router_host_lock:
            _9router_host_cache = "127.0.0.1"
            _9router_host_at = time.time()
        return "127.0.0.1"

    try:
        r = subprocess.run(
            ["docker", "inspect", ROUTER_CONTAINER, "--format", "{{.Id}}"],
            capture_output=True, text=True, timeout=INFO_TIMEOUT,
        )
        if r.returncode == 0 and r.stdout.strip():
            with _9router_host_lock:
                _9router_host_cache = "127.0.0.1"
                _9router_host_at = time.time()
            return "127.0.0.1"
    except Exception:
        pass

    # 2. Discovery: Tailscale peers only (no author-specific hardcoded hosts)
    known_hosts = []
    try:
        r = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            import json as _json
            status = _json.loads(r.stdout)
            for peer in status.get("Peer", {}).values():
                addrs = peer.get("TailscaleIPs", [])
                known_hosts.extend(addrs)
    except Exception:
        pass

    # 3. Probe each host for 9router on port 20128
    for host in known_hosts:
        try:
            r = subprocess.run(
                ["curl", "-s", "--connect-timeout", "2", f"http://{host}:20128/"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                # Any valid response (even error JSON) means 9router is there
                with _9router_host_lock:
                    _9router_host_cache = host
                    _9router_host_at = time.time()
                return host
        except Exception:
            continue

    # 4. Fallback to localhost
    with _9router_host_lock:
        _9router_host_cache = "127.0.0.1"
        _9router_host_at = time.time()
    return "127.0.0.1"


def get_9router_port() -> int:
    """Auto-detect 9router's host port from Docker."""
    try:
        r = subprocess.run(
            ["docker", "inspect", ROUTER_CONTAINER,
             "--format", "{{range $p, $conf := .NetworkSettings.Ports}}{{$p}}->{{(index $conf 0).HostPort}}{{end}}"],
            capture_output=True, text=True, timeout=INFO_TIMEOUT,
        )
        if r.returncode == 0:
            for mapping in r.stdout.strip().split("\n"):
                if "->" in mapping:
                    return int(mapping.split("->")[1])
    except Exception:
        pass
    return 20128  # fallback default


def get_9router_public_url() -> str:
    """Return external/LAN URL for browser opening (never 127.0.0.1)."""
    host = get_9router_host()
    port = get_9router_port()
    if host in ("127.0.0.1", "localhost", "0.0.0.0"):
        _, lan_ip = get_server_ips()
        host = lan_ip or "127.0.0.1"
    return f"http://{host}:{port}"


# Cache for 9router host detection
_9router_host_cache: str = ""
_9router_host_at: float = 0
_9router_host_lock = threading.Lock()

RATE_LIMIT_CARD = """<div class="card card-warn">
  <div class="card-title">{icon} Provider bermasalah (10 menit terakhir)</div>
  {rows}
</div>"""

COUNTDOWN_BLOCK = """<div class="countdown" id="cd">{seconds}</div>
<div class="hint">{message}</div>
<script>
let n = {seconds};
const el = document.getElementById('cd');
const t = setInterval(() => {{
  n -= 1;
  el.textContent = n;
  if (n <= 0) {{
    clearInterval(t);
    window.location.href = '/status';
  }}
}}, 1000);
</script>"""

# Auto-refresh via SSE (Server-Sent Events). The server pushes updates only
# when data actually changes — no wasted polls, instant updates, one persistent
# connection. Falls back to polling if SSE is unavailable.
SSE_SCRIPT = """<script>
(function(){
    var pbar=document.getElementById('pbar'), spin=document.getElementById('spin');
  var es=null, retryTimer=null;
  var cpuHistory = [10, 15, 12, 18, 22, 19, 14, 16, 20, 25, 22, 18, 15, 12, 10, 14, 18, 22, 19, 15, 20, 25, 18, 14, 12, 16, 18, 20, 15, 16];
  var ramHistory = [30, 30, 30, 31, 31, 30, 30, 31, 31, 30, 30, 30, 31, 31, 31, 30, 30, 31, 31, 30, 30, 31, 31, 30, 30, 31, 31, 30, 30, 30];
  function updateSparkline(svgId, val, historyArr){
    if(typeof val !== 'number' || isNaN(val)) return;
    historyArr.push(val);
    if(historyArr.length > 30) historyArr.shift();
    var svg = document.getElementById(svgId);
    if(!svg) return;
    var poly = svg.querySelector('.spark-poly');
    var fill = svg.querySelector('.spark-fill');
    if(!poly || !fill || historyArr.length < 2) return;
    var pts = [];
    var w = 300, h = 80;
    var step = w / (historyArr.length - 1);
    for(var i = 0; i < historyArr.length; i++){
      var x = (i * step).toFixed(1);
      var y = (h - (Math.max(0, Math.min(100, historyArr[i])) / 100 * (h - 16)) - 8).toFixed(1);
      pts.push(x + ',' + y);
    }
    poly.setAttribute('points', pts.join(' '));
    fill.setAttribute('points', '0,' + h + ' ' + pts.join(' ') + ' ' + w + ',' + h);
  }
  // BEGIN sse-render-gate
  // SSE pushes every second. Rebuilding ~30 slots each tick (identical HTML included) kept the
  // main thread busy and made phone scrolling stutter, so identical content is skipped, and
  // updates that arrive mid-scroll are held until scrolling has been idle briefly.
  function set(id,v){
    var el=document.getElementById(id); if(!el||v==null) return;
    // Skip only if it is still exactly our last write (a user action may have replaced it).
    if(el._sseHtml===v && el._sseFirst===el.firstChild) return;
    el.innerHTML=v; el._sseHtml=v; el._sseFirst=el.firstChild;
  }
  var _scrolling=false, _scrollTimer=null, _pendingUpdate=null;
  function onUpdate(d){ if(_scrolling){ _pendingUpdate=d; return; } apply(d); }
  window.addEventListener('scroll', function(e){
    if(e && e.target && e.target !== window && e.target !== document && e.target !== document.documentElement && e.target !== document.body) return;
    _scrolling=true;
    if(_scrollTimer) clearTimeout(_scrollTimer);
    _scrollTimer=setTimeout(function(){
      _scrolling=false; _scrollTimer=null;
      if(_pendingUpdate){ var d=_pendingUpdate; _pendingUpdate=null; try { apply(d); } catch(e){} }
    }, 180);
  }, {passive:true, capture:true});
  // END sse-render-gate
  function pulse(){
    if(!pbar) return;
    pbar.style.transition='none'; pbar.style.width='0%';
    void pbar.offsetWidth;
    pbar.style.transition='width 600ms ease-out'; pbar.style.width='100%';
    setTimeout(function(){ pbar.style.opacity='0'; }, 600);
    setTimeout(function(){ pbar.style.opacity='1'; }, 700);
  }
  function stickySet(id,v){
    var el=document.getElementById(id); if(!el||v==null) return;
    if(el.innerHTML === v) return;
    var force = window._forceBottom === true;
    var curBoxes = el.querySelectorAll('.logbox');
    if(curBoxes.length > 0){
      var t = document.createElement('div');
      t.innerHTML = v;
      var newBoxes = t.querySelectorAll('.logbox');
      if(newBoxes.length === curBoxes.length){
        for(var k=0; k<curBoxes.length; k++){
          var cb = curBoxes[k], nb = newBoxes[k];
          if(cb.innerHTML !== nb.innerHTML){
            var isNearBottom = force || (cb.scrollHeight <= cb.clientHeight) ||
                               (cb.scrollHeight - cb.scrollTop - cb.clientHeight < 80) ||
                               (cb.clientHeight === 0);
            cb.innerHTML = nb.innerHTML;
            if(isNearBottom){
              cb.scrollTop = cb.scrollHeight;
              (function(box){ setTimeout(function(){ box.scrollTop = box.scrollHeight; }, 30); })(cb);
            }
          }
        }
        var curHeader = el.querySelector('[style*="justify-content:space-between"]') || el.querySelector('.card-title');
        var newHeader = t.querySelector('[style*="justify-content:space-between"]') || t.querySelector('.card-title');
        if(curHeader && newHeader && curHeader.innerHTML !== newHeader.innerHTML){
          curHeader.innerHTML = newHeader.innerHTML;
          if(window.syncGwLogTabUI) window.syncGwLogTabUI();
        }
        if(force) window._forceBottom = false;
        return;
      }
    }
    var boxes=el.querySelectorAll('.logbox'), sticky=[];
    for(var i=0;i<boxes.length;i++){
      var b=boxes[i];
      if(force || (b.scrollHeight <= b.clientHeight) || (b.scrollHeight - b.scrollTop - b.clientHeight < 80) || (b.clientHeight === 0)) sticky.push(i);
    }
    el.innerHTML=v;
    var nb=el.querySelectorAll('.logbox');
    for(var j=0;j<nb.length;j++){
      if(sticky.indexOf(j)!==-1){
        nb[j].scrollTop=nb[j].scrollHeight;
        var tb = nb[j];
        setTimeout(function(){ tb.scrollTop = tb.scrollHeight; }, 30);
      }
    }
    if(force) window._forceBottom = false;
    if(window.syncGwLogTabUI) window.syncGwLogTabUI();
  }
  function apply(d){
    if(d.cells){ set('cell-dash',d.cells.dash); set('cell-bot',d.cells.bot);
      set('cell-gw',d.cells.gw); set('cell-model',d.cells.model); set('cell-providers',d.cells.providers); set('cell-router',d.cells.router); set('cell-hermes',d.cells.hermes);
      set('cell-ram',d.cells.ram); set('cell-zram',d.cells.zram); set('cell-temp',d.cells.temp); set('cell-emmc',d.cells.emmc);
      set('cell-disk',d.cells.disk); set('cell-uptime',d.cells.uptime);
      set('cell-lan',d.cells.lan); set('cell-ts',d.cells.ts);
      set('cell-internet',d.cells.internet);
      if(d.cells.gw_platforms) set('cell-gw-platforms', d.cells.gw_platforms); }
    // Static controls stay untouched: replacing them resets scroll/focus.
    // SSE updates only live metrics, process data, and active update logs.
    if(d.processes_table) set('process-table-slot',d.processes_table);
    if(d.gateway_list_block) set('gateway-list-slot', d.gateway_list_block);
    if(d.gw_summary_text) set('gw-summary-badge', d.gw_summary_text);
    if(d.gw_summary_badge_class) {{
      var gwb = document.getElementById('gw-summary-badge');
      if(gwb) gwb.className = 'badge ' + d.gw_summary_badge_class;
    }}
    if(d.cpu_pct !== undefined) {{
      updateSparkline('cpu-sparkline', d.cpu_pct, cpuHistory);
      var cval = document.getElementById('perf-cpu-val');
      if(cval) cval.textContent = d.cpu_pct.toFixed(1) + '%';
    }}
    if(d.ram_pct !== undefined) {{
      updateSparkline('ram-sparkline', d.ram_pct, ramHistory);
      var rval = document.getElementById('perf-ram-val');
      if(rval) rval.textContent = d.ram_pct.toFixed(1) + '%';
    }}
    if(d.cell_load) set('perf-load', d.cell_load);
    if(d.cells && d.cells.temp) set('perf-temp', d.cells.temp);
    if(d.cells && d.cells.ram) set('perf-ram-sub', d.cells.ram);
    if(d.cells && d.cells.zram) set('cell-zram-perf', d.cells.zram);
    if(d.cells && d.cells.emmc) set('cell-emmc-perf', d.cells.emmc);
    if(d.cells && d.cells.disk) set('cell-disk-perf', d.cells.disk);
    if(d.cells && d.cells.lan) set('cell-lan-perf', d.cells.lan);
    if(d.cells && d.cells.ts) set('cell-ts-perf', d.cells.ts);
    if(d.cells && d.cells.uptime) set('cell-uptime-perf', d.cells.uptime);
    if(d.cells && d.cells.internet) set('cell-internet-perf', d.cells.internet);
    if(d.log_card){{
      window._lastRouterLogCard = d.log_card;
      window._hasRouterLog = true;
    }}
    if(!safeStore('getItem','logDismissed')) stickySet('log-slot',d.log_card);
    else {{ var rc=document.getElementById('router-log-card'); if(rc) {{ window._lastRouterLogCard = rc.outerHTML; window._hasRouterLog = true; rc.remove(); }} }}
    if(d.hermes_log_card){{
      window._lastHermesLogCard = d.hermes_log_card;
      window._hasHermesLog = true;
    }}
    if(!safeStore('getItem','hermesLogDismissed')) {{
      var hls = document.getElementById('hermes-log-slot');
      if(hls && d.hermes_log_card) stickySet('hermes-log-slot', d.hermes_log_card);
    }} else {{
      var hc = document.getElementById('hermes-log-card');
      if(hc) {{ window._lastHermesLogCard = hc.outerHTML; window._hasHermesLog = true; hc.remove(); }}
    }}
    if(d.clean_junk_card) {{
      window._lastCleanJunkCard = d.clean_junk_card;
      window._hasCleanLog = true;
    }}
    if(!safeStore('getItem','cleanLogDismissed')) {{
      var cls = document.getElementById('clean-log-slot');
      if(cls && d.clean_junk_card) stickySet('clean-log-slot', d.clean_junk_card);
    }} else {{
      var cc = document.getElementById('clean-log-card');
      if(cc) {{ window._lastCleanJunkCard = cc.outerHTML; window._hasCleanLog = true; cc.remove(); }}
    }}
    if(!safeStore('getItem','gatewayLogDismissed')) {
      if(d.gateway_log_card) stickySet('gateway-log-slot', d.gateway_log_card);
    } else {
      var gc = document.getElementById('gateway-log-card');
      if(gc) { window._lastGatewayLogCard = gc.outerHTML; window._hasGatewayLog = true; gc.remove(); }
    }
    if(d.gateway_log_card) {
      window._lastGatewayLogCard = d.gateway_log_card;
      window._hasGatewayLog = true;
    }
    syncLogUI();
    restorePatchPages();
  }
  function connect(){
    if(retryTimer){ clearTimeout(retryTimer); retryTimer=null; }
    if(es) try{ es.close(); }catch(x){}
    es=new EventSource('/events');
    es.onopen=function(){
      if(spin) spin.classList.remove('on');
      var live=document.querySelector('.live-badge');
      if(live) live.classList.add('connected');
    };
    es.addEventListener('update', function(e){
      try{ onUpdate(JSON.parse(e.data)); }catch(ex){}
    });
    es.onerror=function(){
      if(spin) spin.classList.add('on');
      var live=document.querySelector('.live-badge');
      if(live) live.classList.remove('connected');
      es.close(); es=null;
      retryTimer=setTimeout(connect, 3000);
    };
  }
  function disconnect(){
    if(retryTimer){ clearTimeout(retryTimer); retryTimer=null; }
    if(es){ es.close(); es=null; }
  }
  document.addEventListener('visibilitychange', function(){
    if(document.hidden){ disconnect(); }
    else{ connect(); }
  });
  if(!document.hidden) connect();
})();
</script>"""


# Click-to-loading feedback for real same-tab navigations (/toggle,
# /restart-bot, /update-router, /switch-model chips, ...). These are plain
# <a href> links — clicking causes a full page navigation, and
# build_status_page() can take a couple seconds (systemctl/docker/sqlite
# checks), so without this the browser just shows a blank gap. Showing the
# overlay synchronously on click (before the browser starts unloading the
# page) covers that gap. "Buka Dashboard" (target=_blank) is deliberately
# excluded: it opens a new tab and never navigates this page away, so the
# overlay would just stay stuck on screen with nothing to dismiss it. No
# placeholders to fill, so this is inserted as a literal — no render_*()
# wrapper needed.
NAV_SCRIPT = """<script>
var MUTATING_PREFIXES = [
  '/toggle', '/on', '/off', '/restart-bot', '/bot-toggle',
  '/update-router', '/check-update',
  '/check-hermes-update', '/update-hermes',
  '/clean-junk',
  '/fetch-models', '/reload-panel-config',
  '/set-aux-model', '/reset-aux',
  '/set-fallback-model', '/remove-fallback-model',
  '/process-action', '/switch-model',
  '/save-gateway-platform', '/toggle-gateway-platform', '/remove-gateway-platform'
];
var CONFIRM_ROUTES = [
  {match:'/update-hermes', title:'Perbarui Hermes Agent', msg:'Perbarui Hermes via git pull + install dependency + mulai ulang gateway. Bot tidak bisa dibalas selama proses (beberapa menit). Lanjutkan?'},
  {match:'/update-router', title:'Perbarui 9router', msg:'Perbarui 9router via docker compose pull + up -d. Kontainer 9router akan mulai ulang. Lanjutkan?'},
  {match:'/restart-bot', title:'Mulai Ulang Hermes Gateway', msg:'Mulai ulang service hermes-gateway? Seluruh koneksi platform perpesanan akan mulai ulang dalam beberapa detik.'},
  {match:'/bot-toggle', title:'Ubah Status Gateway', msg:'Ubah status hidup/mati Hermes Gateway? Seluruh platform perpesanan yang terhubung akan ikut mati/hidup.'},
  {match:'/clean-junk', title:'Bersihkan Cache & Sampah', msg:'Bersihkan log pembaruan, cache package uv/pip, dan builder docker dangling untuk melegakan penyimpanan STB?'},
  {match:'/reset-aux', title:'Kembalikan Model Tugas', msg:'Kembalikan semua model tugas tambahan ke otomatis? Setiap tugas akan ikut model obrolan utama.'},
  {match:'/remove-fallback-model', title:'Hapus Model Cadangan', msg:'Hapus model ini dari daftar cadangan?'},
  {match:'/process-action?service=9router&action=stop', title:'Hentikan 9router', msg:'Hentikan kontainer 9router? AI routing akan mati sampai dinyalakan lagi.'},
  {match:'/process-action?service=cloudflared&action=stop', title:'Hentikan Cloudflared', msg:'Hentikan tunnel Cloudflared? Akses eksternal putus sampai dinyalakan lagi.'},
  {match:'/process-action?service=pihole-pihole-1&action=stop', title:'Hentikan Pi-hole', msg:'Hentikan Pi-hole? DNS dan anti-iklan mati sampai dinyalakan lagi.'},
  {match:'/process-action?service=hermes-dashboard&action=restart', title:'Mulai Ulang Dasbor', msg:'Mulai ulang layanan hermes-dashboard? Halaman dasbor :9119 terputus sebentar.'},
  {match:'/process-action?service=hermes-panel&action=restart', title:'Mulai Ulang Panel', msg:'Mulai ulang layanan hermes-panel? Panel tersambung lagi dalam beberapa detik.'},
  {match:'/process-action?action=restart', title:'Mulai Ulang Tugas', msg:'Mulai ulang layanan yang dipilih sekarang?'},
];

function isMutatingPath(url) {
  var path = (url || '').split('?')[0];
  for (var i = 0; i < MUTATING_PREFIXES.length; i++) {
    if (path === MUTATING_PREFIXES[i]) return true;
  }
  return false;
}

function postNavigate(url) {
  var form = document.createElement('form');
  form.method = 'POST';
  var parts = url.split('?');
  form.action = parts[0];
  if (parts[1]) {
    var params = new URLSearchParams(parts[1]);
    params.forEach(function(v, k) {
      var inp = document.createElement('input');
      inp.type = 'hidden';
      inp.name = k;
      inp.value = v;
      form.appendChild(inp);
    });
  }
  document.body.appendChild(form);
  form.submit();
}

document.addEventListener('click', function(e){
  var a = e.target.closest('a.toggle, a.open, a.model-chip, a.btn-end-task, a.btn-restart-task, a.btn-start-task, a.btn-action-danger');
  if(!a || !a.getAttribute('href') || a.getAttribute('href') === '#' || a.getAttribute('href').indexOf('javascript:') === 0 || a.target === '_blank'
     || a.classList.contains('is-loading')) return;
  var href = a.getAttribute('href');
  for(var i=0;i<CONFIRM_ROUTES.length;i++){
    if(href.indexOf(CONFIRM_ROUTES[i].match) !== -1){
      e.preventDefault();
      confirmAction(CONFIRM_ROUTES[i], href);
      return;
    }
  }
  if(isMutatingPath(href)){
    e.preventDefault();
    a.classList.add('is-loading');
    var nl = document.getElementById('nav-label');
    if(nl) nl.textContent = a.textContent.trim();
    var ov = document.getElementById('navloader');
    if(ov) ov.classList.add('show');
    postNavigate(href);
    return;
  }
  a.classList.add('is-loading');
  var nl = document.getElementById('nav-label');
  if(nl) nl.textContent = a.textContent.trim();
  var ov = document.getElementById('navloader');
  if(ov) ov.classList.add('show');
}, true);

function confirmAction(route, href){
  var modal=document.getElementById('confirm-modal');
  if(!modal) return;
  var titleEl = document.getElementById('confirm-title');
  var msgEl = document.getElementById('confirm-msg');
  var cancelBtn = document.getElementById('confirm-cancel');
  var okBtn = document.getElementById('confirm-ok');
  if(titleEl) titleEl.textContent = route.title;
  if(msgEl) msgEl.textContent = route.msg;
  modal.classList.add('show');
  if(cancelBtn) cancelBtn.onclick=function(){ modal.classList.remove('show'); };
  if(okBtn) okBtn.onclick=function(){
    try{
      safeStore('removeItem','logDismissed');
      safeStore('removeItem','hermesLogDismissed');
      safeStore('removeItem','cleanLogDismissed');
      safeStore('removeItem','gatewayLogDismissed');
    }catch(x){}
    if(isMutatingPath(href)){
      postNavigate(href);
    } else {
      window.location.href = href;
    }
  };
}
function safeStore(fn, key, val){
  try{ return val===undefined ? sessionStorage[fn](key) : sessionStorage[fn](key,val); }
  catch(x){ return null; }
}
</script>"""


def render_poll_script() -> str:
    return SSE_SCRIPT


def render_log_card(log_text: str, result: dict | None = None) -> str:
    body = html.escape(redact_sensitive_tokens(log_text)) if log_text.strip() else "(menunggu output…)"
    status = (result or {}).get("status", "")
    summary = html.escape((result or {}).get("summary", ""))
    if status == "success":
        badge = f'<span class="up">{ICON_CHECK}{summary}</span>'
    elif status == "failed":
        badge = f'<span class="down">{ICON_ALERT_TRIANGLE}{summary}</span>'
    elif status == "running":
        badge = f'<span class="warn">{ICON_CLOCK}Update berjalan…</span>'
    else:
        badge = '<span class="pulse-dot pulse"></span>'
    # onclick uses safeStore(): sessionStorage throws in private/strict
    # browsers, which would leave the card un-dismissable.
    return (
        f'<div class="card" id="router-log-card">'
        f'<div class="card-title" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px">'
        f'<div>{ICON_TERMINAL} Log pembaruan 9router {badge}</div>'
        f'<button type="button" class="btn" style="width:auto;padding:0.2rem 0.6rem;font-size:0.72rem;margin:0" '
        f"onclick=\"safeStore('setItem','logDismissed','1');var c=document.getElementById('router-log-card');if(c)c.remove();if(window.syncLogUI)syncLogUI()\">"
        f'Sembunyikan Log</button>'
        f'</div>'
        f'<div class="logbox" id="logbox">{body}</div>'
        f'</div>'
    )


HERMES_LIB_DIR = "/opt/AppData/hermes-native/hermes-lib"
_hermes_update_cache = {"status": "unknown", "local": "?", "remote": "?", "behind": 0, "at": 0}
_hermes_update_lock = threading.Lock()
_hermes_update_running = False
_hermes_update_result = {"status": "idle", "exit_code": None, "summary": "", "finished_at": 0.0}


def _clear_stale_hermes_git_lock() -> None:
    """Remove abandoned shallow.lock, never an active updater's lock."""
    lock = Path(HERMES_LIB_DIR) / ".git" / "shallow.lock"
    try:
        if not lock.exists() or time.time() - lock.stat().st_mtime <= 600:
            return
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                cmd = (proc / "cmdline").read_bytes().replace(b"\\0", b" ").decode(errors="replace")
            except Exception:
                continue
            active_git = bool(re.search(r"(?:^|[\s/])git(?:[\s/]|$)", cmd))
            if HERMES_LIB_DIR in cmd and (active_git or "hermes update" in cmd):
                return
        lock.unlink()
    except OSError:
        pass


def _refresh_hermes_update() -> None:
    """Background: check latest commit on GitHub API & git rev-parse HEAD."""
    global _hermes_update_cache
    try:
        _clear_stale_hermes_git_lock()

        # Get local HEAD SHA and tag
        local_r = subprocess.run(["git", "-C", HERMES_LIB_DIR, "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=5)
        local_sha = local_r.stdout.strip()
        
        tag_r = subprocess.run(["git", "-C", HERMES_LIB_DIR, "describe", "--tags", "--abbrev=0", "HEAD"],
                                capture_output=True, text=True, timeout=5)
        local_tag = tag_r.stdout.strip() or (local_sha[:8] if local_sha else "?")

        # Try GitHub API compare endpoint first (fast, works on shallow git checkouts without un-shallowing)
        behind = 0
        status = "current"
        remote_tag = local_tag
        cmp_data = None

        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/NousResearch/hermes-agent/compare/{local_sha}...main",
                headers={"User-Agent": "Hermes-Panel"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                cmp_data = json.loads(resp.read().decode("utf-8"))
                # When comparing local...main:
                # 'ahead_by' in GitHub API compare/local...main means commits main is ahead of local (i.e. behind count)
                # 'total_commits' gives the count of new commits
                behind = int(cmp_data.get("ahead_by", 0) or cmp_data.get("total_commits", 0) or 0)
                status = "available" if behind > 0 else "current"
        except Exception:
            # Fallback to git fetch + rev-list if GitHub API fails
            subprocess.run(["git", "-C", HERMES_LIB_DIR, "fetch", "--depth", "1", "--quiet", "origin", "main"],
                           capture_output=True, timeout=30)
            target_r = subprocess.run(["git", "-C", HERMES_LIB_DIR, "rev-parse", "FETCH_HEAD"],
                                      capture_output=True, text=True, timeout=5)
            target_sha = target_r.stdout.strip()
            if target_sha and local_sha:
                behind = 0 if target_sha == local_sha else 1
                status = "available" if behind > 0 else "current"

        # Sanity check: if behind count is unreasonably large (>500 on shallow check) or 0
        if behind == 0:
            status = "current"

        patch_notes = []
        if cmp_data and cmp_data.get("commits"):
            raw_msgs = [c.get("commit", {}).get("message", "").strip().split("\n")[0] for c in cmp_data.get("commits", []) if c.get("commit")]
            if raw_msgs:
                patch_notes = list(reversed(raw_msgs[-25:]))
        if not patch_notes:
            try:
                gl = subprocess.run(["git", "-C", HERMES_LIB_DIR, "log", "-n", "25", "--pretty=format:%s"],
                                    capture_output=True, text=True, timeout=5)
                if gl.returncode == 0 and gl.stdout.strip():
                    patch_notes = [line.strip() for line in gl.stdout.strip().split("\n") if line.strip()]
            except Exception:
                pass
        if not patch_notes:
            patch_notes = ["Pembaruan stabilitas dan refaktor berkala"]

        with _hermes_update_lock:
            _hermes_update_cache = {
                "status": status,
                "local": local_tag,
                "remote": remote_tag,
                "behind": behind,
                "patch_notes": patch_notes,
                "at": time.time(),
            }
    except Exception:
        with _hermes_update_lock:
            _hermes_update_cache = {"status": "unknown", "local": "?", "remote": "?", "behind": 0, "patch_notes": [], "at": time.time()}


def translate_to_id(text: str) -> str:
    """Pass-through default raw text without translation."""
    return text


def render_patch_notes_block(title: str, notes: list[str]) -> str:
    """Render raw patch notes with five entries per client-side page."""
    if not notes:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "updates"
    box_id = f"patch-notes-{slug}"
    page_size = 5
    items = []
    for index, note in enumerate(notes):
        page = index // page_size + 1
        display = "" if page == 1 else ' style="display:none"'
        items.append(
            f'<li data-patch-page="{page}"{display}>{html.escape(note.strip())}</li>'
        )
    total_pages = (len(notes) + page_size - 1) // page_size
    return (
        f'<div class="patch-notes-box" id="{box_id}" data-patch-current="1">'
        f'<div class="patch-notes-title">{ICON_TERMINAL} Catatan Pembaruan ({html.escape(title)}):</div>'
        f'<ul class="patch-notes-list">{"".join(items)}</ul>'
        f'<div class="patch-notes-pages">'
        f'<button type="button" class="btn patchPrev" disabled '
        f'onclick="patchPage(\'{box_id}\',parseInt(document.getElementById(\'{box_id}\').getAttribute(\'data-patch-current\')||\'1\',10)-1)">‹ Sebelumnya</button>'
        f'<span class="patch-notes-page-label">1 / {total_pages}</span>'
        f'<button type="button" class="btn patchNext"{("" if total_pages > 1 else " disabled")}'
        f' onclick="patchPage(\'{box_id}\',parseInt(document.getElementById(\'{box_id}\').getAttribute(\'data-patch-current\')||\'1\',10)+1)">Berikutnya ›</button>'
        f'</div></div>'
    )


def get_hermes_patch_notes() -> list[str]:
    """Return latest commits / patch notes for Hermes Agent."""
    with _hermes_update_lock:
        notes = _hermes_update_cache.get("patch_notes")
        if notes and len(notes) >= 5:
            return list(notes)
    try:
        gl = subprocess.run(["git", "-C", HERMES_LIB_DIR, "log", "-n", "25", "--pretty=format:%s"],
                            capture_output=True, text=True, timeout=5)
        if gl.returncode == 0 and gl.stdout.strip():
            return [line.strip() for line in gl.stdout.strip().split("\n") if line.strip()]
    except Exception:
        pass
    return ["Pembaruan stabilitas dan refaktor berkala"]


def get_hermes_update() -> dict:
    """Return cached update status; kick off background refresh if stale."""
    with _hermes_update_lock:
        age = time.time() - _hermes_update_cache["at"]
        if age > 1800 and not _hermes_update_running:
            _hermes_update_cache["at"] = time.time()  # debounce
            threading.Thread(target=_refresh_hermes_update, daemon=True).start()
    with _hermes_update_lock:
        return dict(_hermes_update_cache)


def is_hermes_updating() -> bool:
    global _hermes_update_running
    if _hermes_update_running:
        return True
    prog = Path("/root/.hermes/.hermes-update-in-progress")
    if prog.exists():
        try:
            lines = prog.read_text().splitlines()
            if lines:
                pid = int(lines[0].strip())
                os.kill(pid, 0)
                return True
        except (OSError, ValueError):
            pass
    try:
        r = subprocess.run(["pgrep", "-f", "hermes update"], capture_output=True, text=True, timeout=2)
        pids = [int(p) for p in r.stdout.strip().split() if p.isdigit() and int(p) != os.getpid()]
        if pids:
            return True
    except Exception:
        pass
    return False


def get_hermes_update_result() -> dict:
    running = is_hermes_updating()
    with _hermes_update_lock:
        result = dict(_hermes_update_result)
        result["running"] = running
        return result


def tail_hermes_update_log(n: int = 100) -> str:
    try:
        with open(HERMES_UPDATE_LOG_PATH, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            return "".join(lines[-n:]) if lines else ""
    except Exception:
        return ""


def run_hermes_update() -> None:
    """Use official updater: pull, validate/rollback, deps, migrate, restart."""
    global _hermes_update_running, _hermes_update_result, _hermes_update_cache
    if is_hermes_updating():
        return
    with _hermes_update_lock:
        _hermes_update_running = True
        _hermes_update_result = {"status": "running", "exit_code": None,
                                 "summary": "Pembaruan Hermes berjalan…", "finished_at": 0.0}

    def _run():
        global _hermes_update_running, _hermes_update_result, _hermes_update_cache
        try:
            lock = Path(HERMES_LIB_DIR) / ".git" / "shallow.lock"
            if lock.exists():
                age = time.time() - lock.stat().st_mtime
                if age > 600:
                    lock.unlink()
            os.makedirs(os.path.dirname(HERMES_UPDATE_LOG_PATH), exist_ok=True)
            with open(HERMES_UPDATE_LOG_PATH, "w", encoding="utf-8") as log:
                log.write("[panel] official command: hermes update --yes --no-backup\n")
                log.flush()
                proc = subprocess.Popen(
                    [HERMES_BIN, "update", "--yes", "--no-backup"],
                    stdout=log, stderr=subprocess.STDOUT, text=True,
                    start_new_session=True,
                )
                try:
                    rc = proc.wait(timeout=HERMES_UPDATE_TIMEOUT)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                            proc.wait(timeout=2)
                        except Exception:
                            pass
                    log.write(f"\n[panel] ERROR: timeout {HERMES_UPDATE_TIMEOUT} detik\n")
                    rc = 124
            if rc == 0:
                summary = "Hermes sukses diperbarui: dependency, validasi, dan mulai ulang selesai"
            else:
                summary = f"Hermes update gagal (exit {rc}); cek log lengkap"
            with _hermes_update_lock:
                _hermes_update_result = {"status": "success" if rc == 0 else "failed",
                                         "exit_code": rc, "summary": summary,
                                         "finished_at": time.time()}
                _hermes_update_cache = {"status": "unknown", "local": "?", "remote": "?", "behind": 0, "at": 0}
        except Exception as exc:
            with open(HERMES_UPDATE_LOG_PATH, "a", encoding="utf-8") as log:
                log.write(f"\n[panel] ERROR: {type(exc).__name__}: {exc}\n")
            with _hermes_update_lock:
                _hermes_update_result = {"status": "failed", "exit_code": 1,
                                         "summary": f"Hermes update gagal: {type(exc).__name__}",
                                         "finished_at": time.time()}
        finally:
            with _hermes_update_lock:
                _hermes_update_running = False
    threading.Thread(target=_run, daemon=True).start()


def service_active(name: str, user: bool = False) -> bool:
    cmd = ["systemctl"]
    env = os.environ.copy()
    if user:
        cmd.append("--user")
        env.setdefault("XDG_RUNTIME_DIR", "/run/user/0")
    cmd += ["is-active", name]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=INFO_TIMEOUT, env=env
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False


_config_cache = {"mtime": 0.0, "val": {}}
_config_cache_lock = threading.Lock()


def get_parsed_config() -> dict:
    """Cached parsing of config.yaml based on file mtime."""
    try:
        mt = os.path.getmtime(CONFIG_PATH)
        with _config_cache_lock:
            if _config_cache["val"] and _config_cache["mtime"] == mt:
                return _config_cache["val"]
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        with _config_cache_lock:
            _config_cache["mtime"] = mt
            _config_cache["val"] = data
        return data
    except Exception:
        return {}


def invalidate_config_cache():
    with _config_cache_lock:
        _config_cache["mtime"] = 0.0
        _config_cache["val"] = {}


def get_current_model() -> str:
    try:
        data = get_parsed_config()
        return str(data.get("model", {}).get("default") or "?")
    except Exception:
        return "?"


def get_router_api_key() -> str:
    """Read 9router credentials from the active parsed config.

    The current Hermes schema stores custom-provider credentials under
    ``fallback_providers`` or top-level ``custom_providers``; the old regex
    expected a legacy ``name: 9router`` block and silently returned empty.
    """
    try:
        data = get_parsed_config()
        candidates = []
        candidates.extend(data.get("fallback_providers") or [])
        custom = data.get("custom_providers") or {}
        if isinstance(custom, dict):
            provider = custom.get("9router")
            if isinstance(provider, dict):
                candidates.append(provider)
        model_custom = (data.get("model") or {}).get("custom_providers") or {}
        if isinstance(model_custom, dict):
            provider = model_custom.get("9router")
            if isinstance(provider, dict):
                candidates.append(provider)
        for item in candidates:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider", ""))
            if provider in ("", "9router", "custom:9router"):
                key = item.get("api_key") or item.get("apiKey")
                if key:
                    return str(key)
    except Exception:
        pass
    return ""


def _group_available_models(data: dict) -> dict:
    """Group a /v1/models response by provider without performing I/O."""
    result = {}

    for item in data.get("data", []):
        if not isinstance(item, dict):
            continue

        mid = item.get("id", "")
        if not mid:
            continue

        ob = str(item.get("owned_by", "")).lower()

        if ob == "combo":
            group = "9router (Kombo)"
        elif ob == "ag" or mid.startswith("ag/"):
            group = "Antigravity (ag)"
        elif ob == "cx" or mid.startswith("cx/"):
            group = "Codex (cx)"
        elif ob == "gemini" or mid.startswith("gemini/"):
            group = "Google Gemini"
        elif ob == "kr" or mid.startswith("kr/"):
            group = "Kiro (kr)"
        elif ob == "ollama" or mid.startswith("ollama/"):
            group = "Ollama"
        elif ob == "groq" or mid.startswith("groq/"):
            group = "Groq"
        elif ob == "openrouter" or mid.startswith("openrouter/"):
            group = "OpenRouter"
        elif ob == "cmc" or mid.startswith("cmc/"):
            group = "CommandCode (cmc)"
        elif ob == "nara" or mid.startswith("nara/"):
            group = "Nara / KiloCode"
        elif ob:
            group = ob.upper()
        else:
            group = "Lainnya"

        result.setdefault(group, []).append(mid)

    ordered_result = {}

    if "9router (Kombo)" in result:
        ordered_result["9router (Kombo)"] = sorted(
            result.pop("9router (Kombo)")
        )

    for group in sorted(result):
        ordered_result[group] = sorted(result[group])

    return ordered_result


def fetch_remote_models() -> dict:
    """Explicitly query 9router's /v1/models endpoint, refresh cache, return status."""
    host = get_9router_host()
    port = get_9router_port()
    url = f"http://{host}:{port}/v1/models"
    try:
        headers = {"Accept": "application/json"}
        api_key = get_router_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=MODELS_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        grouped = _group_available_models(data)

        with _models_cache_lock:
            _models_cache["val"] = grouped
            _models_cache["at"] = time.time()

        return {
            "status": "success",
            "count": sum(len(models) for models in grouped.values()),
            "host": host,
        }
    except Exception as exc:
        return {"status": "failed", "error": str(exc), "host": host}


def reload_panel_config() -> dict:
    """Reload dynamic caches from disk and refresh 9router models/version info."""
    global _9router_host_cache, _9router_host_at
    with _9router_host_lock:
        _9router_host_cache = ""
        _9router_host_at = 0
    with _models_cache_lock:
        _models_cache["at"] = 0
    with _router_image_date_lock:
        _router_image_date_cache.update(at=0, val="?")
    fetch_remote_models()
    return {"status": "reloaded", "time": time.time()}


def get_all_configured_providers() -> list[dict]:
    """Parse config.yaml and return list of providers (custom_providers + fallback_providers + primary)."""
    providers = []
    seen = set()
    try:
        data = get_parsed_config()
        # Primary provider
        p_name = data.get("model", {}).get("provider", "")
        if p_name:
            label = p_name.replace("custom:", "") if p_name.startswith("custom:") else p_name
            providers.append({"name": label, "full_name": p_name, "type": "primary"})
            seen.add(label)
            seen.add(p_name)
        # Custom providers
        for cp in data.get("custom_providers", []):
            if isinstance(cp, dict):
                cname = cp.get("name", "")
                if cname and cname not in seen:
                    providers.append({"name": cname, "full_name": cname, "type": "custom"})
                    seen.add(cname)
        # Fallback providers
        for fp in data.get("fallback_providers", []):
            if isinstance(fp, dict):
                fname = fp.get("provider", "")
                label = fname.replace("custom:", "") if fname.startswith("custom:") else fname
                if label and label not in seen:
                    providers.append({"name": label, "full_name": fname, "type": "fallback"})
                    seen.add(label)
                    seen.add(fname)
    except Exception:
        pass
    return providers


def get_available_models() -> dict:
    """Fetch live models from 9router /v1/models and group ALL models by provider/category."""
    key = get_router_api_key()
    if not key:
        return {"9router (Kombo)": []}
    try:
        host = get_9router_host()
        port = get_9router_port()
        url = f"http://{host}:{port}/v1/models"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=MODELS_TIMEOUT) as resp:
            data = json.load(resp)

        return _group_available_models(data)
    except Exception:
        pass

    return {"9router (Kombo)": []}


def get_available_models_cached() -> dict:
    """Cached version of get_available_models()."""
    with _models_cache_lock:
        if _models_cache["val"] and time.time() - _models_cache["at"] < MODELS_CACHE_TTL:
            return _models_cache["val"]
    val = get_available_models()
    if any(val.values()):
        with _models_cache_lock:
            _models_cache["val"] = val
            _models_cache["at"] = time.time()
        return val
    with _models_cache_lock:
        return _models_cache["val"] if isinstance(_models_cache["val"], dict) else {"9router": [], "OpenCode Zen": [], "Nous Portal": [], "Provider Lain": []}


def set_current_model(model_id: str) -> bool:
    """Rewrite model.default in config.yaml, atomically."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            text = f.read()
        # Handle both 'model:\n  default: ...' and 'model:\n  ...:\n  default: ...'
        new_text, n = re.subn(
            r"(\bdefault:\s*)\S+",
            lambda m: m.group(1) + model_id,
            text,
            count=1,
        )
        if n == 0:
            return False
        _write_config_atomic(new_text)
        return True
    except Exception as e:
        sys.stderr.write(f"[panel] set_current_model error: {e}\n")
        return False


# --- Konfigurasi Model Tugas Tambahan ---
AUX_TASK_DEFINITIONS: list[tuple[str, str, str]] = [
    ("vision", "Vision", "Image & screenshot analysis"),
    ("compression", "Compression", "Context summarization"),
    ("skills_hub", "Skills Hub", "Skill search & installation"),
    ("approval", "Approval", "Smart auto-approve commands"),
    ("mcp", "MCP", "MCP tool reasoning"),
    ("title_generation", "Title Gen", "Session titles"),
    ("review", "Review", "/review subagent"),
    ("triage_specifier", "Triage Specifier", "Kanban spec fleshing"),
    ("kanban_decomposer", "Kanban Decomposer", "Task decomposition"),
    ("profile_describer", "Profile Describer", "Auto profile descriptions"),
    ("curator", "Curator", "Skill-usage review pass"),
    ("delegation", "Delegation", "Subagent model (delegate_task)"),
    ("web_extract", "Web Extract", "Web content extraction"),
    ("memory_query_rewrite", "Memory Query Rewrite", "Memory retrieval queries"),
    ("tts_audio_tags", "TTS Audio Tags", "Penyisipan tag audio TTS"),
    ("monitor", "Monitor", "System monitoring & watchdogs"),
]


def get_aux_tasks_config() -> list[dict]:
    """Return live status of all auxiliary tasks from config.yaml."""
    tasks = []
    try:
        cfg = get_parsed_config()
    except Exception:
        cfg = {}

    aux = cfg.get("auxiliary", {}) if isinstance(cfg.get("auxiliary"), dict) else {}
    dele = cfg.get("delegation", {}) if isinstance(cfg.get("delegation"), dict) else {}

    for key, label, hint in AUX_TASK_DEFINITIONS:
        if key == "delegation":
            provider = str(dele.get("provider") or "").strip()
            model = str(dele.get("model") or "").strip()
            is_auto = not provider or provider == "auto"
        else:
            task_cfg = aux.get(key, {}) if isinstance(aux.get(key), dict) else {}
            provider = str(task_cfg.get("provider") or "auto").strip() or "auto"
            model = str(task_cfg.get("model") or "").strip()
            is_auto = (provider == "auto" or not provider) and not model

        if is_auto:
            display_val = "otomatis (pakai model utama)"
        elif provider and model:
            display_val = f"{provider} · {model}"
        elif model:
            display_val = model
        else:
            display_val = provider

        tasks.append({
            "key": key,
            "label": label,
            "hint": hint,
            "provider": provider,
            "model": model,
            "is_auto": is_auto,
            "display": display_val,
        })
    return tasks


def set_aux_task_model(task: str, provider: str, model: str) -> bool:
    """Set provider/model for a specific auxiliary task in config.yaml."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        host = get_9router_host()
        key = get_router_api_key()

        if task == "delegation":
            dele = cfg.setdefault("delegation", {})
            if not isinstance(dele, dict):
                dele = cfg["delegation"] = {}
            if provider == "auto" or not provider:
                dele["provider"] = ""
                dele["model"] = ""
            else:
                dele["provider"] = provider
                dele["model"] = model
        else:
            aux = cfg.setdefault("auxiliary", {})
            if not isinstance(aux, dict):
                aux = cfg["auxiliary"] = {}
            task_cfg = aux.setdefault(task, {})
            if not isinstance(task_cfg, dict):
                task_cfg = aux[task] = {}
            if provider == "auto" or not provider:
                task_cfg["provider"] = "auto"
                task_cfg["model"] = ""
                task_cfg.pop("base_url", None)
                task_cfg.pop("api_key", None)
            else:
                task_cfg["provider"] = provider
                task_cfg["model"] = model
                if provider == "custom:9router":
                    task_cfg["base_url"] = f"http://{host}:20128/v1"
                    task_cfg["api_key"] = key or ""

        _write_config_atomic(cfg)
        return True
    except Exception as e:
        sys.stderr.write(f"[panel] set_aux_task_model error: {e}\n")
        return False


def reset_all_aux_tasks() -> bool:
    """Reset every auxiliary task and delegation back to auto."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        aux = cfg.setdefault("auxiliary", {})
        if isinstance(aux, dict):
            for key, _, _ in AUX_TASK_DEFINITIONS:
                if key != "delegation" and key in aux and isinstance(aux[key], dict):
                    aux[key]["provider"] = "auto"
                    aux[key]["model"] = ""

        dele = cfg.setdefault("delegation", {})
        if isinstance(dele, dict):
            dele["provider"] = ""
            dele["model"] = ""

        _write_config_atomic(cfg)
        return True
    except Exception as e:
        sys.stderr.write(f"[panel] reset_all_aux_tasks error: {e}\n")
        return False


def render_aux_tasks_block() -> str:
    """Render list of auxiliary tasks for the dashboard."""
    tasks = get_aux_tasks_config()
    rows = []
    for t in tasks:
        key = t["key"]
        label = html.escape(t["label"])
        hint = html.escape(t["hint"])
        display_val = html.escape(t["display"])
        val_cls = "mono-sub auto" if t["is_auto"] else "mono-sub custom"
        safe_key = html.escape(key.replace(chr(92), chr(92)*2).replace(chr(39), chr(92)+chr(39)), quote=True)
        safe_label = html.escape(t["label"].replace(chr(92), chr(92)*2).replace(chr(39), chr(92)+chr(39)), quote=True)
        rows.append(
            f'<div class="aux-task-row" data-task="{key}" data-label="{label}">'
            f'  <div class="aux-task-info">'
            f'    <div class="aux-task-title">'
            f'      <span class="aux-task-name">{label}</span>'
            f'      <span class="aux-task-hint">{hint}</span>'
            f'    </div>'
            f'    <div class="{val_cls}">{display_val}</div>'
            f'  </div>'
            f'  <button type="button" class="btn-action-sm" onclick="openAuxPicker(\'{safe_key}\', \'{safe_label}\')">'
            f'    Ganti'
            f'  </button>'
            f'</div>'
        )
    return "".join(rows)


# --- Fallback / Backup Models Configuration ---
def get_fallback_models_config() -> list[dict]:
    """Return list of configured fallback/backup models from config.yaml."""
    try:
        cfg = get_parsed_config()
    except Exception:
        cfg = {}

    fps = cfg.get("fallback_providers")
    if not isinstance(fps, list):
        fm = cfg.get("fallback_model")
        fps = [fm] if isinstance(fm, dict) else (fm if isinstance(fm, list) else [])

    result = []
    for idx, entry in enumerate(fps):
        if isinstance(entry, dict):
            provider = str(entry.get("provider") or "").strip()
            model = str(entry.get("model") or "").strip()
            base_url = str(entry.get("base_url") or "").strip()
            if model:
                result.append({
                    "index": idx,
                    "provider": provider or "custom:9router",
                    "model": model,
                    "base_url": base_url,
                    "display": f"{provider} · {model}" if provider else model,
                })
    return result


def set_fallback_model(index: int, provider: str, model: str) -> bool:
    """Set or add a fallback provider in config.yaml."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        fps = cfg.setdefault("fallback_providers", [])
        if not isinstance(fps, list):
            fps = cfg["fallback_providers"] = []

        key = get_router_api_key()
        host = get_9router_host()
        base_url = f"http://{host}:20128/v1"

        entry = {
            "provider": provider or "custom:9router",
            "model": model,
            "base_url": base_url,
            "api_key": key or "",
        }

        if 0 <= index < len(fps):
            old_base = fps[index].get("base_url")
            old_key = fps[index].get("api_key")
            if old_base:
                entry["base_url"] = old_base
            if old_key:
                entry["api_key"] = old_key
            fps[index] = entry
        else:
            fps.append(entry)

        _write_config_atomic(cfg)
        return True
    except Exception as e:
        sys.stderr.write(f"[panel] set_fallback_model error: {e}\n")
        return False


def remove_fallback_model(index: int) -> bool:
    """Remove a fallback provider by index in config.yaml."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        fps = cfg.get("fallback_providers")
        if isinstance(fps, list) and 0 <= index < len(fps):
            fps.pop(index)
            _write_config_atomic(cfg)
            return True
        return False
    except Exception as e:
        sys.stderr.write(f"[panel] remove_fallback_model error: {e}\n")
        return False


def render_backup_models_block() -> str:
    """Render list of fallback/backup models for dashboard."""
    models = get_fallback_models_config()
    if not models:
        return (
            '<div class="update-hint" style="margin:0;font-size:0.75rem">'
            'Belum ada model cadangan. Klik "+ Tambah Cadangan" untuk mengaktifkan pengalihan otomatis.'
            '</div>'
        )
    rows = []
    for m in models:
        idx = m["index"]
        priority = idx + 1
        model_name = html.escape(m["model"])
        provider = html.escape(m["provider"])
        safe_model = html.escape(m["model"].replace("\\", "\\\\").replace("'", "\\'"), quote=True)
        rows.append(
            f'<div class="aux-task-row" style="margin-bottom:0.45rem">'
            f'  <div class="aux-task-info">'
            f'    <div class="aux-task-title">'
            f'      <span class="aux-task-name">Cadangan #{priority}</span>'
            f'    </div>'
            f'    <div class="mono-sub custom" style="font-weight:600">{model_name}</div>'
            f'    <div style="font-size:.68rem;color:var(--text-dim);margin-top:.1rem">{provider}</div>'
            f'  </div>'
            f'  <div style="display:flex;gap:6px;align-items:center">'
            f'    <button type="button" class="btn-action-sm" onclick="openFallbackPicker({idx}, \'{safe_model}\')">'
            f'      Ganti'
            f'    </button>'
            f'    <a class="btn-action-sm btn-action-danger" href="/remove-fallback-model?index={idx}">'
            f'      Hapus'
            f'    </a>'
            f'  </div>'
            f'</div>'
        )
    return "".join(rows)


def restart_bot() -> None:
    _invalidate_status_cache("gateway_platforms")
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/0")
    # Non-blocking: a drain (active task) can make this take a while, and
    # we don't want the HTTP request itself to hang waiting for it — the
    # resulting page shows its own countdown instead.
    p = subprocess.Popen(["systemctl", "--user", "restart", "hermes-gateway"], env=env)
    threading.Thread(target=p.wait, daemon=True).start()


def bot_action(action: str) -> None:
    _invalidate_status_cache("gateway_platforms")
    """start/stop hermes-gateway. Used for the STB-vs-new-server switch:
    only one hermes instance may poll a given Telegram bot token at a time,
    Telegram rejects a second concurrent poller (409). Enable/disable, not
    just stop/start, so it survives a reboot in the chosen state.

    Non-blocking (Popen), same reason as restart_bot() above: stopping can
    take up to TimeoutStopSec=210 to drain an in-flight message, and a
    blocking call here would hang the HTTP response for minutes."""
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/0")
    if action == "start":
        p = subprocess.Popen(["systemctl", "--user", "enable", "--now", "hermes-gateway"], env=env)
        threading.Thread(target=p.wait, daemon=True).start()
    elif action == "stop":
        p = subprocess.Popen(["systemctl", "--user", "disable", "--now", "hermes-gateway"], env=env)
        threading.Thread(target=p.wait, daemon=True).start()


def _router_ssh_argv(command: str) -> list[str]:
    """Build remote command; never silently fall back to local Docker."""
    host = get_9router_host()
    cmd = ["ssh", "-q", "-o", "ConnectTimeout=4",
           "-o", "ConnectionAttempts=1", "-o", "StrictHostKeyChecking=accept-new"]
    if not os.environ.get("ROUTER_SSH_PASSWORD"):
        cmd.insert(1, "-o")
        cmd.insert(2, "BatchMode=yes")
    if os.path.isfile(ROUTER_SSH_KEY):
        cmd += ["-i", ROUTER_SSH_KEY]
    # sh -c (no login shell) + TERM=dumb: login shells and tput-based prompts
    # emit 'tput: No value for $TERM' noise into the update log.
    cmd += [f"{ROUTER_SSH_USER}@{host}", "sh", "-c", "TERM=dumb export TERM; " + command]
    if os.environ.get("ROUTER_SSH_PASSWORD") and shutil.which("sshpass"):
        return ["sshpass", "-e"] + cmd
    return cmd


def _router_ssh(command: str, timeout: float = INFO_TIMEOUT):
    env = os.environ.copy()
    if env.get("ROUTER_SSH_PASSWORD"):
        env["SSHPASS"] = env["ROUTER_SSH_PASSWORD"]
    return subprocess.run(
        _router_ssh_argv(command), capture_output=True, text=True,
        timeout=timeout, env=env,
    )




def _fmt_dur(seconds: float) -> str:
    """Format duration: X hari Y jam Z mnt (unambiguous, no h/j collision)."""
    if seconds < 0:
        return ""
    s = int(seconds)
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days > 0:
        return f"{days} hari {hours} jam" if hours > 0 else f"{days} hari"
    if hours > 0:
        return f"{hours} jam {mins} mnt" if mins > 0 else f"{hours} jam"
    return f"{max(1, mins)} mnt"


def _parse_docker_started(raw: str) -> float | None:
    """Parse Docker inspect .State.StartedAt (ISO 8601) to unix epoch."""
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    # Docker returns: 2026-08-22T16:33:05.123456789Z or +00:00
    cleaned = re.sub(r"\.[0-9]+", "", cleaned)  # drop fractional seconds
    cleaned = cleaned.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(cleaned, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def _router_exec(command: str, timeout: float = INFO_TIMEOUT):
    """Run command locally if 9router is on localhost, else via SSH."""
    host = get_9router_host()
    if host in ("127.0.0.1", "localhost", "0.0.0.0"):
        return subprocess.run(
            ["sh", "-c", command], capture_output=True, text=True, timeout=timeout
        )
    return _router_ssh(command, timeout=timeout)


def _refresh_router_uptime() -> None:
    """Fetch 9router container StartedAt in background."""
    try:
        r = _router_exec(
            f"docker inspect {shlex.quote(ROUTER_CONTAINER)} "
            f"--format '{{{{.State.StartedAt}}}}'",
            timeout=INFO_TIMEOUT,
        )
        started_epoch = _parse_docker_started(r.stdout)
        if started_epoch:
            val = _fmt_dur(time.time() - started_epoch)
        else:
            val = ""
    except Exception:
        val = ""
    with _router_uptime_lock:
        _router_uptime_cache.update(at=time.time(), val=val)


def get_router_uptime() -> str:
    """Read 9router container uptime (Indonesian format, cached 30s)."""
    with _router_uptime_lock:
        stale = (time.time() - _router_uptime_cache["at"]) > 30
        val = _router_uptime_cache["val"]
        if stale:
            _router_uptime_cache["at"] = time.time()
            threading.Thread(target=_refresh_router_uptime, daemon=True).start()
    return val


def get_router_image_date() -> str:
    with _router_image_date_lock:
        if time.time() - _router_image_date_cache["at"] < 300:
            return _router_image_date_cache["val"]
    try:
        r = _router_exec(
            f"docker image inspect {shlex.quote(ROUTER_IMAGE)} --format '{{{{.Created}}}}'"
        )
        val = r.stdout.strip()[:10] or "?"
    except Exception:
        val = "?"
    with _router_image_date_lock:
        _router_image_date_cache.update(at=time.time(), val=val)
    return val


def get_router_version() -> str:
    """Read semver from the running 9router container."""
    try:
        r = _router_exec(
            f"docker exec {shlex.quote(ROUTER_CONTAINER)} cat /app/package.json"
        )
        return json.loads(r.stdout).get("version") or "?"
    except Exception:
        return "?"


def get_cached_router_version() -> str:
    try:
        with open(UPDATE_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f).get("version") or "?"
    except Exception:
        return "?"


def get_cached_router_info() -> dict:
    try:
        with open(UPDATE_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_local_image_digest() -> str:
    """The manifest-list digest of the locally-pulled image (the part after
    '@' in RepoDigests). This is exactly what Docker Hub returns as the
    content-digest for the :latest tag, so the two compare directly."""
    try:
        r = _router_exec(
            f"docker image inspect {shlex.quote(ROUTER_IMAGE)} --format '{{{{index .RepoDigests 0}}}}'"
        )
        out = r.stdout.strip()
        return out.split("@", 1)[1] if "@" in out else ""
    except Exception:
        return ""


def get_remote_image_digest() -> str:
    """HEAD the Docker Hub manifest for :latest and return its content digest.
    Anonymous pull token; HEAD (not GET) so it is not counted as a pull
    against Docker Hub's rate limit. The Accept header requests the multi-arch
    index media types so the digest matches the manifest-list digest stored
    locally in RepoDigests (not an arch-specific sub-manifest)."""
    try:
        token_url = (
            "https://auth.docker.io/token?service=registry.docker.io"
            f"&scope=repository:{DOCKERHUB_REPO}:pull"
        )
        with urllib.request.urlopen(token_url, timeout=UPDATE_NET_TIMEOUT) as resp:
            token = json.load(resp).get("token", "")
        if not token:
            return ""
        req = urllib.request.Request(
            f"https://registry-1.docker.io/v2/{DOCKERHUB_REPO}/manifests/{DOCKERHUB_TAG}",
            method="HEAD",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": (
                    "application/vnd.docker.distribution.manifest.list.v2+json, "
                    "application/vnd.oci.image.index.v1+json"
                ),
            },
        )
        with urllib.request.urlopen(req, timeout=UPDATE_NET_TIMEOUT) as resp:
            return resp.headers.get("docker-content-digest", "")
    except Exception:
        return ""


def get_router_release() -> dict:
    """Read 9router's own public release check from the detected host."""
    host = get_9router_host()
    port = get_9router_port()
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/version", timeout=INFO_TIMEOUT) as resp:
            data = json.load(resp)
        return {
            "current": data.get("currentVersion", "?"),
            "latest": data.get("latestVersion", "?"),
            "has_update": bool(data.get("hasUpdate")),
        }
    except Exception:
        return {"current": "?", "latest": "?", "has_update": False}


_router_patch_notes_cache = {"at": 0.0, "notes": []}
_router_patch_notes_lock = threading.Lock()


def get_9router_patch_notes() -> list[str]:
    """Return latest commits / release notes for 9router."""
    global _router_patch_notes_cache
    with _router_patch_notes_lock:
        if _router_patch_notes_cache["notes"] and (time.time() - _router_patch_notes_cache["at"] < 600):
            return list(_router_patch_notes_cache["notes"])
    cached = get_cached_router_info().get("patch_notes")
    if cached and len(cached) >= 15:
        with _router_patch_notes_lock:
            _router_patch_notes_cache = {"at": time.time(), "notes": list(cached)}
        return list(cached)
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/decolua/9router/commits?per_page=25",
            headers={"User-Agent": "Hermes-Panel"}
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            commits = json.load(resp)
        notes = []
        for c in commits:
            msg = c.get("commit", {}).get("message", "").strip()
            lines = msg.split("\n")
            for l in lines:
                l_s = l.strip()
                if (l_s.startswith("- ") or l_s.startswith("* ")) and len(l_s) > 4:
                    cleaned = re.sub(r"^\s*[-*]\s*", "", l_s)
                    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
                    cleaned = re.sub(r"\(#[0-9]+\)", "", cleaned).strip()
                    if cleaned and not cleaned.startswith("Merge"):
                        notes.append(cleaned)
                        if len(notes) >= 25:
                            break
            if len(notes) >= 25:
                break
            first = lines[0].strip()
            if any(first.startswith(p) for p in ("feat", "fix", "refactor", "perf", "docs", "chore")):
                notes.append(first)
                if len(notes) >= 25:
                    break
        if notes:
            with _router_patch_notes_lock:
                _router_patch_notes_cache = {"at": time.time(), "notes": list(notes)}
            return notes
    except Exception:
        pass
    if cached:
        return list(cached)
    return [
        "Support DeepSeek-V4.1-Flash and Xiaomi MiMo models",
        "Resolve 403 FreeTierError and 429 rate limits on OpenCode",
        "Add 1M-context toggle option on Claude Code interface"
    ]


def get_dockerhub_latest_tag() -> str:
    """Fetch the latest semver tag from Docker Hub for decolua/9router."""
    try:
        url = f"https://hub.docker.com/v2/repositories/{DOCKERHUB_REPO}/tags/?page_size=10"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=INFO_TIMEOUT) as resp:
            data = json.load(resp)
        tags = [r["name"] for r in data.get("results", []) if r.get("name") and r["name"] != "latest"]
        return tags[0] if tags else ""
    except Exception:
        return ""


def _parse_semver(v: str) -> list[int]:
    """Parse version string into integer list for comparison (e.g. '0.5.75' -> [0, 5, 75])."""
    nums = re.findall(r"\d+", str(v or ""))
    return [int(n) for n in nums] if nums else [0]


def _refresh_update_cache() -> None:
    """Compare Docker Hub digest, Docker Hub tags, and 9router release/version API."""
    global _update_refreshing
    try:
        local = get_local_image_digest()
        remote = get_remote_image_digest()
        release = get_router_release()
        docker_tag = get_dockerhub_latest_tag()

        digest_known = bool(local and remote)
        digest_differs = bool(digest_known and local != remote)

        installed_ver = release.get("current")
        if not installed_ver or installed_ver == "?":
            installed_ver = get_cached_router_version()

        # Compare installed version with latest tag on Docker Hub
        tag_is_newer = False
        if docker_tag and installed_ver and installed_ver != "?":
            tag_is_newer = _parse_semver(docker_tag) > _parse_semver(installed_ver)

        # Docker container can ONLY update if a new image exists on Docker Hub!
        has_docker_update = digest_differs or tag_is_newer

        # Check if npm has released a newer version before Docker Hub builds it
        npm_latest = release.get("latest", "")
        npm_ahead = False
        if npm_latest and npm_latest != "?" and installed_ver and installed_ver != "?":
            npm_ahead = _parse_semver(npm_latest) > _parse_semver(installed_ver)

        if has_docker_update:
            status = "available"
            target_ver = docker_tag or npm_latest or "baru"
        elif digest_known and local == remote:
            status = "current"
            target_ver = docker_tag or installed_ver
        elif installed_ver and installed_ver != "?":
            status = "current"
            target_ver = installed_ver
        else:
            status = "unknown"
            target_ver = "?"

        data = {
            "checked_at": time.time(),
            "status": status,
            "local": local,
            "remote": remote,
            "version": installed_ver,
            "latest_version": target_ver,
            "docker_tag": docker_tag,
            "npm_latest": npm_latest,
            "npm_ahead": npm_ahead,
            "digest_update": digest_differs,
            "tag_update": tag_is_newer,
            "patch_notes": get_9router_patch_notes(),
        }
        try:
            tmp = UPDATE_CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, UPDATE_CACHE_PATH)
        except OSError as e:
            sys.stderr.write(f"[panel] update cache save error: {e}\n")
    finally:
        with _update_refresh_lock:
            _update_refreshing = False


def get_update_status() -> str:
    """Return 'available' | 'current' | 'unknown' | 'checking'. Reads the cache
    file only (fast, never touches the network on the request thread); kicks off
    a background refresh when the cache is missing, stale, or 'unknown'."""
    global _update_refreshing
    status = "checking"
    fresh = False
    try:
        with open(UPDATE_CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        status = data.get("status", "checking")
        # Only definitive answers are cacheable; 'unknown' always re-checks so a
        # transient Docker Hub blip recovers on the next page load.
        fresh = status in ("current", "available") and (
            time.time() - data.get("checked_at", 0) < UPDATE_CACHE_TTL
        )
    except Exception:
        status, fresh = "checking", False

    if not fresh:
        with _update_refresh_lock:
            if not _update_refreshing:
                _update_refreshing = True
                threading.Thread(target=_refresh_update_cache, daemon=True).start()
    return status


def _router_update_command() -> str:
    container = shlex.quote(ROUTER_CONTAINER)
    image = shlex.quote(ROUTER_IMAGE)
    candidates = " ".join(shlex.quote(c) for c in ROUTER_COMPOSE_CANDIDATES if c)
    return (
        # Resolve the compose file ON THE TARGET HOST: CasaOS keeps the real
        # file under /var/lib/casaos/apps/<app>/, while the panel dir holds
        # only update.log/update-check.json. A missing -f path made `compose
        # up` fail, the `docker start` fallback reused the OLD image, and the
        # panel reported success while the container stayed on the old build.
        f"CF=''; for f in {candidates}; do [ -f \"$f\" ] && {{ CF=\"$f\"; break; }}; done; "
        "echo \"[1/5] host=$(hostname) compose=${CF:-none}\"; "
        f"before=$(docker inspect -f '{{{{.Image}}}}' {container} 2>&1); echo \"[2/5] before=$before\"; "
        "echo '[3/5] Mengunduh (pull) image baru...'; "
        f"if [ -n \"$CF\" ]; then docker compose -f \"$CF\" pull 2>&1 || docker pull {image} 2>&1; else docker pull {image} 2>&1; fi; pull_rc=$?; "
        "echo \"compose pull exit=$pull_rc\"; "
        "echo '[4/5] Membangun ulang container dari image baru...'; "
        f"if [ -n \"$CF\" ]; then docker compose -f \"$CF\" up -d --force-recreate 2>&1; up_rc=$?; else docker start {container} 2>&1; up_rc=$?; fi; "
        f"if [ \"$up_rc\" -ne 0 ]; then echo 'compose up gagal, fallback docker start'; docker start {container} 2>&1; fi; "
        "echo \"compose up exit=$up_rc\"; "
        f"after=$(docker inspect -f '{{{{.Image}}}}' {container} 2>&1); echo \"[5/5] after=$after\"; "
        "if [ -n \"$before\" ] && [ \"$before\" = \"$after\" ]; then echo 'changed=false'; else echo 'changed=true'; fi; "
        f"echo \"image_id=$(docker image inspect {image} --format '{{{{.Id}}}}' 2>&1)\"; "
        "[ \"$up_rc\" -eq 0 ] || exit \"$up_rc\"; "
        "[ \"$pull_rc\" -eq 0 ] || exit \"$pull_rc\""
    )


def get_clean_junk_result() -> dict:
    """Return the last clean junk result (from memory cache or file)."""
    global _clean_junk_result
    with _clean_junk_lock:
        if _clean_junk_result.get("at", 0) > 0:
            return dict(_clean_junk_result)
    if os.path.exists(CLEAN_JUNK_JSON):
        try:
            with open(CLEAN_JUNK_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
                with _clean_junk_lock:
                    _clean_junk_result = data
                return data
        except Exception:
            pass
    return {"status": "idle", "freed_bytes": 0, "freed_human": "0 B", "freed_mb": 0.0, "files_count": 0, "log": "", "at": 0.0}


def render_clean_junk_card() -> str:
    """Render the clean junk log card with total freed summary and dismiss button."""
    res = get_clean_junk_result()
    if not res.get("log") and res.get("at", 0) == 0:
        return ""
    freed_human = html.escape(str(res.get("freed_human", "0 B")))
    files_count = res.get("files_count", 0)
    log_text = html.escape(str(res.get("log", "")))

    return (
        f'<div id="clean-log-card" style="margin-top:0.85rem">'
        f'<div class="clean-log-header">'
        f'<div class="update-hint up" style="margin:0;flex:1;text-align:left;justify-content:space-between;flex-wrap:wrap;gap:6px">'
        f'  <span style="display:inline-flex;align-items:center;gap:6px;white-space:nowrap">'
        f'    {ICON_CHECK}<strong>Pembersihan Selesai</strong>'
        f'  </span>'
        f'  <span style="white-space:nowrap">'
        f'    Terhapus: <strong style="font-family:var(--font-mono);color:var(--success)">{freed_human}</strong> '
        f'    <span style="font-size:0.75rem;color:var(--text-dim)">({files_count} item)</span>'
        f'  </span>'
        f'</div>'
        f'<button type="button" class="btn btn-action-sm" '
        f"onclick=\"safeStore('setItem','cleanLogDismissed','1');var c=document.getElementById('clean-log-card');if(c)c.remove();if(window.syncLogUI)syncLogUI()\">"
        f'Sembunyikan Log</button>'
        f'</div>'
        f'<div class="logbox">{log_text or "(belum ada log)"}</div>'
        f'</div>'
    )


def cleanup_system_junk() -> dict:
    """Thoroughly clean update logs, package caches, temporary files, old rotated logs, and docker build/image caches."""
    log_lines = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S WIB")
    log_lines.append(f"=== [START] Pembersihan Sampah & Cache: {now_str} ===")

    total_freed = 0
    total_items = 0

    def _fmt(b: int) -> str:
        if b < 1024:
            return f"{b} B"
        elif b < 1024 * 1024:
            return f"{b / 1024:.1f} KB"
        elif b < 1024 * 1024 * 1024:
            return f"{b / (1024 * 1024):.1f} MB"
        else:
            return f"{b / (1024 * 1024 * 1024):.2f} GB"

    # 1. Truncate update logs & mcp stderr log (keep file descriptors valid)
    for logf in ["/opt/AppData/9router/update.log", "/root/.hermes/logs/update.log", "/root/.hermes/logs/mcp-stderr.log"]:
        try:
            if os.path.exists(logf):
                sz = os.path.getsize(logf)
                if sz > 0:
                    with open(logf, "w", encoding="utf-8") as f:
                        f.write("")
                    log_lines.append(f"[LOG] Mengosongkan {os.path.basename(logf)}: {_fmt(sz)} dibersihkan")
                    total_freed += sz
                    total_items += 1
                else:
                    log_lines.append(f"[LOG] {os.path.basename(logf)}: bersih (0 B)")
        except Exception as e:
            log_lines.append(f"[LOG] {os.path.basename(logf)} gagal: {e}")

    # 2. Remove old rotated hermes logs and shutdown/exit diags
    rotated_count = 0
    rotated_freed = 0
    for pattern in ["/root/.hermes/logs/*.log.[0-9]*", "/root/.hermes/logs/*-diag.log"]:
        for fpath in glob.glob(pattern):
            try:
                sz = os.path.getsize(fpath)
                os.remove(fpath)
                rotated_freed += sz
                rotated_count += 1
            except OSError:
                pass
    if rotated_count > 0:
        log_lines.append(f"[LOG] Menghapus {rotated_count} log arsip lama: {_fmt(rotated_freed)} dibersihkan")
        total_freed += rotated_freed
        total_items += rotated_count
    else:
        log_lines.append("[LOG] Log arsip lama: bersih (0 B)")

    # 3. Clean package caches (uv, pip, go-build, typescript, opencode)
    pkg_dirs = [
        ("Cache UV", "/root/.cache/uv"),
        ("Cache PIP", "/root/.cache/pip"),
        ("Cache Go", "/root/.cache/go-build"),
        ("Cache TypeScript", "/root/.cache/typescript"),
        ("Cache OpenCode", "/root/.cache/opencode"),
    ]
    for label, cdir in pkg_dirs:
        if os.path.exists(cdir):
            dir_freed = 0
            dir_items = 0
            try:
                for dp, _, fns in os.walk(cdir):
                    for fn in fns:
                        fp = os.path.join(dp, fn)
                        try:
                            sz = os.path.getsize(fp)
                            os.remove(fp)
                            dir_freed += sz
                            dir_items += 1
                        except OSError:
                            pass
            except Exception:
                pass
            if dir_items > 0:
                log_lines.append(f"[CACHE] {label}: {_fmt(dir_freed)} ({dir_items} file) dibersihkan")
                total_freed += dir_freed
                total_items += dir_items
            else:
                log_lines.append(f"[CACHE] {label}: bersih (0 B)")

    # 4. Clean Hermes runtime caches (terminal-output, spillover, scratch, delegation, web)
    hermes_cache_dirs = [
        ("Terminal Output", "/DATA/AppData/hermes-native/hermes-data/cache/terminal-output"),
        ("Spillover Data", "/DATA/AppData/hermes-native/hermes-data/cache/spillover"),
        ("Scratch Files", "/DATA/AppData/hermes-native/hermes-data/cache/scratch"),
        ("Delegation Subagent", "/DATA/AppData/hermes-native/hermes-data/cache/delegation"),
        ("Web Scrape Cache", "/DATA/AppData/hermes-native/hermes-data/cache/web"),
    ]
    for label, hdir in hermes_cache_dirs:
        if os.path.exists(hdir):
            h_freed = 0
            h_items = 0
            try:
                for dp, _, fns in os.walk(hdir):
                    for fn in fns:
                        fp = os.path.join(dp, fn)
                        try:
                            sz = os.path.getsize(fp)
                            os.remove(fp)
                            h_freed += sz
                            h_items += 1
                        except OSError:
                            pass
            except Exception:
                pass
            if h_items > 0:
                log_lines.append(f"[HERMES] Cache {label}: {_fmt(h_freed)} ({h_items} file) dibersihkan")
                total_freed += h_freed
                total_items += h_items
            else:
                log_lines.append(f"[HERMES] Cache {label}: bersih (0 B)")

    # 5. Clean /tmp screenshots and node cache
    tmp_freed = 0
    tmp_items = 0
    for p in glob.glob("/tmp/*.png"):
        try:
            sz = os.path.getsize(p)
            os.remove(p)
            tmp_freed += sz
            tmp_items += 1
        except OSError:
            pass
    if os.path.exists("/tmp/panel-screens"):
        for dp, _, fns in os.walk("/tmp/panel-screens"):
            for fn in fns:
                fp = os.path.join(dp, fn)
                try:
                    sz = os.path.getsize(fp)
                    os.remove(fp)
                    tmp_freed += sz
                    tmp_items += 1
                except OSError:
                    pass
    if tmp_items > 0:
        log_lines.append(f"[TEMP] Tangkapan layar /tmp: {_fmt(tmp_freed)} ({tmp_items} file) dibersihkan")
        total_freed += tmp_freed
        total_items += tmp_items
    else:
        log_lines.append("[TEMP] Tangkapan layar /tmp: bersih (0 B)")

    # 6. Docker builder & image prune
    try:
        r_b = subprocess.run(["docker", "builder", "prune", "-f"], capture_output=True, text=True, timeout=15)
        out_b = r_b.stdout.strip()
        log_lines.append(f"[DOCKER] Builder prune: {out_b or 'Selesai'}")
    except Exception as e:
        log_lines.append(f"[DOCKER] Builder prune gagal: {e}")

    try:
        r_i = subprocess.run(["docker", "image", "prune", "-f"], capture_output=True, text=True, timeout=15)
        out_i = r_i.stdout.strip()
        log_lines.append(f"[DOCKER] Image prune: {out_i or 'Selesai'}")
    except Exception as e:
        log_lines.append(f"[DOCKER] Image prune gagal: {e}")

    # 7. Systemd journal vacuum
    try:
        r_j = subprocess.run(["journalctl", "--vacuum-time=2d"], capture_output=True, text=True, timeout=15)
        lines_j = [l.strip() for l in r_j.stdout.strip().split("\n") if l.strip()]
        log_lines.append(f"[SYSTEM] Journal vacuum: {lines_j[0] if lines_j else 'Selesai'}")
    except Exception as e:
        log_lines.append(f"[SYSTEM] Journal vacuum gagal: {e}")

    # Summary
    freed_human = _fmt(total_freed)
    log_lines.append(f"=== [SELESAI] Total Sampah Terhapus: {freed_human} ({total_items} file/item) ===")

    full_log = "\n".join(log_lines)
    result_data = {
        "status": "success",
        "freed_bytes": total_freed,
        "freed_human": freed_human,
        "freed_mb": round(total_freed / (1024 * 1024), 2),
        "files_count": total_items,
        "log": full_log,
        "at": time.time(),
    }

    try:
        os.makedirs(os.path.dirname(CLEAN_JUNK_JSON), exist_ok=True)
        with open(CLEAN_JUNK_JSON, "w", encoding="utf-8") as f:
            json.dump(result_data, f)
        with open(CLEAN_JUNK_LOG, "w", encoding="utf-8") as f:
            f.write(full_log)
    except OSError as e:
        sys.stderr.write(f"[panel] clean junk save error: {e}\n")

    global _clean_junk_result
    with _clean_junk_lock:
        _clean_junk_result = result_data

    return result_data


def _append_router_update_log(log_path: str, message: str) -> None:
    """Best-effort append used by updater error paths. Never masks the root error."""
    try:
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(message)
    except OSError:
        pass


def update_router() -> None:
    """Update 9router on detected host; retain live log and real exit status."""
    global _router_updating, _router_update_result
    with _router_update_lock:
        if _router_updating:
            return
        _router_updating = True
        _router_update_result = {"status": "running", "exit_code": None,
                                 "changed": None, "summary": "Update berjalan…", "finished_at": 0.0}
    try:
        os.remove(UPDATE_CACHE_PATH)
    except OSError:
        pass
    log_path = f"{ROUTER_COMPOSE_DIR}/update.log"

    def _run():
        global _router_updating, _router_update_result
        try:
            with open(log_path, "w", encoding="utf-8") as log:
                host = get_9router_host()
                if host in ("127.0.0.1", "localhost", "0.0.0.0"):
                    cmd_args = ["sh", "-c", _router_update_command()]
                else:
                    cmd_args = _router_ssh_argv(_router_update_command())
                proc = subprocess.Popen(
                    cmd_args,
                    stdout=log, stderr=subprocess.STDOUT, text=True,
                )
                try:
                    rc = proc.wait(timeout=UPDATE_TIMEOUT)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                    log.write("\n[ERROR] timeout: update melebihi 300 detik\n")
                    rc = 124
            try:
                with open(log_path, encoding="utf-8", errors="replace") as lf:
                    log_text = lf.read()
            except Exception:
                log_text = ""
            changed = None
            if "changed=true" in log_text:
                changed = True
            elif "changed=false" in log_text:
                changed = False
            if rc == 0 and changed is True:
                summary = "Update nyata: image berubah, container direcreate"
            elif rc == 0 and changed is False:
                summary = "Pull sukses, image tidak berubah — sudah terbaru"
            elif rc == 0:
                summary = "Compose selesai, perubahan image belum terdeteksi"
            else:
                summary = f"Update gagal (exit {rc})"
            with _router_update_lock:
                _router_update_result = {"status": "success" if rc == 0 else "failed",
                                         "exit_code": rc, "changed": changed,
                                         "summary": summary, "finished_at": time.time()}
        except Exception as exc:
            _append_router_update_log(
                log_path, f"\n[ERROR] {type(exc).__name__}: {exc}\n"
            )
            with _router_update_lock:
                _router_update_result = {"status": "failed", "exit_code": 1,
                                         "changed": None,
                                         "summary": f"Update gagal: {type(exc).__name__}",
                                         "finished_at": time.time()}
        finally:
            with _router_update_lock:
                _router_updating = False
            threading.Thread(target=_refresh_update_cache, daemon=True).start()
    threading.Thread(target=_run, daemon=True).start()


def tail_update_log(n: int = UPDATE_LOG_TAIL) -> str:
    try:
        with open(f"{ROUTER_COMPOSE_DIR}/update.log", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except Exception:
        return ""


def get_router_update_result() -> dict:
    with _router_update_lock:
        return dict(_router_update_result)


def get_router_status() -> tuple[bool, float, str]:
    host = get_9router_host()
    port = get_9router_port()
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=INFO_TIMEOUT) as s:
            pass
        return True, time.monotonic() - start, host
    except Exception:
        return False, time.monotonic() - start, host


def get_ram_info() -> tuple[float, str]:
    try:
        info = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key] = int(rest.strip().split()[0])  # kB
        total = info["MemTotal"]
        avail = info["MemAvailable"]
        used_pct = (total - avail) / total * 100
        return used_pct, f"{used_pct:.1f}% ({avail // 1024} MB tersedia)"
    except Exception:
        return 0.0, "?"


_last_cpu_time: tuple[float, float] = (0.0, 0.0)
_cpu_percent_cache: float = 0.0
_cpu_lock = threading.Lock()
_cpu_primed = False


def _prime_cpu_sample() -> None:
    """Seed the /proc/stat baseline so the first real reading is live."""
    global _last_cpu_time, _cpu_primed
    try:
        with open("/proc/stat", "r") as f:
            line = f.readline()
        fields = [float(x) for x in line.strip().split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
        total = sum(fields)
        with _cpu_lock:
            _last_cpu_time = (total, idle)
            _cpu_primed = True
    except Exception:
        pass


try:
    _prime_cpu_sample()
except Exception:
    pass


def get_cpu_percent() -> float:
    """Calculate CPU usage percentage across intervals using /proc/stat."""
    global _last_cpu_time, _cpu_percent_cache
    try:
        with open("/proc/stat", "r") as f:
            line = f.readline()
        fields = [float(x) for x in line.strip().split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
        total = sum(fields)
        with _cpu_lock:
            prev_total, prev_idle = _last_cpu_time
            if prev_total > 0:
                diff_total = total - prev_total
                diff_idle = idle - prev_idle
                if diff_total > 0:
                    _cpu_percent_cache = max(0.0, min(100.0, ((diff_total - diff_idle) / diff_total) * 100.0))
            _last_cpu_time = (total, idle)
            return round(_cpu_percent_cache, 1)
    except Exception:
        return 0.0


def get_docker_metric(cname: str) -> tuple[str, str, float]:
    """Return (status, pid, mem_mb) for a Docker container."""
    try:
        r = subprocess.run(
            ["docker", "inspect", cname, "--format", "{{.State.Status}}\t{{.State.Pid}}\t{{.Id}}"],
            capture_output=True, text=True, timeout=INFO_TIMEOUT
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = r.stdout.strip().split("\t")
            if len(parts) >= 3:
                dst, dpid, cid = parts[0], parts[1], parts[2]
                mem = 0.0
                p_cg = f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/memory.current"
                if os.path.exists(p_cg):
                    try:
                        with open(p_cg) as f:
                            mem = round(int(f.read().strip()) / (1024 * 1024), 1)
                    except Exception:
                        pass
                if mem == 0.0 and dpid and dpid != "0":
                    try:
                        with open(f"/proc/{dpid}/status") as f:
                            for l in f:
                                if l.startswith("VmRSS:"):
                                    mem = round(int(l.split()[1]) / 1024, 1)
                                    break
                    except Exception:
                        pass
                return dst, dpid, mem
    except Exception:
        pass
    return "stopped", "0", 0.0


def get_process_list() -> list[dict]:
    """Inspect managed services and Docker containers for Linux service manager view."""
    procs = []

    # 1. Hermes Gateway (Bot Telegram)
    try:
        r = subprocess.run(
            ["systemctl", "--user", "show", "hermes-gateway", "--property=ActiveState,MainPID"],
            capture_output=True, text=True, timeout=INFO_TIMEOUT,
            env={**os.environ, "XDG_RUNTIME_DIR": "/run/user/0"}
        )
        props = dict(line.split("=", 1) for line in r.stdout.strip().split("\n") if "=" in line)
        st = props.get("ActiveState", "inactive")
        pid = props.get("MainPID", "0")
        mem = 0.0
        if pid and pid != "0":
            try:
                with open(f"/proc/{pid}/status") as f:
                    for l in f:
                        if l.startswith("VmRSS:"):
                            mem = round(int(l.split()[1]) / 1024, 1)
                            break
            except Exception:
                pass
        procs.append({
            "id": "hermes-gateway",
            "name": "Hermes Gateway (Messaging)",
            "kind": "Layanan Systemd",
            "status": "Berjalan" if st == "active" else "Berhenti",
            "is_active": (st == "active"),
            "pid": pid if pid != "0" else "-",
            "mem_mb": mem,
            "stop_url": "/bot-toggle",
            "start_url": "/bot-toggle",
            "restart_url": "/restart-bot",
        })
    except Exception:
        pass

    # 2. 9router AI Engine
    try:
        dst, dpid, dmem = get_docker_metric("9router")
        is_run = (dst.lower() == "running")
        procs.append({
            "id": "9router",
            "name": "9router AI Routing Engine",
            "kind": "Kontainer Docker",
            "status": "Berjalan" if is_run else "Berhenti",
            "is_active": is_run,
            "pid": dpid if is_run else "-",
            "mem_mb": dmem,
            "stop_url": "/process-action?service=9router&action=stop",
            "start_url": "/process-action?service=9router&action=start",
            "restart_url": "/process-action?service=9router&action=restart",
        })
    except Exception:
        pass

    # 3. Hermes Control Panel (:9120)
    try:
        cur_pid = os.getpid()
        panel_mem = 0.0
        try:
            with open(f"/proc/{cur_pid}/status") as f:
                for l in f:
                    if l.startswith("VmRSS:"):
                        panel_mem = round(int(l.split()[1]) / 1024, 1)
                        break
        except Exception:
            pass
        procs.append({
            "id": "hermes-panel",
            "name": "Panel Kontrol Hermes (:9120)",
            "kind": "Layanan Systemd",
            "status": "Berjalan",
            "is_active": True,
            "pid": str(cur_pid),
            "mem_mb": panel_mem,
            "restart_url": "/process-action?service=hermes-panel&action=restart",
        })
    except Exception:
        pass

    # 4. Hermes Dashboard (:9119)
    try:
        dash_active = service_active("hermes-dashboard")
        dash_mem = 0.0
        dash_pid = "-"
        if dash_active:
            r = subprocess.run(
                ["systemctl", "show", "hermes-dashboard", "--property=MainPID"],
                capture_output=True, text=True, timeout=INFO_TIMEOUT
            )
            for line in r.stdout.strip().split("\n"):
                if line.startswith("MainPID="):
                    p = line.split("=")[1]
                    if p and p != "0":
                        dash_pid = p
                        try:
                            with open(f"/proc/{p}/status") as f:
                                for l in f:
                                    if l.startswith("VmRSS:"):
                                        dash_mem = round(int(l.split()[1]) / 1024, 1)
                                        break
                        except Exception:
                            pass
        procs.append({
            "id": "hermes-dashboard",
            "name": "Dasbor Web Hermes (:9119)",
            "kind": "Layanan Systemd",
            "status": "Berjalan" if dash_active else "Berhenti",
            "is_active": dash_active,
            "pid": dash_pid,
            "mem_mb": dash_mem,
            "stop_url": "/toggle",
            "start_url": "/on",
            "restart_url": "/process-action?service=hermes-dashboard&action=restart",
        })
    except Exception:
        pass

    # 5. Cloudflared Tunnel
    try:
        dst, dpid, dmem = get_docker_metric("cloudflared")
        is_run = (dst.lower() == "running")
        procs.append({
            "id": "cloudflared",
            "name": "Cloudflared (Tunnel Aman)",
            "kind": "Kontainer Docker",
            "status": "Berjalan" if is_run else "Berhenti",
            "is_active": is_run,
            "pid": dpid if is_run else "-",
            "mem_mb": dmem,
            "stop_url": "/process-action?service=cloudflared&action=stop",
            "start_url": "/process-action?service=cloudflared&action=start",
            "restart_url": "/process-action?service=cloudflared&action=restart",
        })
    except Exception:
        pass

    # 6. Pi-hole DNS
    try:
        dst, dpid, dmem = get_docker_metric("pihole-pihole-1")
        is_run = (dst.lower() == "running")
        procs.append({
            "id": "pihole-pihole-1",
            "name": "Pi-hole (DNS Anti-Iklan)",
            "kind": "Kontainer Docker",
            "status": "Berjalan" if is_run else "Berhenti",
            "is_active": is_run,
            "pid": dpid if is_run else "-",
            "mem_mb": dmem,
            "stop_url": "/process-action?service=pihole-pihole-1&action=stop",
            "start_url": "/process-action?service=pihole-pihole-1&action=start",
            "restart_url": "/process-action?service=pihole-pihole-1&action=restart",
        })
    except Exception:
        pass

    return procs


def _render_processes_table_uncached() -> str:
    """Render process table for running services & containers."""
    procs = get_process_list()
    rows = []
    for p in procs:
        badge_cls = "badge-up" if p["is_active"] else "badge-down"
        status_text = html.escape(p["status"])
        name = html.escape(p["name"])
        kind = html.escape(p.get("kind", ""))
        pid = html.escape(str(p.get("pid", "-")))
        mem = f"{p['mem_mb']:.0f} MB" if isinstance(p["mem_mb"], (int, float)) and p["mem_mb"] > 0 else "-"

        actions = []
        if p["is_active"] and p.get("stop_url"):
            actions.append(f'<a href="{p["stop_url"]}" class="btn-end-task">Hentikan</a>')
        elif not p["is_active"] and p.get("start_url"):
            actions.append(f'<a href="{p["start_url"]}" class="btn-start-task">Nyalakan</a>')
        if p.get("restart_url"):
            actions.append(f'<a href="{p["restart_url"]}" class="btn-restart-task">Mulai Ulang</a>')

        act_html = " ".join(actions) if actions else "-"
        rows.append(
            f'<tr>'
            f'  <td class="task-name-cell" data-c="layanan">'
            f'    <div><div style="font-weight:600;color:var(--text)">{name}</div>'
            f'    <div style="font-size:0.72rem;color:var(--text-dim)">{kind}</div></div>'
            f'  </td>'
            f'  <td data-c="status"><span class="td-label">Status</span><span class="badge {badge_cls}">{status_text}</span></td>'
            f'  <td data-c="pid" style="text-align:right;font-family:var(--font-mono);font-size:0.75rem;font-variant-numeric:tabular-nums"><span class="td-label">PID</span><span class="td-val">{pid}</span></td>'
            f'  <td data-c="mem" style="text-align:right;font-family:var(--font-mono);font-size:0.75rem;font-weight:600;font-variant-numeric:tabular-nums"><span class="td-label">Memori</span><span class="td-val">{mem}</span></td>'
            f'  <td data-c="aksi" style="text-align:right;white-space:nowrap">{act_html}</td>'
            f'</tr>'
        )

    return (
        f'<div class="task-table-wrap" id="process-table">'
        f'<table class="task-table">'
        f'<thead>'
        f'  <tr>'
        f'    <th>Tugas / Layanan</th>'
        f'    <th>Status</th>'
        f'    <th style="text-align:right">PID</th>'
        f'    <th style="text-align:right">Memori</th>'
        f'    <th style="text-align:right">Aksi</th>'
        f'  </tr>'
        f'</thead>'
        f'<tbody>'
        f'  {"".join(rows)}'
        f'</tbody>'
        f'</table>'
        f'</div>'
    )


def render_processes_table() -> str:
    return _ttl_cached(
        "processes_table",
        5.0,
        _render_processes_table_uncached,
    )


def _probe_emmc_health() -> tuple[str, str]:
    """Check eMMC wear data from any mmcblk device that exposes eMMC sysfs fields."""
    readings = []

    for device_dir in glob.glob("/sys/block/mmcblk*/device"):
        try:
            with open(os.path.join(device_dir, "life_time")) as f:
                values = f.read().split()[:2]

            if len(values) < 2:
                continue

            wear = max(int(value, 16) * 10 for value in values)

            with open(os.path.join(device_dir, "pre_eol_info")) as f:
                eol = int(f.read().strip(), 16)

            readings.append((wear, eol))
        except (OSError, ValueError):
            continue

    if not readings:
        return "", "N/A"

    wear, eol = max(readings, key=lambda item: (item[1], item[0]))

    cls = "down" if (wear >= 80 or eol == 3) else (
        "warn" if (wear >= 60 or eol == 2) else "up"
    )
    status = "Urgent" if eol == 3 else (
        "Warn" if eol == 2 else "Normal"
    )

    return cls, f"{wear}% aus ({status})"


def get_emmc_health() -> tuple[str, str]:
    return _ttl_cached(
        "emmc_health",
        60.0,
        _probe_emmc_health,
    )


def get_zram_info() -> str:
    """Read zram compression status."""
    try:
        with open("/sys/block/zram0/mm_stat") as f:
            orig, compr = [int(x) for x in f.read().split()[:2]]
        if orig == 0:
            return "0 MB"
        ratio = (orig / compr) if compr else 1.0
        return f"{orig // 1048576}MB ({ratio:.1f}x)"
    except Exception:
        return "–"


def get_cpu_temp() -> tuple[float, str]:
    """CPU temperature in °C from the thermal zone."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            millideg = int(f.read().strip())
        c = millideg / 1000.0
        return c, f"{c:.0f}°C"
    except Exception:
        return 0.0, "?"


def _probe_disk_stats() -> tuple[str, float]:
    """Scan real mounts once and return (display_text, highest_used_percent)."""
    skip_prefixes = ("/var/lib/docker", "/boot")
    skip_fstypes = {"tmpfs", "devtmpfs", "squashfs", "sysfs", "proc", "devpts"}
    parts = []
    worst = 0.0
    mounts = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts_line = line.split()
                if len(parts_line) < 3:
                    continue
                dev, mp, fstype = parts_line[:3]
                if fstype in skip_fstypes:
                    continue
                if any(mp.startswith(p) for p in skip_prefixes):
                    continue
                if "/rootfs/" in mp:  # docker overlay
                    continue
                if mp in [m[1] for m in mounts]:
                    continue  # duplicate mount point
                mounts.append((dev, mp, fstype))
    except Exception:
        return "?", 0.0

    # Assign labels: root = ROOT, others by device type
    for dev, mp, _ in mounts:
        try:
            st = os.statvfs(mp)
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            if total == 0:
                continue
            used_pct = (total - free) / total * 100
            worst = max(worst, used_pct)
            total_gb = total / (1024 ** 3)
            used_gb = (total - free) / (1024 ** 3)
            if mp == "/":
                label = "ROOT"
            elif mp == "/DATA":
                label = "DATA"
            elif mp.startswith("/mnt/"):
                label = mp.split("/")[-1].upper()
            else:
                label = mp
            parts.append(f"{label} {used_pct:.0f}% ({used_gb:.1f}/{total_gb:.1f} GB)")
        except Exception:
            pass
    display_text = " · ".join(parts) if parts else "?"
    return display_text, worst


def _get_disk_stats() -> tuple[str, float]:
    return _ttl_cached("disk_stats", 10.0, _probe_disk_stats)


def get_disk_info() -> str:
    return _get_disk_stats()[0]


def get_disk_pct() -> float:
    return _get_disk_stats()[1]


def get_uptime() -> str:
    """Human-readable uptime from /proc/uptime (hari/jam/mnt)."""
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        days, rem = divmod(int(secs), 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        if days > 0:
            return f"{days} hari {hours} jam {minutes} mnt"
        return f"{hours} jam {minutes} mnt"
    except Exception:
        return "?"


def _probe_server_ips() -> tuple[str, str]:
    """Return (tailscale_ipv4, lan_ipv4). Empty string if not available."""
    ts_ip, lan_ip = "", ""
    try:
        r = subprocess.run(["tailscale", "ip", "-4"],
                           capture_output=True, text=True, timeout=INFO_TIMEOUT)
        if r.returncode == 0:
            ts_ip = r.stdout.strip().split("\n")[0]
    except Exception:
        pass
    try:
        r = subprocess.run(["hostname", "-I"],
                           capture_output=True, text=True, timeout=INFO_TIMEOUT)
        if r.returncode == 0:
            for ip in r.stdout.strip().split():
                if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
                    lan_ip = ip
                    break
            if not lan_ip and r.stdout.strip():
                lan_ip = r.stdout.strip().split()[0]
    except Exception:
        pass
    return ts_ip, lan_ip


def get_server_ips() -> tuple[str, str]:
    return _ttl_cached(
        "server_ips",
        30.0,
        _probe_server_ips,
    )


_internet_status_cache = {"at": 0.0, "online": False, "ms": 0.0, "target": "1.1.1.1"}
_internet_status_lock = threading.Lock()
INTERNET_CACHE_TTL = 8.0  # seconds


def get_internet_status() -> tuple[bool, float, str]:
    """Check internet connectivity by reaching public DNS IPs (1.1.1.1 / 8.8.8.8) on port 53.
    Returns (is_online, latency_ms, target_host). Thread-safe with 8s TTL cache."""
    now = time.monotonic()
    with _internet_status_lock:
        if now - _internet_status_cache["at"] < INTERNET_CACHE_TTL:
            return (
                _internet_status_cache["online"],
                _internet_status_cache["ms"],
                _internet_status_cache.get("target", "1.1.1.1"),
            )

    online = False
    latency_ms = 0.0
    active_target = "1.1.1.1"
    for target in [("1.1.1.1", 53), ("8.8.8.8", 53)]:
        t0 = time.monotonic()
        try:
            with socket.create_connection(target, timeout=1.2) as s:
                pass
            latency_ms = (time.monotonic() - t0) * 1000
            online = True
            active_target = target[0]
            break
        except Exception:
            continue

    with _internet_status_lock:
        _internet_status_cache["at"] = now
        _internet_status_cache["online"] = online
        _internet_status_cache["ms"] = latency_ms
        _internet_status_cache["target"] = active_target

    return online, latency_ms, active_target


def get_rate_limited_providers() -> list[dict]:
    """Retrieve detailed recent errors for accounts/providers with actual account name."""
    details = []
    try:
        conn = sqlite3.connect(f"file:{ROUTER_DB_PATH}?mode=ro", uri=True, timeout=INFO_TIMEOUT)
        cur = conn.cursor()
        cur.execute("select id, provider, name, email, data from providerConnections")
        rows = cur.fetchall()
        conn.close()
    except Exception:
        return details

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=RATE_LIMIT_RECENCY_MINUTES)
    for cid, provider, col_name, col_email, data in rows:
        try:
            d = json.loads(data)
        except Exception:
            continue
        err = d.get("lastError")
        err_at = d.get("lastErrorAt")
        if not (err and err_at):
            continue
        try:
            ts = datetime.fromisoformat(err_at.replace("Z", "+00:00"))
        except Exception:
            continue
        if ts > cutoff:
            acc_name = col_name or col_email or d.get("name") or d.get("email") or cid[:8]
            # Convert to WIB (UTC+7, 24-hour format HH:MM:SS)
            wib_ts = ts + timedelta(hours=7)
            wib_str = wib_ts.strftime("%H:%M:%S")
            details.append({
                "id": cid[:8],
                "name": str(acc_name),
                "provider": provider,
                "error_at": wib_str,
                "error": str(err).strip()
            })
    return details


def build_fragments() -> dict:
    """Compute every dynamic HTML fragment (plus a couple of raw flags) in one
    place, so the initial full-page render and the /api/status poll can never
    drift apart. Returns a JSON-serializable dict."""
    dash_active = service_active(SERVICE)
    gw_active = service_active("hermes-gateway", user=True)
    router_up, router_ms, router_host = get_router_status()
    router_uptime = get_router_uptime() if router_up else ""
    model = get_current_model()
    ram_pct, ram_text = get_ram_info()
    ram_class = "down" if ram_pct >= 90 else ("warn" if ram_pct >= 75 else "up")
    cpu_temp, cpu_temp_text = get_cpu_temp()
    temp_class = "down" if cpu_temp >= 80 else ("warn" if cpu_temp >= 65 else "up")
    disk_text = get_disk_info()
    disk_pct = get_disk_pct()
    disk_class = "down" if disk_pct >= 95 else ("warn" if disk_pct >= 85 else "up")
    uptime_text = get_uptime()
    ts_ip, lan_ip = get_server_ips()
    internet_up, internet_ms, internet_target = get_internet_status()
    emmc_cls, emmc_text = get_emmc_health()
    zram_text = get_zram_info()
    gw_info = get_gateway_info()
    gw_summary_text, gw_summary_badge_class, cell_gw_platforms = get_gateway_platforms_summary()
    gateway_list_block = render_gateway_platforms_html()
    gateway_log_card = render_gateway_log_card()
    updating = _router_updating
    all_models = get_available_models_cached() if router_up else {}
    all_flat_models = [m for m_list in all_models.values() for m in m_list]
    model_not_listed = bool(all_flat_models) and model not in all_flat_models

    def cell(cls: str, text: str, title: str = "") -> str:
        t_attr = f' title="{html.escape(title)}"' if title else ""
        return f'<span class="value {cls}"{t_attr}><span class="dot {cls}"></span>{text}</span>'

    # Format providers info
    all_providers = get_all_configured_providers()
    prov_badges = []
    for p in all_providers:
        pname = html.escape(p["name"])
        ptype = p["type"]
        cls_name = "up" if ptype == "primary" else "warn"
        prov_badges.append(f'<span class="model-chip" style="font-size:.72rem;padding:.15rem .45rem"><span class="dot {cls_name}" style="width:5px;height:5px;margin-right:.3rem"></span>{pname}</span>')
    prov_html = " ".join(prov_badges) or '<span class="value">9router</span>'

    cells = {
        "dash": f'<span class="value" style="font-size:.72rem;color:var(--text-dim)">citra {get_router_image_date()}</span>' if router_up else '<span class="value down" style="font-size:.72rem">Offline</span>',
        "bot": cell("up" if gw_active else "down", "Aktif" if gw_active else "Mati"),
        "gw": f'<span class="value up">{gw_info}</span>' if gw_active else f'<span class="value down">{gw_info}</span>',
        "model": f'<span class="value {"warn" if model_not_listed else ""}" title="{"Model aktif tidak muncul di daftar model" if model_not_listed else ""}">{html.escape(model)}{" ⚠ tidak terdaftar" if model_not_listed else ""}</span>',
        "providers": f'<div style="display:flex;gap:.3rem;flex-wrap:wrap">{prov_html}</div>',
        "router": cell(
            "up" if router_up else "down",
            (f"Terhubung · {router_host} ({router_ms*1000:.0f}ms)"
             + (f" · aktif {router_uptime}" if router_uptime else "")
             if router_up else f"Tidak terhubung ({router_host})"),
        ),
        "ram": f'<span class="value {ram_class}">{ram_text}</span>',
        "zram": f'<span class="value">{zram_text}</span>',
        "temp": f'<span class="value {temp_class}">{cpu_temp_text}</span>',
        "emmc": f'<span class="value {emmc_cls}">{emmc_text}</span>',
        "disk": f'<span class="value {disk_class}">{disk_text}</span>',
        "uptime": f'<span class="value">{uptime_text}</span>',
        "lan": f'<span class="value">{lan_ip or "–"}</span>',
        "ts": f'<span class="value">{ts_ip or "–"}</span>',
        "internet": cell(
            "up" if internet_up else "down",
            f"Terhubung · {internet_target} ({internet_ms:.0f}ms)" if internet_up else f"Terputus ({internet_target})",
            title=f"Server tes: {internet_target} (DNS port 53, fallback: 8.8.8.8)",
        ),
        "gw_platforms": cell_gw_platforms,
    }

    fetch_btn = (
        f'<a class="toggle restart" style="width:auto;flex:1;min-height:38px;padding:.4rem .8rem;font-size:.76rem" href="/fetch-models">'
        f'{ICON_REFRESH}Ambil Daftar Model</a>'
    )
    reload_btn = (
        f'<a class="toggle restart" style="width:auto;flex:1;min-height:38px;padding:.4rem .8rem;font-size:.76rem" href="/reload-panel-config">'
        f'{ICON_REFRESH}Muat Ulang Konfig</a>'
    )
    action_hdr = (
        f'<div style="display:flex;gap:8px;align-items:center;margin-bottom:14px;width:100%">'
        f'{fetch_btn}{reload_btn}</div>'
    )

    def render_chips_group(title: str, models: list[str]) -> str:
        if not models:
            return ""
        chips = []
        for mm in models:
            display_name = mm
            is_free = False
            if display_name.endswith(":free"):
                display_name = display_name[:-5]
                is_free = True
            elif display_name.endswith("-free"):
                display_name = display_name[:-5]
                is_free = True

            badge_html = '<span class="model-chip-badge">GRATIS</span>' if is_free else ""

            # Vendor prefix styling
            if "/" in display_name:
                parts = display_name.split("/", 1)
                formatted_label = f'<span style="opacity:.45">{html.escape(parts[0])}/</span>{html.escape(parts[1])}'
            else:
                formatted_label = html.escape(display_name)

            chip_inner = (
                f'<span class="model-chip-content">'
                f'{(ICON_CHECK if mm == model else "")}'
                f'<span class="model-chip-name">{formatted_label}</span>'
                f'</span>'
                f'{badge_html}'
            )

            if mm == model:
                chips.append(f'<span class="model-chip active" title="{html.escape(mm)}">{chip_inner}</span>')
            else:
                model_query = quote(mm, safe="")
                chips.append(
                    f'<a class="model-chip" title="{html.escape(mm)}" href="/switch-model?model={model_query}">{chip_inner}</a>'
                )
        return (
            f'<div class="model-group" style="margin-bottom:1.15rem">'
            f'<div class="model-group-title">{html.escape(title)}</div>'
            f'<div class="models-grid">{"".join(chips)}</div>'
            f'</div>'
        )

    if router_up or all_flat_models:
        groups_html = "".join(render_chips_group(gname, mlist) for gname, mlist in all_models.items() if mlist)
        model_chips = action_hdr + groups_html
    else:
        model_chips = action_hdr + '<span class="model-chip">Belum ada daftar model — 9router tidak terhubung</span>'

    rl_errors = get_rate_limited_providers()
    if rl_errors:
        rows = "".join(
            f'<div class="rl-row" style="flex-direction:column;align-items:flex-start;gap:.25rem;padding:.5rem 0;border-bottom:1px solid rgba(255,255,255,0.06)">'
            f'<div style="display:flex;justify-content:space-between;width:100%">'
            f'<span class="rl-warn" style="font-weight:600">{html.escape(item["provider"].upper())} · {html.escape(item["name"])}</span>'
            f'<span style="font-size:.72rem;color:var(--text-dim);font-family:var(--font-mono)">{item["error_at"]} WIB</span>'
            f'</div>'
            f'<div style="font-size:.75rem;color:#fca5a5;font-family:var(--font-mono);word-break:break-all;line-height:1.3">{html.escape(item["error"][:180])}</div>'
            f'</div>'
            for item in rl_errors
        )
        rate_limit_card = RATE_LIMIT_CARD.format(rows=rows, icon=ICON_ALERT_TRIANGLE)
    else:
        rate_limit_card = ""

    # Keep the remote Docker log visible during and after update. The exit code
    # is rendered so a green-looking button cannot masquerade as success.
    update_result = get_router_update_result()
    if updating:
        update_block = '<div class="update-hint">{}</div>'.format(
            f'{ICON_CLOCK}Menjalankan pembaruan 9router di {html.escape(router_host)}…'
        )
        log_card = render_log_card(tail_update_log(), update_result)
    else:
        log_card = render_log_card(tail_update_log(), update_result) if update_result.get("status") in ("success", "failed") else ""
        upd = get_update_status()
        cached_info = get_cached_router_info()
        installed_version = cached_info.get("version") or get_cached_router_version()
        latest_version = cached_info.get("latest_version", "")
        npm_ahead = cached_info.get("npm_ahead", False)
        npm_latest = cached_info.get("npm_latest", "")

        version_label = f"v{installed_version} &rarr; v{latest_version}" if (latest_version and latest_version != "?" and latest_version != installed_version) else f"v{installed_version}"
        cek_btn = (f'<a class="toggle restart" href="/check-update">'
                   f'{ICON_REFRESH}Cek Pembaruan 9router</a>')
        router_notes = get_9router_patch_notes()
        router_patch_notes_html = render_patch_notes_block("9router", router_notes)
        if upd == "available":
            update_block = (
                f'<a class="toggle" style="background:linear-gradient(135deg,var(--warning),#d9860bcc);'
                f'color:#141922" href="/update-router">'
                f'{ICON_ARROW_UP_CIRCLE}Pembaruan 9router tersedia ({version_label})</a>'
                + cek_btn
                + router_patch_notes_html
            )
        elif upd == "current":
            npm_note = ""
            if npm_ahead and npm_latest and npm_latest != installed_version:
                npm_note = f' <span style="font-size:0.75rem;color:var(--text-dim)">(v{npm_latest} rilis di npm, menunggu build image Docker Hub)</span>'
            update_block = (
                f'<div class="update-hint">{ICON_CHECK}9router sudah versi terbaru di Docker Hub'
                f' (v{installed_version}){npm_note}</div>' + cek_btn + router_patch_notes_html
            )
        elif upd == "unknown":
            update_block = (
                f'<div class="update-hint">{ICON_ALERT_TRIANGLE}Gagal cek pembaruan Docker Hub — '
                f'<a href="/update-router">paksa perbarui</a></div>' + cek_btn + router_patch_notes_html
            )
        else:  # checking — the auto-poll picks up the settled result
            update_block = f'<div class="update-hint">{ICON_CLOCK}Mengecek pembaruan 9router…</div>' + router_patch_notes_html

    # Hermes update status
    hermes_upd = get_hermes_update()
    hermes_behind = hermes_upd.get("behind", 0)
    hermes_local = hermes_upd.get("local", "?")
    hermes_status = hermes_upd.get("status", "unknown")
    hermes_result = get_hermes_update_result()
    hermes_log = html.escape(redact_sensitive_tokens(tail_hermes_update_log()))
    hermes_notes = get_hermes_patch_notes()
    hermes_patch_notes_html = render_patch_notes_block("Hermes Agent", hermes_notes)

    if hermes_result.get("running"):
        cells["hermes"] = f'<span class="value warn">Update berjalan…</span>'
        hermes_update_block = (
            f'<div class="update-hint">{ICON_CLOCK}Updater Hermes sedang berjalan live…</div>'
            + hermes_patch_notes_html
        )
    elif hermes_status == "available":
        cells["hermes"] = f'<span class="value warn">{html.escape(hermes_local)} ({hermes_behind} pembaruan tersedia)</span>'
        hermes_update_block = (
            f'<a class="toggle" style="background:linear-gradient(135deg,var(--warning),#d9860bcc);color:#141922" href="/update-hermes">'
            f'{ICON_ARROW_UP_CIRCLE}Perbarui Hermes ({hermes_behind} komit)</a>'
            f'<a class="toggle restart" href="/check-hermes-update">{ICON_REFRESH}Cek Pembaruan Hermes</a>'
            + hermes_patch_notes_html
        )
    elif hermes_status == "current":
        cells["hermes"] = f'<span class="value up">{html.escape(hermes_local)} (terbaru)</span>'
        hermes_update_block = (
            f'<div class="update-hint">{ICON_CHECK}Hermes sudah versi terbaru ({html.escape(hermes_local)})</div>'
            f'<a class="toggle restart" href="/check-hermes-update">{ICON_REFRESH}Cek Pembaruan Hermes</a>'
            + hermes_patch_notes_html
        )
    else:
        cells["hermes"] = f'<span class="value">{html.escape(hermes_local)}</span>'
        hermes_update_block = f'<div class="update-hint">{ICON_CLOCK}Mengecek pembaruan Hermes…</div>' + hermes_patch_notes_html

    # Permanently render Hermes log card whenever log file exists or update result exists
    hermes_log_card = ""
    if hermes_log or hermes_result.get("status") != "idle" or os.path.exists("/root/.hermes/logs/update.log"):
        cls = "up" if hermes_result.get("status") == "success" else ("down" if hermes_result.get("status") == "failed" else "warn")
        summary_text = html.escape(hermes_result.get("summary", "")) if hermes_result.get("summary") else ("Pembaruan sedang berjalan…" if hermes_result.get("running") else "Log Terakhir Pembaruan Hermes")
        hermes_log_card = (
            f'<div id="hermes-log-card" style="margin-top:0.8rem">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:0.4rem">'
            f'<div class="update-hint {cls}" style="margin:0;flex:1">{summary_text}</div>'
            f'<button type="button" class="btn" style="width:auto;padding:0.2rem 0.6rem;font-size:0.72rem;margin:0" '
            f"onclick=\"safeStore('setItem','hermesLogDismissed','1');var c=document.getElementById('hermes-log-card');if(c)c.remove();if(window.syncLogUI)syncLogUI()\">"
            f'Sembunyikan Log</button>'
            f'</div>'
            f'<div class="logbox">{hermes_log or "(belum ada log)"}</div>'
            f'</div>'
        )
    hermes_update_block += f'<div id="hermes-log-slot">{hermes_log_card}</div>'

    quick_links_block = (
        (get_open_block_active() if dash_active else OPEN_BLOCK_INACTIVE)
        + (f'<a class="open" href="{get_9router_public_url()}" '
           f'target="_blank">{ICON_EXTERNAL_LINK}Buka 9router</a>')
    )

    dash_label = "Matikan Dasbor" if dash_active else "Nyalakan Dasbor"
    dash_toggle_class = "btn-off" if dash_active else "btn-on"
    bot_label = "Matikan Gateway" if gw_active else "Nyalakan Gateway"
    bot_toggle_class = "btn-off" if gw_active else "btn-on"

    dash_bot_btns_block = (
        f'<a class="toggle {dash_toggle_class}" id="btn-dash-toggle" href="/toggle">{ICON_POWER}{dash_label}</a>'
        f'<a class="toggle {bot_toggle_class}" id="btn-bot-toggle" href="/bot-toggle">{ICON_POWER}{bot_label}</a>'
        f'<a class="toggle restart" href="/restart-bot">{ICON_REFRESH}Mulai Ulang Gateway</a>'
        f'<a class="toggle restart" href="/clean-junk">{ICON_TRASH}Bersihkan Sampah</a>'
    )

    cpu_pct = get_cpu_percent()
    try:
        load1, load5, load15 = os.getloadavg()
        cell_load = f"{load1:.2f}, {load5:.2f}, {load15:.2f}"
    except Exception:
        cell_load = "?"

    return {
        "cells": cells,
        "model_chips": model_chips,
        "rate_limit_card": rate_limit_card,
        "update_block": update_block,
        "hermes_update_block": hermes_update_block,
        "log_card": log_card,
        "hermes_log_card": hermes_log_card,
        "clean_junk_card": render_clean_junk_card(),
        "quick_links_block": quick_links_block,
        "dash_bot_btns_block": dash_bot_btns_block,
        "aux_tasks_block": render_aux_tasks_block(),
        "backup_models_block": render_backup_models_block(),
        "processes_table": render_processes_table(),
        "gateway_list_block": gateway_list_block,
        "gateway_log_card": gateway_log_card,
        "gw_summary_text": gw_summary_text,
        "gw_summary_badge_class": gw_summary_badge_class,
        "cpu_pct": cpu_pct,
        "ram_pct": round(ram_pct, 1),
        "cell_load": cell_load,
        "updating": updating,
        "dash_active": dash_active,
        "gw_active": gw_active,
    }


VALID_TABS = {"status", "performance", "control", "auxiliary"}


def _to_bool(val, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if not s:
        return default
    return s in ("1", "true", "yes", "on")


def build_status_page(just: str = "", active_tab: str = "") -> str:
    if active_tab not in VALID_TABS:
        active_tab = ""
    frag = build_fragments()
    dash_active = frag["dash_active"]
    gw_active = frag["gw_active"]

    if just == "start":
        countdown_block = COUNTDOWN_BLOCK.format(
            seconds=STARTUP_COUNTDOWN_SECONDS,
            message="Server sedang menyala, halaman ini refresh otomatis...",
        )
    elif just == "model":
        countdown_block = (
            f'<div class="hint">{ICON_CHECK}Default model diganti. Berlaku untuk '
            'percakapan baru (/new) — obrolan yang sedang aktif tetap '
            'pakai model lama sampai direset.</div>'
        )
    elif just == "aux":
        countdown_block = (
            f'<div class="hint">{ICON_CHECK}Model tugas tambahan berhasil diperbarui! '
            'Konfigurasi langsung tersimpan ke config.yaml.</div>'
        )
    elif just == "aux-reset":
        countdown_block = (
            f'<div class="hint">{ICON_CHECK}Semua model tugas tambahan dikembalikan ke otomatis!</div>'
        )
    elif just == "fallback":
        countdown_block = (
            f'<div class="hint">{ICON_CHECK}Model cadangan berhasil disimpan ke config.yaml!</div>'
        )
    elif just == "fallback-del":
        countdown_block = (
            f'<div class="hint">{ICON_CHECK}Model cadangan berhasil dihapus.</div>'
        )
    elif just == "restart":
        countdown_block = COUNTDOWN_BLOCK.format(
            seconds=STARTUP_COUNTDOWN_SECONDS,
            message="Bot Telegram sedang mulai ulang...",
        )
    elif just == "bot-off":
        countdown_block = (
            f'<div class="hint">{ICON_PAUSE}Bot Telegram dimatikan di STB ini. '
            'Aman dipakai kalau instance lain (server baru) yang sedang aktif.</div>'
        )
    elif just == "cleaned":
        res = get_clean_junk_result()
        freed_str = res.get("freed_human", "0 B")
        files_str = f" ({res.get('files_count', 0)} item)" if res.get("files_count") else ""
        countdown_block = (
            f'<div class="hint">{ICON_CHECK}Pembersihan berhasil! Total sampah terhapus: '
            f'<strong>{freed_str}</strong>{files_str}. Log rinci ditampilkan di bawah tombol.</div>'
        )
    elif just == "hermes-updating":
        countdown_block = COUNTDOWN_BLOCK.format(
            seconds=60,
            message="Hermes sedang update dan restart...",
        )
    else:
        countdown_block = ""

    models_dict = get_available_models_cached()
    available_models_json = json.dumps(models_dict).replace("</", "<\\/")

    return PAGE.format(
        icon_hermes_logo=ICON_HERMES_LOGO,
        cell_dash=frag["cells"]["dash"],
        cell_bot=frag["cells"]["bot"],
        cell_gw=frag["cells"]["gw"],
        cell_model=frag["cells"]["model"],
        cell_providers=frag["cells"]["providers"],
        cell_router=frag["cells"]["router"],
        cell_hermes=frag["cells"]["hermes"],
        cell_ram=frag["cells"]["ram"],
        cell_zram=frag["cells"]["zram"],
        cell_temp=frag["cells"]["temp"],
        cell_emmc=frag["cells"]["emmc"],
        cell_disk=frag["cells"]["disk"],
        cell_uptime=frag["cells"]["uptime"],
        cell_lan=frag["cells"]["lan"],
        cell_ts=frag["cells"]["ts"],
        cell_internet=frag["cells"]["internet"],
        model_chips=frag["model_chips"],
        rate_limit_card=frag["rate_limit_card"],
        log_card=frag["log_card"],
        clean_junk_card=frag["clean_junk_card"],
        update_block=frag["update_block"],
        hermes_update_block=frag["hermes_update_block"],
        aux_tasks_block=frag["aux_tasks_block"],
        backup_models_block=frag["backup_models_block"],
        processes_table=frag["processes_table"],
        cpu_pct=frag["cpu_pct"],
        ram_pct=frag["ram_pct"],
        cell_load=frag["cell_load"],
        available_models_json=available_models_json,
        active_tab=active_tab,
        gateway_list_block=frag["gateway_list_block"],
        gateway_log_card=frag.get("gateway_log_card", ""),
        gw_summary_text=frag["gw_summary_text"],
        gw_summary_badge_class=frag["gw_summary_badge_class"],
        cell_gw_platforms=frag["cells"]["gw_platforms"],
        countdown_block=countdown_block,
        open_block=get_open_block_active() if dash_active else OPEN_BLOCK_INACTIVE,
        router_open_block=(f'<a class="open" href="{get_9router_public_url()}" '
                           f'target="_blank">{ICON_EXTERNAL_LINK}Buka 9router</a>'),
        toggle_label="Matikan Dasbor" if dash_active else "Nyalakan Dasbor",
        dash_toggle_class="btn-off" if dash_active else "btn-on",
        bot_toggle_label="Matikan Gateway" if gw_active else "Nyalakan Gateway",
        bot_toggle_class="btn-off" if gw_active else "btn-on",
        nav_script=NAV_SCRIPT,
        script=render_poll_script(),
        icon_monitor=ICON_MONITOR,
        icon_layers=ICON_LAYERS,
        icon_power=ICON_POWER,
        icon_refresh=ICON_REFRESH,
        icon_trash=ICON_TRASH,
        icon_shield=ICON_SHIELD,
        icon_clock=ICON_CLOCK,
        icon_cpu=ICON_CPU,
        icon_ram=ICON_RAM,
        icon_disk=ICON_DISK,
        icon_network=ICON_NETWORK,
        icon_globe=ICON_GLOBE,
        icon_bot=ICON_BOT,
        icon_router=ICON_ROUTER,
        icon_hermes=ICON_HERMES,
        icon_activity=ICON_ACTIVITY,
    )


# --- SSE (Server-Sent Events) infrastructure ---
_sse_clients: list = []  # list of (queue.Queue, threading.Event) tuples
_sse_clients_lock = threading.Lock()
# Keys SSE_SCRIPT's apply() actually reads. build_fragments() also renders static slots
# (model_chips alone is ~38 KB) that the page never replaces over SSE; sending them made every
# phone download and JSON.parse ~93 KB per second for nothing. /api/status still returns everything.
SSE_CLIENT_KEYS = (
    "cells", "cell_load", "cpu_pct", "ram_pct", "processes_table", "gateway_list_block",
    "gw_summary_text", "gw_summary_badge_class", "log_card", "hermes_log_card",
    "clean_junk_card", "gateway_log_card",
)


def _sse_payload(frag: dict) -> str:
    return json.dumps({k: frag[k] for k in SSE_CLIENT_KEYS if k in frag})


_sse_last_data: str = ""  # last serialized fragments, for change detection
_sse_last_data_lock = threading.Lock()

def _sse_push_loop():
    """Background worker thread:
    When clients are connected (user HAS the web page open and visible), compute
    fragments every 1s and push live updates immediately.
    When NO clients are connected, sleep efficiently without polling/computing."""
    global _sse_last_data
    last_state: dict = {}
    tick_count = 0
    while True:
        # Visibility check: only run work if at least one client is active
        with _sse_clients_lock:
            has_clients = len(_sse_clients) > 0

        if not has_clients:
            time.sleep(1)
            continue

        time.sleep(1)
        tick_count += 1
        try:
            frag = build_fragments()
            data = _sse_payload(frag)

            # Extract meaningful signals from cells for change detection
            cells = frag.get("cells", {})
            def _extract_status(cell_html: str) -> str:
                import re as _re
                for kw in ("Berjalan", "Berhenti", "Aktif", "Mati", "Terhubung", "Tidak terhubung", "Terputus"):
                    if kw in cell_html:
                        return kw
                m = _re.search(r'class="value(?:\s+[^\"]+)?"[^>]*>(.*?)</span>', cell_html)
                return m.group(1) if m else cell_html[:30]

            current_state = {
                "dash": _extract_status(cells.get("dash", "")),
                "bot": _extract_status(cells.get("bot", "")),
                "gw": _extract_status(cells.get("gw", "")),
                "model": _extract_status(cells.get("model", "")),
                "router": _extract_status(cells.get("router", "")),
                "internet": _extract_status(cells.get("internet", "")),
                "temp": cells.get("temp", ""),
                "disk": cells.get("disk", ""),
                "uptime": cells.get("uptime", ""),
                "lan": cells.get("lan", ""),
                "ts": cells.get("ts", ""),
                "model_chips": frag.get("model_chips", "")[:50],
                "rate_limit": bool(frag.get("rate_limit_card", "")),
                "update_block": frag.get("update_block", ""),
                "log_card": frag.get("log_card", ""),
                "hermes_update_block": frag.get("hermes_update_block", ""),
                "quick_links_block": frag.get("quick_links_block", ""),
                "dash_bot_btns_block": frag.get("dash_bot_btns_block", ""),
                "aux_tasks_block": frag.get("aux_tasks_block", "")[:100],
                "backup_models_block": frag.get("backup_models_block", "")[:100],
                "processes_table": frag.get("processes_table", "")[:80],
                "updating": frag.get("updating", False),
                "gateway_list_block": frag.get("gateway_list_block", "")[:100],
                "gw_summary_text": frag.get("gw_summary_text", ""),
                "gw_summary_badge_class": frag.get("gw_summary_badge_class", ""),
                "hermes_log_card": frag.get("hermes_log_card", ""),
                "clean_junk_card": frag.get("clean_junk_card", ""),
                "gateway_log_card": frag.get("gateway_log_card", ""),
            }

            should_push = current_state != last_state or frag.get("updating", False)
            # Periodic sync every 10s to keep metrics strictly live
            if tick_count >= 10:
                should_push = True
                tick_count = 0

            if not should_push:
                continue

            last_state = current_state
            with _sse_last_data_lock:
                _sse_last_data = data

            with _sse_clients_lock:
                dead = []
                for q, evt in _sse_clients:
                    try:
                        q.put_nowait(data)
                    except queue.Full:
                        dead.append((q, evt))
                    except Exception:
                        dead.append((q, evt))
                for d in dead:
                    try:
                        d[1].set()
                        _sse_clients.remove(d)
                    except (ValueError, Exception):
                        pass
        except Exception as e:
            sys.stderr.write(f"[panel] SSE push loop error: {e}\n")
            continue

# Start SSE push thread
threading.Thread(target=_sse_push_loop, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep it quiet — no noisy access log filling up disk

    @staticmethod
    def _token_matches(candidate: str) -> bool:
        return (
            bool(TOKEN)
            and bool(candidate)
            and hmac.compare_digest(candidate, TOKEN)
        )

    def _has_valid_session(self) -> bool:
        try:
            cookie = http_cookies.SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            morsel = cookie.get(SESSION_COOKIE_NAME)
            return bool(morsel) and self._token_matches(morsel.value)
        except Exception:
            return False

    def _set_session_cookie(self) -> None:
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE_NAME}={TOKEN}; Path=/; HttpOnly; SameSite=Strict",
        )

    def _send_method_not_allowed(self):
        self._send_html(
            "<h1>405 Method Not Allowed</h1>"
            "<p>Aksi perubahan hanya lewat POST (atau GET shortcut dengan token eksplisit).</p>",
            405,
        )

    def _send_html(self, body: str, code: int = 200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, data: dict, code: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect_to_status(self, just: str = "", tab: str = ""):
        _invalidate_status_cache(
            "processes_table",
            "gateway_info",
        )
        # Clean URL: session cookie authenticates, no token in the Location.
        location = "/status"
        if just:
            location += f"?just={quote(just)}"
        if tab and tab in VALID_TABS:
            location += f"{'&' if just else '?'}tab={quote(tab)}"
        self.send_response(302)
        if getattr(self, "_bootstrap", False) or not self._has_valid_session():
            self._set_session_cookie()
        self.send_header("Location", location)
        self.end_headers()

    def _authenticate(self, qs: dict) -> tuple[bool, bool]:
        """Return (authorized, bootstrap_needed).

        Bootstrap: valid ?token= in the URL sets the session cookie and
        redirects to the same page without the token (only for GET page
        routes). All other requests must present a valid cookie.
        """
        query_token = (qs.get("token") or [""])[0]
        if self._token_matches(query_token):
            return True, self.command == "GET"
        if self._has_valid_session():
            return True, False
        return False, False

    def do_POST(self):
        """Mutations arrive here (UI fetch POST). Query string is parsed the
        same way as GET; body (form-encoded) is merged into qs."""
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        json_data = {}
        if length > 0:
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            content_type = self.headers.get("Content-Type", "")
            if "application/json" in content_type or body.strip().startswith("{"):
                try:
                    loaded = json.loads(body)
                    if isinstance(loaded, dict):
                        json_data = loaded
                        if "token" in json_data and json_data["token"]:
                            qs["token"] = [str(json_data["token"])]
                    else:
                        self._send_json({"ok": False, "error": "Format JSON harus berupa objek"}, code=400)
                        return
                except Exception:
                    self._send_json({"ok": False, "error": "Sintaks JSON tidak valid"}, code=400)
                    return
            else:
                body_qs = parse_qs(body)
                for k, v in body_qs.items():
                    qs.setdefault(k, v)

        query_token = (qs.get("token") or [""])[0]
        has_explicit_token = self._token_matches(query_token)
        is_json_client = parsed.path.startswith("/api/") or "application/json" in self.headers.get("Accept", "")

        if not has_explicit_token:
            client_ip = self.client_address[0] if self.client_address else ""
            raw_host = (self.headers.get("Host") or "").lower().strip()
            forwarded = ""
            if client_ip in ("127.0.0.1", "::1", "localhost"):
                forwarded = (self.headers.get("X-Forwarded-Host") or "").split(",")[0].strip().lower()
            def _norm_host(netloc_str: str) -> str:
                if not netloc_str:
                    return ""
                try:
                    u = urlsplit("//" + netloc_str.strip())
                    h = (u.hostname or "").lower()
                    p = u.port
                    if p in (80, 443):
                        p = None
                    host_part = f"[{h}]" if ":" in h else h
                    return f"{host_part}:{p}" if p else host_part
                except Exception:
                    return netloc_str.strip().lower()

            allowed_hosts = {_norm_host(h) for h in (raw_host, forwarded) if h}

            def _is_host_allowed(candidate: str) -> bool:
                if not candidate:
                    return False
                return _norm_host(candidate) in allowed_hosts

            origin = self.headers.get("Origin")
            referer = self.headers.get("Referer")
            if origin:
                origin_netloc = (urlparse(origin).netloc or "").lower().strip()
                if not _is_host_allowed(origin_netloc):
                    if is_json_client:
                        self._send_json({"ok": False, "error": "CSRF: Invalid Origin"}, code=403)
                    else:
                        self._send_html("<h1>403 — CSRF: Invalid Origin</h1>", 403)
                    return
            elif referer:
                ref_netloc = (urlparse(referer).netloc or "").lower().strip()
                if not _is_host_allowed(ref_netloc):
                    if is_json_client:
                        self._send_json({"ok": False, "error": "CSRF: Invalid Referer"}, code=403)
                    else:
                        self._send_html("<h1>403 — CSRF: Invalid Referer</h1>", 403)
                    return

        authed, _ = self._authenticate(qs)
        if not authed:
            if is_json_client:
                self._send_json({"ok": False, "error": "token salah"}, code=403)
            else:
                self._send_html("<h1>403 — token salah</h1>", 403)
            return
        self._handle_mutation(parsed, qs, json_data=json_data)

    def do_GET(self):
        global _last_action_at, _last_model_switch_at, _last_aux_model_at
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        authed, bootstrap = self._authenticate(qs)
        self._bootstrap = bootstrap
        if not authed:
            self._send_html("<h1>403 — token salah</h1>", 403)
            return

        # Mutation over plain GET is not allowed (CasaOS shortcuts excepted:
        # /toggle, /on, /off with an explicit valid token keep working).
        if parsed.path in MUTATING_PATHS:
            query_token = (qs.get("token") or [""])[0]
            if not (parsed.path in LEGACY_GET_SHORTCUTS and self._token_matches(query_token)):
                self._send_method_not_allowed()
                return

        if parsed.path in ("/status", "/"):
            if bootstrap:
                # Strip the token from the URL: set cookie, redirect clean.
                just = (qs.get("just") or [""])[0]
                tab = (qs.get("tab") or [""])[0]
                self.send_response(302)
                self._set_session_cookie()
                clean = "/status"
                if just:
                    clean += f"?just={quote(just)}"
                if tab and tab in VALID_TABS:
                    clean += f"{'&' if just else '?'}tab={quote(tab)}"
                self.send_header("Location", clean)
                self.end_headers()
                return
            just = (qs.get("just") or [""])[0]
            tab = (qs.get("tab") or [""])[0]
            if tab not in VALID_TABS:
                tab = ""
            self._send_html(build_status_page(just, active_tab=tab))
            return

        if parsed.path == "/api/status":
            body = json.dumps(build_fragments()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/gateway-log":
            n = 100
            try:
                n = int((qs.get("n") or ["100"])[0])
            except Exception:
                n = 100
            raw_log = tail_gateway_log(n=n)
            body = json.dumps({"ok": True, "log": redact_sensitive_tokens(raw_log)}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/whatsapp-log":
            n = 100
            try:
                n = int((qs.get("n") or ["100"])[0])
            except Exception:
                n = 100
            raw_log = tail_whatsapp_bridge_log(n=n)
            body = json.dumps({"ok": True, "log": redact_sensitive_tokens(raw_log)}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/whatsapp/pair-status":
            st = get_wa_pair_status()
            body = json.dumps({"ok": True, **st}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/gateway-config":
            plat = (qs.get("platform") or [""])[0].strip().lower()
            data = get_gateway_platform_config(plat)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path in ("/api/models", "/api/available-models"):
            body = json.dumps(get_available_models_cached()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/events":
            # SSE endpoint: stream updates to client
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = queue.Queue(maxsize=10)
            evt = threading.Event()
            with _sse_clients_lock:
                _sse_clients.append((q, evt))
            try:
                # Send current state immediately
                frag = build_fragments()
                data = _sse_payload(frag)
                self.wfile.write(f"event: update\ndata: {data}\n\n".encode())
                self.wfile.flush()
                while not evt.is_set():
                    try:
                        msg = q.get(timeout=15)
                        self.wfile.write(f"event: update\ndata: {msg}\n\n".encode())
                        self.wfile.flush()
                    except queue.Empty:
                        if evt.is_set():
                            break
                        # Send heartbeat to keep connection alive
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with _sse_clients_lock:
                    try:
                        _sse_clients.remove((q, evt))
                    except ValueError:
                        pass
            return

        if parsed.path in LEGACY_GET_SHORTCUTS:
            self._handle_mutation(parsed, qs)
            return

        self._send_html("<h1>404</h1>", 404)
        return
    def _handle_mutation(self, parsed, qs: dict, json_data: dict = None):
        if json_data is None:
            json_data = {}
        is_ajax = bool(json_data) or bool((qs.get("ajax") or [""])[0]) or "application/json" in self.headers.get("Accept", "")

        if parsed.path == "/api/gateway-config-preview":
            plat = str(json_data.get("platform") or "").strip().lower()
            base_yaml = json_data.get("base_yaml") if isinstance(json_data.get("base_yaml"), str) else None
            ok, text = preview_gateway_platform_config(plat, str(json_data.get("yaml") or ""), base_yaml)
            self._send_json({"ok": ok, "yaml": text if ok else "", "error": "" if ok else text}, code=200 if ok else 400)
            return

        if parsed.path == "/save-gateway-platform":
            plat = str(json_data.get("platform") or (qs.get("platform") or [""])[0]).strip().lower()
            yaml_content = str(json_data.get("yaml") if "yaml" in json_data else (qs.get("yaml") or [""])[0])
            enabled_raw = json_data.get("enabled") if "enabled" in json_data else (qs.get("enabled") or [None])[0]
            enabled = enabled_raw if isinstance(enabled_raw, bool) else _to_bool(enabled_raw) if enabled_raw is not None else None
            restart_gw = _to_bool(json_data.get("restart_gw") if "restart_gw" in json_data else (qs.get("restart_gw") or ["1"])[0], default=True)
            merge_raw = json_data.get("merge") if "merge" in json_data else (qs.get("merge") or ["0"])[0]
            merge = merge_raw if isinstance(merge_raw, bool) else _to_bool(merge_raw, default=False)
            base_yaml = json_data.get("base_yaml") if isinstance(json_data.get("base_yaml"), str) else None

            ok, err = save_gateway_platform_config(plat, yaml_content, enabled, merge=merge, base_yaml=base_yaml)
            if ok and restart_gw:
                restart_bot()

            if is_ajax:
                code = 200 if ok else 400
                self._send_json({"ok": ok, "error": err, "html": render_gateway_platforms_html() if ok else ""}, code=code)
                return
            if not ok:
                self._send_html(f"<h1>400 — {html.escape(err)}</h1>", 400)
                return
            self._redirect_to_status(just="gw-save", tab="control")
            return

        if parsed.path == "/toggle-gateway-platform":
            plat = str(json_data.get("platform") or (qs.get("platform") or [""])[0]).strip().lower()
            enabled_raw = json_data.get("enabled") if "enabled" in json_data else (qs.get("enabled") or ["1"])[0]
            enabled = _to_bool(enabled_raw, default=True)
            restart_gw = _to_bool(json_data.get("restart_gw") if "restart_gw" in json_data else (qs.get("restart_gw") or ["1"])[0], default=True)

            ok, err = toggle_gateway_platform_config(plat, enabled)
            if ok and restart_gw:
                restart_bot()

            if is_ajax:
                code = 200 if ok else 400
                self._send_json({"ok": ok, "error": err, "html": render_gateway_platforms_html() if ok else ""}, code=code)
                return
            if not ok:
                self._send_html(f"<h1>400 — {html.escape(err)}</h1>", 400)
                return
            self._redirect_to_status(tab="control")
            return

        if parsed.path == "/remove-gateway-platform":
            plat = str(json_data.get("platform") or (qs.get("platform") or [""])[0]).strip().lower()
            restart_gw = _to_bool(json_data.get("restart_gw") if "restart_gw" in json_data else (qs.get("restart_gw") or ["1"])[0], default=True)

            ok, err = remove_gateway_platform_config(plat)
            if ok and restart_gw:
                restart_bot()

            if is_ajax:
                code = 200 if ok else 400
                self._send_json({"ok": ok, "error": err, "html": render_gateway_platforms_html() if ok else ""}, code=code)
                return
            if not ok:
                self._send_html(f"<h1>400 — {html.escape(err)}</h1>", 400)
                return
            self._redirect_to_status(tab="control")
            return

        if parsed.path == "/api/whatsapp/pair-start":
            force_new_raw = json_data.get("force_new") if "force_new" in json_data else (qs.get("force_new") or ["0"])[0]
            force_new = _to_bool(force_new_raw, default=False)
            ok, msg = start_wa_pair(clear_session=force_new)
            self._send_json({"ok": ok, "message": msg, **get_wa_pair_status()})
            return

        if parsed.path == "/api/whatsapp/pair-cancel":
            cancel_wa_pair()
            self._send_json({"ok": True, "message": "Pairing dibatalkan", **get_wa_pair_status()})
            return

        if parsed.path == "/api/whatsapp/pair-apply":
            restart_gw = _to_bool(json_data.get("restart_gw") if "restart_gw" in json_data else (qs.get("restart_gw") or ["1"])[0], default=True)
            ok, msg = apply_wa_pair(restart_gw=restart_gw)
            self._send_json({"ok": ok, "message": msg, **get_wa_pair_status()})
            return
        """Execute an already-authenticated action route, then redirect."""
        global _last_action_at, _last_model_switch_at, _last_aux_model_at

        if parsed.path == "/switch-model":
            requested = str(json_data.get("model") or (qs.get("model") or [""])[0]).strip()
            now = time.monotonic()
            with _last_model_switch_lock:
                debounced = (now - _last_model_switch_at) < DEBOUNCE_SECONDS
                if not debounced:
                    _last_model_switch_at = now
            if not debounced and requested:
                # Only allow switching to a model 9router actually reports
                # right now — check against all model categories
                models_dict = get_available_models_cached()
                all_models = {m for group in models_dict.values() for m in group}
                if requested in all_models:
                    if set_current_model(requested):
                        if is_ajax:
                            self._send_json({"ok": True, "model": requested})
                            return
                        self._redirect_to_status(just="model")
                        return
            if is_ajax:
                self._send_json({"ok": False, "error": f"Model tidak valid atau gagal disetel: {requested}"}, code=400)
                return
            self._redirect_to_status()
            return

        if parsed.path == "/set-aux-model":
            task = str(json_data.get("task") or (qs.get("task") or [""])[0]).strip()
            provider = str(json_data.get("provider") or (qs.get("provider") or [""])[0]).strip()
            model = str(json_data.get("model") or (qs.get("model") or [""])[0]).strip()
            is_ajax = bool(json_data) or bool((qs.get("ajax") or [""])[0]) or "application/json" in self.headers.get("Accept", "")
            now = time.monotonic()
            with _last_aux_model_lock:
                debounced = (now - _last_aux_model_at) < 0.3
                if not debounced:
                    _last_aux_model_at = now
            if not debounced and task:
                saved = set_aux_task_model(task, provider, model)
                if is_ajax:
                    payload = {"ok": saved, "task": task, "provider": provider, "model": model}
                    if saved:
                        payload["html"] = render_aux_tasks_block()
                    else:
                        payload["reason"] = "failed to update config"
                    self._send_json(payload, code=200 if saved else 500)
                    return
                if not saved:
                    self._send_html("<h1>500 — gagal menyimpan konfigurasi auxiliary model</h1>", 500)
                    return
                self._redirect_to_status(just="aux", tab="auxiliary")
                return
            if is_ajax:
                self._send_json({"ok": False, "reason": "debounced or empty task"})
                return
            self._redirect_to_status(tab="auxiliary")
            return

        if parsed.path == "/reset-aux":
            now = time.monotonic()
            with _last_action_lock:
                debounced = (now - _last_action_at) < DEBOUNCE_SECONDS
                if not debounced:
                    _last_action_at = now
            if not debounced:
                if not reset_all_aux_tasks():
                    self._send_html("<h1>500 — gagal mereset konfigurasi auxiliary model</h1>", 500)
                    return
                self._redirect_to_status(just="aux-reset", tab="auxiliary")
                return
            self._redirect_to_status(tab="auxiliary")
            return

        if parsed.path == "/set-fallback-model":
            index_raw = json_data.get("index") if "index" in json_data else (qs.get("index") or ["-1"])[0]
            try:
                index = int(index_raw)
            except (ValueError, TypeError):
                index = -1
            provider = str(json_data.get("provider") or (qs.get("provider") or ["custom:9router"])[0]).strip()
            model = str(json_data.get("model") or (qs.get("model") or [""])[0]).strip()
            is_ajax = bool(json_data) or bool((qs.get("ajax") or [""])[0]) or "application/json" in self.headers.get("Accept", "")
            now = time.monotonic()
            with _last_action_lock:
                debounced = (now - _last_action_at) < DEBOUNCE_SECONDS
                if not debounced:
                    _last_action_at = now
            if not debounced and model:
                saved = set_fallback_model(index, provider, model)
                if is_ajax:
                    payload = {"ok": saved}
                    if saved:
                        payload["html"] = render_backup_models_block()
                    else:
                        payload["reason"] = "failed to update config"
                    self._send_json(payload, code=200 if saved else 500)
                    return
                if not saved:
                    self._send_html("<h1>500 — gagal menyimpan fallback model</h1>", 500)
                    return
                self._redirect_to_status(just="fallback", tab="control")
                return
            if is_ajax:
                self._send_json({"ok": False, "reason": "debounced or empty model"})
                return
            self._redirect_to_status(tab="control")
            return

        if parsed.path == "/remove-fallback-model":
            index_raw = json_data.get("index") if "index" in json_data else (qs.get("index") or ["-1"])[0]
            try:
                index = int(index_raw)
            except (ValueError, TypeError):
                index = -1
            now = time.monotonic()
            with _last_action_lock:
                debounced = (now - _last_action_at) < DEBOUNCE_SECONDS
                if not debounced:
                    _last_action_at = now
            if not debounced and index >= 0:
                if not remove_fallback_model(index):
                    self._send_html("<h1>500 — gagal menghapus fallback model</h1>", 500)
                    return
                self._redirect_to_status(just="fallback-del", tab="control")
                return
            self._redirect_to_status(tab="control")
            return

        if parsed.path == "/process-action":
            service = str(json_data.get("service") or (qs.get("service") or [""])[0]).strip()
            action = str(json_data.get("action") or (qs.get("action") or [""])[0]).strip()
            now = time.monotonic()
            with _last_action_lock:
                debounced = (now - _last_action_at) < DEBOUNCE_SECONDS
                if not debounced:
                    _last_action_at = now
            if not debounced and service and action:
                try:
                    if service == "9router":
                        _cf = router_compose_file()
                        if action == "stop":
                            if _cf:
                                subprocess.run(["docker", "compose", "-f", _cf, "stop"], timeout=15)
                            else:
                                subprocess.run(["docker", "stop", "9router"], timeout=15)
                        elif action == "start":
                            if _cf:
                                subprocess.run(["docker", "compose", "-f", _cf, "up", "-d"], timeout=15)
                            else:
                                subprocess.run(["docker", "start", "9router"], timeout=15)
                        elif action == "restart":
                            subprocess.run(["docker", "restart", "9router"], timeout=15)
                    elif service == "cloudflared" and action in ("start", "stop", "restart"):
                        subprocess.run(["docker", action, "cloudflared"], timeout=15)
                    elif service in ("pihole", "pihole-pihole-1") and action in ("start", "stop", "restart"):
                        subprocess.run(["docker", action, "pihole-pihole-1"], timeout=15)
                    elif service == "hermes-dashboard" and action == "restart":
                        subprocess.run(["systemctl", "restart", "hermes-dashboard"], timeout=15)
                    elif service == "hermes-panel" and action == "restart":
                        def _delayed_restart():
                            time.sleep(0.5)
                            try:
                                subprocess.run(["systemctl", "restart", "hermes-panel.service"], timeout=15)
                            except Exception:
                                pass
                        threading.Thread(target=_delayed_restart, daemon=True).start()
                except Exception as ex:
                    sys.stderr.write(f"[panel] Process action error for {service} {action}: {ex}\n")
            self._redirect_to_status(tab="status")
            return

        # Action route: perform once (debounced against duplicate/prefetch
        # requests), then redirect — never render an action route directly,
        # so a refresh of the resulting page can never re-trigger it.
        VALID_POST_ACTIONS = {
            "/toggle", "/on", "/off", "/restart-bot", "/bot-toggle",
            "/update-router", "/check-update", "/clean-junk",
            "/check-hermes-update", "/update-hermes", "/fetch-models", "/reload-panel-config"
        }
        if parsed.path not in VALID_POST_ACTIONS:
            self._send_html("<h1>404</h1>", 404)
            return

        now = time.monotonic()
        with _last_action_lock:
            debounced = (now - _last_action_at) < DEBOUNCE_SECONDS
            if not debounced:
                _last_action_at = now

        just = ""
        target_tab = ""
        if not debounced:
            try:
                if parsed.path == "/toggle":
                    will_start = not service_active(SERVICE)
                    subprocess.run(["systemctl", "start" if will_start else "stop", SERVICE], timeout=15)
                    just = "start" if will_start else ""
                    target_tab = "control"
                elif parsed.path == "/on":
                    subprocess.run(["systemctl", "start", SERVICE], timeout=15)
                    just = "start"
                    target_tab = "control"
                elif parsed.path == "/off":
                    subprocess.run(["systemctl", "stop", SERVICE], timeout=15)
                    target_tab = "control"
                elif parsed.path == "/restart-bot":
                    restart_bot()
                    just = "restart"
                    target_tab = "control"
                elif parsed.path == "/bot-toggle":
                    was_active = service_active("hermes-gateway", user=True)
                    bot_action("stop" if was_active else "start")
                    just = "bot-off" if was_active else "bot-on"
                    target_tab = "control"
                elif parsed.path == "/update-router":
                    # updating flag (set in update_router) makes /status show the
                    # live pull-log card; the auto-poll then streams it. No banner.
                    update_router()
                    target_tab = "control"
                elif parsed.path == "/check-update":
                    # Force a fresh check: drop the cache so /status re-checks in
                    # the background (its "checking" state auto-refreshes).
                    try:
                        os.remove(UPDATE_CACHE_PATH)
                    except OSError:
                        pass
                    target_tab = "control"
                elif parsed.path == "/clean-junk":
                    cleanup_system_junk()
                    just = "cleaned"
                    target_tab = "control"
                elif parsed.path == "/check-hermes-update":
                    # Force a fresh Hermes update check
                    with _hermes_update_lock:
                        _hermes_update_cache["at"] = 0
                    threading.Thread(target=_refresh_hermes_update, daemon=True).start()
                    target_tab = "control"
                elif parsed.path == "/update-hermes":
                    # Official updater owns backup, stash policy, validation,
                    # rollback, dependencies, migration, and gateway restart.
                    run_hermes_update()
                    target_tab = "control"
                elif parsed.path == "/fetch-models":
                    fetch_remote_models()
                    just = "model"
                    target_tab = "status"
                elif parsed.path == "/reload-panel-config":
                    reload_panel_config()
                    just = "model"
                    target_tab = "status"
            except Exception as ex:
                sys.stderr.write(f"[panel] Action route error {parsed.path}: {ex}\n")

        self._redirect_to_status(just=just, tab=target_tab)


class TimeoutThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    timeout = 15

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        super().server_bind()


if __name__ == "__main__":
    if not TOKEN:
        print("[ERROR] PANEL_TOKEN is required. Set it in environment.", file=sys.stderr)
        sys.exit(2)
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    server = TimeoutThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
