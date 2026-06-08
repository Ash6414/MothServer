from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import time
import uuid
import zlib
from pathlib import Path
from urllib.parse import urlencode

from fastapi.testclient import TestClient

NODE_ID = "BATNODE_001"
KEY_ID = "key-1"
SECRET = "REPLACE_WITH_64_HEX_OR_SERVER_SECRET"


def sign(method: str, path: str, body: bytes = b"") -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body_sha = hashlib.sha256(body).hexdigest()
    canonical = "\n".join([method.upper(), path, timestamp, nonce, body_sha])
    signature = hmac.new(SECRET.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "X-Node-ID": NODE_ID,
        "X-Key-ID": KEY_ID,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Body-SHA256": body_sha,
        "X-Signature": signature,
    }


def post_json(client: TestClient, path: str, payload: dict):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return client.post(path, content=body, headers={**sign("POST", path, body), "Content-Type": "application/json"})


def build_client(tmp_root: Path) -> TestClient:
    os.environ["BAT_DB_PATH"] = str(tmp_root / "bat_nodes_v2.db")
    os.environ["BAT_DATA_DIR"] = str(tmp_root / "data")
    os.environ["BAT_UPLOAD_ROOT"] = str(tmp_root / "data" / "uploads")
    os.environ["MOTH_NODE_ID"] = NODE_ID
    os.environ["MOTH_KEY_ID"] = KEY_ID
    os.environ["MOTH_DEVICE_SECRET"] = SECRET

    import bat_server
    import bat_server_contract

    importlib.reload(bat_server)
    importlib.reload(bat_server_contract)
    return TestClient(bat_server_contract.app)


def test_server_time(tmp_path: Path):
    client = build_client(tmp_path)
    response = client.get("/v1/public/server_time")
    assert response.status_code == 200
    assert isinstance(response.json()["epoch_utc"], int)
    assert response.json()["iso_utc"].endswith("Z")


def test_heartbeat_hmac_json(tmp_path: Path):
    client = build_client(tmp_path)
    response = post_json(client, "/v1/device/heartbeat", {"node_id": NODE_ID, "battery_v": 4.05, "charging": True})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_start_chunk_finish_upload(tmp_path: Path):
    client = build_client(tmp_path)
    wav = b"RIFF" + b"\x00" * 32 + b"WAVE" + b"payload"
    source_path = "20260608_220530.WAV"

    start = post_json(
        client,
        f"/v1/device/{NODE_ID}/upload/start",
        {"node_id": NODE_ID, "path": source_path, "size": len(wav), "chunk_bytes": 512, "started_epoch": int(time.time())},
    )
    assert start.status_code == 200

    query = urlencode(
        {
            "node_id": NODE_ID,
            "path": source_path,
            "offset": 0,
            "length": len(wav),
            "total": len(wav),
            "crc32": f"{zlib.crc32(wav) & 0xFFFFFFFF:08X}",
        }
    )
    chunk_path = f"/v1/device/{NODE_ID}/upload/chunk?{query}"
    chunk = client.post(
        chunk_path,
        content=wav,
        headers={**sign("POST", chunk_path, wav), "Content-Type": "application/octet-stream"},
    )
    assert chunk.status_code == 200

    finish = post_json(
        client,
        f"/v1/device/{NODE_ID}/upload/finish",
        {"node_id": NODE_ID, "path": source_path, "size": len(wav), "finished_epoch": int(time.time())},
    )
    assert finish.status_code == 200
    stored = Path(finish.json()["stored_path"])
    assert stored.exists()
    assert stored.read_bytes() == wav


def test_bad_crc_rejected(tmp_path: Path):
    client = build_client(tmp_path)
    data = b"abcdef"
    post_json(
        client,
        f"/v1/device/{NODE_ID}/upload/start",
        {"node_id": NODE_ID, "path": "badcrc.WAV", "size": len(data), "chunk_bytes": 512, "started_epoch": int(time.time())},
    )
    query = urlencode({"node_id": NODE_ID, "path": "badcrc.WAV", "offset": 0, "length": len(data), "total": len(data), "crc32": "00000000"})
    path = f"/v1/device/{NODE_ID}/upload/chunk?{query}"
    response = client.post(path, content=data, headers={**sign("POST", path, data), "Content-Type": "application/octet-stream"})
    assert response.status_code >= 400


def test_bad_signature_rejected(tmp_path: Path):
    client = build_client(tmp_path)
    body = json.dumps({"node_id": NODE_ID}).encode("utf-8")
    headers = sign("POST", "/v1/device/heartbeat", body)
    headers["X-Signature"] = "0" * 64
    response = client.post("/v1/device/heartbeat", content=body, headers=headers)
    assert response.status_code in (401, 403)


def test_path_traversal_rejected(tmp_path: Path):
    client = build_client(tmp_path)
    response = post_json(
        client,
        f"/v1/device/{NODE_ID}/upload/start",
        {"node_id": NODE_ID, "path": "../bad.WAV", "size": 8, "chunk_bytes": 512, "started_epoch": int(time.time())},
    )
    assert response.status_code >= 400
