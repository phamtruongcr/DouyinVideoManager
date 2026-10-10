@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "APP_NAME=DouyinVideoManager"
set "ICON=assets\app_icon.ico"
set "VENV=.venv-build311"

echo ============================================
echo  Build %APP_NAME% (Windows .exe, Python 3.11)
echo ============================================
echo Goi y: dat BUNDLE_VIENEU=1 truoc khi chay de dong goi san VieNeu vao exe
echo        (file se rat nang). Mac dinh KHONG dong goi: nguoi dung bam
echo        "Cai VieNeu" trong app, app se cai bang Python 3.11 tren may.

rem --- Bat buoc Python 3.11 qua "py" launcher ---
where py >nul 2>nul
if errorlevel 1 (
    echo [LOI] Khong tim thay "py" launcher. Cai Python 3.11 tu https://www.python.org/downloads/
    echo       va tick "py launcher" + "tcl/tk and IDLE" khi cai.
    goto :fail
)
py -3.11 --version >nul 2>nul
if errorlevel 1 (
    echo [LOI] Chua cai Python 3.11. Cai tai https://www.python.org/downloads/release/python-3119/
    echo       roi chay lai file nay. Cac phien ban da cai tren may:
    py -0
    goto :fail
)
for /f "delims=" %%v in ('py -3.11 --version') do echo Dung %%v
py -3.11 -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo [LOI] Python 3.11 nay thieu tkinter. Cai lai bang bo cai python.org
    echo       va tick "tcl/tk and IDLE".
    goto :fail
)

rem --- Moi truong ao rieng de khong dung vao Python khac tren may ---
echo.
echo [1/5] Tao moi truong ao %VENV% ...
if not exist "%VENV%\Scripts\python.exe" (
    py -3.11 -m venv "%VENV%"
    if errorlevel 1 goto :fail
)
set "PY=%VENV%\Scripts\python.exe"

echo.
echo [2/5] Cai dat thu vien...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install --prefer-binary -r requirements.txt
if errorlevel 1 goto :fail
set "VIENEU_ARGS="
if "%BUNDLE_VIENEU%"=="1" (
    echo Dong goi kem VieNeu...
    "%PY%" -m pip install --prefer-binary vieneu
    if errorlevel 1 goto :fail
    set "VIENEU_ARGS=--hidden-import vieneu --collect-all vieneu"
)

echo.
echo [3/5] Kiem tra icon...
if not exist "%ICON%" (
    echo Chua co %ICON%, dang tao icon mac dinh...
    "%PY%" make_icon.py
    if errorlevel 1 goto :fail
)
if not exist "%ICON%" (
    echo [LOI] Khong tao duoc icon: %ICON%
    goto :fail
)

echo.
echo [4/5] Don dep ban build cu...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "%APP_NAME%.spec" del /q "%APP_NAME%.spec"

echo.
echo [5/5] Dang build, vui long doi...
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name "%APP_NAME%" ^
    --icon "%ICON%" ^
    --add-data "assets;assets" ^
    --hidden-import openpyxl ^
    --hidden-import PIL ^
    --hidden-import PIL.ImageFont ^
    --hidden-import yt_dlp ^
    --hidden-import curl_cffi ^
    --hidden-import playwright ^
    --collect-all playwright ^
    %VIENEU_ARGS% ^
    main.py
if errorlevel 1 goto :fail

echo.
echo ============================================
echo  BUILD THANH CONG (Python 3.11)
echo  File chay: %~dp0dist\%APP_NAME%.exe
echo ============================================
echo Luu y 1: ghep audio can ffmpeg + ffprobe (PATH hoac chon trong Cai dat).
echo Luu y 2: de dung VieNeu tren may khac, may do can Python 3.10+ (bam "Cai VieNeu"
echo          trong app) - hoac build voi BUNDLE_VIENEU=1.
echo Luu y 3: neu Explorer van hien icon cu, khoi dong lai Explorer.
if not defined CI (
    explorer "%~dp0dist"
    pause
)
exit /b 0

:fail
echo.
echo [LOI] Build that bai. Xem thong bao phia tren.
if not defined CI pause
exit /b 1
