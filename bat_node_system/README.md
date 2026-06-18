# Bat Node Server

FastAPI ingest server, SQLite database, Streamlit dashboard, Raspberry Pi
deployment scripts, and ESP32 node provisioning for the AudioMoth bat monitoring
system.

## What is here

```text
bat_node_system/
  server/
    bat_server.py              Main authenticated ingest API.
    bat_server_runtime.py      Production entrypoint with provisioning/FLAC runtime helpers.
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

## Raspberry Pi 4 deployment

The deploy script copies this folder to the Pi, installs system packages,
creates Python virtual environments, writes environment files, and enables two
systemd services.

Default Pi identity used by the helper:

```text
host: raspberrypi.local
user: pchem
```

Run from Windows PowerShell:

```powershell
cd "C:\Users\ashw6\OneDrive\Desktop\Audiomoth Project\Code\Server\MothServer-main\bat_node_system"
.\deployment\pi\deploy_to_pi.ps1 -PiHost raspberrypi.local -PiUser pchem
```

If mDNS is not resolving, use the Pi LAN IP:

```powershell
.\deployment\pi\deploy_to_pi.ps1 -PiHost 192.168.0.20 -PiUser pchem
```

For a clean Pi install without copying the current local database or recordings:

```powershell
.\deployment\pi\deploy_to_pi.ps1 -PiHost raspberrypi.local -PiUser pchem -SkipRuntimeData
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

Use a public URL only after router port forwarding, VPN, reverse proxy, or a
secure tunnel is configured.

## ESP32 node provisioning

The ESP32 firmware no longer needs per-node edits in source code. If required
settings are missing from ESP32 NVS flash, it starts a setup Wi-Fi network:

```text
SSID: BatNode-XXXXXX
Password: batnode-setup
Setup page: http://192.168.4.1
```

Recommended setup path:

1. Run `bat-node-info` on the Pi and copy the `PROVISIONING_TOKEN`.
2. Connect to the ESP32 setup Wi-Fi network.
3. Open `http://192.168.4.1`.
4. Enter field Wi-Fi, server URL, and provisioning token.
5. The server creates the node ID, key ID, and device secret.
6. The ESP32 saves those values in NVS and reboots into normal bridge mode.

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
REQUIRE_FLAC_BEFORE_DELETE=0
REQUIRE_BACKUP_BEFORE_DELETE=0
FLAC_ENCODER=auto
FLAC_COMPRESSION_LEVEL=5
```

Leave `PROVISIONING_TOKEN` blank to disable automatic node provisioning.

## FLAC compression

Install either `flac` or `ffmpeg` on the machine running the server. The Pi
installer installs `flac` automatically.

```powershell
flac --version
ffmpeg -version
```

Uploaded WAV files are compressed after upload completion when an encoder is
available. The server records the compression result in SQLite:

```text
OK                   FLAC was created.
SKIPPED_NO_ENCODER   No flac or ffmpeg executable was available.
ERROR                Encoder ran but did not produce a valid output file.
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
