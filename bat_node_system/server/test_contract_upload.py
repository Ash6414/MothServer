from __future__ import annotations

import ast
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
from types import SimpleNamespace

from fastapi.testclient import TestClient

NODE_ID = "BATNODE_001"
KEY_ID = "key-1"
SECRET = "REPLACE_WITH_64_HEX_OR_SERVER_SECRET"


def literal_string_set(path: Path, name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        if not isinstance(node.value, (ast.Set, ast.List, ast.Tuple)):
            raise AssertionError(f"{name} is not a literal set/list/tuple")
        values = set()
        for item in node.value.elts:
            assert isinstance(item, ast.Constant) and isinstance(item.value, str)
            values.add(item.value)
        return values
    raise AssertionError(f"{name} not found in {path}")


def sign(
    method: str,
    path: str,
    body: bytes = b"",
    *,
    secret: str = SECRET,
    node_id: str = NODE_ID,
    key_id: str = KEY_ID,
) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body_sha = hashlib.sha256(body).hexdigest()
    canonical = "\n".join([method.upper(), path, timestamp, nonce, body_sha])
    signature = hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "X-Node-ID": node_id,
        "X-Key-ID": key_id,
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
    os.environ["FLAC_ENCODER"] = "none"
    os.environ["PROVISIONING_TOKEN"] = "test-provision-token"
    os.environ["DASHBOARD_USER"] = "admin"
    os.environ["DASHBOARD_PASSWORD"] = "test-dashboard-password"

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


def test_public_gateway_exposes_devices_not_admin():
    import gateway_policy

    assert gateway_policy.is_public_device_path("/v1/public/server_time")
    assert gateway_policy.is_public_device_path("/v1/enrollment/request")
    assert gateway_policy.is_public_device_path("/v1/device/heartbeat")
    assert gateway_policy.is_public_device_path("/v1/uploads/abc/chunks/0")
    assert not gateway_policy.is_public_device_path("/admin/enrollment/requests")
    assert not gateway_policy.is_public_device_path("/dashboard")
    assert not gateway_policy.is_public_device_path("/docs")
    assert not gateway_policy.is_public_device_path("/v1/provision/node")


def test_command_allowlists_match_runtime_contract_and_dashboard_actions():
    import bat_server
    import bat_server_contract

    dashboard_path = Path(__file__).resolve().parents[1] / "dashboard" / "bat_dashboard_app.py"
    dashboard_actions = literal_string_set(dashboard_path, "DASHBOARD_COMMAND_TYPES")
    dashboard_source = dashboard_path.read_text(encoding="utf-8")

    assert bat_server.ALLOWED_COMMAND_TYPES == bat_server_contract.ALLOWED_COMMAND_TYPES
    assert bat_server.ALLOWED_COMMAND_TYPES == dashboard_actions
    assert "Custom command type" not in dashboard_source
    assert "Queue custom command" not in dashboard_source


def test_admin_command_endpoint_rejects_unsupported_commands(tmp_path: Path):
    client = build_client(tmp_path)
    response = client.post(
        f"/admin/commands/{NODE_ID}/FORCE_MANIFEST",
        auth=("admin", "test-dashboard-password"),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "unsupported command type"


def test_admin_command_endpoint_queues_supported_commands_uppercase(tmp_path: Path):
    client = build_client(tmp_path)
    response = client.post(
        f"/admin/commands/{NODE_ID}/moth_status",
        auth=("admin", "test-dashboard-password"),
    )
    assert response.status_code == 200
    command_id = response.json()["command_id"]

    import bat_server

    with bat_server.db_connect() as conn:
        row = conn.execute("SELECT command_type FROM commands WHERE id=?", (command_id,)).fetchone()
    assert row["command_type"] == "MOTH_STATUS"


def test_provision_node_creates_hmac_credentials(tmp_path: Path):
    client = build_client(tmp_path)
    response = client.post(
        "/v1/provision/node",
        json={
            "provisioning_token": "test-provision-token",
            "node_id": "BATNODE_FIELD_01",
            "node_name": "Field Node 1",
            "hardware_version": "ESP32 AudioMoth bridge",
            "firmware_version": "Moth_Node_ESPBridge",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["node_id"] == "BATNODE_FIELD_01"
    assert data["key_id"] == "key-1"
    assert len(data["device_secret"]) == 64

    import bat_server

    assert bat_server.get_node_secret(data["node_id"], data["key_id"]) == data["device_secret"]


def test_approved_enrollment_reuses_hardware_node_id(tmp_path: Path):
    client = build_client(tmp_path)
    hardware_uid = "A1B2C3D4E5F6"

    first = client.post(
        "/v1/enrollment/request",
        json={
            "hardware_uid": hardware_uid,
            "node_name": "Reflashed field node",
            "hardware_version": "ESP32 AudioMoth bridge",
            "firmware_version": "Moth_Node_ESPBridge",
        },
    )
    assert first.status_code == 200
    pending = first.json()
    assert pending["status"] == "PENDING"
    assert "device_secret" not in pending

    approval = client.post(
        f"/admin/enrollment/{pending['request_id']}/approve",
        json={"target_node_id": NODE_ID},
        auth=("admin", "test-dashboard-password"),
    )
    assert approval.status_code == 200
    assert approval.json()["node_id"] == NODE_ID

    delivered = client.post(
        f"/v1/enrollment/status/{pending['request_id']}",
        json={"poll_token": pending["poll_token"]},
    )
    assert delivered.status_code == 200
    first_credentials = delivered.json()
    assert first_credentials["status"] == "APPROVED"
    assert first_credentials["node_id"] == NODE_ID
    assert len(first_credentials["device_secret"]) == 64

    second = client.post(
        "/v1/enrollment/request",
        json={"hardware_uid": hardware_uid, "node_name": "Same physical node"},
    ).json()
    assert second["recognized_node"] == NODE_ID

    second_approval = client.post(
        f"/admin/enrollment/{second['request_id']}/approve",
        json={},
        auth=("admin", "test-dashboard-password"),
    )
    assert second_approval.status_code == 200
    assert second_approval.json()["node_id"] == NODE_ID
    assert second_approval.json()["re_enrolled"] is True

    second_delivered = client.post(
        f"/v1/enrollment/status/{second['request_id']}",
        json={"poll_token": second["poll_token"]},
    ).json()
    assert second_delivered["node_id"] == NODE_ID
    assert second_delivered["device_secret"] != first_credentials["device_secret"]

    heartbeat_body = json.dumps({"node_id": NODE_ID}, separators=(",", ":")).encode("utf-8")
    heartbeat = client.post(
        "/v1/device/heartbeat",
        content=heartbeat_body,
        headers={
            **sign(
                "POST",
                "/v1/device/heartbeat",
                heartbeat_body,
                secret=second_delivered["device_secret"],
                node_id=NODE_ID,
                key_id=second_delivered["key_id"],
            ),
            "Content-Type": "application/json",
        },
    )
    assert heartbeat.status_code == 200

    import bat_server

    with bat_server.db_connect() as conn:
        node = conn.execute("SELECT hardware_uid FROM nodes WHERE node_id=?", (NODE_ID,)).fetchone()
        node_count = conn.execute("SELECT COUNT(*) AS count FROM nodes").fetchone()["count"]
        enrollment = conn.execute(
            "SELECT status, device_secret FROM enrollment_requests WHERE request_id=?",
            (second["request_id"],),
        ).fetchone()
    assert node["hardware_uid"] == hardware_uid
    assert node_count == 1
    assert enrollment["status"] == "CLAIMED"
    assert enrollment["device_secret"] is None


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

    import bat_server

    with bat_server.db_connect() as conn:
        file_id = int(conn.execute(
            "SELECT id FROM files WHERE node_id=? AND filename=?",
            (NODE_ID, filename),
        ).fetchone()["id"])
        bat_server.catalog_recording(conn, file_id)
        conn.commit()
        catalog_row = conn.execute(
            "SELECT id, canonical_name, recorded_at_utc, recorded_at_source FROM files WHERE node_id=? AND filename=?",
            (NODE_ID, filename),
        ).fetchone()
    assert catalog_row["canonical_name"] == f"{NODE_ID}_20260609T010203Z_{int(catalog_row['id']):06d}.WAV"
    assert catalog_row["recorded_at_utc"] == 1780966923
    assert catalog_row["recorded_at_source"] == "filename_utc"

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
    assert complete.json()["flac_status"] == "PENDING"
    assert complete.json()["finalize_ms"] >= 0

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


def test_upload_accepts_esp_64k_production_chunks(tmp_path: Path):
    client = build_client(tmp_path)
    chunk_size = 64 * 1024
    payload = b"A" * chunk_size + b"tail"
    manifest_id = f"{NODE_ID}-FAST-CHUNK"
    local_file_id = 64001
    filename = "fast-transfer.bin"

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
                    "file_size_bytes": len(payload),
                }
            ],
        },
    )
    assert manifest.status_code == 200

    init = post_json(
        client,
        "/v1/uploads/init",
        {
            "manifest_id": manifest_id,
            "local_file_id": local_file_id,
            "filename": filename,
            "file_size_bytes": len(payload),
            "chunk_size": chunk_size,
        },
    )
    assert init.status_code == 200
    upload = init.json()
    assert upload["chunk_size"] == chunk_size
    assert upload["total_chunks"] == 2

    for index, body in enumerate((payload[:chunk_size], payload[chunk_size:])):
        path = f"/v1/uploads/{upload['upload_id']}/chunks/{index}"
        response = client.put(
            path,
            content=body,
            headers={**sign("PUT", path, body), "Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 200
        assert response.json()["server_ms"] >= 0
        assert response.json()["bytes_received"] == min((index + 1) * chunk_size, len(payload))


def test_upload_init_supersedes_changed_chunk_size_and_removes_temp_file(tmp_path: Path):
    client = build_client(tmp_path)
    import bat_server

    for index, (first_chunk, second_chunk) in enumerate(((64 * 1024, 128 * 1024), (128 * 1024, 64 * 1024))):
        manifest_id = f"{NODE_ID}-RESIZE-CHUNK-{index}"
        local_file_id = 128002 + index
        filename = f"resize-transfer-{index}.bin"
        file_size = 192 * 1024

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
                        "file_size_bytes": file_size,
                    }
                ],
            },
        )
        assert manifest.status_code == 200

        first = post_json(
            client,
            "/v1/uploads/init",
            {
                "manifest_id": manifest_id,
                "local_file_id": local_file_id,
                "filename": filename,
                "file_size_bytes": file_size,
                "chunk_size": first_chunk,
            },
        )
        assert first.status_code == 200
        first_upload_id = first.json()["upload_id"]

        with bat_server.db_connect() as conn:
            row = conn.execute("SELECT temp_path FROM upload_sessions WHERE upload_id=?", (first_upload_id,)).fetchone()
        old_temp = Path(row["temp_path"])
        assert old_temp.exists()

        second = post_json(
            client,
            "/v1/uploads/init",
            {
                "manifest_id": manifest_id,
                "local_file_id": local_file_id,
                "filename": filename,
                "file_size_bytes": file_size,
                "chunk_size": second_chunk,
            },
        )
        assert second.status_code == 200
        assert second.json()["upload_id"] != first_upload_id
        assert second.json()["chunk_size"] == second_chunk

        with bat_server.db_connect() as conn:
            row = conn.execute("SELECT status FROM upload_sessions WHERE upload_id=?", (first_upload_id,)).fetchone()
        assert row["status"] == "SUPERSEDED"
        assert not old_temp.exists()


