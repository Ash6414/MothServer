@echo off
setlocal
cd /d "%~dp0server"

if not exist .venv\Scripts\python.exe (
    echo ERROR: Server environment is missing. Start DashboardApp.bat once to create it.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
echo Starting public ESP32 device gateway on local port 8001...
echo Tailscale Funnel should target this port, never the admin server on port 8000.
python -m uvicorn bat_public_gateway:app --host 127.0.0.1 --port 8001

echo.
echo Device gateway stopped.
pause
