from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import time
import uuid
import zlib
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

SECRET = "REPLACE_WITH_64_HEX_OR_SERVER_SECRET"
NODE_ID = "BATNODE_001"
KEY_ID = "key-1"


def sign(method: str, path: str, body: bytes = b"", nonce: str | None = None) -> dict[str, str]:
    ts = str(int(time.time()))
    nonce = nonce or uuid.uuid4().hex
    body_sha = hashlib.sha256(body).hexdigest()
    canonical = "\n".join([method.upper(), path, ts, nonce, body_sha])
    sig = hmac.new(SECRET.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "X-Node-ID": NODE_ID,
        "X-Key-ID": KEY_ID,
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "X-Body-SHA256": body_sha,
        "X-Signature": sig,
    }


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MOTHSERVER_DB_PATH", str(tmp_path / "mothserver.sqlite3"))
    monkeypatch.setenv("MOTHSERVER_UPLOAD_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("MOTH_NODE_ID", NODE_ID)
    monkeypatch.setenv("MOTH_KEY_ID", KEY_ID)
    monkeypatch.setenv("MOTH_DEVICE_SECRET", SECRET)
    import mothserver.config as config
    import mothserver.db as db
    import mothserver.security as security
    import mothserver.paths as paths
    import mothserver.main as main

    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(security)
    importlib.reload(paths)
    importlib.reload(main)
    return TestClient(main.app)


def post_json(client: TestClient, path: str, payload: dict) -> object:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return client.post(path, content=body, headers={**sign("POST", path, body), "Content-Type": "application/json"})


def test_server_time_returns_epoch(client: TestClient) -> None:
    resp = client.get("/v1/public/server_time")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["epoch_utc"], int)
    assert data["iso_utc"].endswith("Z")


def test_hmac_json_heartbeat(client: TestClient) -> None:
    resp = post_json(
        client,
        "/v1/device/heartbeat",
        {
            "node_id": NODE_ID,
            "battery_v": 4.05,
            "battery_percent": 84,
            "charging": True,
            "charge_done": False,
            "recording_status": "MOTH_IDLE",
            "upload_status": "upload session complete",
            "wifi_rssi_dbm": -61,
            "mode": "ESPBRIDGE_UART_UPLOAD",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_hmac_binary_chunk_with_query_and_finish(client: TestClient, tmp_path: Path) -> None:
    data = b"RIFF" + b"\x00" * 32 + b"WAVE" + b"test-data"
    start = post_json(
        client,
        f"/v1/device/{NODE_ID}/upload/start",
        {"node_id": NODE_ID, "path": "20260608_220530.WAV", "size": len(data), "chunk_bytes": 512, "started_epoch": int(time.time())},
    )
    assert start.status_code == 200

    qs = urlencode(
        {
            "node_id": NODE_ID,
            "path": "20260608_220530.WAV",
            "offset": 0,
            "length": len(data),
            "total": len(data),
            "crc32": f"{zlib.crc32(data) & 0xFFFFFFFF:08X}",
        }
    )
    chunk_path = f"/v1/device/{NODE_ID}/upload/chunk?{qs}"
    chunk = client.post(
        chunk_path,
        content=data,
        headers={**sign("POST", chunk_path, data), "Content-Type": "application/octet-stream"},
    )
    assert chunk.status_code == 200
    assert chunk.json()["received_bytes"] == len(data)

    finish = post_json(
        client,
        f"/v1/device/{NODE_ID}/upload/finish",
        {"node_id": NODE_ID, "path": "20260608_220530.WAV", "size": len(data), "finished_epoch": int(time.time())},
    )
    assert finish.status_code == 200
    stored = Path(finish.json()["stored_path"])
    assert stored.exists()
    assert stored.read_bytes() == data


def test_bad_crc_returns_non_2xx(client: TestClient) -> None:
    data = b"abc123"
    start = post_json(
        client,
        f"/v1/device/{NODE_ID}/upload/start",
        {"node_id": NODE_ID, "path": "badcrc.WAV", "size": len(data), "chunk_bytes": 512, "started_epoch": int(time.time())},
    )
    assert start.status_code == 200
    qs = urlencode({"node_id": NODE_ID, "path": "badcrc.WAV", "offset": 0, "length": len(data), "total": len(data), "crc32": "00000000"})
    chunk_path = f"/v1/device/{NODE_ID}/upload/chunk?{qs}"
    resp = client.post(chunk_path, content=data, headers={**sign("POST", chunk_path, data), "Content-Type": "application/octet-stream"})
    assert resp.status_code >= 400


def test_bad_signature_returns_401_or_403(client: TestClient) -> None:
    body = json.dumps({"node_id": NODE_ID}).encode("utf-8")
    headers = sign("POST", "/v1/device/heartbeat", body)
    headers["X-Signature"] = "0" * 64
    resp = client.post("/v1/device/heartbeat", content=body, headers=headers)
    assert resp.status_code in (401, 403)


def test_path_traversal_rejected(client: TestClient) -> None:
    resp = post_json(
        client,
        f"/v1/device/{NODE_ID}/upload/start",
        {"node_id": NODE_ID, "path": "../bad.WAV", "size": 16, "chunk_bytes": 512, "started_epoch": int(time.time())},
    )
    assert resp.status_code >= 400
