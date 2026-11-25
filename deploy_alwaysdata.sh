#!/usr/bin/env bash
# Tarona Izla — Alwaysdata (1GB disk, 256MB RAM, 0.25 CPU) 1-buyruqli o'rnatish skripti
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
ACCOUNT="$(whoami)"
FFMPEG_BIN="$HOME/bin/ffmpeg"

echo "========================================================"
echo "  Tarona Izla — Alwaysdata O'rnatish ($ACCOUNT)"
echo "========================================================"

# 1. Disk holatini ko'rsatish
echo "[1/5] Disk tekshirilmoqda..."
df -h "$HOME" || true

# 2. FFmpeg o'rnatish (agar tizimda bo'lmasa ~/bin/ffmpeg ga statik versiya)
echo "[2/5] FFmpeg tekshirilmoqda..."
if command -v ffmpeg >/dev/null 2>&1; then
    FFMPEG_BIN="$(command -v ffmpeg)"
    echo "      Tizimda topildi: $FFMPEG_BIN"
elif [ -x "$FFMPEG_BIN" ]; then
    echo "      Mavjud: $FFMPEG_BIN"
else
    echo "      Statik ffmpeg yuklanmoqda (~40 MB)..."
    mkdir -p "$HOME/bin"
    TMP_FF="$(mktemp -d)"
    curl -fsSL "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz" -o "$TMP_FF/ffmpeg.tar.xz"
    tar -xJf "$TMP_FF/ffmpeg.tar.xz" -C "$TMP_FF"
    cp "$TMP_FF"/ffmpeg-*-amd64-static/ffmpeg "$FFMPEG_BIN"
    chmod +x "$FFMPEG_BIN"
    rm -rf "$TMP_FF"
    echo "      O'rnatildi: $FFMPEG_BIN"
fi

# 3. Python venv (--no-cache-dir disk tejash uchun)
echo "[3/5] Python muhiti (venv) va kutubxonalar o'rnatilmoqda..."
cd "$APP_DIR"
PYTHON_BIN="$(command -v python3.12 || command -v python3.11 || command -v python3)"
echo "      Python: $($PYTHON_BIN --version)"
"$PYTHON_BIN" -m venv venv
./venv/bin/pip install --no-cache-dir --upgrade pip
./venv/bin/pip install --no-cache-dir -r requirements.txt

# 4. .env faylini Alwaysdata Webhook + Low-RAM uchun sozlash
echo "[4/5] .env sozlanmoqda (Webhook + Low-RAM)..."
if [ ! -f "$APP_DIR/.env" ]; then
    echo "XATO: $APP_DIR/.env fayli topilmadi!"
    exit 1
fi

SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
grep -q "^LOW_RAM_MODE=" "$APP_DIR/.env" || echo "LOW_RAM_MODE=1" >> "$APP_DIR/.env"
grep -q "^FFMPEG_PATH=" "$APP_DIR/.env" || echo "FFMPEG_PATH=$FFMPEG_BIN" >> "$APP_DIR/.env"
grep -q "^WEBHOOK_HOST=" "$APP_DIR/.env" || echo "WEBHOOK_HOST=https://${ACCOUNT}.alwaysdata.net" >> "$APP_DIR/.env"
grep -q "^WEBHOOK_PATH=" "$APP_DIR/.env" || echo "WEBHOOK_PATH=/webhook" >> "$APP_DIR/.env"
grep -q "^WEBHOOK_SECRET=" "$APP_DIR/.env" || echo "WEBHOOK_SECRET=${SECRET}" >> "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"
mkdir -p "$APP_DIR/downloads"

echo "[5/5] Tayyor! Disk sarfi:"
du -sh "$APP_DIR" "$HOME/bin" 2>/dev/null || true

echo ""
echo "========================================================"
echo "  Endi Alwaysdata panelida quyidagini kiriting:"
echo "  Web -> Sites -> (mavjud saytni tahrirlang -> Edit)"
echo "    Type:              User program"
echo "    Command:           $APP_DIR/venv/bin/python -u $APP_DIR/bot.py"
echo "    Working directory: $APP_DIR"
echo "  So'ng 'Submit' va 'Restart' tugmasini bosing!"
echo "========================================================"
