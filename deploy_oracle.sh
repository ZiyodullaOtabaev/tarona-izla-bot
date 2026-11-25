#!/usr/bin/env bash
# Tarona Izla — Oracle Cloud (Ubuntu / Oracle Linux) xavfsiz 24/7 o'rnatish skripti
# Boshqa botlarga umuman tegmaydi va CPU/RAM chegarasi (cgroup) qo'yadi.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="tarona-bot"
RUN_USER="${SUDO_USER:-$(whoami)}"

echo "========================================================"
echo "  Tarona Izla — Oracle Cloud Xavfsiz 24/7 O'rnatish"
echo "========================================================"

# 1. Server resurslarini o'lchash va xavfsiz limit hisoblash
CORES=$(nproc)
TOTAL_RAM_MB=$(free -m | awk '/^Mem:/{print $2}')

if [ "$CORES" -ge 4 ]; then
    CPU_QUOTA="250%"
elif [ "$CORES" -ge 2 ]; then
    CPU_QUOTA="130%"
else
    CPU_QUOTA="70%"
fi

if [ "$TOTAL_RAM_MB" -ge 8000 ]; then
    MEM_MAX="3G"
elif [ "$TOTAL_RAM_MB" -ge 3000 ]; then
    MEM_MAX="1536M"
elif [ "$TOTAL_RAM_MB" -ge 1500 ]; then
    MEM_MAX="768M"
else
    MEM_MAX="450M"
fi

echo "[*] Aniqlangan server: ${CORES} CPU, ${TOTAL_RAM_MB} MB RAM"
echo "[*] Tarona bot uchun xavfsiz chegara: CPUQuota=${CPU_QUOTA}, MemoryMax=${MEM_MAX}"

# 2. Tizim paketlari va FFmpeg (faqat yetishmaganlarini o'rnatadi)
echo "[1/5] Python va FFmpeg tekshirilmoqda..."
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y python3 python3-venv python3-pip ffmpeg curl ca-certificates
elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3 python3-pip curl ca-certificates
    if ! command -v ffmpeg >/dev/null 2>&1; then
        sudo dnf install -y epel-release || true
        sudo dnf install -y ffmpeg || echo "[!] FFmpeg qo'lda o'rnatilishi kerak bo'lishi mumkin"
    fi
fi

# 3. Alohida izolyatsiyalangan Python venv
echo "[2/5] Alohida Python muhiti (venv) yaratilmoqda..."
cd "$APP_DIR"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 4. .env tekshiruvi
echo "[3/5] .env fayli tekshirilmoqda..."
if [ ! -f "$APP_DIR/.env" ]; then
    echo "XATO: $APP_DIR/.env fayli topilmadi!"
    exit 1
fi
chmod 600 "$APP_DIR/.env"
mkdir -p "$APP_DIR/downloads"

# 5. Xavfsiz systemd xizmati (boshqa botlarga tegmaydi!)
echo "[4/5] 24/7 systemd xizmati (${SERVICE_NAME}) sozlanmoqda..."
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
CPUQuota=${CPU_QUOTA}
MemoryMax=${MEM_MAX}
Nice=5

[Install]
WantedBy=multi-user.target
EOF

echo "[5/5] Xizmat ishga tushirilmoqda..."
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"

sleep 3
sudo systemctl status "${SERVICE_NAME}" --no-pager -l

echo ""
echo "========================================================"
echo "  Tarona Izla 24/7 muvaffaqiyatli ishga tushdi!"
echo "  Loglar:       sudo journalctl -u ${SERVICE_NAME} -f"
echo "  Qayta yuklash: sudo systemctl restart ${SERVICE_NAME}"
echo "  To'xtatish:    sudo systemctl stop ${SERVICE_NAME}"
echo "========================================================"
