# Bat Node System v2

FastAPI/SQLite ingest server, Streamlit dashboard, ESP32 bridge firmware support, and AudioMoth UART/SD arbitration notes for the bat acoustic monitoring node system.

## Repository layout

```text
bat_node_system/
  server/
    bat_server.py              # Current secure ingest server used by ESP32 bridge firmware.
    bat_server_contract.py     # Legacy raw-query upload adapter kept for older ESP sketches.
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
POST /v1/files/manifest
POST /v1/uploads/init
PUT  /v1/uploads/{upload_id}/chunks/{chunk_index}
POST /v1/uploads/{upload_id}/complete
GET  /v1/nodes/{node_id}/delete_authorization?manifest_id=BATNODE_001-AUDIOMOTH-SD
POST /v1/nodes/{node_id}/delete_confirm
```

Use `server/bat_server.py` for this firmware. The ESP asks `/v1/uploads/init` for a 512-byte chunk size so each AudioMoth UART `GET` response maps directly to one server chunk.

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
PATH
TIMESTAMP
NONCE
BODY_SHA256
```

Rules implemented in `bat_server.py`:

- JSON endpoints sign `request.url.path`.
- Binary chunk endpoints also sign `request.url.path`.
- Query parameters are not included in the HMAC canonical string.
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
uvicorn bat_server:app --host 0.0.0.0 --port 8000
```

Older raw-query ESP sketches can still run against the legacy adapter:

```powershell
uvicorn bat_server_contract:app --host 0.0.0.0 --port 8000
```

## Linux/Raspberry Pi setup

```bash
cd bat_node_system/server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage_node.py create BATNODE_001 "Bench Node 1"
uvicorn bat_server:app --host 0.0.0.0 --port 8000
```

## Environment variables

```text
BAT_DB_PATH=bat_nodes_v2.db
BAT_DATA_DIR=data
AUTH_WINDOW_SECONDS=300
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=change-me-now
REQUIRE_FLAC_BEFORE_DELETE=0
REQUIRE_BACKUP_BEFORE_DELETE=0
```

Production nodes should use `manage_node.py` and unique secrets.

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
3. Manifest -> init -> PUT chunks -> complete stores and parses a `.WAV` file.
4. Upload init honors the ESP bridge chunk size.
5. Delete authorization and delete confirm work after server verification.
6. Bad signature returns 401.
7. Duplicate nonce replay returns 401.

## Delete policy

The ESP32 deletes AudioMoth files only after `/v1/uploads/{upload_id}/complete` verifies the WAV and `/v1/nodes/{node_id}/delete_authorization` explicitly lists the file as safe to delete.

The upload complete endpoint:

- verifies upload session state
- verifies byte count
- verifies final `.part` file size
- atomically moves the file into `data/original_wav/{node_id}/`
- computes server SHA-256
- parses WAV metadata
- updates the `files` table for dashboard/delete authorization
- returns `ok: true` only after the server copy is verified

FLAC conversion is attempted when `ffmpeg` is available, but it does not block delete authorization unless `REQUIRE_FLAC_BEFORE_DELETE=1`.
