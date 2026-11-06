@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo  POLYBOT setup (PAPER-first, no live trading is enabled)
echo ============================================================
echo.

set "PYTHON=py -3.11"
%PYTHON% -c "import sys" >nul 2>&1
if errorlevel 1 set "PYTHON=python"
%PYTHON% -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found. Install Python 3.11+ from python.org
    echo        and enable "Add python.exe to PATH".
    exit /b 1
)

%PYTHON% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo ERROR: Python 3.11 or newer is required.
    %PYTHON% --version
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/5] Creating virtual environment .venv ...
    %PYTHON% -m venv .venv
    if errorlevel 1 (
        echo ERROR: failed to create the virtual environment.
        exit /b 1
    )
) else (
    echo [1/5] Virtual environment already exists.
)

set "VENV_PY=.venv\Scripts\python.exe"

echo [2/5] Upgrading pip ...
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 (
    echo ERROR: failed to upgrade pip.
    exit /b 1
)

echo [3/5] Installing POLYBOT and development tools ...
"%VENV_PY%" -m pip install -e ".[dev]"
if errorlevel 1 (
    echo ERROR: dependency installation failed. Check your network and Python version.
    exit /b 1
)

if not exist ".env" (
    echo [4/5] Creating .env from .env.example ...
    copy /Y ".env.example" ".env" >nul
    echo       IMPORTANT: edit .env and set the dashboard password hash and
    echo       session secret before relying on the dashboard.
) else (
    echo [4/5] .env already exists; leaving it untouched.
)

echo [5/5] Initialising database, directories, and PAPER runtime config ...
"%VENV_PY%" -m btc5m_bot init
if errorlevel 1 (
    echo ERROR: initialisation failed.
    exit /b 1
)

echo.
echo Setup complete.
echo.
echo Next steps:
echo   1. Create the dashboard credentials:
echo        .venv\Scripts\python.exe -m btc5m_bot hash-password
echo      Paste the two generated lines into .env, then restart your shell.
echo   2. Review config\runtime.json. Mode stays PAPER until LIVE is explicitly
echo      activated through the 23-condition gate.
echo   3. Start the bot with:  start.bat
echo      Dashboard:           http://127.0.0.1:8765
echo   4. Review resolution rules on the dashboard Rules page, then approve the
echo      intended fingerprint there or with:
echo        .venv\Scripts\python.exe -m btc5m_bot approve-rule ^<fingerprint^>
echo.
echo LIVE trading is NOT enabled by this script. Keep it PAPER while validating.
exit /b 0
