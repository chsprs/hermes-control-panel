#!/usr/bin/env bash
# ==============================================================================
# Hermes Agent & Hermes Control Panel Installer
# Automated setup for Linux SBC / STB / VPS (ARM64 / x86_64)
# Repository: https://github.com/chsprs/hermes-control-panel
# ==============================================================================

set -euo pipefail

# Text styling
BOLD='\033[1m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BOLD}${BLUE}=====================================================${NC}"
echo -e "${BOLD}${BLUE}    Hermes Agent & Control Panel Installer           ${NC}"
echo -e "${BOLD}${BLUE}=====================================================${NC}"

# 1. Root Check
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Error: Installer harus dijalankan sebagai root (gunakan sudo).${NC}"
    exit 1
fi

TARGET_USER="${SUDO_USER:-root}"
INSTALL_DIR="/opt/AppData/hermes-native/hermes-data/scripts"
SECONDARY_DIR="/DATA/AppData/hermes-native/hermes-data/scripts"
SERVICE_FILE="/etc/systemd/system/hermes-panel.service"
REPO_URL="https://github.com/chsprs/hermes-control-panel.git"
RAW_BASE_URL="https://raw.githubusercontent.com/chsprs/hermes-control-panel/main"

# Generate random secure token if not supplied via environment
if [ -z "${PANEL_TOKEN:-}" ]; then
    PANEL_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
fi
PANEL_PORT="${PANEL_PORT:-9120}"

echo -e "${GREEN}* Memeriksa dan memasang dependensi sistem...${NC}"
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq python3 python3-yaml curl git lsof systemd >/dev/null 2>&1
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q python3 python3-pyyaml curl git lsof systemd
elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache python3 py3-yaml curl git lsof
fi

# 2. Check & Install Hermes Agent
echo -e "${GREEN}* Memeriksa instalasi Hermes Agent...${NC}"
if ! command -v hermes >/dev/null 2>&1; then
    echo -e "${YELLOW}! Hermes Agent belum terpasang. Memasang Hermes Agent resmi...${NC}"
    curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
    echo -e "${GREEN}✓ Hermes Agent berhasil dipasang.${NC}"
else
    echo -e "${GREEN}✓ Hermes Agent sudah terpasang: $(command -v hermes)${NC}"
fi

# 3. Enable Systemd Linger for user
echo -e "${GREEN}* Mengonfigurasi systemd linger untuk ${TARGET_USER}...${NC}"
if command -v loginctl >/dev/null 2>&1; then
    loginctl enable-linger "${TARGET_USER}" || true
    echo -e "${GREEN}✓ Linger aktif (gateway berjalan otomatis saat boot tanpa login).${NC}"
fi

# 4. Setup Control Panel Directories & Files
echo -e "${GREEN}* Menyiapkan direktori Control Panel di ${INSTALL_DIR}...${NC}"
mkdir -p "${INSTALL_DIR}"
if [ -d "/DATA" ]; then
    mkdir -p "${SECONDARY_DIR}"
fi

# Determine source files (local dir if cloned, otherwise download via curl)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SCRIPT_DIR}/dashboard-toggle-server.py" ]; then
    echo -e "${GREEN}* Menyalin berkas lokal...${NC}"
    cp -f "${SCRIPT_DIR}/dashboard-toggle-server.py" "${INSTALL_DIR}/dashboard-toggle-server.py"
    if [ -d "/DATA" ]; then
        cp -f "${SCRIPT_DIR}/dashboard-toggle-server.py" "${SECONDARY_DIR}/dashboard-toggle-server.py"
    fi
else
    echo -e "${GREEN}* Mengunduh berkas dashboard terbaru dari GitHub...${NC}"
    curl -fsSL "${RAW_BASE_URL}/dashboard-toggle-server.py" -o "${INSTALL_DIR}/dashboard-toggle-server.py"
    if [ -d "/DATA" ]; then
        cp -f "${INSTALL_DIR}/dashboard-toggle-server.py" "${SECONDARY_DIR}/dashboard-toggle-server.py"
    fi
fi

chmod +x "${INSTALL_DIR}/dashboard-toggle-server.py"

# 5. Setup Systemd Service
echo -e "${GREEN}* Memasang systemd service (${SERVICE_FILE})...${NC}"
cat <<EOF > "${SERVICE_FILE}"
[Unit]
Description=Hermes Control Panel
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/dashboard-toggle-server.py
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1
Environment=PANEL_TOKEN=${PANEL_TOKEN}
Environment=PANEL_PORT=${PANEL_PORT}

[Install]
WantedBy=multi-user.target
EOF

# Free port 9120 if held by zombie process
ZOMBIE_PID=$(lsof -ti :${PANEL_PORT} || true)
if [ -n "${ZOMBIE_PID}" ]; then
    echo -e "${YELLOW}! Menghentikan proses lama di port ${PANEL_PORT} (PID: ${ZOMBIE_PID})...${NC}"
    kill -9 ${ZOMBIE_PID} 2>/dev/null || true
fi

systemctl daemon-reload
systemctl enable hermes-panel.service
systemctl restart hermes-panel.service

# 6. Verify Service Status
sleep 2
if systemctl is-active --quiet hermes-panel.service; then
    echo -e "${GREEN}✓ hermes-panel.service aktif dan berjalan.${NC}"
else
    echo -e "${RED}✗ hermes-panel.service gagal menyala. Cek log: journalctl -u hermes-panel.service -n 20${NC}"
    exit 1
fi

# Detect Server IP
PRIMARY_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")

echo -e "\n${BOLD}${GREEN}=====================================================${NC}"
echo -e "${BOLD}${GREEN}       Instalasi Berhasil Selesai!                   ${NC}"
echo -e "${BOLD}${GREEN}=====================================================${NC}"
echo -e "${BOLD}Buka browser di:${NC}"
echo -e "  ${BLUE}http://${PRIMARY_IP}:${PANEL_PORT}/?token=${PANEL_TOKEN}${NC}"
echo -e "\n${BOLD}Manajemen Service:${NC}"
echo -e "  Status : systemctl status hermes-panel.service"
echo -e "  Restart: systemctl restart hermes-panel.service"
echo -e "  Logs   : journalctl -u hermes-panel.service -f"
echo -e "${BOLD}${GREEN}=====================================================${NC}\n"
