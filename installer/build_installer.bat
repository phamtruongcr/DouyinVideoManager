@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0\.."

set "VER=%~1"
if "%VER%"=="" set "VER=1.0.0"

echo ============================================
echo  Dong goi bo cai Douyin Video Manager %VER%
echo ============================================

if not exist "dist\DouyinVideoManager.exe" (
    echo [LOI] Chua co dist\DouyinVideoManager.exe. Chay build_windows_py311.bat truoc.
    goto :fail
)

if not exist "installer\redist\vc_redist.x64.exe" goto :prep
if not exist "installer\redist\python-installer.exe" goto :prep
if not exist "installer\redist\ffmpeg\ffmpeg.exe" goto :prep
if not exist "installer\redist\ffmpeg\ffprobe.exe" goto :prep
goto :iscc

:prep
echo Thieu thanh phan trong installer\redist, dang tai ve...
powershell -NoProfile -ExecutionPolicy Bypass -File "installer\prepare_redist.ps1"
if errorlevel 1 goto :fail

:iscc
set "ISCC="
for %%P in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe") do (
    if exist %%P set "ISCC=%%~P"
)
if not defined ISCC (
    where ISCC >nul 2>nul && set "ISCC=ISCC"
)
if not defined ISCC (
    echo [LOI] Khong tim thay Inno Setup 6. Cai tai https://jrsoftware.org/isdl.php roi chay lai.
    goto :fail
)

"%ISCC%" /DAppVersion=%VER% "installer\DouyinVideoManager.iss"
if errorlevel 1 goto :fail

echo.
echo ============================================
echo  XONG: installer\Output\DouyinVideoManager-Setup-%VER%.exe
echo ============================================
if not defined CI (
    explorer "%CD%\installer\Output"
    pause
)
exit /b 0

:fail
echo.
echo [LOI] Dong goi that bai. Xem thong bao phia tren.
if not defined CI pause
exit /b 1
