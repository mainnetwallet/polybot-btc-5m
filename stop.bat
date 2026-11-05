@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Run setup.bat first.
    exit /b 1
)

echo Requesting graceful shutdown through the POLYBOT STOP file ...
".venv\Scripts\python.exe" -m btc5m_bot stop
if errorlevel 1 (
    echo ERROR: failed to write the STOP file.
    exit /b 1
)

echo.
echo The supervisor checks this file every few seconds and shuts down the
echo engine before the heartbeat worker. Wait for the start.bat window to close.
echo If the process is still alive after 30 seconds, inspect logs\ and
echo data\supervisor.json before terminating it manually.
exit /b 0
