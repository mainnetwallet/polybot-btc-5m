@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Run setup.bat first.
    exit /b 1
)

".venv\Scripts\python.exe" -m btc5m_bot status
exit /b %ERRORLEVEL%