def test_make_flac_uses_flac_cli_when_available(tmp_path: Path, monkeypatch):
    os.environ["BAT_DB_PATH"] = str(tmp_path / "bat_nodes_v2.db")
    os.environ["BAT_DATA_DIR"] = str(tmp_path / "data")
    os.environ["FLAC_ENCODER"] = "flac"
    os.environ["FLAC_COMPRESSION_LEVEL"] = "8"

    import bat_server

    importlib.reload(bat_server)
    wav_path = tmp_path / "sample.WAV"
    wav_path.write_bytes(make_wav())
    captured = {}

    monkeypatch.setattr(bat_server.shutil, "which", lambda name: "C:/fake/flac.exe" if name == "flac" else None)

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"fLaC")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(bat_server.subprocess, "run", fake_run)

    status, flac_path, error = bat_server.make_flac(wav_path, NODE_ID)

    assert status == "OK"
    assert error is None
    assert flac_path is not None
    assert flac_path.name == "sample.flac"
    assert captured["cmd"][:5] == ["C:/fake/flac.exe", "-8", "-f", "-s", "-o"]
    assert captured["kwargs"]["timeout"] == 600


def test_catalog_falls_back_to_upload_day_for_unusual_filename(tmp_path: Path):
    build_client(tmp_path)
    import bat_server

    created_at = 1781827200
    with bat_server.db_connect() as conn:
        conn.execute(
            "INSERT INTO manifests (manifest_id, node_id, created_at, updated_at, raw_json) VALUES ('odd-manifest', ?, ?, ?, '{}')",
            (NODE_ID, created_at, created_at),
        )
        cur = conn.execute(
            """
            INSERT INTO files (node_id, manifest_id, filename, file_size_bytes, upload_status, created_at, updated_at)
            VALUES (?, 'odd-manifest', 'bat.WAV', 1234, 'ON_SD_ONLY', ?, ?)
            """,
            (NODE_ID, created_at, created_at),
        )
        file_id = int(cur.lastrowid)
        bat_server.catalog_recording(conn, file_id)
        row = conn.execute(
            "SELECT canonical_name, recorded_at_utc, recorded_at_source FROM files WHERE id=?",
            (file_id,),
        ).fetchone()
        conn.commit()

    assert row["canonical_name"] == f"{NODE_ID}_UPLOADED_20260619_{file_id:06d}.WAV"
    assert row["recorded_at_utc"] is None
    assert row["recorded_at_source"] == "upload_day_fallback"


