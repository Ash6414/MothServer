if (!(Test-Path .venv)) {
    py -3 -m venv .venv
}
. .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn bat_server_runtime:app --host 0.0.0.0 --port 8000
