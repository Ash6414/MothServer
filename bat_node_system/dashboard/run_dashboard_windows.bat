@echo off
cd /d %~dp0
if not exist .venv (
    py -3 -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements_dashboard.txt
streamlit run bat_dashboard_app.py
