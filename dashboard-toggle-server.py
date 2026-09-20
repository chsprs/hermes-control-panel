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

import html
import json
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import shlex
import shutil
import subprocess
import threading
import time
import urllib.request
import yaml
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

TOKEN = os.environ.get("PANEL_TOKEN", "vita-stb-2026")
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

CONFIG_PATH = os.environ.get("HERMES_CONFIG_PATH", "/root/.hermes/config.yaml")
ROUTER_URL = "http://{host}:20128/"
INFO_TIMEOUT = 2.0  # seconds — every live check below is capped at this
ROUTER_DB_PATH = "/DATA/AppData/9router/db/data.sqlite"  # host-side path of
# the same file 9router itself reads at /app/data/db/data.sqlite — reading
# it directly avoids a docker exec round-trip.
ROUTER_IMAGE = "decolua/9router:latest"
ROUTER_COMPOSE_DIR = "/opt/AppData/9router"
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
HERMES_UPDATE_TIMEOUT = 900  # dependency install can take several minutes

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
ICON_BOT = _icon('<rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><line x1="8" y1="16" x2="8.01" y2="16"/><line x1="16" y1="16" x2="16.01" y2="16"/>', size=16)
ICON_ROUTER = _icon('<rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6" y2="14"/><line x1="18" y1="6" x2="18" y2="14"/><line x1="6" y1="18" x2="6.01" y2="18"/><line x1="10" y1="18" x2="10.01" y2="18"/>', size=16)
ICON_HERMES = _icon('<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>', size=16)
ICON_TRASH = _icon('<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>', size=16)
ICON_SHIELD = _icon('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>', size=16)

ICON_HERMES_LOGO = '<img src="https://cdn.jsdelivr.net/gh/selfhst/icons/webp/hermes-agent.webp" width="28" height="28" style="vertical-align:middle;object-fit:contain;filter:drop-shadow(0 2px 4px rgba(0,0,0,0.4))" alt="Hermes Logo">'

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
letter-spacing:.04em;text-transform:uppercase;backdrop-filter:blur(10px)}}
.live-badge .dot{{width:6px;height:6px;border-radius:50%;background:var(--success);
box-shadow:0 0 8px var(--success)}}
.live-badge.connected{{color:var(--success);background:rgba(16,185,129,0.1);border-color:rgba(16,185,129,0.25)}}
.live-badge.connected .dot{{background:var(--success)}}

/* Apple CC Segmented Nav */
.tabs{{display:flex;gap:.3rem;margin:0 auto 1.35rem;width:100%;max-width:520px;
background:rgba(20,25,35,0.75);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
padding:.3rem;border-radius:var(--radius-xl);border:1px solid var(--border)}}
.tab{{flex:1 1 0;min-height:42px;display:flex;align-items:center;justify-content:center;
border-radius:12px;text-align:center;gap:.35rem;
font-weight:500;font-size:.82rem;cursor:pointer;border:none;white-space:nowrap;
background:transparent;color:var(--text-muted);transition:all .18s var(--ease)}}
.tab:hover{{color:var(--text);background:rgba(255,255,255,0.04)}}
.tab.active{{background:var(--surface-solid);color:var(--text);
border:1px solid var(--border-hover);box-shadow:0 3px 12px rgba(0,0,0,0.35)}}

/* Panels & Layout */
.content-wrapper{{display:block;width:100%;max-width:1040px;margin:0 auto}}
.tab-panel{{display:none;width:100%;margin:0 auto}}
.tab-panel.active{{display:block}}
#tab-status{{max-width:1040px}}
#tab-performance{{max-width:920px}}
#tab-control{{max-width:880px}}
#tab-auxiliary{{max-width:880px}}

/* Cards */
.card{{background:var(--surface);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
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
font-variant-numeric:tabular-nums;display:flex;align-items:center;gap:.35rem}}
.cc-tile-sub{{font-size:.72rem;color:var(--text-dim);margin-top:.2rem}}

