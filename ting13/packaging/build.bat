@echo off
setlocal

REM -- Switch to script directory (required for double-click) --
cd /d "%~dp0"

set "NO_PAUSE="
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"
if defined CI set "NO_PAUSE=1"
if defined GITHUB_ACTIONS set "NO_PAUSE=1"

set "MAX_HISTORY=4"
set "DIST_ROOT=..\dist"
set "RELEASES_DIR=%DIST_ROOT%\releases"
set "CURRENT_DIR=%RELEASES_DIR%\current"
set "HISTORY_DIR=%RELEASES_DIR%\history"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "BUILD_TS=%%i"

echo ============================================================
echo   ting13.cc Audiobook Downloader - Build Tool
echo   Directory: %CD%
echo ============================================================
echo.

REM -- Check Python --
where python >nul 2>&1
if errorlevel 1 (
    echo [FAIL] Python not found. Please install Python and add to PATH.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo [OK] %%v

REM -- Check PyInstaller --
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [*] Installing PyInstaller...
    pip install pyinstaller
    if errorlevel 1 (
        echo [FAIL] PyInstaller install failed!
        pause
        exit /b 1
    )
)
echo [OK] PyInstaller ready

REM -- Check Playwright Chromium --
python -c "import os,glob;assert glob.glob(os.path.join(os.path.expanduser('~'),'AppData','Local','ms-playwright','chromium-*'))" 2>nul
if errorlevel 1 (
    echo [!] Chromium not found, installing...
    python -m playwright install chromium
    if errorlevel 1 (
        echo [FAIL] Chromium install failed!
        pause
        exit /b 1
    )
)
echo [OK] Chromium ready
echo.

REM -- Prepare release folders --
if not exist "%RELEASES_DIR%" mkdir "%RELEASES_DIR%"
if not exist "%HISTORY_DIR%" mkdir "%HISTORY_DIR%"
if exist "%CURRENT_DIR%" (
    echo [*] Archiving previous current release...
    move "%CURRENT_DIR%" "%HISTORY_DIR%\release_%BUILD_TS%" >nul
)
mkdir "%CURRENT_DIR%" >nul 2>&1

REM -- Build CLI version --
echo [1/2] Building CLI version...
echo.
pyinstaller --clean --noconfirm --distpath "%CURRENT_DIR%" --workpath "..\build" ting13_downloader.spec
if errorlevel 1 (
    echo.
    echo [FAIL] CLI build failed!
    if not defined NO_PAUSE pause
    exit /b 1
)
echo.

REM -- Build GUI version --
echo [2/2] Building GUI version...
echo.
pyinstaller --clean --noconfirm --distpath "%CURRENT_DIR%" --workpath "..\build" ting13_gui.spec
if errorlevel 1 (
    echo.
    echo [FAIL] GUI build failed!
    if not defined NO_PAUSE pause
    exit /b 1
)

REM -- Keep only latest MAX_HISTORY history releases --
powershell -NoProfile -Command ^
  "$max=%MAX_HISTORY%; $h='%HISTORY_DIR%'; if(Test-Path $h){Get-ChildItem $h -Directory | Sort-Object LastWriteTime -Descending | Select-Object -Skip $max | Remove-Item -Recurse -Force}"

echo.
echo ============================================================
echo [OK] All builds succeeded!
echo.
echo   Current release: %CURRENT_DIR%
echo   CLI: %CURRENT_DIR%\ting13_downloader\ting13_downloader.exe
echo   GUI: %CURRENT_DIR%\ting13_downloader_gui\ting13_downloader_gui.exe
echo   History cache: %HISTORY_DIR%  (max %MAX_HISTORY% versions)
echo.
echo   Distribute: zip folders under %CURRENT_DIR%
echo ============================================================
if not defined NO_PAUSE pause
