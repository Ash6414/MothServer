@echo off
setlocal

REM This launcher must be placed in the root bat_node_system folder.
REM Expected layout:
REM bat_node_system\
REM   run_both_debug_windows.bat
REM   run_server_debug_windows.bat
REM   run_dashboard_debug_windows.bat
REM   server\
REM   dashboard\

cd /d "%~dp0"

echo ========================================
echo Bat Node Run Both Debug Launcher
echo Folder: %CD%
echo ========================================
echo.

if not exist run_server_debug_windows.bat (
    echo ERROR: run_server_debug_windows.bat was not found.
    echo Put this file in the root bat_node_system folder.
    echo.
    pause
    exit /b 1
)

if not exist run_dashboard_debug_windows.bat (
    echo ERROR: run_dashboard_debug_windows.bat was not found.
    echo Put this file in the root bat_node_system folder.
    echo.
    pause
    exit /b 1
)

echo Starting FastAPI server on port 8000...
start "Bat Node FastAPI Server - Port 8000" cmd /k ""%CD%\run_server_debug_windows.bat""

echo Waiting 5 seconds before starting dashboard...
timeout /t 5 /nobreak >nul

echo Starting Streamlit dashboard on port 8501...
start "Bat Node Dashboard - Port 8501" cmd /k ""%CD%\run_dashboard_debug_windows.bat""

echo.
echo Started both services in separate windows.
echo.
echo FastAPI server:
echo   http://127.0.0.1:8000/health
echo.
echo Dashboard:
echo   http://localhost:8501
echo.
echo If port 8000 is already in use, close the old FastAPI server window first.
echo.
pause
exit /b 0