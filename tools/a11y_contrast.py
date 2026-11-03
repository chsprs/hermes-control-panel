"""Hitung rasio kontras WCAG 2.1 dari CSS panel yang sebenarnya dikirim ke browser.

Dipakai oleh ``tools/audit_contrast.py`` (laporan) *dan* oleh test statis di
``tests/test_panel.py``. Satu sumber kebenaran: test membaca warna dari
``panel.page_source()`` dan menghitung ulang rasionya, jadi menurunkan kontras
kembali akan langsung menggagalkan test.

Kenapa perlu compositing: elemen kecil di panel ini menumpuk beberapa latar
semi-transparan (panel -> card -> qz-prov -> qz-acct -> badge). Membandingkan
warna teks dengan ``--bg`` saja menghasilkan rasio yang salah.
"""

from __future__ import annotations

import re

# --- matematika WCAG 2.1 ---------------------------------------------------


def _srgb_to_lin(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb) -> float:
    r, g, b = rgb[0], rgb[1], rgb[2]
    return (
        0.2126 * _srgb_to_lin(r)
        + 0.7152 * _srgb_to_lin(g)
        + 0.0722 * _srgb_to_lin(b)
    )


def contrast_ratio(fg, bg) -> float:
    l1, l2 = relative_luminance(fg), relative_luminance(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def over(top, bottom):
    """Composite ``top`` (r, g, b, a) onto opaque ``bottom`` (r, g, b)."""
    r, g, b, a = top
    br, bg_, bb = bottom
    return (r * a + br * (1 - a), g * a + bg_ * (1 - a), b * a + bb * (1 - a))


def to_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(round(max(0, min(255, v)))) for v in rgb[:3])


# --- parsing warna ---------------------------------------------------------

_RGB_FUNC = re.compile(r"rgba?\(([^)]+)\)")


def parse_color(value: str):
    """Parse ``#rgb``, ``#rrggbb`` atau ``rgb[a](...)`` -> (r, g, b, a)."""
    v = value.strip()
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = _RGB_FUNC.search(v)
    if not m:
        raise ValueError("not a color: %r" % value)
    parts = [p.strip() for p in m.group(1).replace("/", ",").split(",") if p.strip()]
    r, g, b = (float(parts[0]), float(parts[1]), float(parts[2]))
    a = float(parts[3]) if len(parts) > 3 else 1.0
    return (r, g, b, a)


TRANS = (0.0, 0.0, 0.0, 0.0)


# --- pembacaan CSS panel ---------------------------------------------------


def css_text(page: str) -> str:
    """Isi elemen ``<style>`` pertama dari halaman panel."""
    start = page.index("<style>") + len("<style>")
    return page[start:page.index("</style>")]


def root_vars(css: str) -> dict:
    """Deklarasi custom property di blok ``:root``."""
    m = re.search(r":root\{\{([^{}]*)\}\}", css)
    if not m:
        raise AssertionError("blok :root tidak ditemukan di CSS panel")
    return dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", m.group(1)))


_RULE = re.compile(r"([^{}]+)\{\{([^{}]*)\}\}")


def iter_rules(css: str):
    """(frozenset selector, body) untuk setiap aturan CSS di panel.

    Selector diambil dari baris terakhir sebelum ``{{`` supaya ``@media`` yang
    membungkus (kemungkinan letaknya) tidak ikut terbaca sebagai selector.
    """
    for m in _RULE.finditer(css):
        raw = m.group(1)
        lines = [ln.strip() for ln in raw.replace("\r", "").split("\n") if ln.strip()]
        if not lines:
            continue
        sel = lines[-1]
        parts = frozenset(p.strip() for p in sel.split(",") if p.strip())
        yield parts, m.group(2)


def rule_table(css: str) -> dict:
    """Peta selector -> body; aturan pertama menang (sama seperti cascade di sini)."""
    table = {}
    for sels, body in iter_rules(css):
        for s in sels:
            table.setdefault(s, body)
    return table


def rule_body(css: str, selector: str, prop: str = None) -> str:
    """Body deklarasi untuk ``selector``; ``prop`` memaksa aturan yang punya properti itu."""
    wanted = [s.strip() for s in selector.split(",") if s.strip()]
    for sels, body in iter_rules(css):
        if all(w in sels for w in wanted):
            if prop is None or decl(body, prop) is not None:
                return body
    raise AssertionError(
        "aturan CSS %s%s tidak ditemukan" % (selector, "" if prop is None else " (%s)" % prop)
    )


def body_background(css: str, variables: dict):
    """Latar ``body`` yang benar-benar mendeklarasikan background (bukan html,body)."""
    return color_of(rule_body(css, "body", "background"), "background", variables)


def decl(body: str, prop: str):
    """Nilai satu properti dari body deklarasi, atau None."""
    m = re.search(r"(?:^|;)\s*" + re.escape(prop) + r"\s*:\s*([^;]+)", body)
    return m.group(1).strip() if m else None


def resolve(value: str, variables: dict, depth: int = 0) -> str:
    """Ganti ``var(--x)`` dengan nilainya (rekursif, satu level per panggilan)."""
    v = value.strip()
    m = re.fullmatch(r"var\((--[a-z0-9-]+)\)", v)
    if m:
        if depth > 8:
            raise AssertionError("siklus var() pada %s" % value)
        return resolve(variables[m.group(1)].strip(), variables, depth + 1)
    return v


def color_of(body: str, prop: str, variables: dict):
    raw = decl(body, prop)
    if raw is None:
        return TRANS
    raw = re.split(r"\s*!important", raw)[0]
    return parse_color(resolve(raw, variables))


