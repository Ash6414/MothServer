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
- Add Nodes with approval-based enrollment
- Fleet with one-click node commands
- Satellite map
- Recordings
- Data Management with database backup, FLAC reconciliation, and guarded clear actions
- SD Cleanup
- Telemetry charts
- Command Queue
- Diagnostics
- Raw Database viewer

The Recordings page shows the server's canonical name, original AudioMoth name,
recording time source, and deployment location. Select one recording to download
its WAV or FLAC copy, or delete its server files while retaining a catalog
tombstone.

Data Management provides three separate cleanup levels:

- `CLEAR HISTORY` keeps nodes and recordings.
- `DELETE RECORDINGS` keeps enrolled nodes but removes recording storage.
- `RESET SERVER` removes nodes and credentials too, so devices must enroll again.

A database backup is created before each bulk cleanup and retained in
`server/data/backups`.

## Security note

Run this locally on the home PC or behind a private tunnel/VPN. Do not expose the Streamlit port directly to the public internet.

For remote use on Windows, run `StartInternetAccess.cmd` from the parent folder.
It publishes the dashboard privately with Tailscale Serve while Funnel exposes
the separate device-only gateway. Do not Funnel port 8501 or admin port 8000.