/* Row Metrics */
.row{{display:flex;justify-content:space-between;align-items:center;
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
@media(max-width:540px){{.grid{{grid-template-columns:1fr}}.grid .row:nth-child(even){{border-left:none;padding-left:0}}}}

/* Buttons */
a.toggle,.btn,a.open{{display:inline-flex;align-items:center;justify-content:center;gap:.55rem;
text-align:center;padding:.65rem 1.05rem;min-height:44px;border-radius:var(--radius-md);
text-decoration:none;font-weight:500;font-size:.84rem;color:var(--text);
width:100%;background:rgba(255,255,255,0.035);border:1px solid var(--border);
transition:all .18s var(--ease);cursor:pointer;backdrop-filter:blur(8px)}}
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
  background:rgba(16,185,129,0.12) !important;color:var(--success) !important;
  font-weight:600;border-color:rgba(16,185,129,0.4) !important;box-shadow:0 0 16px rgba(16,185,129,0.15);
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
  background:rgba(16,185,129,0.2);color:var(--success);
}}
@media (max-width:480px){{
  .models-grid{{grid-template-columns:1fr;}}
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
#confirm-modal, #aux-picker-modal{{position:fixed;inset:0;background:rgba(7,9,14,0.85);backdrop-filter:blur(8px);
display:none;align-items:center;justify-content:center;z-index:300;padding:1.5rem}}
#confirm-modal.show, #aux-picker-modal.show{{display:flex}}
.confirm-box{{background:rgba(22,27,38,0.95);border:1px solid var(--border-hover);
border-radius:var(--radius-xl);padding:1.6rem 1.5rem;max-width:360px;width:100%;
box-shadow:0 12px 48px rgba(0,0,0,0.7);backdrop-filter:blur(20px)}}
.confirm-box h3{{font-size:1rem;font-weight:600;margin-bottom:.5rem}}
.confirm-box p{{font-size:.82rem;color:var(--text-muted);margin-bottom:1.3rem;line-height:1.5}}
.confirm-actions{{display:flex;gap:.6rem}}
.confirm-actions .btn{{flex:1;margin:0}}
.btn-danger{{background:rgba(239,68,68,0.18);color:#fca5a5;border:1px solid rgba(239,68,68,0.4)}}
.btn-danger:hover{{background:rgba(239,68,68,0.3);border-color:var(--danger);color:#fff}}

/* Auxiliary Tasks */
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

/* Windows Task Manager Elements */
.task-table-wrap{{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:var(--radius-sm);border:1px solid var(--border);background:rgba(255,255,255,0.015);margin-bottom:.5rem}}
.task-table{{width:100%;border-collapse:collapse;font-size:.78rem;text-align:left}}
.task-table th{{background:rgba(255,255,255,0.04);color:var(--text-muted);font-size:.68rem;text-transform:uppercase;letter-spacing:.05em;padding:.65rem .85rem;border-bottom:1px solid var(--border);white-space:nowrap;font-family:var(--font-mono)}}
.task-table td{{padding:.75rem .85rem;border-bottom:1px solid rgba(255,255,255,0.04);vertical-align:middle}}
.task-table tr:hover td{{background:rgba(255,255,255,0.03)}}
.task-table tr:last-child td{{border-bottom:none}}
.task-name-cell{{display:flex;align-items:center;gap:.45rem;font-weight:500}}
.badge-up{{background:var(--success-dim);color:var(--success);border:1px solid rgba(16,185,129,0.3);padding:.15rem .45rem;border-radius:4px;font-size:.68rem;font-weight:600}}
.badge-down{{background:var(--danger-dim);color:var(--danger);border:1px solid rgba(239,68,68,0.3);padding:.15rem .45rem;border-radius:4px;font-size:.68rem;font-weight:600}}
.btn-end-task{{padding:.28rem .6rem;font-size:.72rem;border-radius:var(--radius-sm);border:1px solid rgba(239,68,68,0.35);background:rgba(239,68,68,0.12);color:#fca5a5;text-decoration:none;display:inline-block;cursor:pointer;font-weight:500;transition:all .15s ease}}
.btn-end-task:hover{{background:rgba(239,68,68,0.28);border-color:var(--danger);color:#fff}}
.btn-restart-task{{padding:.28rem .6rem;font-size:.72rem;border-radius:var(--radius-sm);border:1px solid var(--border);background:rgba(255,255,255,0.06);color:var(--text);text-decoration:none;display:inline-block;cursor:pointer;margin-left:.3rem;transition:all .15s ease}}
.btn-restart-task:hover{{background:rgba(255,255,255,0.14);color:#fff}}
.btn-start-task{{padding:.28rem .6rem;font-size:.72rem;border-radius:var(--radius-sm);border:1px solid rgba(16,185,129,0.35);background:rgba(16,185,129,0.12);color:#6ee7b7;text-decoration:none;display:inline-block;cursor:pointer;font-weight:500}}
.btn-start-task:hover{{background:rgba(16,185,129,0.28);color:#fff}}

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
      <h3 id="aux-picker-title" style="margin:0;font-size:1.05rem">Pilih Model Auxiliary</h3>
      <button type="button" class="btn" style="width:auto;padding:0.25rem 0.6rem;font-size:0.85rem;line-height:1;margin:0" onclick="closeAuxPicker()">✕</button>
    </div>
    <input type="text" id="aux-model-search" class="search-input" placeholder="Cari model… (filter)" oninput="filterAuxPicker(this.value)" style="margin-bottom:0.8rem">
    <div id="aux-picker-list" style="overflow-y:auto;flex:1;max-height:55vh;display:flex;flex-direction:column;gap:0.45rem;padding-right:2px">
    </div>
  </div>
</div>
<div class="header">
  <div class="header-brand">
    <img src="https://cdn.jsdelivr.net/gh/selfhst/icons/webp/hermes-agent.webp" class="header-logo" alt="Hermes Logo">
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
  <!-- Windows Task Manager Process Table -->
  <div class="card card-status" style="padding:1.1rem;margin-bottom:1.25rem">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.8rem">
      <div class="card-title" style="margin-bottom:0">{icon_activity} Daftar Proses & Layanan Sistem</div>
      <span style="font-size:0.72rem;color:var(--text-dim);font-family:var(--font-mono)">Windows Task Manager</span>
    </div>
    <div id="process-table-slot">
      {processes_table}
    </div>
  </div>

  <!-- Apple CC Highlights (Bento 4-Tile Grid) -->
  <div class="card card-status" style="padding:1.1rem;margin-bottom:1.25rem">
    <div class="card-title" style="margin-bottom:.8rem">{icon_monitor} Ringkasan Bot & Model AI</div>
    <div class="cc-grid">
      <div class="cc-tile">
        <div class="cc-tile-header">
          <span class="cc-tile-label">Hermes Bot</span>
          <div class="cc-icon-box cc-icon-blue">{icon_bot}</div>
        </div>
        <div class="cc-tile-val" id="cell-bot">{cell_bot}</div>
        <div class="cc-tile-sub" id="cell-gw">{cell_gw}</div>
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
          <span class="cc-tile-label">Active Model AI</span>
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
      <div class="row"><span class="label">{icon_disk} Storage</span>
        <span id="cell-disk">{cell_disk}</span></div>
      <div class="row"><span class="label">{icon_clock} Uptime</span>
        <span id="cell-uptime">{cell_uptime}</span></div>
      <div class="row"><span class="label">{icon_network} IP LAN</span>
        <span id="cell-lan">{cell_lan}</span></div>
      <div class="row"><span class="label">{icon_network} Tailscale</span>
        <span id="cell-ts">{cell_ts}</span></div>
      <div class="row" style="display:none"><span class="label">Hermes CLI</span>
        <span id="cell-hermes">{cell_hermes}</span></div>
    </div>
  </div>

  <div class="card card-info">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.75rem">
      <div class="card-title" style="margin-bottom:0">{icon_layers} Model 9router</div>
    </div>
    <input type="text" id="model-search" class="search-input" placeholder="Cari model… (filter chip)" oninput="filterModels(this.value)">
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
        <div class="perf-sub">4 Cores @ 1.9GHz · Suhu: <span id="perf-temp">{cell_temp}</span> · Load: <span id="perf-load">{cell_load}</span></div>
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
        <div class="perf-sub">Penggunaan: <span id="perf-ram-sub">{cell_ram}</span> · Swap ZRAM: <span id="cell-zram-perf">{cell_zram}</span></div>
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
        <div class="row"><span class="label">{icon_disk} Ruang Disk</span>
          <span id="cell-disk-perf">{cell_disk}</span></div>
        <div class="row"><span class="label">{icon_shield} Status eMMC</span>
          <span id="cell-emmc-perf">{cell_emmc}</span></div>
      </div>
    </div>

    <div class="card card-info">
      <div class="card-title">{icon_network} Jaringan & Uptime</div>
      <div class="grid">
        <div class="row"><span class="label">{icon_network} IP LAN</span>
          <span id="cell-lan-perf">{cell_lan}</span></div>
        <div class="row"><span class="label">{icon_network} Tailscale</span>
          <span id="cell-ts-perf">{cell_ts}</span></div>
        <div class="row" style="grid-column: span 2"><span class="label">{icon_clock} Uptime Server</span>
          <span id="cell-uptime-perf">{cell_uptime}</span></div>
      </div>
    </div>
  </div>
</div>

<!-- CONTROL TAB -->
<div class="tab-panel" id="tab-control">
  {countdown_block}
  <div class="card card-info">
    <div class="card-title">Quick Links</div>
    <div class="btn-row" id="quick-links-slot">
      {open_block}
      {router_open_block}
    </div>
  </div>
  <div class="card card-control">
    <div class="card-title">Dashboard & Bot</div>
    <div class="btn-row" id="dash-bot-btns-slot">
      <a class="toggle {dash_toggle_class}" id="btn-dash-toggle" href="/toggle?token={token}">{icon_power}{toggle_label}</a>
      <a class="toggle {bot_toggle_class}" id="btn-bot-toggle" href="/bot-toggle?token={token}">{icon_power}{bot_toggle_label}</a>
      <a class="toggle restart" href="/restart-bot?token={token}">{icon_refresh}Restart Bot</a>
      <a class="toggle restart" href="/clean-junk?token={token}">{icon_trash}Bersihkan Cache</a>
    </div>
  </div>
  <div class="card card-info" style="margin-bottom:1.25rem">
    <div class="aux-header">
      <div class="card-title" style="margin-bottom:0">{icon_shield} Model Cadangan (Fallback)</div>
      <button type="button" class="btn btn-action-sm" onclick="openFallbackPicker(-1, 'Tambah Backup Baru')">
        + Tambah Backup
      </button>
    </div>
    <div class="aux-desc">
      Model cadangan otomatis digunakan saat model utama gagal atau kena limit (HTTP 429/500). Urutan fallback dieksekusi dari atas ke bawah.
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
        onclick="toggleLog('logDismissed','log-show-wrap')">Tampilkan Log Update 9router</button>
    </div>
  </div>
  <div class="card card-warn" id="hermes-update-slot">{hermes_update_block}</div>
  <div id="hermes-log-show" style="display:none;margin-top:.6rem">
    <button type="button" class="btn" style="width:auto;padding:0.35rem 0.8rem;font-size:0.75rem;margin:0"
      onclick="toggleLog('hermesLogDismissed','hermes-log-show')">Tampilkan Log Hermes</button>
  </div>
</div>

<!-- AUXILIARY TAB -->
<div class="tab-panel" id="tab-auxiliary">
  <div class="card card-status" style="padding:1.25rem">
    <div class="aux-header">
      <div class="card-title" style="margin-bottom:0">{icon_cpu} Auxiliary Tasks</div>
      <a class="toggle restart" style="width:auto;min-height:34px;padding:0.35rem 0.8rem;font-size:0.75rem;margin:0" href="/reset-aux?token={token}">
        {icon_refresh}Reset All to Auto
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
var TOKEN = "{token}";
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
  fetch('/api/models?token=' + encodeURIComponent(TOKEN), {{headers: {{'Accept': 'application/json'}}}})
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
  if(title) title.textContent = 'Pilih Model: ' + taskLabel;
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
  if(title) title.textContent = (index === -1 ? 'Tambah Model Cadangan (Backup)' : 'Ganti Model Cadangan: ' + label);
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
        out += '<div style="font-size:0.7rem;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;color:var(--text-dim);margin-top:0.4rem;padding:0 0.2rem">' + groupName + '</div>';
        for(var i = 0; i < matched.length; i++){{
          var mId = matched[i];
          var safeId = mId.replace(/"/g, '&quot;');
          var isFree = mId.toLowerCase().indexOf('free') !== -1;
          var badge = isFree ? '<span class="model-chip-badge">FREE</span>' : '<span class="model-chip-badge" style="background:rgba(255,255,255,0.06);color:var(--text-dim)">9ROUTER</span>';
          out += '<a class="aux-model-opt" href="javascript:void(0)" onclick="selectFallbackModel(\\'custom:9router\\', \\'' + safeId + '\\')">' +
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

  var url = '/set-fallback-model?token=' + encodeURIComponent(TOKEN) + '&index=' + encodeURIComponent(idx) +
            '&provider=' + encodeURIComponent(provider) + '&model=' + encodeURIComponent(model) + '&ajax=1';

  fetch(url, {{headers: {{'Accept': 'application/json'}}}})
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
  if(!q || 'auto (gunakan model utama)'.indexOf(q) !== -1 || 'main model'.indexOf(q) !== -1){{
    out += '<a class="aux-model-opt" href="javascript:void(0)" onclick="selectAuxModel(\\'auto\\', \\'\\')">' +
           '<div>' +
             '<div class="aux-model-opt-name" style="color:var(--text)">⚡ Auto (Gunakan Model Utama)</div>' +
             '<div class="aux-model-opt-sub">Inherit dari model obrolan default Hermes</div>' +
           '</div>' +
           '<span class="model-chip-badge">AUTO</span>' +
         '</a>';
  }}
  if(typeof AVAILABLE_MODELS === 'object' && AVAILABLE_MODELS !== null){{
    for(var groupName in AVAILABLE_MODELS){{
      var list = AVAILABLE_MODELS[groupName] || [];
      var matched = list.filter(function(m){{ return !q || m.toLowerCase().indexOf(q) !== -1; }});
      if(matched.length > 0){{
        out += '<div style="font-size:0.7rem;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;color:var(--text-dim);margin-top:0.4rem;padding:0 0.2rem">' + groupName + '</div>';
        for(var i = 0; i < matched.length; i++){{
          var mId = matched[i];
          var safeId = mId.replace(/"/g, '&quot;');
          var isFree = mId.toLowerCase().indexOf('free') !== -1;
          var badge = isFree ? '<span class="model-chip-badge">FREE</span>' : '<span class="model-chip-badge" style="background:rgba(255,255,255,0.06);color:var(--text-dim)">9ROUTER</span>';
          out += '<a class="aux-model-opt" href="javascript:void(0)" onclick="selectAuxModel(\\'custom:9router\\', \\'' + safeId + '\\')">' +
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

  var url = '/set-aux-model?token=' + encodeURIComponent(TOKEN) + '&task=' + encodeURIComponent(task) +
            '&provider=' + encodeURIComponent(provider) + '&model=' + encodeURIComponent(model) + '&ajax=1';

  fetch(url, {{headers: {{'Accept': 'application/json'}}}})
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
          valEl.textContent = isAuto ? 'auto (use main model)' : (provider && model ? provider + ' · ' + model : (model || provider));
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
}}

// Tab restore from URL or safeStore
var activeTabFromUrl = "{active_tab}";
if(activeTabFromUrl){{
  var btn = document.querySelector('.tab[onclick*="' + activeTabFromUrl + '"]');
  if(btn) switchTab(activeTabFromUrl, btn);
}} else {{
  var saved = safeStore('getItem', 'activeTab');
  if(saved){{
    var btn = document.querySelector('.tab[onclick*="' + saved + '"]');
    if(btn) switchTab(saved, btn);
  }}
}}
restorePatchPages();

function scrollAllLogsToBottom(){{
  document.querySelectorAll('.logbox').forEach(function(b){{
    b.scrollTop = b.scrollHeight;
  }});
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
}}
function syncLogUI(){{
  var routerDismissed = safeStore('getItem','logDismissed');
  var rw = document.getElementById('log-show-wrap');
  if(rw) rw.style.display = routerDismissed ? '' : 'none';
  if(routerDismissed){{
    var rc = document.getElementById('router-log-card');
    if(rc) rc.remove();
  }}
  var hermesDismissed = safeStore('getItem','hermesLogDismissed');
  var hw = document.getElementById('hermes-log-show');
  if(hw) hw.style.display = hermesDismissed ? '' : 'none';
  if(hermesDismissed){{
    var hc = document.getElementById('hermes-log-card');
    if(hc) hc.remove();
  }}
}}
syncLogUI();
// Initial load: scroll all existing log boxes to the bottom once
document.addEventListener('DOMContentLoaded', scrollAllLogsToBottom);
setTimeout(scrollAllLogsToBottom, 100);
setTimeout(scrollAllLogsToBottom, 600);
</script>
</body></html>"""

def get_open_block_active():
    # Hermes Dashboard selalu di H96 Max X3 (192.168.1.100)
    return f'<a class="open" href="http://192.168.1.100:9119" target="_blank">{ICON_EXTERNAL_LINK}Buka Dashboard Hermes</a>'

OPEN_BLOCK_INACTIVE = '<div class="hint-pill">Nyalakan dashboard dulu untuk membukanya</div>'

def get_gateway_info() -> str:
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
                    hours, rem = divmod(int(delta.total_seconds()), 3600)
                    mins, _ = divmod(rem, 60)
                    uptime_part = f" · {hours}j{mins}m"
            except Exception:
                pass
        return f"PID {pid} · {mem_mb:.0f}MB{uptime_part}"
    except Exception:
        return "?"


def get_9router_host() -> str:
    """Auto-detect 9router host IP. Check local Docker first, then scan network."""
    global _9router_host_cache, _9router_host_at
    with _9router_host_lock:
        if _9router_host_cache and (time.time() - _9router_host_at) < 300:  # 5 min cache
            return _9router_host_cache

    # 1. Check local Docker / Compose first: if compose file or container exists locally, it's local!
    if os.path.exists(f"{ROUTER_COMPOSE_DIR}/docker-compose.yml"):
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

    # 2. Check known hosts (from config or Tailscale peers)
    known_hosts = ["192.168.1.50", "192.168.1.100", "100.99.159.9"]
    # Also add Tailscale peers if available
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
        host = lan_ip or "192.168.1.100"
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
    window.location.href = '/status?token={token}';
  }}
}}, 1000);
</script>"""

# Auto-refresh via SSE (Server-Sent Events). The server pushes updates only
# when data actually changes — no wasted polls, instant updates, one persistent
# connection. Falls back to polling if SSE is unavailable.
SSE_SCRIPT = """<script>
(function(){
  var TOKEN="__TOKEN__";
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
    if(!poly || !fill) return;
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
  function set(id,v){ var el=document.getElementById(id); if(el&&v!=null) el.innerHTML=v; }
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
    // Capture stickiness on the OLD box before innerHTML replaces it —
    // a fresh element always reports scrollTop=0 and would never scroll.
    var force = window._forceBottom === true;
    var boxes=el.querySelectorAll('.logbox'), sticky=[];
    for(var i=0;i<boxes.length;i++){
      var b=boxes[i];
      if(force || b.scrollHeight-b.scrollTop-b.clientHeight < 48) sticky.push(i);
    }
    el.innerHTML=v;
    var nb=el.querySelectorAll('.logbox');
    for(var j=0;j<nb.length;j++){
      if(sticky.indexOf(j)!==-1) nb[j].scrollTop=nb[j].scrollHeight;
    }
    if(force) window._forceBottom = false;
  }
  function apply(d){
    if(d.cells){ set('cell-dash',d.cells.dash); set('cell-bot',d.cells.bot);
      set('cell-gw',d.cells.gw); set('cell-model',d.cells.model); set('cell-providers',d.cells.providers); set('cell-router',d.cells.router); set('cell-hermes',d.cells.hermes);
      set('cell-ram',d.cells.ram); set('cell-zram',d.cells.zram); set('cell-temp',d.cells.temp); set('cell-emmc',d.cells.emmc);
      set('cell-disk',d.cells.disk); set('cell-uptime',d.cells.uptime);
      set('cell-lan',d.cells.lan); set('cell-ts',d.cells.ts); }
    // Static controls stay untouched: replacing them resets scroll/focus.
    // SSE updates only live metrics, process data, and active update logs.
    if(d.processes_table) set('process-table-slot',d.processes_table);
    if(d.cpu_pct !== undefined) {{
      updateSparkline('cpu-sparkline', d.cpu_pct, cpuHistory);
      var cval = document.getElementById('perf-cpu-val');
      if(cval) cval.textContent = d.cpu_pct.toFixed(1) + '%';
    }}
    if(d.ram_pct !== undefined) {{
      updateSparkline('ram-sparkline', d.ram_pct, ramHistory);
      var rval = document.getElementById('perf-ram-val');
      if(rval) rval.textContent = d.ram_pct.toFixed(0) + '%';
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
    if(!safeStore('getItem','logDismissed')) stickySet('log-slot',d.log_card);
    else { var rc=document.getElementById('router-log-card'); if(rc) rc.remove(); }
    if(!safeStore('getItem','hermesLogDismissed')) {{
      var hlc = document.getElementById('hermes-log-card');
      if(hlc && d.hermes_log_card) stickySet('hermes-log-card', d.hermes_log_card);
      else if(d.hermes_log_card) {{
        var hls = document.getElementById('hermes-log-slot');
        if(hls) stickySet('hermes-log-slot', d.hermes_log_card);
      }}
    }} else {{
      var hc = document.getElementById('hermes-log-card');
      if(hc) hc.remove();
    }}
    syncLogUI();
    restorePatchPages();
  }
  function connect(){
    if(retryTimer){ clearTimeout(retryTimer); retryTimer=null; }
    if(es) try{ es.close(); }catch(x){}
    es=new EventSource('/events?token='+encodeURIComponent(TOKEN));
    es.onopen=function(){
      if(spin) spin.classList.remove('on');
      var live=document.querySelector('.live-badge');
      if(live) live.classList.add('connected');
    };
    es.addEventListener('update', function(e){
      try{ apply(JSON.parse(e.data)); }catch(ex){}
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
var CONFIRM_ROUTES = [
  {match:'/update-hermes', title:'Update Hermes Agent', msg:'Update menjalankan git pull, install dependency, dan restart gateway. Bot tidak bisa dibalas selama proses (beberapa menit). Lanjutkan?'},
  {match:'/update-router', title:'Update 9router', msg:'Update menjalankan docker compose pull + up -d untuk 9router. Container 9router akan restart. Lanjutkan?'},
  {match:'/restart-bot', title:'Restart Hermes Gateway', msg:'Restart service hermes-gateway? Koneksi bot Telegram akan restart dalam beberapa detik.'},
  {match:'/bot-toggle', title:'Ubah Status Bot', msg:'Ubah status hidup/mati Bot Telegram hermes-gateway?'},
  {match:'/clean-junk', title:'Bersihkan Cache & Sampah', msg:'Bersihkan log update, cache package uv/pip, dan builder docker dangling untuk melegakan penyimpanan STB?'},
  {match:'/reset-aux', title:'Reset Auxiliary Models', msg:'Reset semua model tugas auxiliary ke "auto"? Pengaturan model per tugas akan dikembalikan menggunakan model obrolan utama.'},
  {match:'/remove-fallback-model', title:'Hapus Model Cadangan', msg:'Hapus model ini dari daftar cadangan (fallback)?'},
  {match:'/process-action?service=9router&action=stop', title:'Hentikan 9router (End Task)', msg:'Hentikan container 9router? AI routing akan offline sampai dinyalakan lagi.'},
  {match:'/process-action?service=hermes-panel&action=restart', title:'Restart Hermes Control Panel', msg:'Restart service hermes-panel? Panel akan terhubung kembali dalam beberapa detik.'},
  {match:'/process-action?action=restart', title:'Restart Tugas', msg:'Restart layanan yang dipilih sekarang?'}
];
document.addEventListener('click', function(e){
  var a = e.target.closest('a.toggle, a.open, a.model-chip, a.btn-end-task, a.btn-restart-task, a.btn-start-task');
  if(!a || !a.getAttribute('href') || a.target === '_blank'
     || a.classList.contains('is-loading')) return;
  var href = a.getAttribute('href');
  for(var i=0;i<CONFIRM_ROUTES.length;i++){
    if(href.indexOf(CONFIRM_ROUTES[i].match) !== -1){
      e.preventDefault();
      confirmAction(CONFIRM_ROUTES[i], href);
      return;
    }
  }
  a.classList.add('is-loading');
  var nl = document.getElementById('nav-label');
  if(nl) nl.textContent = a.textContent.trim();
  var ov = document.getElementById('navloader');
  if(ov) ov.classList.add('show');
}, true);
function confirmAction(route, href){
  var modal=document.getElementById('confirm-modal');
  document.getElementById('confirm-title').textContent = route.title;
  document.getElementById('confirm-msg').textContent = route.msg;
  modal.classList.add('show');
  document.getElementById('confirm-cancel').onclick=function(){ modal.classList.remove('show'); };
  document.getElementById('confirm-ok').onclick=function(){
    // Navigate FIRST — a blocked sessionStorage (private mode / strict
    // browsers) must never be able to swallow the actual update action.
    window.location.href = href;
    try{
      safeStore('removeItem','logDismissed');
      safeStore('removeItem','hermesLogDismissed');
    }catch(x){}
  };
}
function safeStore(fn, key, val){
  try{ return val===undefined ? sessionStorage[fn](key) : sessionStorage[fn](key,val); }
  catch(x){ return null; }
}
</script>"""


def render_poll_script() -> str:
    return SSE_SCRIPT.replace("__TOKEN__", TOKEN)


def render_log_card(log_text: str, result: dict | None = None) -> str:
    body = html.escape(log_text) if log_text.strip() else "(menunggu output…)"
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
        f'<div>{ICON_TERMINAL} Log update 9router {badge}</div>'
        f'<button type="button" class="btn" style="width:auto;padding:0.2rem 0.6rem;font-size:0.72rem;margin:0" '
        f"onclick=\"safeStore('setItem','logDismissed','1');document.getElementById('router-log-card').remove();if(window.syncLogUI)syncLogUI()\">"
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
        f'<div class="patch-notes-title">📝 Patch Notes ({html.escape(title)}):</div>'
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
    with _hermes_update_lock:
        result = dict(_hermes_update_result)
        result["running"] = is_hermes_updating()
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
                                 "summary": "Update Hermes berjalan…", "finished_at": 0.0}

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
                    os.killpg(proc.pid, signal.SIGTERM)
                    log.write("\n[panel] ERROR: timeout 900 detik\n")
                    rc = 124
            if rc == 0:
                summary = "Hermes update sukses: dependency, validasi, dan restart selesai"
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
        models = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m]
        with _models_cache_lock:
            _models_cache["val"] = get_available_models()
            _models_cache["at"] = time.time()
        return {"status": "success", "count": len(models), "host": host}
    except Exception as exc:
        return {"status": "failed", "error": str(exc), "host": host}


def reload_panel_config() -> dict:
    """Reload dynamic caches from disk and refresh 9router models/version info."""
    global _router_host_cache, _router_host_at
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
    result = {}
    key = get_router_api_key()
    if not key:
        return {"9router (Combos)": []}
    try:
        host = get_9router_host()
        req = urllib.request.Request(
            ROUTER_MODELS_URL.format(host=host), headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=MODELS_TIMEOUT) as resp:
            data = json.load(resp)

        for item in data.get("data", []):
            if not isinstance(item, dict):
                continue
            mid = item.get("id", "")
            if not mid:
                continue
            ob = str(item.get("owned_by", "")).lower()

            if ob == "combo":
                group = "9router (Combos)"
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
                group = f"{ob.upper()}"
            else:
                group = "Lainnya"

            result.setdefault(group, []).append(mid)

        ordered_result = {}
        if "9router (Combos)" in result:
            ordered_result["9router (Combos)"] = sorted(result.pop("9router (Combos)"))
        for g in sorted(result.keys()):
            ordered_result[g] = sorted(result[g])
        return ordered_result
    except Exception:
        pass

    return {"9router (Combos)": []}


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
        tmp_path = CONFIG_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp_path, CONFIG_PATH)
        invalidate_config_cache()
        return True
    except Exception:
        return False


# --- Auxiliary Task Models Configuration ---
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
    ("tts_audio_tags", "TTS Audio Tags", "Gemini TTS tag insertion"),
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
            display_val = "auto (use main model)"
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

        tmp_path = CONFIG_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        os.replace(tmp_path, CONFIG_PATH)
        invalidate_config_cache()
        return True
    except Exception:
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

        tmp_path = CONFIG_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        os.replace(tmp_path, CONFIG_PATH)
        invalidate_config_cache()
        return True
    except Exception:
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
        rows.append(
            f'<div class="aux-task-row" data-task="{key}" data-label="{label}">'
            f'  <div class="aux-task-info">'
            f'    <div class="aux-task-title">'
            f'      <span class="aux-task-name">{label}</span>'
            f'      <span class="aux-task-hint">{hint}</span>'
            f'    </div>'
            f'    <div class="{val_cls}">{display_val}</div>'
            f'  </div>'
            f'  <button type="button" class="btn-action-sm" onclick="openAuxPicker(\'{key}\', \'{label}\')">'
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

        tmp_path = CONFIG_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        os.replace(tmp_path, CONFIG_PATH)
        invalidate_config_cache()
        return True
    except Exception:
        return False


def remove_fallback_model(index: int) -> bool:
    """Remove a fallback provider by index in config.yaml."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        fps = cfg.get("fallback_providers")
        if isinstance(fps, list) and 0 <= index < len(fps):
            fps.pop(index)
            tmp_path = CONFIG_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
            os.replace(tmp_path, CONFIG_PATH)
            invalidate_config_cache()
            return True
        return False
    except Exception:
        return False


def render_backup_models_block() -> str:
    """Render list of fallback/backup models for dashboard."""
    models = get_fallback_models_config()
    if not models:
        return (
            '<div class="update-hint" style="margin:0;font-size:0.75rem">'
            'Belum ada model cadangan. Klik "+ Tambah Backup" untuk mengaktifkan fallback otomatis.'
            '</div>'
        )
    rows = []
    for m in models:
        idx = m["index"]
        priority = idx + 1
        model_name = html.escape(m["model"])
        provider = html.escape(m["provider"])
        safe_model = m["model"].replace("'", "\\'")
        rows.append(
            f'<div class="aux-task-row" style="margin-bottom:0.45rem">'
            f'  <div class="aux-task-info">'
            f'    <div class="aux-task-title">'
            f'      <span class="aux-task-name">Backup #{priority}</span>'
            f'      <span class="aux-task-hint">Prioritas {priority}</span>'
            f'    </div>'
            f'    <div class="mono-sub custom" style="font-weight:600">{model_name}</div>'
            f'    <div style="font-size:.68rem;color:var(--text-dim);margin-top:.1rem">{provider}</div>'
            f'  </div>'
            f'  <div style="display:flex;gap:6px;align-items:center">'
            f'    <button type="button" class="btn-action-sm" onclick="openFallbackPicker({idx}, \'{safe_model}\')">'
            f'      Ganti'
            f'    </button>'
            f'    <a class="btn-action-sm btn-action-danger" href="/remove-fallback-model?token={TOKEN}&index={idx}">'
            f'      Hapus'
            f'    </a>'
            f'  </div>'
            f'</div>'
        )
    return "".join(rows)


def restart_bot() -> None:
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/0")
    # Non-blocking: a drain (active task) can make this take a while, and
    # we don't want the HTTP request itself to hang waiting for it — the
    # resulting page shows its own countdown instead.
    subprocess.Popen(["systemctl", "--user", "restart", "hermes-gateway"], env=env)


def bot_action(action: str) -> None:
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
        subprocess.Popen(["systemctl", "--user", "enable", "--now", "hermes-gateway"], env=env)
    elif action == "stop":
        subprocess.Popen(["systemctl", "--user", "disable", "--now", "hermes-gateway"], env=env)


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
    """Format duration into Indonesian units: Xh Yj Zm."""
    if seconds < 0:
        return ""
    s = int(seconds)
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days > 0:
        return f"{days}h {hours}j" if hours > 0 else f"{days}h"
    if hours > 0:
        return f"{hours}j {mins}m" if mins > 0 else f"{hours}j"
    return f"{max(1, mins)}m"


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
    try:
        with urllib.request.urlopen(ROUTER_VERSION_URL.format(host=host), timeout=INFO_TIMEOUT) as resp:
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
        except Exception:
            pass
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
    compose = shlex.quote(ROUTER_COMPOSE_DIR)
    container = shlex.quote(ROUTER_CONTAINER)
    image = shlex.quote(ROUTER_IMAGE)
    return (
        f"cd {compose} && "
        "printf '[1/5] host='; hostname; "
        f"before=$(docker inspect -f '{{{{.Image}}}}' {container} 2>&1); printf '[2/5] before=%s\\n' \"$before\"; "
        "printf '[3/5] Menghentikan container 9router...\\n'; "
        f"docker compose -f {compose}/docker-compose.yml stop 2>&1 || docker stop {container} 2>&1; "
        "printf '[4/5] Mengunduh (pull) image baru...\\n'; "
        f"docker compose -f {compose}/docker-compose.yml pull 2>&1 || docker pull {image} 2>&1; pull_rc=$?; "
        "printf 'compose pull exit=%s\\n' \"$pull_rc\"; "
        "printf '[5/5] Menyalakan kembali 9router...\\n'; "
        f"docker compose -f {compose}/docker-compose.yml up -d 2>&1 || docker start {container} 2>&1; up_rc=$?; "
        "printf 'compose up exit=%s\\n' \"$up_rc\"; "
        f"after=$(docker inspect -f '{{{{.Image}}}}' {container} 2>&1); printf 'after=%s\\n' \"$after\"; "
        "if [ -n \"$before\" ] && [ \"$before\" = \"$after\" ]; then echo 'changed=false'; else echo 'changed=true'; fi; "
        f"printf 'image_id='; docker image inspect {image} --format '{{{{.Id}}}}' 2>&1; "
        "[ \"$up_rc\" -eq 0 ] || exit \"$up_rc\"; "
        "[ \"$pull_rc\" -eq 0 ] || exit \"$pull_rc\""
    )


def cleanup_system_junk() -> dict:
    """Clear update logs, build cache, and old pip/uv caches safely."""
    freed = 0
    # 1. Truncate update logs (keep file descriptors valid)
    for logf in ["/opt/AppData/9router/update.log", "/root/.hermes/logs/update.log"]:
        try:
            if os.path.exists(logf):
                sz = os.path.getsize(logf)
                with open(logf, "w", encoding="utf-8") as f:
                    f.write("")
                freed += sz
        except Exception:
            pass

    # 2. Clear uv / pip download caches
    for cdir in ["/root/.cache/uv", "/root/.cache/pip", "/DATA/AppData/hermes-native/hermes-data/cache/delegation"]:
        try:
            if os.path.exists(cdir):
                for dp, _, fns in os.walk(cdir):
                    for fn in fns:
                        fp = os.path.join(dp, fn)
                        try:
                            freed += os.path.getsize(fp)
                            os.remove(fp)
                        except OSError:
                            pass
        except Exception:
            pass

    # 3. Docker build cache & dangling images (run in background thread to avoid HTTP timeout)
    def _docker_prune():
        try:
            subprocess.run(["docker", "builder", "prune", "-f"], capture_output=True, timeout=30)
            subprocess.run(["docker", "image", "prune", "-f"], capture_output=True, timeout=30)
        except Exception:
            pass

    threading.Thread(target=_docker_prune, daemon=True).start()
    return {"status": "success", "freed_mb": round(freed / (1024 * 1024), 2)}


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
                    proc.kill()
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
            log.write(f"\\n[ERROR] {type(exc).__name__}: {exc}\\n")
            with _router_update_lock:
                _router_update_result = {"status": "failed", "exit_code": 1,
                                         "changed": None,
                                         "summary": f"Update gagal: {type(exc).__name__}",
                                         "finished_at": time.time()}
        finally:
            log.close()
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
        s = socket.create_connection((host, port), timeout=INFO_TIMEOUT)
        s.close()
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
        return used_pct, f"{used_pct:.0f}% ({avail // 1024} MB free)"
    except Exception:
        return 0.0, "?"


_last_cpu_time: tuple[float, float] = (0.0, 0.0)
_cpu_percent_cache: float = 0.0
_cpu_lock = threading.Lock()


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
    """Inspect managed services and Docker containers for Windows Task Manager view."""
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
            "name": "Hermes Gateway (Bot Telegram)",
            "kind": "Systemd Service",
            "status": "Running" if st == "active" else "Stopped",
            "is_active": (st == "active"),
            "pid": pid if pid != "0" else "-",
            "mem_mb": mem,
            "stop_url": f"/bot-toggle?token={TOKEN}",
            "restart_url": f"/restart-bot?token={TOKEN}",
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
            "kind": "Docker Container",
            "status": "Running" if is_run else "Stopped",
            "is_active": is_run,
            "pid": dpid if is_run else "-",
            "mem_mb": dmem,
            "stop_url": f"/process-action?service=9router&action=stop&token={TOKEN}",
            "start_url": f"/process-action?service=9router&action=start&token={TOKEN}",
            "restart_url": f"/process-action?service=9router&action=restart&token={TOKEN}",
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
            "name": "Hermes Control Panel (:9120)",
            "kind": "Systemd Service",
            "status": "Running",
            "is_active": True,
            "pid": str(cur_pid),
            "mem_mb": panel_mem,
            "restart_url": f"/process-action?service=hermes-panel&action=restart&token={TOKEN}",
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
            "name": "Hermes Web Dashboard (:9119)",
            "kind": "Systemd Service",
            "status": "Running" if dash_active else "Stopped",
            "is_active": dash_active,
            "pid": dash_pid,
            "mem_mb": dash_mem,
            "stop_url": f"/toggle?token={TOKEN}",
            "start_url": f"/on?token={TOKEN}",
        })
    except Exception:
        pass

    # 5. Cloudflared Tunnel
    try:
        dst, dpid, dmem = get_docker_metric("cloudflared")
        is_run = (dst.lower() == "running")
        procs.append({
            "id": "cloudflared",
            "name": "Cloudflared (Secure Tunnel)",
            "kind": "Docker Container",
            "status": "Running" if is_run else "Stopped",
            "is_active": is_run,
            "pid": dpid if is_run else "-",
            "mem_mb": dmem,
            "restart_url": f"/process-action?service=cloudflared&action=restart&token={TOKEN}",
        })
    except Exception:
        pass

    # 6. Pi-hole DNS
    try:
        dst, dpid, dmem = get_docker_metric("pihole-pihole-1")
        is_run = (dst.lower() == "running")
        procs.append({
            "id": "pihole-pihole-1",
            "name": "Pi-hole (DNS Ad-blocker)",
            "kind": "Docker Container",
            "status": "Running" if is_run else "Stopped",
            "is_active": is_run,
            "pid": dpid if is_run else "-",
            "mem_mb": dmem,
            "restart_url": f"/process-action?service=pihole-pihole-1&action=restart&token={TOKEN}",
        })
    except Exception:
        pass

    return procs


def render_processes_table() -> str:
    """Render Windows Task Manager styled table for running services & containers."""
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
            actions.append(f'<a href="{p["stop_url"]}" class="btn-end-task">Hentikan (End Task)</a>')
        elif not p["is_active"] and p.get("start_url"):
            actions.append(f'<a href="{p["start_url"]}" class="btn-start-task">Nyalakan</a>')
        if p.get("restart_url"):
            actions.append(f'<a href="{p["restart_url"]}" class="btn-restart-task">Restart</a>')

        act_html = " ".join(actions) if actions else "-"
        rows.append(
            f'<tr>'
            f'  <td class="task-name-cell">'
            f'    <div><div style="font-weight:600;color:var(--text)">{name}</div>'
            f'    <div style="font-size:0.72rem;color:var(--text-dim)">{kind}</div></div>'
            f'  </td>'
            f'  <td><span class="badge {badge_cls}">{status_text}</span></td>'
            f'  <td style="text-align:right;font-family:var(--font-mono);font-size:0.75rem;font-variant-numeric:tabular-nums">{pid}</td>'
            f'  <td style="text-align:right;font-family:var(--font-mono);font-size:0.75rem;font-weight:600;font-variant-numeric:tabular-nums">{mem}</td>'
            f'  <td style="text-align:right;white-space:nowrap">{act_html}</td>'
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


def get_emmc_health() -> tuple[str, str]:
    """Check eMMC wear level and pre-EOL status directly from sysfs."""
    try:
        with open("/sys/block/mmcblk2/device/life_time") as f:
            a, b = [int(x, 16) * 10 for x in f.read().split()[:2]]
        with open("/sys/block/mmcblk2/device/pre_eol_info") as f:
            eol = int(f.read().strip(), 16)
        wear = max(a, b)
        cls = "down" if (wear >= 80 or eol == 3) else ("warn" if (wear >= 60 or eol == 2) else "up")
        status = "Urgent" if eol == 3 else ("Warn" if eol == 2 else "Normal")
        return cls, f"{wear}% aus ({status})"
    except Exception:
        return "up", "Normal"


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


def get_disk_info() -> str:
    """Auto-detect all real partitions and show usage for each."""
    # Skip virtual/pseudo filesystems and overlay docker mounts
    skip_prefixes = ("/var/lib/docker", "/boot")
    skip_fstypes = {"tmpfs", "devtmpfs", "squashfs", "sysfs", "proc", "devpts"}
    parts = []
    try:
        with open("/proc/mounts") as f:
            mounts = []
            for line in f:
                dev, mp, fstype = line.split()[:3]
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
        return "?"

    # Assign labels: root = eMMC (mmcblk), others by device type
    for dev, mp, _ in mounts:
        try:
            st = os.statvfs(mp)
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            if total == 0:
                continue
            used_pct = (total - free) / total * 100
            total_gb = total / (1024 ** 3)
            free_gb = free / (1024 ** 3)
            # Label: / = eMMC, /DATA or sdX = by mount point
            if mp == "/":
                label = "eMMC"
            elif mp == "/DATA":
                label = "DATA"
            elif mp.startswith("/mnt/"):
                label = mp.split("/")[-1].upper()
            else:
                label = mp
            parts.append(f"{label} {used_pct:.0f}% ({free_gb:.1f}/{total_gb:.1f} GB)")
        except Exception:
            pass
    return " · ".join(parts) if parts else "?"


def get_disk_pct() -> float:
    """Highest disk usage % across all real mounts (for color coding)."""
    skip_prefixes = ("/var/lib/docker", "/boot")
    skip_fstypes = {"tmpfs", "devtmpfs", "squashfs", "sysfs", "proc", "devpts"}
    worst = 0.0
    try:
        with open("/proc/mounts") as f:
            seen = set()
            for line in f:
                _, mp, fstype = line.split()[:3]
                if fstype in skip_fstypes or mp in seen:
                    continue
                if any(mp.startswith(p) for p in skip_prefixes):
                    continue
                if "/rootfs/" in mp:
                    continue
                seen.add(mp)
                try:
                    st = os.statvfs(mp)
                    total = st.f_blocks * st.f_frsize
                    free = st.f_bavail * st.f_frsize
                    if total:
                        worst = max(worst, (total - free) / total * 100)
                except Exception:
                    pass
    except Exception:
        pass
    return worst


def get_uptime() -> str:
    """Human-readable uptime from /proc/uptime."""
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        days, rem = divmod(int(secs), 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        if days > 0:
            return f"{days}h {hours}j {minutes}m"
        return f"{hours}j {minutes}m"
    except Exception:
        return "?"


def get_server_ips() -> tuple[str, str]:
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
    emmc_cls, emmc_text = get_emmc_health()
    zram_text = get_zram_info()
    gw_info = get_gateway_info()
    updating = _router_updating
    all_models = get_available_models_cached() if router_up else {}
    all_flat_models = [m for m_list in all_models.values() for m in m_list]
    model_not_listed = bool(all_flat_models) and model not in all_flat_models

    def cell(cls: str, text: str) -> str:
        return f'<span class="value {cls}"><span class="dot {cls}"></span>{text}</span>'

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
        "dash": f'<span class="value" style="font-size:.72rem;color:var(--text-dim)">{router_host} · {router_ms*1000:.0f}ms</span>' if router_up else '<span class="value down" style="font-size:.72rem">Offline</span>',
        "bot": cell("up" if gw_active else "down", "Aktif" if gw_active else "Mati"),
        "gw": f'<span class="value up">{gw_info}</span>' if gw_active else f'<span class="value down">{gw_info}</span>',
        "model": f'<span class="value {"warn" if model_not_listed else ""}" title="{"Model aktif tidak muncul di daftar model" if model_not_listed else ""}">{html.escape(model)}{" ⚠ tidak terdaftar" if model_not_listed else ""}</span>',
        "providers": f'<div style="display:flex;gap:.3rem;flex-wrap:wrap">{prov_html}</div>',
        "router": cell(
            "up" if router_up else "down",
            (f"Terhubung"
             + (f" · {router_uptime}" if router_uptime else "")
             + f" · {router_host} ({router_ms*1000:.0f}ms) · image {get_router_image_date()}"
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
    }

    fetch_btn = (
        f'<a class="toggle restart" style="width:auto;flex:1;min-height:38px;padding:.4rem .8rem;font-size:.76rem" href="/fetch-models?token={TOKEN}">'
        f'{ICON_REFRESH}Fetch /models</a>'
    )
    reload_btn = (
        f'<a class="toggle restart" style="width:auto;flex:1;min-height:38px;padding:.4rem .8rem;font-size:.76rem" href="/reload-panel-config?token={TOKEN}">'
        f'{ICON_REFRESH}Refresh Config</a>'
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

            badge_html = '<span class="model-chip-badge">FREE</span>' if is_free else ""

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
                chips.append(f'<span class="model-chip active">{chip_inner}</span>')
            else:
                model_query = quote(mm, safe="")
                chips.append(
                    f'<a class="model-chip" href="/switch-model?token={TOKEN}&model={model_query}">{chip_inner}</a>'
                )
        return (
            f'<div class="model-group" style="margin-bottom:1.15rem">'
            f'<div class="model-group-title">{title}</div>'
            f'<div class="models-grid">{"".join(chips)}</div>'
            f'</div>'
        )

    if router_up or all_flat_models:
        groups_html = "".join(render_chips_group(gname, mlist) for gname, mlist in all_models.items() if mlist)
        model_chips = action_hdr + groups_html
    else:
        model_chips = action_hdr + '<span class="model-chip">Model tidak terhubung</span>'

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
            f'{ICON_CLOCK}Menjalankan update 9router di {html.escape(router_host)}…'
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
        cek_btn = (f'<a class="toggle restart" href="/check-update?token={TOKEN}">'
                   f'{ICON_REFRESH}Cek Update 9router</a>')
        router_notes = get_9router_patch_notes()
        router_patch_notes_html = render_patch_notes_block("9router", router_notes)
        if upd == "available":
            update_block = (
                f'<a class="toggle" style="background:linear-gradient(135deg,var(--warning),#d9860bcc);'
                f'color:#141922" href="/update-router?token={TOKEN}">'
                f'{ICON_ARROW_UP_CIRCLE}Update 9router tersedia ({version_label})</a>'
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
                f'<div class="update-hint">{ICON_ALERT_TRIANGLE}Gagal cek update Docker Hub — '
                f'<a href="/update-router?token={TOKEN}">paksa update</a></div>' + cek_btn + router_patch_notes_html
            )
        else:  # checking — the auto-poll picks up the settled result
            update_block = f'<div class="update-hint">{ICON_CLOCK}Mengecek update 9router…</div>' + router_patch_notes_html

    # Hermes update status
    hermes_upd = get_hermes_update()
    hermes_behind = hermes_upd.get("behind", 0)
    hermes_local = hermes_upd.get("local", "?")
    hermes_status = hermes_upd.get("status", "unknown")
    hermes_result = get_hermes_update_result()
    hermes_log = html.escape(tail_hermes_update_log())
    hermes_notes = get_hermes_patch_notes()
    hermes_patch_notes_html = render_patch_notes_block("Hermes Agent", hermes_notes)

    if hermes_result.get("running"):
        cells["hermes"] = f'<span class="value warn">Update berjalan…</span>'
        hermes_update_block = (
            f'<div class="update-hint">{ICON_CLOCK}Updater Hermes sedang berjalan live…</div>'
            + hermes_patch_notes_html
        )
    elif hermes_status == "available":
        cells["hermes"] = f'<span class="value warn">{html.escape(hermes_local)} ({hermes_behind} update tersedia)</span>'
        hermes_update_block = (
            f'<a class="toggle" style="background:linear-gradient(135deg,var(--warning),#d9860bcc);color:#141922" href="/update-hermes?token={TOKEN}">'
            f'{ICON_ARROW_UP_CIRCLE}Update Hermes ({hermes_behind} commit)</a>'
            f'<a class="toggle restart" href="/check-hermes-update?token={TOKEN}">{ICON_REFRESH}Cek Update Hermes</a>'
            + hermes_patch_notes_html
        )
    elif hermes_status == "current":
        cells["hermes"] = f'<span class="value up">{html.escape(hermes_local)} (terbaru)</span>'
        hermes_update_block = (
            f'<div class="update-hint">{ICON_CHECK}Hermes sudah versi terbaru ({html.escape(hermes_local)})</div>'
            f'<a class="toggle restart" href="/check-hermes-update?token={TOKEN}">{ICON_REFRESH}Cek Update Hermes</a>'
            + hermes_patch_notes_html
        )
    else:
        cells["hermes"] = f'<span class="value">{html.escape(hermes_local)}</span>'
        hermes_update_block = f'<div class="update-hint">{ICON_CLOCK}Mengecek update Hermes…</div>' + hermes_patch_notes_html

    # Permanently render Hermes log card whenever log file exists or update result exists
    if hermes_log or hermes_result.get("status") != "idle" or os.path.exists("/root/.hermes/logs/update.log"):
        cls = "up" if hermes_result.get("status") == "success" else ("down" if hermes_result.get("status") == "failed" else "warn")
        summary_text = html.escape(hermes_result.get("summary", "")) if hermes_result.get("summary") else ("Update sedang berjalan…" if hermes_result.get("running") else "Log Terakhir Update Hermes")
        hermes_update_block += (
            f'<div id="hermes-log-card" style="margin-top:0.8rem">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:0.4rem">'
            f'<div class="update-hint {cls}" style="margin:0;flex:1">{summary_text}</div>'
            f'<button type="button" class="btn" style="width:auto;padding:0.2rem 0.6rem;font-size:0.72rem;margin:0" '
            f"onclick=\"safeStore('setItem','hermesLogDismissed','1');document.getElementById('hermes-log-card').remove();if(window.syncLogUI)syncLogUI()\">"
            f'Sembunyikan Log</button>'
            f'</div>'
            f'<div class="logbox">{hermes_log or "(belum ada log)"}</div>'
            f'</div>'
        )

    quick_links_block = (
        (get_open_block_active() if dash_active else OPEN_BLOCK_INACTIVE)
        + (f'<a class="open" href="{get_9router_public_url()}" '
           f'target="_blank">{ICON_EXTERNAL_LINK}Buka 9router</a>')
    )

    dash_label = "Matikan Dashboard" if dash_active else "Nyalakan Dashboard"
    dash_toggle_class = "btn-off" if dash_active else "btn-on"
    bot_label = "Matikan Bot Telegram" if gw_active else "Nyalakan Bot Telegram"
    bot_toggle_class = "btn-off" if gw_active else "btn-on"

    dash_bot_btns_block = (
        f'<a class="toggle {dash_toggle_class}" id="btn-dash-toggle" href="/toggle?token={TOKEN}">{ICON_POWER}{dash_label}</a>'
        f'<a class="toggle {bot_toggle_class}" id="btn-bot-toggle" href="/bot-toggle?token={TOKEN}">{ICON_POWER}{bot_label}</a>'
        f'<a class="toggle restart" href="/restart-bot?token={TOKEN}">{ICON_REFRESH}Restart Bot</a>'
        f'<a class="toggle restart" href="/clean-junk?token={TOKEN}">{ICON_TRASH}Bersihkan Cache</a>'
    )

    cpu_pct = get_cpu_percent()
    try:
        load1, load5, _ = os.getloadavg()
        cell_load = f"{load1:.2f}, {load5:.2f}"
    except Exception:
        cell_load = "?"

    return {
        "cells": cells,
        "model_chips": model_chips,
        "rate_limit_card": rate_limit_card,
        "update_block": update_block,
        "hermes_update_block": hermes_update_block,
        "log_card": log_card,
        "quick_links_block": quick_links_block,
        "dash_bot_btns_block": dash_bot_btns_block,
        "aux_tasks_block": render_aux_tasks_block(),
        "backup_models_block": render_backup_models_block(),
        "processes_table": render_processes_table(),
        "cpu_pct": cpu_pct,
        "ram_pct": round(ram_pct, 1),
        "cell_load": cell_load,
        "updating": updating,
        "dash_active": dash_active,
        "gw_active": gw_active,
    }


def build_status_page(just: str = "", active_tab: str = "") -> str:
    frag = build_fragments()
    dash_active = frag["dash_active"]
    gw_active = frag["gw_active"]

    if just == "start":
        countdown_block = COUNTDOWN_BLOCK.format(
            seconds=STARTUP_COUNTDOWN_SECONDS,
            token=TOKEN,
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
            f'<div class="hint">{ICON_CHECK}Model auxiliary berhasil diperbarui! '
            'Konfigurasi langsung tersimpan ke config.yaml.</div>'
        )
    elif just == "aux-reset":
        countdown_block = (
            f'<div class="hint">{ICON_CHECK}Semua model auxiliary berhasil di-reset ke auto!</div>'
        )
    elif just == "fallback":
        countdown_block = (
            f'<div class="hint">{ICON_CHECK}Model cadangan (fallback) berhasil disimpan ke config.yaml!</div>'
        )
    elif just == "fallback-del":
        countdown_block = (
            f'<div class="hint">{ICON_CHECK}Model cadangan berhasil dihapus.</div>'
        )
    elif just == "restart":
        countdown_block = COUNTDOWN_BLOCK.format(
            seconds=STARTUP_COUNTDOWN_SECONDS,
            token=TOKEN,
            message="Bot Telegram sedang restart...",
        )
    elif just == "bot-off":
        countdown_block = (
            f'<div class="hint">{ICON_PAUSE}Bot Telegram dimatikan di STB ini. '
            'Aman dipakai kalau instance lain (server baru) yang sedang aktif.</div>'
        )
    elif just == "cleaned":
        countdown_block = (
            f'<div class="hint">{ICON_CHECK}Pembersihan berhasil! Log update, cache package, '
            'dan sisa build docker telah dibersihkan.</div>'
        )
    elif just == "hermes-updating":
        countdown_block = COUNTDOWN_BLOCK.format(
            seconds=60,
            token=TOKEN,
            message="Hermes sedang update dan restart...",
        )
    else:
        countdown_block = ""

    models_dict = get_available_models_cached()
    available_models_json = json.dumps(models_dict)

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
        model_chips=frag["model_chips"],
        rate_limit_card=frag["rate_limit_card"],
        log_card=frag["log_card"],
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
        countdown_block=countdown_block,
        open_block=get_open_block_active() if dash_active else OPEN_BLOCK_INACTIVE,
        router_open_block=(f'<a class="open" href="{get_9router_public_url()}" '
                           f'target="_blank">{ICON_EXTERNAL_LINK}Buka 9router</a>'),
        toggle_label="Matikan Dashboard" if dash_active else "Nyalakan Dashboard",
        dash_toggle_class="btn-off" if dash_active else "btn-on",
        bot_toggle_label="Matikan Bot Telegram" if gw_active else "Nyalakan Bot Telegram",
        bot_toggle_class="btn-off" if gw_active else "btn-on",
        token=TOKEN,
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
        icon_bot=ICON_BOT,
        icon_router=ICON_ROUTER,
        icon_hermes=ICON_HERMES,
        icon_activity=ICON_ACTIVITY,
    )


# --- SSE (Server-Sent Events) infrastructure ---
_sse_clients: list = []  # list of (queue.Queue, threading.Event) tuples
_sse_clients_lock = threading.Lock()
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
            data = json.dumps(frag)
        except Exception:
            continue

        # Extract meaningful signals from cells for change detection
        cells = frag.get("cells", {})
        def _extract_status(cell_html: str) -> str:
            import re as _re
            for kw in ("NYALA", "MATI", "Aktif", "Mati", "Terhubung", "Tidak terhubung"):
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
            "ram": cells.get("ram", ""),
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
            "cpu_pct": frag.get("cpu_pct", 0.0),
            "processes_table": frag.get("processes_table", "")[:80],
            "updating": frag.get("updating", False),
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
                    _sse_clients.remove(d)
                except ValueError:
                    pass

# Start SSE push thread
threading.Thread(target=_sse_push_loop, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep it quiet — no noisy access log filling up disk

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
        location = f"/status?token={TOKEN}"
        if just:
            location += f"&just={just}"
        if tab:
            location += f"&tab={tab}"
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        global _last_action_at, _last_model_switch_at, _last_aux_model_at
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        token = (qs.get("token") or [""])[0]

        if token != TOKEN:
            self._send_html("<h1>403 — token salah</h1>", 403)
            return

        if parsed.path in ("/status", "/"):
            just = (qs.get("just") or [""])[0]
            tab = (qs.get("tab") or [""])[0]
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
            import queue
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
                data = json.dumps(frag)
                self.wfile.write(f"event: update\ndata: {data}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15)
                        self.wfile.write(f"event: update\ndata: {msg}\n\n".encode())
                        self.wfile.flush()
                    except queue.Empty:
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

        if parsed.path == "/switch-model":
            requested = (qs.get("model") or [""])[0]
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
                        self._redirect_to_status(just="model")
                        return
            self._redirect_to_status()
            return

        if parsed.path == "/set-aux-model":
            task = (qs.get("task") or [""])[0]
            provider = (qs.get("provider") or [""])[0]
            model = (qs.get("model") or [""])[0]
            is_ajax = bool((qs.get("ajax") or [""])[0]) or "application/json" in self.headers.get("Accept", "")
            now = time.monotonic()
            with _last_aux_model_lock:
                debounced = (now - _last_aux_model_at) < 0.3
                if not debounced:
                    _last_aux_model_at = now
            if not debounced and task:
                set_aux_task_model(task, provider, model)
                if is_ajax:
                    self._send_json({"ok": True, "task": task, "provider": provider, "model": model, "html": render_aux_tasks_block()})
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
                reset_all_aux_tasks()
                self._redirect_to_status(just="aux-reset", tab="auxiliary")
                return
            self._redirect_to_status(tab="auxiliary")
            return

        if parsed.path == "/set-fallback-model":
            index_str = (qs.get("index") or ["-1"])[0]
            try:
                index = int(index_str)
            except ValueError:
                index = -1
            provider = (qs.get("provider") or ["custom:9router"])[0]
            model = (qs.get("model") or [""])[0]
            is_ajax = bool((qs.get("ajax") or [""])[0]) or "application/json" in self.headers.get("Accept", "")
            now = time.monotonic()
            with _last_aux_model_lock:
                debounced = (now - _last_aux_model_at) < 0.3
                if not debounced:
                    _last_aux_model_at = now
            if not debounced and model:
                set_fallback_model(index, provider, model)
                if is_ajax:
                    self._send_json({"ok": True, "html": render_backup_models_block()})
                    return
                self._redirect_to_status(just="fallback", tab="control")
                return
            if is_ajax:
                self._send_json({"ok": False, "reason": "debounced or empty model"})
                return
            self._redirect_to_status(tab="control")
            return

        if parsed.path == "/remove-fallback-model":
            index_str = (qs.get("index") or ["-1"])[0]
            try:
                index = int(index_str)
            except ValueError:
                index = -1
            now = time.monotonic()
            with _last_action_lock:
                debounced = (now - _last_action_at) < DEBOUNCE_SECONDS
                if not debounced:
                    _last_action_at = now
            if not debounced and index >= 0:
                remove_fallback_model(index)
                self._redirect_to_status(just="fallback-del", tab="control")
                return
            self._redirect_to_status(tab="control")
            return

        if parsed.path == "/process-action":
            service = (qs.get("service") or [""])[0]
            action = (qs.get("action") or [""])[0]
            now = time.monotonic()
            with _last_action_lock:
                debounced = (now - _last_action_at) < DEBOUNCE_SECONDS
                if not debounced:
                    _last_action_at = now
            if not debounced and service and action:
                if service == "9router":
                    if action == "stop":
                        subprocess.run(["docker", "compose", "-f", f"{ROUTER_COMPOSE_DIR}/docker-compose.yml", "stop"])
                    elif action == "start":
                        subprocess.run(["docker", "compose", "-f", f"{ROUTER_COMPOSE_DIR}/docker-compose.yml", "up", "-d"])
                    elif action == "restart":
                        subprocess.run(["docker", "restart", "9router"])
                elif service == "cloudflared" and action == "restart":
                    subprocess.run(["docker", "restart", "cloudflared"])
                elif service in ("pihole", "pihole-pihole-1") and action == "restart":
                    subprocess.run(["docker", "restart", "pihole-pihole-1"])
                elif service == "hermes-panel" and action == "restart":
                    def _delayed_restart():
                        time.sleep(0.5)
                        subprocess.run(["systemctl", "restart", "hermes-panel.service"])
                    threading.Thread(target=_delayed_restart, daemon=True).start()
            self._redirect_to_status(tab="status")
            return

        if parsed.path not in (
            "/toggle", "/on", "/off", "/restart-bot", "/bot-toggle",
            "/update-router", "/check-update",
            "/check-hermes-update", "/update-hermes",
            "/clean-junk",
            "/fetch-models", "/reload-panel-config",
            "/set-aux-model", "/reset-aux",
            "/set-fallback-model", "/remove-fallback-model",
            "/process-action",
        ):
            self._send_html("<h1>404</h1>", 404)
            return

        # Action route: perform once (debounced against duplicate/prefetch
        # requests), then redirect — never render an action route directly,
        # so a refresh of the resulting page can never re-trigger it.
        now = time.monotonic()
        with _last_action_lock:
            debounced = (now - _last_action_at) < DEBOUNCE_SECONDS
            if not debounced:
                _last_action_at = now

        just = ""
        target_tab = ""
        if not debounced:
            if parsed.path == "/toggle":
                will_start = not service_active(SERVICE)
                subprocess.run(["systemctl", "start" if will_start else "stop", SERVICE])
                just = "start" if will_start else ""
                target_tab = "control"
            elif parsed.path == "/on":
                subprocess.run(["systemctl", "start", SERVICE])
                just = "start"
                target_tab = "control"
            elif parsed.path == "/off":
                subprocess.run(["systemctl", "stop", SERVICE])
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

        self._redirect_to_status(just=just, tab=target_tab)


class TimeoutThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    timeout = 15


if __name__ == "__main__":
    server = TimeoutThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
