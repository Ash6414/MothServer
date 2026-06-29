# Bat Node Server

FastAPI ingest server, SQLite database, Streamlit dashboard, Raspberry Pi
deployment scripts, and ESP32 node provisioning for the AudioMoth bat monitoring
system.

## What is here

```text
bat_node_system/
  server/
    bat_server.py              Main authenticated ingest API.
    bat_server_contract.py     Legacy raw-query upload adapter.
    compress_existing_wavs.py  Backfill compression command.
    manage_node.py             Manual node credential helper.
    test_contract_upload.py    API/upload/provisioning tests.
    requirements.txt

  dashboard/
    bat_dashboard_app.py       Streamlit dashboard.
    requirements_dashboard.txt

  deployment/pi/
    deploy_to_pi.ps1           Windows-to-Pi deploy helper.
    install_pi.sh              Pi installer and systemd service setup.

  deployment/windows/
    BatNodeControl.ps1         Native dark Windows control app.
    Manage-BatNodeStack.ps1    Safe start, restart, and stop engine.

  BatNode Control.vbs          Silent one-double-click launcher.
```

## Current node flow

1. ESP32 wakes and connects to Wi-Fi.
2. ESP32 signs requests with node HMAC credentials stored in NVS flash.
3. ESP32 asks AudioMoth for SD file metadata over UART.
4. Server receives manifest and upload session requests.
5. ESP32 uploads raw WAV chunks.
6. Server verifies the completed file, parses WAV metadata, and attempts FLAC
   compression.
7. ESP32 deletes the AudioMoth file only after delete authorization confirms the
   server copy is safe.

## First-time Windows setup

Open PowerShell in `bat_node_system/server`.

```powershell
py -3 -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn bat_server_runtime:app --host 0.0.0.0 --port 8000
```

In another PowerShell window, open the dashboard:

```powershell
cd ..\dashboard
py -3 -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -r requirements_dashboard.txt
streamlit run bat_dashboard_app.py
```

Default local URLs:

```text
server:    http://127.0.0.1:8000
dashboard: http://127.0.0.1:8501
```

## Windows control app

After the first-time Python setup, double-click:

```text
BatNode Control.vbs
```

`DashboardApp.bat` now opens the same app, so existing shortcuts continue to
work. The control app automatically starts and monitors:

- FastAPI server on port 8000
- device-only gateway on port 8001
- Streamlit dashboard on port 8501
- Tailscale Funnel for the ESP32 API
- private Tailscale Serve access for the dashboard

The dark console shows live service state and remote URLs. It includes **Open
Dashboard**, **Start**, **Restart**, **Stop**, and **Open Logs** controls. Closing
the window leaves the services running. Enable **Launch at sign-in** inside the
app when the PC should bring the stack up automatically after login.

Operational logs are kept in `logs/control.log`; server, gateway, and dashboard
stdout/stderr remain in their existing files under `logs/`.

## Raspberry Pi 4 deployment

The deploy script copies this folder to the Pi, installs system packages,
creates Python virtual environments, writes environment files, and enables two
systemd services.

Default Pi identity used by the helper:

```text
host: raspberrypi.local
user: <pi-user>
```

Run from Windows PowerShell:

```powershell
cd "C:\path\to\bat_node_system"
.\deployment\pi\deploy_to_pi.ps1 -PiHost raspberrypi.local -PiUser <pi-user>
```

If mDNS is not resolving, use the Pi LAN IP:

```powershell
.\deployment\pi\deploy_to_pi.ps1 -PiHost <pi-lan-ip> -PiUser <pi-user>
```

For a clean Pi install without copying the current local database or recordings:

```powershell
.\deployment\pi\deploy_to_pi.ps1 -PiHost raspberrypi.local -PiUser <pi-user> -SkipRuntimeData
```

After deployment, run this on the Pi:

```bash
bat-node-info
```

Useful service commands:

```bash
systemctl status bat-node-server.service
systemctl status bat-node-dashboard.service
journalctl -u bat-node-server.service -f
journalctl -u bat-node-dashboard.service -f
```

Service URLs on the LAN:

```text
server:    http://raspberrypi.local:8000
dashboard: http://raspberrypi.local:8501
```

## Windows internet access without port forwarding

Install and sign in to Tailscale on the server PC, start `DashboardApp.bat`,
then run:

```text
StartInternetAccess.cmd
```

The helper verifies both local services and configures:

- Tailscale Funnel on HTTPS port 443 for device-only gateway port 8001.
- Tailscale Serve on HTTPS port 8443 for the private dashboard.

It prints both stable URLs. Enter the HTTPS Funnel URL as the ESP32 server URL.
Only devices signed into the same Tailscale network can open the dashboard URL.
Run `StopInternetAccess.cmd` to remove both mappings.

The Funnel never targets admin server port 8000. `bat_public_gateway.py` allows
only enrollment, heartbeat, command polling, upload, and deletion endpoints;
admin routes and API documentation return 404 through the public tunnel.

## ESP32 node provisioning

The ESP32 firmware no longer needs per-node edits in source code. If required
settings are missing from ESP32 NVS flash, it starts a setup Wi-Fi network:

```text
SSID: BatNode-XXXXXX
Password: batnode-setup
Setup page: http://192.168.4.1
```

Recommended setup path:

