# Hermes Control Panel

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Linux%20ARM64%20%7C%20x86__64-brightgreen.svg)](https://github.com/chsprs/hermes-control-panel)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Control panel web ultra-ringan (RAM <20MB, zero external frameworks, Python standard library murni + SSE push) untuk memonitor dan mengelola [Hermes Agent](https://github.com/NousResearch/hermes-agent) serta [9router](https://github.com/decolua/9router) pada server Linux SBC, STB Android Box (Armbian S905X/S905X3), Mini PC, maupun VPS.

---

## 🚀 Fitur Utama

- ⚡ **Ultra-Ringan & Efisien**: Berjalan menggunakan Python standard library (`http.server`, `threading`, `json`, `urllib`). Tanpa dependensi framework berat (Node.js/React/Vue), konsumsi memori hanya ~18–20MB RAM.
- 🔄 **Real-time SSE Push**: Metrik sistem (RAM, ZRAM, suhu CPU, kesehatan eMMC/HDD, status bot Telegram, IP LAN/Tailscale) diperbarui secara live via *Server-Sent Events* tanpa membebani browser.
- 🎛️ **Manajemen Model 9router**: Ganti default model Hermes langsung dari browser. Daftar model dikelompokkan rapi (9router Combos, OpenCode Zen Free, Nous Portal Free, Provider Lain).
- 🛡️ **Model Cadangan (Fallback)**: Atur urutan prioritas model backup jika model utama mengalami limit / error (HTTP 429 / 500) langsung ke `config.yaml`.
- 🧩 **Auxiliary Task Models**: Konfigurasi model AI spesifik untuk tugas khusus (Vision, Context Compression, Skills Hub, MCP, Delegation, Approval, Title Generation, Triage, Curator) mirip dashboard resmi.
- ⚡ **Interaksi Asinkron (AJAX)**: Pemilihan chip model dan tugas auxiliary menggunakan modal pencarian live real-time dengan update DOM instan tanpa reload halaman penuh.
- 🧹 **Pembersih Sampah & Cache**: Truncate log update, bersihkan cache paket `uv`/`pip`, dan bersihkan cache layer docker dangling untuk melegakan penyimpanan eMMC.
- 📦 **Updater Terintegrasi**:
  - Update 9router Docker container dengan proteksi OOM (otomatis menghentikan container sebelum melakukan `pull` image).
  - Update native Hermes Agent resmi (`hermes update --yes`).
- 📱 **Mobile UI/UX Modern**: Desain Apple Control Center dark-mode elegan, responsif di HP/tablet/laptop.

---

## 📥 Instalasi Cepat (One-Liner)

Jalankan perintah berikut di terminal server Linux (sebagai root / sudo):

```bash
curl -fsSL https://raw.githubusercontent.com/chsprs/hermes-control-panel/main/install.sh | sudo bash
```

Installer otomatis:
1. Memasang dependensi sistem (`python3`, `python3-yaml`, `curl`, `git`, `lsof`).
2. Memasang **Hermes Agent** resmi jika belum terpasang.
3. Mengonfigurasi `loginctl enable-linger` agar gateway menyala saat boot tanpa perlu login SSH.
4. Memasang script dashboard dan unit service `hermes-panel.service`.
5. Menjalankan service di port `9120`.

---

## 🛠️ Instalasi Manual

Jika ingin meng-clone repositori secara manual:

```bash
# 1. Clone repository
git clone https://github.com/chsprs/hermes-control-panel.git /opt/hermes-control-panel
cd /opt/hermes-control-panel

# 2. Berikan izin eksekusi
chmod +x install.sh uninstall.sh dashboard-toggle-server.py

# 3. Jalankan installer
sudo ./install.sh
```

---

## ⚙️ Konfigurasi Environment

Service dikelola melalui systemd pada berkas `/etc/systemd/system/hermes-panel.service`. Anda dapat menyesuaikan konfigurasi dengan menambahkan `Environment`:

| Variabel | Default | Keterangan |
|---|---|---|
| `PANEL_TOKEN` | `vita-stb-2026` | Token autentikasi URL panel |
| `PANEL_PORT` | `9120` | Port listening HTTP web panel |
| `HERMES_CONFIG_PATH` | `/root/.hermes/config.yaml` | Lokasi berkas konfigurasi Hermes |
| `ROUTER_COMPOSE_DIR` | `/opt/AppData/9router` | Direktori docker-compose 9router |
| `ROUTER_DB_PATH` | `/DATA/AppData/9router/db/data.sqlite` | Lokasi database SQLite 9router |

Contoh kustomisasi token & port:
```ini
[Service]
Environment=PANEL_TOKEN=rahasia123
Environment=PANEL_PORT=8080
```
Setelah mengubah berkas service, terapkan perubahan:
```bash
sudo systemctl daemon-reload
sudo systemctl restart hermes-panel.service
```

---

## 🔧 Manajemen Service

```bash
# Cek status panel
sudo systemctl status hermes-panel.service

# Restart panel
sudo systemctl restart hermes-panel.service

# Melihat log langsung
sudo journalctl -u hermes-panel.service -f

# Uninstall panel
sudo ./uninstall.sh
```

---

## 📄 Lisensi

Proyek ini dilisensikan di bawah lisensi [MIT](LICENSE).
