Set-Location $PSScriptRoot
if (!(Test-Path .venv)) {
    py -3 -m venv .venv
}
. .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements_dashboard.txt
streamlit run bat_dashboard_app.py
