@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "APP_NAME=DouyinVideoManager"
set "ICON=assets\app_icon.ico"

echo ============================================
echo  Build %APP_NAME% (Windows .exe + icon)
echo ============================================

rem --- Tim Python: uu tien "py" launcher, sau do "python" ---
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [LOI] Khong tim thay Python. Hay cai Python 3.9+ va tick "Add Python to PATH".
    goto :fail
)
echo Dung Python: %PY%

rem --- Cai thu vien can thiet ---
echo.
echo [1/4] Cai dat thu vien...
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto :fail

rem --- Icon: neu chua co assets\app_icon.ico thi tu tao bang make_icon.py ---
rem Muon dung icon rieng: chep file .ico cua ban de len assets\app_icon.ico
echo.
echo [2/4] Kiem tra icon...
if not exist "%ICON%" (
    echo Chua co %ICON%, dang tao icon mac dinh...
    %PY% make_icon.py
    if errorlevel 1 goto :fail
)
if not exist "%ICON%" (
    echo [LOI] Khong tao duoc icon: %ICON%
    goto :fail
)
echo Icon: %ICON%

rem --- Don ban build cu ---
echo.
echo [3/4] Don dep ban build cu...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "%APP_NAME%.spec" del /q "%APP_NAME%.spec"

rem --- Build ---
rem --onefile   : gop tat ca vao 1 file .exe duy nhat
rem --windowed  : khong hien cua so console den
rem --icon      : icon cua file .exe (hien trong Explorer / taskbar khi ghim)
rem --add-data  : dong goi thu muc assets vao exe de cua so app cung dung icon
rem openpyxl / PIL duoc import "luoi" (trong ham) nen can khai bao hidden-import
echo.
echo [4/4] Dang build, vui long doi...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name "%APP_NAME%" ^
    --icon "%ICON%" ^
    --add-data "assets;assets" ^
    --hidden-import openpyxl ^
    --hidden-import PIL ^
    --hidden-import PIL.ImageFont ^
    main.py
if errorlevel 1 goto :fail

echo.
echo ============================================
echo  BUILD THANH CONG
echo  File chay: %~dp0dist\%APP_NAME%.exe
echo ============================================
echo Luu y 1: tinh nang ghep audio can ffmpeg + ffprobe (cai vao PATH
echo hoac chon duong dan trong phan Cai dat cua app).
echo Luu y 2: neu Explorer van hien icon cu, xoa cache icon hoac doi ten
echo file .exe / khoi dong lai Explorer de cap nhat.
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