def test_catalog_backfill_adopts_existing_canonical_file(tmp_path: Path):
    build_client(tmp_path)
    import bat_server

    created_at = 1781827200
    node_dir = tmp_path / "data" / "original_wav" / NODE_ID
    node_dir.mkdir(parents=True, exist_ok=True)
    old_path = node_dir / "old-name.WAV"
    old_path.write_bytes(b"old duplicate")

    with bat_server.db_connect() as conn:
        conn.execute(
            "INSERT INTO manifests (manifest_id, node_id, created_at, updated_at, raw_json) VALUES ('canonical-manifest', ?, ?, ?, '{}')",
            (NODE_ID, created_at, created_at),
        )
        cur = conn.execute(
            """
            INSERT INTO files (
                node_id, manifest_id, filename, file_size_bytes, upload_status,
                original_wav_path, created_at, updated_at
            ) VALUES (?, 'canonical-manifest', 'odd-name.WAV', ?, 'SERVER_COPY_VERIFIED',
                      ?, ?, ?)
            """,
            (NODE_ID, old_path.stat().st_size, str(old_path), created_at, created_at),
        )
        file_id = int(cur.lastrowid)
        canonical_name = f"{NODE_ID}_UPLOADED_20260619_{file_id:06d}.WAV"
        canonical_path = node_dir / canonical_name
        canonical_path.write_bytes(b"canonical copy")

        bat_server.catalog_recording(conn, file_id, rename_files=True)
        row = conn.execute(
            "SELECT canonical_name, original_wav_path FROM files WHERE id=?",
            (file_id,),
        ).fetchone()
        conn.commit()

    assert row["canonical_name"] == canonical_name
    assert row["original_wav_path"] == str(canonical_path)
    assert canonical_path.exists()
    assert old_path.exists()


