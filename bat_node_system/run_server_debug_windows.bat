@echo off
setlocal

REM This launcher must be placed in the root bat_node_system folder.
REM Expected layout:
REM bat_node_system\
REM   run_server_debug_windows.bat
REM   server\
REM     bat_server.py
REM     manage_node.py
REM     requirements.txt

cd /d "%~dp0server"

echo ========================================
echo Bat Node FastAPI Server Debug Launcher
echo Folder: %CD%
echo ========================================
echo.

if not exist bat_server.py (
    echo ERROR: bat_server.py was not found.
    echo Put this launcher in the root bat_node_system folder, next to the server folder.
    echo.
    pause
    exit /b 1
)

if not exist requirements.txt (
    echo WARNING: requirements.txt was not found in the server folder.
    echo Creating a minimal requirements.txt now.
    echo fastapi> requirements.txt
    echo uvicorn[standard]>> requirements.txt
    echo pydantic>> requirements.txt
    echo python-multipart>> requirements.txt
    echo.
)

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: python was not found.
    echo Install Python or fix PATH.
    echo.
    pause
    exit /b 1
)

echo Using Python:
python --version
python -c "import sys; print(sys.executable)"
echo.

if not exist .venv (
    echo Creating server virtual environment...
    python -m venv .venv
    if errorlevel 1 goto fail
)

call .venv\Scripts\activate.bat
if errorlevel 1 goto fail

echo.
echo Virtual environment Python:
python --version
python -c "import sys; print(sys.executable)"
echo.

echo Upgrading pip...
python -m pip install --upgrade pip
if errorlevel 1 goto fail

echo.
echo Installing server requirements...
python -m pip install -r requirements.txt
if errorlevel 1 goto fail

echo.
echo Starting FastAPI server. Keep this window open.
echo Local health check: http://127.0.0.1:8000/health
echo LAN health check:   http://192.168.0.207:8000/health
echo Server time:        http://127.0.0.1:8000/v1/public/server_time
echo Database:           %CD%\bat_nodes_v2.db
echo.

python -m uvicorn bat_server:app --host 0.0.0.0 --port 8000

echo.
echo Server stopped.
pause
exit /b 0

:fail
echo.
echo FAILED. Scroll up and copy the first ERROR or Traceback line.
pause
exit /b 1