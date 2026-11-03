#!/usr/bin/env python3
"""Health check ringkas untuk Hermes Control Panel (stdlib saja).

Dipakai untuk memantau panel dari terminal atau cron:

    python3 tools/panel-health-check.py                 # ringkas, exit != 0 bila ada yang gagal
    python3 tools/panel-health-check.py --json          # keluaran JSON untuk parsing
    python3 tools/panel-health-check.py --base http://127.0.0.1:1000

Yang diperiksa:
- `/status` merespons 200 dan berukuran wajar;
- `/api/status` mengembalikan fragmen yang diharapkan;
- `/api/perf/series` berbentuk benar (ring buffer riwayat performa);
- stream `/events` mengirim delta pertama dalam batas waktu;
- latensi tiap pemeriksaan dilaporkan (ambang bawaan 2.5s, bisa diatur).

Keluar dengan kode 1 bila ada pemeriksaan yang gagal — cocok untuk cron/alert.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:1000"
DEFAULT_TIMEOUT = 25.0
MIN_STATUS_BYTES = 200_000          # halaman penuh ~490 KB; di bawah ini mencurigakan
REQUIRED_FRAGMENTS = ("cpu_pct", "ram_pct", "processes_table", "profiles_block")


def _get(base: str, path: str, timeout: float) -> tuple[int, bytes, float]:
    t0 = time.perf_counter()
    req = urllib.request.Request(base + path, headers={"User-Agent": "panel-health-check/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, body, time.perf_counter() - t0
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b"", time.perf_counter() - t0
    except (urllib.error.URLError, OSError) as exc:
        # Panel mati/tidak terjangkau: laporkan sebagai kegagalan, bukan traceback.
        return 0, str(exc).encode(), time.perf_counter() - t0


def _first_sse_delta(base: str, timeout: float) -> tuple[bool, float, str]:
    """Buka /events dan ukur waktu sampai delta `update` pertama."""
    t0 = time.perf_counter()
    req = urllib.request.Request(base + "/events", headers={"Accept": "text/event-stream",
                                                            "User-Agent": "panel-health-check/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            while True:
                line = resp.readline()
                if not line:
                    return False, time.perf_counter() - t0, "stream tertutup sebelum ada delta"
                text = line.decode("utf-8", "replace")
                if text.startswith("data:"):
                    payload = text[5:].strip()
                    try:
                        json.loads(payload)
                    except ValueError:
                        continue
                    return True, time.perf_counter() - t0, "delta pertama diterima"
    except (urllib.error.URLError, OSError) as exc:
        return False, time.perf_counter() - t0, f"stream gagal: {exc}"


def run(base: str, timeout: float, slow_s: float) -> dict:
    checks = []

    def add(name: str, ok: bool, detail: str, seconds: float | None = None) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail,
                       "seconds": None if seconds is None else round(seconds, 3),
                       "slow": bool(seconds is not None and seconds > slow_s)})

    code, body, secs = _get(base, "/status", timeout)
    add("GET /status", code == 200 and len(body) >= MIN_STATUS_BYTES,
        f"HTTP {code}, {len(body) // 1024} KB", secs)

    code, body, secs = _get(base, "/api/status", timeout)
    frags = {}
    parse_error = ""
    if code == 200:
        try:
            frags = json.loads(body)
        except ValueError as exc:
            parse_error = str(exc)
    missing = [k for k in REQUIRED_FRAGMENTS if k not in frags]
    add("GET /api/status", code == 200 and not parse_error and not missing,
        parse_error or (f"{len(frags)} fragmen" + (f", hilang: {missing}" if missing else "")), secs)

    code, body, secs = _get(base, "/api/perf/series", timeout)
    ok, detail = False, f"HTTP {code}"
    if code == 200:
        try:
            series = json.loads(body)
            ok = isinstance(series, dict) and series.get("ok") is True and isinstance(series.get("cpu"), list)
            detail = f"{series.get('count', 0)} titik riwayat"
        except ValueError as exc:
            detail = f"JSON tidak valid: {exc}"
    add("GET /api/perf/series", ok, detail, secs)

    sse_ok, sse_secs, sse_detail = _first_sse_delta(base, timeout)
    add("GET /events (delta pertama)", sse_ok, sse_detail, sse_secs)

    failed = [c for c in checks if not c["ok"]]
    slow = [c for c in checks if c["slow"]]
    return {"ok": not failed, "base": base, "checked_at": int(time.time()),
            "failed": len(failed), "slow": len(slow), "checks": checks}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Health check Hermes Control Panel")
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"basis URL (bawaan {DEFAULT_BASE})")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="timeout per permintaan (detik)")
    ap.add_argument("--slow", type=float, default=2.5, help="ambang dianggap lambat (detik)")
    ap.add_argument("--json", action="store_true", help="keluarkan JSON")
    args = ap.parse_args(argv)

    result = run(args.base.rstrip("/"), args.timeout, args.slow)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Hermes Control Panel — {result['base']}")
        for c in result["checks"]:
            mark = "OK  " if c["ok"] else "GAGAL"
            timing = f"{c['seconds']:.2f}s" + (" (lambat)" if c["slow"] else "") if c["seconds"] is not None else "-"
            print(f"  [{mark}] {c['check']:28s} {timing:>12s}  {c['detail']}")
        print("Hasil:", "SEHAT" if result["ok"] else f"{result['failed']} pemeriksaan gagal")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
