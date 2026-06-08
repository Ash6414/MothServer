# Bat Node System v2

FastAPI/SQLite ingest server, Streamlit dashboard, ESP32 skeleton, and AudioMoth UART/SD arbitration notes for the bat acoustic monitoring node system.

## Repository layout

```text
bat_node_system/
  server/
    bat_server.py              # Original v2 secure ingest server. Preserved.
    bat_server_contract.py     # Current ESP32-WROOM-U upload-contract adapter.
    manage_node.py             # Node credential helper.
    test_contract_upload.py    # Contract/HMAC/upload validation tests.
    requirements.txt
  dashboard/
    bat_dashboard_app.py       # SQLite dashboard.
  esp32/
  audiomoth/
```

## Current ESP32 firmware contract

The current ESP32 firmware uses these endpoints exactly:

```http
GET  /v1/public/server_time
POST /v1/device/heartbeat
POST /v1/device/time_check
GET  /v1/device/{node_id}/commands
POST /v1/device/{node_id}/commands/{command_id}/ack
POST /v1/device/{node_id}/upload/start
POST /v1/device/{node_id}/upload/chunk?node_id=BATNODE_001&path=20260608_220530.WAV&offset=0&length=512&total=352044&crc32=ABCD1234
POST /v1/device/{node_id}/upload/finish
```

Use `server/bat_server_contract.py` for that firmware. It imports the original `bat_server.py`, preserves the older manifest/upload/delete endpoints, and adds the raw-binary ESP32 upload contract.

## HMAC canonical string

Protected requests include:

```text
X-Node-ID
X-Key-ID
X-Timestamp
X-Nonce
X-Body-SHA256
X-Signature
```

Canonical string:

```text
METHOD
PATH_OR_PATH_WITH_QUERY
TIMESTAMP
NONCE
BODY_SHA256
```

Rules implemented in `bat_server_contract.py`:

- JSON endpoints sign `request.url.path`.
- Binary chunk endpoint signs `request.url.path + "?" + request.url.query` exactly.
- Query parameters are not sorted or reconstructed.
- Body hash is lowercase SHA-256 of the exact raw body bytes.
- HMAC key is the literal UTF-8 `DEVICE_SECRET` string.
- Timestamp drift, duplicate nonce, bad body hash, bad node/key, and bad signature are rejected.

## Windows setup

Open PowerShell in `bat_node_system/server`.

```powershell
py -3 -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a real node credential:

```powershell
python manage_node.py create BATNODE_001 "Bench Node 1" --lat 46.8772 --lon -96.7898 --location-label "Bench"
```

Copy the printed `NODE_ID`, `KEY_ID`, and `DEVICE_SECRET` into the ESP32 sketch.

Run the current ESP32 contract server:

```powershell
uvicorn bat_server_contract:app --host 0.0.0.0 --port 8000
```

Older manifest/chunk firmware can still run directly against the original server:

```powershell
uvicorn bat_server:app --host 0.0.0.0 --port 8000
```

## Linux/Raspberry Pi setup

```bash
cd bat_node_system/server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage_node.py create BATNODE_001 "Bench Node 1"
uvicorn bat_server_contract:app --host 0.0.0.0 --port 8000
```

## Environment variables

```text
BAT_DB_PATH=bat_nodes_v2.db
BAT_DATA_DIR=data
BAT_UPLOAD_ROOT=data/uploads
AUTH_WINDOW_SECONDS=900
AUTH_NONCE_RETENTION_SECONDS=3600
MOTH_NODE_ID=BATNODE_001
MOTH_KEY_ID=key-1
MOTH_DEVICE_SECRET=REPLACE_WITH_64_HEX_OR_SERVER_SECRET
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=change-me-now
```

`MOTH_*` variables provide a bench fallback credential only. Production nodes should use `manage_node.py` and unique secrets.

## Dashboard

From `bat_node_system/dashboard`:

```powershell
streamlit run bat_dashboard_app.py
```

The dashboard reads `BAT_DB_PATH`, defaulting to `bat_node_system/server/bat_nodes_v2.db`.

It shows node state, telemetry, files, commands, map, errors, and delete state from the same SQLite database used by the server.

## Contract validation tests

From `bat_node_system/server`:

```powershell
pytest -q test_contract_upload.py
```

The tests verify:

1. `/v1/public/server_time` returns `epoch_utc`.
2. HMAC verification works for JSON heartbeat.
3. HMAC verification works for raw binary upload chunks with the exact query string.
4. Start → chunk → finish produces a final `.WAV` file.
5. Bad CRC returns non-2xx.
6. Bad signature returns 401/403.
7. Path traversal like `../bad.WAV` is rejected.

## Delete policy

The ESP32 deletes AudioMoth files only after `/v1/device/{node_id}/upload/finish` returns 2xx.

The contract finish endpoint:

- verifies upload session state
- verifies byte count
- verifies final `.part` file size
- atomically moves the file into `data/uploads/{node_id}/recordings/`
- computes server SHA-256
- writes `recordings` metadata
- updates the legacy `files` table for dashboard compatibility
- returns 2xx only after the file is committed

Weather lookup is intentionally not blocking upload finish. Filename timestamps such as `YYYYMMDD_HHMMSS.WAV` and `YYYYMMDD/YYYYMMDD_HHMMSS.WAV` are parsed into `recordings.recording_epoch`, and `weather_status` starts as `pending`.
