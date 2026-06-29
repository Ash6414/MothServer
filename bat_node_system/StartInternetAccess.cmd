@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deployment\windows\Start-BatNodeInternet.ps1"
pause
