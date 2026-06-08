# Bat Node Dashboard App

This is the user-friendly local database dashboard for the ESP32 + AudioMoth bat monitoring system.

It reads the same SQLite database used by the FastAPI server:

```text
../server/bat_nodes_v2.db
```

## Run on Windows

Double-click:

```text
run_dashboard_windows.bat
```

or in PowerShell:

```powershell
.\run_dashboard_powershell.ps1
```

Then open the Streamlit URL shown in the terminal, usually:

```text
http://localhost:8501
```

## Run manually

```powershell
py -3 -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -r requirements_dashboard.txt
streamlit run bat_dashboard_app.py
```

## Point to a different database

```powershell
$env:BAT_DB_PATH="C:\path\to\bat_nodes_v2.db"
streamlit run bat_dashboard_app.py
```

## Pages

- Overview
- Nodes
- Satellite map
- Files
- SD cleanup
- Telemetry charts
- Commands
- Diagnostics
- Raw DB viewer

## Security note

Run this locally on the home PC or behind a private tunnel/VPN. Do not expose the Streamlit port directly to the public internet.

For remote use, place it behind:

- Tailscale
- ZeroTier
- Cloudflare Access
- A reverse proxy with authentication

The public device ingest API should remain separate from this human dashboard.