def test_reconcile_compresses_missed_verified_wav(tmp_path: Path, monkeypatch):
    build_client(tmp_path)
    import bat_server

    wav_path = tmp_path / "data" / "original_wav" / NODE_ID / "missed.WAV"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    wav_path.write_bytes(make_wav())
    t = int(time.time())
    with bat_server.db_connect() as conn:
        conn.execute(
            "INSERT INTO manifests (manifest_id, node_id, created_at, updated_at, raw_json) VALUES ('missed-manifest', ?, ?, ?, '{}')",
            (NODE_ID, t, t),
        )
        cur = conn.execute(
            """
            INSERT INTO files (
                node_id, manifest_id, filename, file_size_bytes, upload_status, bytes_received,
                wav_parse_status, flac_status, original_wav_path, created_at, updated_at
            ) VALUES (?, 'missed-manifest', 'missed.WAV', ?, 'SERVER_COPY_VERIFIED', ?,
                      'NOT_RUN_FAST_FINISH', 'SKIPPED_NO_ENCODER', ?, ?, ?)
            """,
            (NODE_ID, wav_path.stat().st_size, wav_path.stat().st_size, str(wav_path), t, t),
        )
        file_id = int(cur.lastrowid)
        bat_server.catalog_recording(conn, file_id)
        conn.commit()

    monkeypatch.setattr(bat_server, "find_flac_encoder", lambda: ("flac", "C:/fake/flac.exe"))

    def fake_make_flac(source: Path, node_id: str):
        output = tmp_path / "data" / "flac" / node_id / source.with_suffix(".flac").name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fLaC" + b"\x00" * 32)
        return "OK", output, None

    monkeypatch.setattr(bat_server, "make_flac", fake_make_flac)
    result = bat_server.reconcile_flac_files(limit=1, file_ids=[file_id])

    assert result["ok"] is True
    assert result["compressed"] == 1
    with bat_server.db_connect() as conn:
        row = conn.execute(
            "SELECT wav_parse_status, flac_status, flac_path, sample_rate FROM files WHERE id=?",
            (file_id,),
        ).fetchone()
    assert row["wav_parse_status"] == "OK"
    assert row["flac_status"] == "OK"
    assert Path(row["flac_path"]).read_bytes()[:4] == b"fLaC"
    assert row["sample_rate"] == 8000


def test_parse_wav_rejects_empty_audio(tmp_path: Path):
    build_client(tmp_path)
    import bat_server

    empty_wav = tmp_path / "empty.WAV"
    with wave.open(str(empty_wav), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"")

    try:
        bat_server.parse_wav(empty_wav)
    except ValueError as exc:
        assert "no audio frames" in str(exc)
    else:
        raise AssertionError("Empty WAV should not pass validation")


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
