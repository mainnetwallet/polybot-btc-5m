@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Run setup.bat first.
    exit /b 1
)
if not exist ".env" (
    echo ERROR: .env not found. Copy .env.example to .env and configure it.
    exit /b 1
)

echo Starting POLYBOT supervisor (engine + optional heartbeat worker).
echo Mode is controlled by config\runtime.json; the default is PAPER.
echo Dashboard: http://127.0.0.1:8765
echo Logs:      logs\
echo Press Ctrl+C in this window to request a graceful shutdown,
echo or run stop.bat from another terminal.
echo.

".venv\Scripts\python.exe" -m btc5m_bot supervisor
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo POLYBOT exited with code %CODE%. Check logs\ for details.
)
exit /b %CODE%
