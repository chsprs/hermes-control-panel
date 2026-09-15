#!/usr/bin/env bash
# ==============================================================================
# Hermes Control Panel Uninstaller
# ==============================================================================

set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Error: Uninstaller harus dijalankan sebagai root (gunakan sudo)."
    exit 1
fi

echo "* Menghentikan dan menonaktifkan hermes-panel.service..."
systemctl stop hermes-panel.service 2>/dev/null || true
systemctl disable hermes-panel.service 2>/dev/null || true

if [ -f "/etc/systemd/system/hermes-panel.service" ]; then
    rm -f "/etc/systemd/system/hermes-panel.service"
    systemctl daemon-reload
    echo "✓ Unit service hermes-panel.service berhasil dihapus."
fi

read -r -p "Hapus berkas script di /opt/AppData/hermes-native/hermes-data/scripts? [y/N] " confirm
if [[ "$confirm" =~ ^[Yy]$ ]]; then
    rm -f "/opt/AppData/hermes-native/hermes-data/scripts/dashboard-toggle-server.py"
    if [ -d "/DATA" ]; then
        rm -f "/DATA/AppData/hermes-native/hermes-data/scripts/dashboard-toggle-server.py"
    fi
    echo "✓ Berkas script dashboard berhasil dihapus."
fi

echo "✓ Hermes Control Panel telah di-uninstall."
