#!/usr/bin/env bash
# Build DouyinVideoManager.app tren macOS.
# Chay:  chmod +x build_mac.sh && ./build_mac.sh
set -e
cd "$(dirname "$0")"

APP_NAME="DouyinVideoManager"
ICON="assets/app_icon.icns"

echo "============================================"
echo " Build $APP_NAME (macOS .app)"
echo "============================================"

# --- Tim Python 3 ---
if ! command -v python3 >/dev/null 2>&1; then
    echo "[LOI] Khong tim thay python3. Cai bang: brew install python python-tk"
    exit 1
fi
# --- Kiem tra tkinter (giao dien app can) ---
if ! python3 -c "import tkinter" >/dev/null 2>&1; then
    echo "[LOI] Python nay thieu tkinter. Cai bang: brew install python-tk"
    echo "      (hoac cai Python tu python.org, ban do da kem tkinter)"
    exit 1
fi
echo "Dung Python: $(python3 --version)"

# --- Dung moi truong ao de khong dung vao Python he thong ---
echo; echo "[1/4] Cai dat thu vien..."
python3 -m venv .venv-build
source .venv-build/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# --- Icon ---
echo; echo "[2/4] Kiem tra icon..."
[ -f "$ICON" ] || python make_icon.py
[ -f "$ICON" ] || { echo "[LOI] Khong tao duoc $ICON"; exit 1; }

# --- Don ban cu ---
echo; echo "[3/4] Don dep ban build cu..."
rm -rf build dist "$APP_NAME.spec"

# --- Build ---
# --windowed : app giao dien, khong mo Terminal
# --icon     : icon .icns cua app
# --add-data : dong goi thu muc assets (dau ':' tren Mac, Windows moi dung ';')
# Mac khong dung --onefile: --windowed se tao thang goi .app
echo; echo "[4/4] Dang build, vui long doi..."
python -m PyInstaller --noconfirm --clean --windowed \
    --name "$APP_NAME" \
    --icon "$ICON" \
    --add-data "assets:assets" \
    --hidden-import openpyxl \
    --hidden-import PIL \
    --hidden-import PIL.ImageFont \
    --hidden-import yt_dlp \
    --hidden-import curl_cffi \
    main.py

# --- Nen thanh .zip de gui cho nguoi khac ---
( cd dist && ditto -c -k --keepParent "$APP_NAME.app" "$APP_NAME-mac.zip" )

echo
echo "============================================"
echo " BUILD THANH CONG"
echo " App : $(pwd)/dist/$APP_NAME.app"
echo " Zip : $(pwd)/dist/$APP_NAME-mac.zip"
echo "============================================"
echo "Luu y: ghep audio can ffmpeg + ffprobe (brew install ffmpeg)."
echo "Lan dau mo app: chuot phai > Open (hoac chay: xattr -cr dist/$APP_NAME.app)"
open dist
