from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import sqlite3
import time
import uuid
import wave
from io import BytesIO
from pathlib import Path

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
    os.environ["REQUIRE_FLAC_BEFORE_DELETE"] = "0"
    os.environ["REQUIRE_BACKUP_BEFORE_DELETE"] = "0"

    import bat_server

    importlib.reload(bat_server)
    bat_server.init_db()
    now = int(time.time())
    with bat_server.db_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO nodes (node_id, node_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (NODE_ID, NODE_ID, now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO node_credentials (node_id, key_id, secret, created_at) VALUES (?, ?, ?, ?)",
            (NODE_ID, KEY_ID, SECRET, now),
        )
        conn.commit()

    return TestClient(bat_server.app)


def make_wav() -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"\x00\x00" * 300)
    return buf.getvalue()


def test_server_time(tmp_path: Path):
    client = build_client(tmp_path)
    response = client.get("/v1/public/server_time")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert isinstance(response.json()["epoch_utc"], int)


def test_heartbeat_hmac_json(tmp_path: Path):
    client = build_client(tmp_path)
    response = post_json(client, "/v1/device/heartbeat", {"node_id": NODE_ID, "battery_v": 4.05, "charging": True})
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_manifest_init_chunk_complete_and_delete_authorization(tmp_path: Path):
    client = build_client(tmp_path)
    wav_bytes = make_wav()
    manifest_id = f"{NODE_ID}-AUDIOMOTH-SD"
    filename = "20260609_010203.WAV"
    local_file_id = 12345

    manifest = post_json(
        client,
        "/v1/files/manifest",
        {
            "node_id": NODE_ID,
            "manifest_id": manifest_id,
            "sd_card_id": "AudioMoth",
            "files": [
                {
                    "local_file_id": local_file_id,
                    "filename": filename,
                    "file_size_bytes": len(wav_bytes),
                }
            ],
        },
    )
    assert manifest.status_code == 200
    assert manifest.json()["ok"] is True
    assert manifest.json()["wanted_files"]

    init = post_json(
        client,
        "/v1/uploads/init",
        {
            "manifest_id": manifest_id,
            "local_file_id": local_file_id,
            "filename": filename,
            "file_size_bytes": len(wav_bytes),
            "chunk_size": 512,
        },
    )
    assert init.status_code == 200
    init_json = init.json()
    assert init_json["ok"] is True
    assert init_json["chunk_size"] == 512
    assert init_json["total_chunks"] == (len(wav_bytes) + 511) // 512
    assert init_json["next_missing_chunk"] == 0
    assert init_json["next_missing_offset"] == 0
    assert init_json["received_chunk_count"] == 0

    upload_id = init_json["upload_id"]
    first_body = wav_bytes[:512]
    first_path = f"/v1/uploads/{upload_id}/chunks/0"
    first_chunk = client.put(
        first_path,
        content=first_body,
        headers={**sign("PUT", first_path, first_body), "Content-Type": "application/octet-stream"},
    )
    assert first_chunk.status_code == 200
    assert first_chunk.json()["ok"] is True

    resume = post_json(
        client,
        "/v1/uploads/init",
        {
            "manifest_id": manifest_id,
            "local_file_id": local_file_id,
            "filename": filename,
            "file_size_bytes": len(wav_bytes),
            "chunk_size": 512,
        },
    )
    assert resume.status_code == 200
    resume_json = resume.json()
    assert resume_json["upload_id"] == upload_id
    assert resume_json["next_missing_chunk"] == 1
    assert resume_json["next_missing_offset"] == 512
    assert resume_json["received_chunk_count"] == 1

    status_path = f"/v1/uploads/{upload_id}/status"
    status = client.get(status_path, headers=sign("GET", status_path))
    assert status.status_code == 200
    status_json = status.json()
    assert status_json["next_missing_chunk"] == 1
    assert status_json["next_missing_offset"] == 512
    assert status_json["received_chunk_count"] == 1
    assert "received_chunks" not in status_json
    assert "missing_chunks" not in status_json

    for index, start in enumerate(range(512, len(wav_bytes), 512), start=1):
        body = wav_bytes[start : start + 512]
        path = f"/v1/uploads/{upload_id}/chunks/{index}"
        chunk = client.put(
            path,
            content=body,
            headers={**sign("PUT", path, body), "Content-Type": "application/octet-stream"},
        )
        assert chunk.status_code == 200
        assert chunk.json()["ok"] is True

    complete_path = f"/v1/uploads/{upload_id}/complete"
    complete = post_json(client, complete_path, {})
    assert complete.status_code == 200
    assert complete.json()["ok"] is True
    assert complete.json()["wav_parse_status"] == "OK"

    auth_path = f"/v1/nodes/{NODE_ID}/delete_authorization"
    auth = client.get(
        f"{auth_path}?manifest_id={manifest_id}",
        headers=sign("GET", auth_path),
    )
    assert auth.status_code == 200
    auth_json = auth.json()
    assert auth_json["ok"] is True
    assert len(auth_json["files"]) == 1
    assert auth_json["files"][0]["local_file_id"] == local_file_id

    body = json.dumps(
        {
            "authorization_id": auth_json["authorization_id"],
            "files": [
                {
                    "file_id": auth_json["files"][0]["file_id"],
                    "local_file_id": local_file_id,
                    "filename": filename,
                    "result": "DELETED",
                    "error": None,
                }
            ],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    confirm_path = f"/v1/nodes/{NODE_ID}/delete_confirm"
    confirm = client.post(
        confirm_path,
        content=body,
        headers={**sign("POST", confirm_path, body), "Content-Type": "application/json"},
    )
    assert confirm.status_code == 200
    assert confirm.json()["ok"] is True


def test_upload_init_rejects_bad_chunk_size(tmp_path: Path):
    client = build_client(tmp_path)
    response = post_json(
        client,
        "/v1/uploads/init",
        {
            "manifest_id": "missing",
            "filename": "bad.WAV",
            "file_size_bytes": 8,
            "chunk_size": 2 * 1024 * 1024,
        },
    )
    assert response.status_code == 400


def test_bad_signature_rejected(tmp_path: Path):
    client = build_client(tmp_path)
    body = json.dumps({"node_id": NODE_ID}).encode("utf-8")
    headers = sign("POST", "/v1/device/heartbeat", body)
    headers["X-Signature"] = "0" * 64
    response = client.post("/v1/device/heartbeat", content=body, headers=headers)
    assert response.status_code == 401


def test_replay_nonce_rejected(tmp_path: Path):
    client = build_client(tmp_path)
    body = json.dumps({"node_id": NODE_ID}).encode("utf-8")
    headers = {**sign("POST", "/v1/device/heartbeat", body), "Content-Type": "application/json"}
    first = client.post("/v1/device/heartbeat", content=body, headers=headers)
    second = client.post("/v1/device/heartbeat", content=body, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 401