1. Connect to the ESP32 setup Wi-Fi network.
2. Open `http://192.168.4.1`.
3. Choose personal, enterprise, or open Wi-Fi and enter its credentials.
4. Enter the public HTTPS server URL printed by `StartInternetAccess.cmd`.
5. Submit the enrollment request.
6. Open **Add Nodes** in the dashboard and press **Approve**.
7. The ESP32 receives its node ID and secret directly, saves them, and restarts.

Approval pickup is driven by the ESP32, not by the setup browser. The device
continues polling after the phone or laptop disconnects from its setup network.
Its first authenticated request marks enrollment `CLAIMED` and clears the
temporary enrollment copy of the device secret.

Device secrets are never copied through the UI. The ESP32 eFuse hardware ID is
linked to the node record. If firmware or NVS is erased later, select the old
node during approval; later reflashes are recognized automatically and retain
the same node ID, history, map location, and files.

Manual setup is still available:

```powershell
cd server
python manage_node.py create BATNODE_001 "Bench Node 1" --lat 46.8772 --lon -96.7898 --location-label "Bench"
```

Paste the printed values into the ESP32 setup portal, not into the source code.

## Environment variables

The Pi installer writes `server/bat_server.env`. Local development can set the
same variables in the shell before starting Uvicorn.

```text
BAT_DB_PATH=bat_nodes_v2.db
BAT_DATA_DIR=data
AUTH_WINDOW_SECONDS=300
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=change-me-now
PROVISIONING_TOKEN=
ENROLLMENT_TTL_SECONDS=1800
ENROLLMENT_POLL_SECONDS=3
REQUIRE_FLAC_BEFORE_DELETE=0
REQUIRE_BACKUP_BEFORE_DELETE=0
FLAC_ENCODER=auto
FLAC_COMPRESSION_LEVEL=5
FLAC_RECONCILE_INTERVAL_SECONDS=900
FLAC_RECONCILE_BATCH_SIZE=5
```

`PROVISIONING_TOKEN` is retained only for older firmware. New nodes use pending
dashboard approval and do not require a shared token.

## FLAC compression

Install either `flac` or `ffmpeg` on the machine running the server. The Pi
installer installs `flac` automatically.

```powershell
flac --version
ffmpeg -version
```

Uploaded WAV files enter the compression queue after upload completion. A
background reconciliation pass runs every 15 minutes by default, so files
missed while the encoder was unavailable are retried automatically. The Data
Management page also has a **Check and compress WAV files** button.

The server validates the WAV before encoding and checks the FLAC file signature
before recording success in SQLite:

```text
OK                   FLAC was created and validated.
PENDING              Waiting for a compression pass.
SKIPPED_NO_ENCODER   No flac or ffmpeg executable was available.
ERROR: ...           Source validation or encoding failed.
```

Backfill old uploads after installing an encoder:

```powershell
cd server
python compress_existing_wavs.py
```

Useful options:

```powershell
python compress_existing_wavs.py --dry-run
python compress_existing_wavs.py --node-id BATNODE_001
python compress_existing_wavs.py --force
```

Compression does not block AudioMoth deletion unless
`REQUIRE_FLAC_BEFORE_DELETE=1`.

## Recording catalog and storage

The database preserves the original AudioMoth filename and assigns every file a
stable server name. A timestamp found in the manifest or filename produces:

```text
BATNODE_001_20260619T013740Z_000003.WAV
```

If a filename has no reliable timestamp, it is still categorized by node and
upload day:

```text
BATNODE_001_UPLOADED_20260619_000003.WAV
```

The recording row snapshots the node location and records where the timestamp
came from. This keeps later weather matching tied to the recording's time and
deployment location even if the node is moved.

The private dashboard's **Data Management** page can download a consistent
SQLite backup, clear operational history, delete all recordings, or reset all
server data. Every destructive bulk action requires an exact confirmation phrase
and creates a timestamped backup under `data/backups` first. Per-recording WAV
and FLAC downloads and deletion are available on the **Recordings** page.

## API contract used by ESP32

```http
GET  /v1/public/server_time
POST /v1/provision/node
POST /v1/device/heartbeat
POST /v1/device/time_check
GET  /v1/device/{node_id}/commands
POST /v1/device/{node_id}/commands/{command_id}/ack
POST /v1/files/manifest
POST /v1/uploads/init
PUT  /v1/uploads/{upload_id}/chunks/{chunk_index}
POST /v1/uploads/{upload_id}/complete
GET  /v1/nodes/{node_id}/delete_authorization?manifest_id=...
POST /v1/nodes/{node_id}/delete_confirm
```

Dashboard commands are normally returned once and marked `DELIVERED`. If a node
receives a command but loses its ACK request, the server redelivers that command
after `COMMAND_REDELIVER_AFTER_SECONDS` seconds, default `120`, until it expires
or receives the ACK.

Protected requests include:

```text
X-Node-ID
X-Key-ID
X-Timestamp
X-Nonce
X-Body-SHA256
X-Signature
```

Canonical HMAC string:

```text
METHOD
PATH
TIMESTAMP
NONCE
BODY_SHA256
```

The chunk endpoint receives raw `application/octet-stream` bytes. Query
parameters are not included in the canonical string.

## Tests

From `bat_node_system/server`:

```powershell
pytest -q test_contract_upload.py
```

The tests cover server time, HMAC validation, provisioning, manifest upload,
chunk resume behavior, upload completion, delete authorization, bad signatures,
and replay rejection.

## Runtime data

These files are intentionally ignored by GitHub:

- `server/bat_nodes_v2.db`
- `server/data/`
- Python virtual environments
- caches and bytecode
- local env files and dashboard secrets
