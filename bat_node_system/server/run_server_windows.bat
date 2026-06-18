@echo off
setlocal
if not exist .venv (
  py -3 -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -r requirements.txt
uvicorn bat_server_runtime:app --host 0.0.0.0 --port 8000
