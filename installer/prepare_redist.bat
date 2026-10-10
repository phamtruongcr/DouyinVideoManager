@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0prepare_redist.ps1"
if errorlevel 1 (
    echo [LOI] Tai thanh phan that bai. Xem thong bao phia tren.
    if not defined CI pause
    exit /b 1
)
if not defined CI pause
exit /b 0
