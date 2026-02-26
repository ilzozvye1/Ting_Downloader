@echo off
setlocal

REM -- Switch to script directory (required for double-click) --
cd /d "%~dp0"

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

REM -- Build CLI version --
echo [1/2] Building CLI version...
echo.
pyinstaller --clean --noconfirm --distpath "..\dist" --workpath "..\build" ting13_downloader.spec
if errorlevel 1 (
    echo.
    echo [FAIL] CLI build failed!
    pause
    exit /b 1
)
echo.

REM -- Build GUI version --
echo [2/2] Building GUI version...
echo.
pyinstaller --clean --noconfirm --distpath "..\dist" --workpath "..\build" ting13_gui.spec
if errorlevel 1 (
    echo.
    echo [FAIL] GUI build failed!
    pause
    exit /b 1
)

echo.
echo ============================================================
echo [OK] All builds succeeded!
echo.
echo   CLI: ..\dist\ting13_downloader\ting13_downloader.exe
echo   GUI: ..\dist\ting13_downloader_gui\ting13_downloader_gui.exe
echo.
echo   Distribute: zip the folders under ..\dist\
echo ============================================================
pause
