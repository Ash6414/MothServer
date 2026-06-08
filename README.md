# MothServer

FastAPI/SQLite server for ESP32-WROOM-U + custom AudioMoth Dev bat acoustic nodes.

This server is built to match the current ESP32 upload contract used by:

- ESP32 firmware: <https://github.com/Ash6414/Espmoth/tree/main>
- AudioMoth bridge firmware: <https://github.com/Ash6414/AudioMoth-Firmware_ESPnode>
- Server repository: <https://github.com/Ash6414/MothServer>

The ESP32 firmware contract is not modified. The server implements the endpoints exactly as the firmware expects.

## Implemented endpoints

Public:

```http
GET /v1/public/server_time
```

Protected HMAC endpoints:

```http
POST /v1/device/heartbeat
POST /v1/device/time_check
GET  /v1/device/{node_id}/commands
POST /v1/device/{node_id}/commands/{command_id}/ack
POST /v1/device/{node_id}/upload/start
POST /v1/device/{node_id}/upload/chunk?node_id=BATNODE_001&path=20260608_220530.WAV&offset=0&length=512&total=352044&crc32=ABCD1234
POST /v1/device/{node_id}/upload/finish
```

Local/admin helper endpoint, not used by ESP32 firmware:

```http
POST /v1/admin/{node_id}/commands/{command_type}
```

Supported command types:

```text
PING
UPLOAD_NOW
SYNC_MOTH_TIME
MOTH_STATUS
```

## HMAC contract

Protected requests must include:

```text
X-Node-ID
X-Key-ID
X-Timestamp
X-Nonce
X-Body-SHA256
X-Signature
```

The ESP32 signs this canonical string:

```text
METHOD
PATH_OR_PATH_WITH_QUERY
TIMESTAMP
NONCE
BODY_SHA256
```

Signature calculation:

```text
signature = HMAC_SHA256(DEVICE_SECRET, canonical)
```

Rules implemented here:

- `METHOD` is uppercase, for example `GET` or `POST`.
- JSON endpoints use `request.url.path` as the canonical path.
- Binary upload chunks use `request.url.path + "?" + request.url.query` exactly. Query parameters are not sorted or reconstructed.
- Body SHA256 is lowercase hex of the exact raw request body.
- Signature is lowercase hex HMAC-SHA256.
- `DEVICE_SECRET` is treated as literal UTF-8 key bytes. It is not hex-decoded.
- Bad timestamp drift, duplicate nonce, bad body hash, unknown node/key, wrong node, and bad signature are rejected.

## Environment variables

Copy `.env.example` and set your secret:

```powershell
copy .env.example .env
```

Required or useful variables:

```text
MOTHSERVER_DB_PATH=data/mothserver.sqlite3
MOTHSERVER_UPLOAD_ROOT=data/uploads
AUTH_MAX_CLOCK_DRIFT_SECONDS=900
AUTH_NONCE_RETENTION_SECONDS=86400
MOTH_NODE_ID=BATNODE_001
MOTH_KEY_ID=key-1
MOTH_DEVICE_SECRET=REPLACE_WITH_64_HEX_OR_SERVER_SECRET
```

Multi-node configuration can be provided as JSON:

```text
NODE_SECRETS_JSON={"BATNODE_001":{"key-1":"REPLACE_WITH_64_HEX_OR_SERVER_SECRET"}}
```

## Run the FastAPI server

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn mothserver.main:app --host 0.0.0.0 --port 8000
```

PowerShell equivalent:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn mothserver.main:app --host 0.0.0.0 --port 8000
```

## Run the dashboard

```bash
streamlit run dashboard/bat_dashboard_app.py
```

The dashboard shows:

- node table
- last seen time
- battery voltage and percentage
- charging/DONE status
- latest upload status
- uploaded recording count
- recording list with file size, stored path, SHA256, and weather status
- command buttons for `PING`, `UPLOAD_NOW`, `SYNC_MOTH_TIME`, and `MOTH_STATUS`

## Upload storage layout

Incoming partial files:

```text
data/uploads/BATNODE_001/incoming/20260608_220530.WAV.part
```

Finalized recordings:

```text
data/uploads/BATNODE_001/recordings/20260608_220530.WAV
```

A one-level daily folder is accepted, for example:

```text
20260608/20260608_220530.WAV
```

Rejected paths include absolute paths, `..`, backslashes, hidden path components, paths deeper than one folder, and non-WAV files.

## Python test upload example

```python
import hashlib
import hmac
import json
import time
import uuid
import zlib
from urllib.parse import urlencode

import requests

BASE_URL = "http://127.0.0.1:8000"
NODE_ID = "BATNODE_001"
KEY_ID = "key-1"
SECRET = "REPLACE_WITH_64_HEX_OR_SERVER_SECRET"


def sign(method, path, body=b""):
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body_sha = hashlib.sha256(body).hexdigest()
    canonical = "\n".join([method.upper(), path, ts, nonce, body_sha])
    signature = hmac.new(SECRET.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "X-Node-ID": NODE_ID,
        "X-Key-ID": KEY_ID,
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "X-Body-SHA256": body_sha,
        "X-Signature": signature,
    }


def post_json(path, payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return requests.post(BASE_URL + path, data=body, headers={**sign("POST", path, body), "Content-Type": "application/json"})

wav = b"RIFF" + b"\x00" * 32 + b"WAVE" + b"payload"
source_path = "20260608_220530.WAV"

print(post_json(f"/v1/device/{NODE_ID}/upload/start", {
    "node_id": NODE_ID,
    "path": source_path,
    "size": len(wav),
    "chunk_bytes": 512,
    "started_epoch": int(time.time()),
}).json())

query = urlencode({
    "node_id": NODE_ID,
    "path": source_path,
    "offset": 0,
    "length": len(wav),
    "total": len(wav),
    "crc32": f"{zlib.crc32(wav) & 0xFFFFFFFF:08X}",
})
chunk_path = f"/v1/device/{NODE_ID}/upload/chunk?{query}"
print(requests.post(
    BASE_URL + chunk_path,
    data=wav,
    headers={**sign("POST", chunk_path, wav), "Content-Type": "application/octet-stream"},
).json())

print(post_json(f"/v1/device/{NODE_ID}/upload/finish", {
    "node_id": NODE_ID,
    "path": source_path,
    "size": len(wav),
    "finished_epoch": int(time.time()),
}).json())
```

## Tests

```bash
pytest -q
```

The test suite verifies:

1. `/v1/public/server_time` returns `epoch_utc`.
2. HMAC verification works for JSON.
3. HMAC verification works for binary upload chunks with query strings.
4. Start → chunk → finish produces a final `.WAV` file.
5. Bad CRC returns non-2xx.
6. Bad signature returns 401/403.
7. Path traversal such as `../bad.WAV` is rejected.

## Weather metadata

Upload completion does not block on weather lookup. The server parses recording time from filenames like:

```text
YYYYMMDD_HHMMSS.WAV
YYYYMMDD/20260608_220530.WAV
```

It stores `recordings.recording_epoch` and sets `weather_status='pending'` for later enrichment.