# --- rantai latar yang diaudit --------------------------------------------
#
# ``BG`` memetakan nama lapisan -> daftar selector latar dari luar ke dalam.
# ``picker-opt`` dibangun di dalam modal, jadi rantainya ditangani khusus.

BG = {
    "card": [".card"],
    "qz-prov": [".card", ".qz-prov"],
    "qz-prov-head": [".card", ".qz-prov", ".qz-prov-head"],
    "qz-acct": [".card", ".qz-prov", ".qz-acct"],
    "model-chip": [".card", ".model-chip"],
    "model-chip-active": [".card", ".model-chip.active"],
    "picker-opt": [".confirm-box", ".aux-model-opt"],
}


# Latar modal: backdrop #aux-picker-modal lalu .confirm-box.
def modal_background(css: str, variables: dict):
    body_bg = body_background(css, variables)
    acc = (body_bg[0], body_bg[1], body_bg[2])
    backdrop = re.search(r"#confirm-modal,\s*#aux-picker-modal[^{]*\{\{([^{}]*)\}\}", css)
    if not backdrop:
        raise AssertionError("aturan backdrop modal tidak ditemukan")
    acc = over(color_of(backdrop.group(1), "background", variables), acc)
    acc = over(color_of(rule_body(css, ".confirm-box"), "background", variables), acc)
    return acc


def effective_background(css: str, variables: dict, chain: str):
    """Latar efektif untuk nama lapisan; ``picker-opt`` menangani modal sendiri."""
    if chain not in BG and chain != "picker-box":
        raise AssertionError("lapisan latar %r tidak dikenal" % chain)
    if chain == "picker-box":
        return modal_background(css, variables)
    if chain == "picker-opt":
        acc = modal_background(css, variables)
        return over(color_of(rule_body(css, ".aux-model-opt"), "background", variables), acc)
    body_bg = body_background(css, variables)
    acc = (body_bg[0], body_bg[1], body_bg[2])
    for sel in BG[chain]:
        acc = over(color_of(rule_body(css, sel), "background", variables), acc)
    return acc


# --- elemen yang diaudit ---------------------------------------------------
#
# (id, selector sumber warna teks, properti, lapisan latar, ambang)
# ``selector=None`` berarti warna diambil dari ``extra_css`` (aturan turunan /
# gaya inline yang tidak punya aturan sendiri).
AUDIT = [
    # --- tab Kuota: badge status ---
    ("qz-badge (default)", ".qz-badge", "color", "qz-acct", 4.5),
    ("qz-badge.ok", ".qz-badge.ok", "color", "qz-acct", 4.5),
    ("qz-badge.low", ".qz-badge.low", "color", "qz-acct", 4.5),
    ("qz-badge.exhausted", ".qz-badge.exhausted,.qz-badge.error", "color", "qz-acct", 4.5),
    ("qz-badge.no_access", ".qz-badge.no_access,.qz-badge.unsupported", "color", "qz-acct", 4.5),
    ("qz-badge.auth_expired", ".qz-badge.auth_expired", "color", "qz-acct", 4.5),
    ("qz-badge.tier", ".qz-badge.tier", "color", "qz-acct", 4.5),
    # --- tab Kuota: meta teks kecil ---
    ("qz-prov-meta", ".qz-prov-meta", "color", "qz-prov-head", 4.5),
    ("qz-acct-sub", ".qz-acct-sub", "color", "qz-acct", 4.5),
    ("qz-row-foot", ".qz-row-foot", "color", "qz-acct", 4.5),
    ("qz-row-pct.muted", ".qz-row-pct.muted", "color", "qz-acct", 4.5),
    ("qz-det .qz-row-label", ".qz-det .qz-row-label", "color", "qz-acct", 4.5),
    ("qz-caret", ".qz-caret", "color", "qz-acct", 4.5),
    ("qz-note", ".qz-note", "color", "card", 4.5),
    ("qz-empty", ".qz-empty", "color", "card", 4.5),
    # --- kartu Model ---
    ("model-chip-badge", ".model-chip-badge", "color", "model-chip", 4.5),
    ("model-group-title", ".model-group-title", "color", "card", 4.5),
    ("model-chip (idle)", ".model-chip", "color", "card", 4.5),
    ("model-chip.active", ".model-chip.active", "color", "model-chip-active", 4.5),
    ("aux-model-opt-sub", ".aux-model-opt-sub", "color", "picker-opt", 4.5),
    # --- tombol ---
    ("btn-action-sm", ".btn-action-sm", "color", "card", 4.5),
    ("btn-action-primary", ".btn-action-primary", "color", "card", 4.5),
    ("btn-action-danger", ".btn-action-danger", "color", "card", 4.5),
]

# Badge/label yang dibangun JavaScript dengan gaya inline (pemilih model).
# ``bg_group`` = nomor grup regex yang memuat warna latar (None = pakai latar
# rantai ``chain``). Nilai diambil dari sumber, bukan disalin ke test.
INLINE_AUDIT = [
    dict(
        name="model-chip-badge 9ROUTER (inline JS)",
        pattern=r'class="model-chip-badge" style="background:(rgba\([^)]*\));color:([^";]+)"',
        bg_group=1,
        fg_group=2,
        chain="model-chip",
        need=4.5,
    ),
    dict(
        name="judul grup pemilih model (inline JS)",
        pattern=r'text-transform:uppercase;color:([^;"]+);margin-top:0\.4rem;padding:0 0\.2rem',
        bg_group=None,
        fg_group=1,
        chain="picker-box",
        need=4.5,
    ),
]
