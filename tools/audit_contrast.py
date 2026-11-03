#!/usr/bin/env python3
"""Laporan kontras tab Kuota & kartu Model — angka dari CSS yang benar-benar dikirim.

Pakai:
    python3 tools/audit_contrast.py            # tabel teks
    python3 tools/audit_contrast.py --json     # JSON untuk laporan

Ambang WCAG 2.1 SC 1.4.3: 4.5:1 untuk teks normal, 3:1 untuk teks besar.
Komponen yang *nonaktif* (`:disabled`) dikecualikan SC 1.4.3 — tetap diukur dan
dilaporkan terpisah supaya keputusan "tidak diubah" punya bukti angka.
"""

import argparse
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import a11y_contrast as a11y  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def load_page():
    os.environ.setdefault("PANEL_TOKEN", "audit-token")
    os.environ.setdefault("PANEL_PASSWORD", "audit-password")
    spec = importlib.util.spec_from_file_location(
        "panel_audit", os.path.join(REPO_ROOT, "dashboard-toggle-server.py")
    )
    panel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(panel)
    return panel.page_source(), panel


def audit(page):
    css = a11y.css_text(page)
    var = a11y.root_vars(css)
    rows = []
    for name, selector, prop, chain, need in a11y.AUDIT:
        body = a11y.rule_body(css, selector)
        fg = a11y.color_of(body, prop, var)
        bg = a11y.effective_background(css, var, chain)
        flat = (bg[0], bg[1], bg[2])
        ratio = a11y.contrast_ratio((fg[0], fg[1], fg[2]), flat)
        rows.append(
            dict(
                element=name,
                selector=selector,
                fg=a11y.to_hex(fg),
                fg_token=a11y.decl(body, prop),
                bg=a11y.to_hex(flat),
                layer=chain,
                ratio=round(ratio, 2),
                need=need,
                verdict="LULUS" if ratio >= need else "GAGAL",
            )
        )
    return rows


def inline_rows(page):
    """Label yang dibangun JavaScript dengan gaya inline (tanpa aturan CSS)."""
    import re

    css = a11y.css_text(page)
    var = a11y.root_vars(css)
    out = []
    for spec in a11y.INLINE_AUDIT:
        matches = re.findall(spec["pattern"], page)
        if not matches:
            continue
        groups = matches[0] if isinstance(matches[0], tuple) else (matches[0],)
        fg_css = groups[spec["fg_group"] - 1]
        flat = a11y.effective_background(css, var, spec["chain"])
        if spec["bg_group"]:
            flat = a11y.over(a11y.parse_color(groups[spec["bg_group"] - 1]), flat)
        fg = a11y.parse_color(a11y.resolve(fg_css, var))
        ratio = a11y.contrast_ratio((fg[0], fg[1], fg[2]), flat)
        out.append(
            dict(
                element=spec["name"],
                selector="inline",
                fg=a11y.to_hex(fg),
                fg_token=fg_css,
                bg=a11y.to_hex(flat),
                layer=spec["chain"],
                ratio=round(ratio, 2),
                need=spec["need"],
                verdict="LULUS" if ratio >= spec["need"] else "GAGAL",
                occurrences=len(matches),
            )
        )
    return out


def disabled_rows(page, panel_module=None):
    """Tombol nonaktif: opacity .55 pada elemen menurunkan kontras teks.

    WCAG SC 1.4.3 mengecualikan komponen UI nonaktif, jadi baris ini hanya
    informasi — bukan temuan yang wajib diperbaiki. Aturan opacity-nya ada di
    NETWORK_CSS (stylesheet terpisah), jadi dibaca dari sana, bukan dari <style>
    utama halaman.
    """
    import re

    css = a11y.css_text(page)
    var = a11y.root_vars(css)
    network_css = getattr(panel_module, "NETWORK_CSS", "") or ""
    opacity = None
    om = re.search(r"\.btn-action-sm:disabled\{\{?([^{}]*)\}\}?", network_css)
    if om:
        pm = re.search(r"opacity:\s*([\d.]+)", om.group(1))
        if pm:
            opacity = float(pm.group(1))
    if opacity is None:
        raise AssertionError(
            "aturan .btn-action-sm:disabled dengan opacity tidak ditemukan di NETWORK_CSS"
        )
    base = a11y.effective_background(css, var, "card")
    out = []
    for name, selector in (
        ("btn-action-sm:disabled", ".btn-action-sm"),
        ("btn-action-primary:disabled", ".btn-action-primary"),
        ("btn-action-danger:disabled", ".btn-action-danger"),
    ):
        body = a11y.rule_body(css, selector)
        fg = a11y.color_of(body, "color", var)
        el_bg = a11y.over(a11y.color_of(body, "background", var), base)
        eff_fg = tuple(opacity * fg[i] + (1 - opacity) * base[i] for i in range(3))
        eff_bg = tuple(opacity * el_bg[i] + (1 - opacity) * base[i] for i in range(3))
        ratio = a11y.contrast_ratio(eff_fg, eff_bg)
        out.append(
            dict(
                element=name,
                fg=a11y.to_hex(eff_fg),
                bg=a11y.to_hex(eff_bg),
                opacity=opacity,
                ratio=round(ratio, 2),
                need=4.5,
                verdict="dikecualikan (SC 1.4.3 nonaktif)",
            )
        )
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    page, panel_module = load_page()
    rows = audit(page) + inline_rows(page)
    disabled = disabled_rows(page, panel_module)

    if args.json:
        print(json.dumps(dict(elements=rows, disabled=disabled), indent=1, ensure_ascii=False))
        return 0

    hdr = "%-34s %-26s %-9s %-9s %6s %5s %s"
    print(hdr % ("ELEMEN", "TOKEN WARNA", "TEKS", "LATAR", "RASIO", "BUTUH", "STATUS"))
    print("-" * 108)
    for r in sorted(rows, key=lambda x: x["ratio"]):
        print(
            hdr
            % (
                r["element"],
                str(r["fg_token"])[:26],
                r["fg"],
                r["bg"],
                "%.2f" % r["ratio"],
                "%.1f" % r["need"],
                r["verdict"],
            )
        )
    fails = [r for r in rows if r["verdict"] == "GAGAL"]
    print("\n%d elemen diaudit, %d gagal ambang" % (len(rows), len(fails)))
    print("\nKomponen nonaktif (dikecualikan SC 1.4.3, tidak diubah):")
    for r in disabled:
        print("  %-30s rasio %.2f (%s)" % (r["element"], r["ratio"], r["verdict"]))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
