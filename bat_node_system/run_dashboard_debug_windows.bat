@echo off
setlocal

REM This launcher must be placed in the root bat_node_system folder.
REM Expected layout:
REM bat_node_system\
REM   run_dashboard_debug_windows.bat
REM   dashboard\
REM     bat_dashboard_app.py
REM     requirements_dashboard.txt
REM   server\
REM     bat_nodes_v2.db

cd /d "%~dp0dashboard"

echo ========================================
echo Bat Node Streamlit Dashboard Debug Launcher
echo Folder: %CD%
echo ========================================
echo.

if not exist bat_dashboard_app.py (
    echo ERROR: bat_dashboard_app.py was not found.
    echo Put this launcher in the root bat_node_system folder, next to the dashboard folder.
    echo.
    pause
    exit /b 1
)

if not exist requirements_dashboard.txt (
    echo ERROR: requirements_dashboard.txt was not found in the dashboard folder.
    echo.
    pause
    exit /b 1
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
    echo Creating dashboard virtual environment...
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
echo Installing dashboard requirements...
python -m pip install -r requirements_dashboard.txt
if errorlevel 1 goto fail

REM Point the dashboard to the server database.
set "BAT_DB_PATH=%~dp0server\bat_nodes_v2.db"

echo.
echo Starting dashboard. Keep this window open.
echo Open in browser: http://localhost:8501
echo Database path: %BAT_DB_PATH%
echo.

python -m streamlit run bat_dashboard_app.py --server.port 8501

echo.
echo Dashboard stopped.
pause
exit /b 0

:fail
echo.
echo FAILED. Scroll up and copy the first ERROR or Traceback line.
pause
exit /b 1