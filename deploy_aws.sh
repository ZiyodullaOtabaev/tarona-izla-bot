#!/usr/bin/env bash
# Tarona Izla — AWS Ubuntu (24.04 / 22.04) 1-buyruqli o'rnatish va 24/7 systemd sozlash skripti
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="tarona-bot"
RUN_USER="${SUDO_USER:-$(whoami)}"

echo "================================================"
echo "  Tarona Izla — AWS Ubuntu 24/7 O'rnatish"
echo "================================================"

echo "[1/5] Tizim paketlari va FFmpeg o'rnatilmoqda..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg curl ca-certificates

echo "[2/5] Python virtual muhiti (venv) yaratilmoqda..."
cd "$APP_DIR"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "[3/5] .env fayli tekshirilmoqda..."
if [ ! -f "$APP_DIR/.env" ]; then
    echo "XATO: $APP_DIR/.env fayli topilmadi!"
    echo "Avval .env faylga BOT_TOKEN=... yozing va skriptni qayta ishga tushiring."
    exit 1
fi
chmod 600 "$APP_DIR/.env"

echo "[4/5] 24/7 systemd xizmati ($SERVICE_NAME) sozlanmoqda..."
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=Tarona Izla Telegram Bot (Turbo + Pro Admin Panel 2.0)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
ExecStartPre=/bin/rm -rf ${APP_DIR}/downloads/*
ExecStart=${APP_DIR}/venv/bin/python -u ${APP_DIR}/bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

echo "[5/5] Xizmat ishga tushirilmoqda..."
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"

sleep 2
sudo systemctl status "${SERVICE_NAME}" --no-pager -l

echo ""
echo "================================================"
echo "  Muvaffaqiyatli ishga tushdi!"
echo "  Loglarni ko'rish: sudo journalctl -u ${SERVICE_NAME} -f"
echo "  Qayta yuklash:    sudo systemctl restart ${SERVICE_NAME}"
echo "  To'xtatish:       sudo systemctl stop ${SERVICE_NAME}"
echo "================================================"
