@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deployment\windows\Stop-BatNodeInternet.ps1"
pause
