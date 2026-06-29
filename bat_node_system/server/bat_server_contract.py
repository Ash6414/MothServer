from __future__ import annotations

"""Current ESP32-WROOM-U upload-contract adapter for the existing bat_server app.

Run:
    uvicorn bat_server_contract:app --host 0.0.0.0 --port 8000

This keeps the older bat_server.py endpoints available and adds/replaces only the
routes required by the current ESP32 firmware contract.
"""

import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import time
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Optional

from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

import bat_server as legacy

app = legacy.app

UPLOAD_ROOT = Path(os.getenv("BAT_UPLOAD_ROOT", str(legacy.DATA_DIR / "uploads")))
AUTH_WINDOW_SECONDS = int(os.getenv("AUTH_WINDOW_SECONDS", "900"))
NONCE_RETENTION_SECONDS = int(os.getenv("AUTH_NONCE_RETENTION_SECONDS", str(AUTH_WINDOW_SECONDS * 4)))
DEFAULT_NODE_ID = os.getenv("MOTH_NODE_ID", "BATNODE_001")
DEFAULT_KEY_ID = os.getenv("MOTH_KEY_ID", "key-1")
DEFAULT_DEVICE_SECRET = os.getenv("MOTH_DEVICE_SECRET", "REPLACE_WITH_64_HEX_OR_SERVER_SECRET")
ALLOWED_COMMAND_TYPES = {
    "PING",
    "UPLOAD_NOW",
    "SYNC_MOTH_TIME",
    "MOTH_STATUS",
    "MOTH_LIST",
    "MOTH_TEST_STREAM",
    "OPEN_SETUP",
}
TIMESTAMP_RE = re.compile(r"(?:^|/)(?P<date>\d{8})_(?P<time>\d{6})\.wav$", re.IGNORECASE)
SAFE_NODE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_PATH_PART_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def now_epoch() -> int:
    return int(time.time())


def iso_utc(epoch: Optional[int] = None) -> str:
    return datetime.fromtimestamp(epoch if epoch is not None else now_epoch(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_connect() -> sqlite3.Connection:
    return legacy.db_connect()


def json_compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def bool_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return 1 if bool(value) else 0


def ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def ensure_contract_schema() -> None:
    legacy.init_db()
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS heartbeats (
                id INTEGER PRIMARY KEY,
                node_id TEXT NOT NULL,
                received_epoch INTEGER NOT NULL,
                battery_v REAL,
                charging INTEGER,
                charge_done INTEGER,
                wifi_rssi_dbm REAL,
                upload_status TEXT,
                raw_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recordings (
                id INTEGER PRIMARY KEY,
                node_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                size INTEGER NOT NULL,
                uploaded_epoch INTEGER NOT NULL,
                recording_epoch INTEGER,
                sha256 TEXT,
                weather_status TEXT,
                weather_json TEXT,
                UNIQUE(node_id, source_path)
            );

            CREATE TABLE IF NOT EXISTS esp32_upload_sessions (
                id INTEGER PRIMARY KEY,
                node_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                expected_size INTEGER NOT NULL,
                chunk_bytes INTEGER,
                received_bytes INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                temp_path TEXT NOT NULL,
                final_path TEXT,
                started_epoch INTEGER,
                finished_epoch INTEGER,
                created_epoch INTEGER NOT NULL,
                updated_epoch INTEGER NOT NULL,
                error_message TEXT,
                legacy_upload_id TEXT,
                legacy_file_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS esp32_upload_chunks (
                session_id INTEGER NOT NULL REFERENCES esp32_upload_sessions(id),
                offset_bytes INTEGER NOT NULL,
                length_bytes INTEGER NOT NULL,
                crc32 TEXT NOT NULL,
                received_epoch INTEGER NOT NULL,
                PRIMARY KEY(session_id, offset_bytes)
            );
            """
        )
        ensure_column(conn, "node_state", "raw_status_json", "TEXT")
        ensure_column(conn, "node_state", "bridge_json", "TEXT")
        ensure_column(conn, "node_state", "stats_json", "TEXT")
        ensure_column(conn, "telemetry", "raw_json", "TEXT")
        ensure_column(conn, "time_checks", "raw_json", "TEXT")
        conn.commit()
        ensure_development_node_secret(conn)


def ensure_development_node_secret(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT 1 FROM node_credentials WHERE node_id=? AND key_id=?",
        (DEFAULT_NODE_ID, DEFAULT_KEY_ID),
    ).fetchone()
    if row:
        return
    t = now_epoch()
    conn.execute(
        "INSERT OR IGNORE INTO nodes (node_id, node_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (DEFAULT_NODE_ID, DEFAULT_NODE_ID, t, t),
    )
    conn.execute(
        "INSERT OR IGNORE INTO node_credentials (node_id, key_id, secret, created_at) VALUES (?, ?, ?, ?)",
        (DEFAULT_NODE_ID, DEFAULT_KEY_ID, DEFAULT_DEVICE_SECRET, t),
    )
    conn.commit()


@app.on_event("startup")
def contract_startup() -> None:
    ensure_contract_schema()


ensure_contract_schema()


def safe_node_id(node_id: str) -> str:
    if not SAFE_NODE_RE.fullmatch(node_id or ""):
        raise HTTPException(status_code=400, detail="Unsafe node_id")
    return node_id


def sanitize_moth_path(path: str) -> PurePosixPath:
    if not isinstance(path, str):
        raise HTTPException(status_code=400, detail="path must be a string")
    value = path.strip()
    if not value:
        raise HTTPException(status_code=400, detail="empty path rejected")
    if "\\" in value:
        raise HTTPException(status_code=400, detail="backslashes are rejected")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise HTTPException(status_code=400, detail="unsafe path rejected")
    if len(parsed.parts) > 2:
        raise HTTPException(status_code=400, detail="only one daily folder level is allowed")
    if not parsed.name.lower().endswith(".wav"):
        raise HTTPException(status_code=400, detail="only WAV files are accepted")
    for part in parsed.parts:
        if part in ("", ".") or part.startswith(".") or not SAFE_PATH_PART_RE.fullmatch(part):
            raise HTTPException(status_code=400, detail="unsafe path component rejected")
    return parsed


def assert_inside(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="resolved path escaped upload root") from exc


def upload_paths(node_id: str, source_path: PurePosixPath) -> tuple[Path, Path]:
    node_root = UPLOAD_ROOT / safe_node_id(node_id)
    incoming = node_root / "incoming"
    recordings = node_root / "recordings"
    temp_path = incoming.joinpath(*source_path.parts).with_suffix(source_path.suffix + ".part")
    final_path = recordings.joinpath(*source_path.parts)
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    assert_inside(temp_path, incoming)
    assert_inside(final_path, recordings)
    return temp_path, final_path


def parse_recording_epoch(source_path: str) -> Optional[int]:
    match = TIMESTAMP_RE.search(source_path)
    if not match:
        return None
    try:
        dt = datetime.strptime(match.group("date") + match.group("time"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp())


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_node_secret(node_id: str, key_id: str) -> Optional[str]:
    secret = legacy.get_node_secret(node_id, key_id)
    if secret:
        return secret
    if node_id == DEFAULT_NODE_ID and key_id == DEFAULT_KEY_ID:
        return DEFAULT_DEVICE_SECRET
    return None


def record_nonce(node_id: str, nonce: str, timestamp_value: int) -> None:
    cutoff = now_epoch() - NONCE_RETENTION_SECONDS
    with db_connect() as conn:
        conn.execute("DELETE FROM auth_nonces WHERE created_at < ?", (cutoff,))
        try:
            conn.execute("INSERT INTO auth_nonces (node_id, nonce, created_at) VALUES (?, ?, ?)", (node_id, nonce, timestamp_value))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=401, detail="duplicate nonce rejected") from exc
        conn.commit()


async def require_contract_auth(request: Request, body: Optional[bytes] = None, include_query: bool = False) -> Dict[str, Any]:
    if body is None:
        body = await request.body()
    node_id = request.headers.get("X-Node-ID", "").strip()
    key_id = request.headers.get("X-Key-ID", "").strip()
    timestamp = request.headers.get("X-Timestamp", "").strip()
    nonce = request.headers.get("X-Nonce", "").strip()
    body_sha = request.headers.get("X-Body-SHA256", "").strip().lower()
    signature = request.headers.get("X-Signature", "").strip().lower()
    if not all([node_id, key_id, timestamp, nonce, body_sha, signature]):
        raise HTTPException(status_code=401, detail="missing device authentication headers")
    safe_node_id(node_id)
    try:
        timestamp_int = int(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="bad timestamp") from exc
    if abs(now_epoch() - timestamp_int) > AUTH_WINDOW_SECONDS:
        raise HTTPException(status_code=401, detail="timestamp outside allowed window")
    actual_body_sha = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(actual_body_sha, body_sha):
        raise HTTPException(status_code=401, detail="body hash mismatch")
    secret = get_node_secret(node_id, key_id)
    if not secret:
        raise HTTPException(status_code=401, detail="unknown, revoked, or inactive device key")
    canonical_path = request.url.path + ("?" + request.url.query if include_query else "")
    canonical = "\n".join([request.method.upper(), canonical_path, timestamp, nonce, body_sha])
    expected = hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=403, detail="bad signature")
    record_nonce(node_id, nonce, timestamp_int)
    return {"node_id": node_id, "key_id": key_id, "secret": secret}


def remove_existing_route(path: str, methods: Iterable[str]) -> None:
    wanted = {m.upper() for m in methods}
    app.router.routes = [
        route for route in app.router.routes
        if not (getattr(route, "path", None) == path and bool(wanted & set(getattr(route, "methods", set()))))
    ]


for route_path, route_methods in [
    ("/v1/public/server_time", ["GET"]),
    ("/v1/device/heartbeat", ["POST"]),
    ("/v1/device/time_check", ["POST"]),
    ("/v1/device/{node_id}/commands", ["GET"]),
    ("/v1/device/{node_id}/commands/{command_id}/ack", ["POST"]),
]:
    remove_existing_route(route_path, route_methods)


@app.exception_handler(HTTPException)
async def contract_http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": exc.detail})


@app.get("/v1/public/server_time")
def contract_server_time() -> Dict[str, Any]:
    epoch = now_epoch()
    return {"epoch_utc": epoch, "iso_utc": iso_utc(epoch)}


@app.post("/v1/device/heartbeat")
async def contract_heartbeat(request: Request) -> Dict[str, bool]:
    body = await request.body()
    ident = await require_contract_auth(request, body)
    data = json.loads(body.decode("utf-8") or "{}")
    node_id = data.get("node_id") or ident["node_id"]
    if node_id != ident["node_id"]:
        raise HTTPException(status_code=403, detail="node_id mismatch")
    t = now_epoch()
    raw_json = json_compact(data)
    upload_status = str(data.get("upload_status") or "") or None
    with db_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO nodes (node_id, node_name, created_at, updated_at) VALUES (?, ?, ?, ?)", (node_id, node_id, t, t))
        conn.execute("UPDATE nodes SET updated_at=? WHERE node_id=?", (t, node_id))
        conn.execute(
            """
            INSERT INTO telemetry (
                node_id, created_at, battery_v, battery_percent, solar_v, charging,
                charge_done, recently_charged, sd_free_mb, recording_status, upload_status,
                wifi_rssi_dbm, mode, message, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (node_id, t, data.get("battery_v"), data.get("battery_percent"), data.get("solar_v"), bool_int(data.get("charging")), bool_int(data.get("charge_done")), bool_int(data.get("recently_charged")), data.get("sd_free_mb"), data.get("recording_status"), upload_status, data.get("wifi_rssi_dbm"), data.get("mode"), upload_status, raw_json),
        )
        conn.execute("INSERT INTO heartbeats (node_id, received_epoch, battery_v, charging, charge_done, wifi_rssi_dbm, upload_status, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (node_id, t, data.get("battery_v"), bool_int(data.get("charging")), bool_int(data.get("charge_done")), data.get("wifi_rssi_dbm"), upload_status, raw_json))
        conn.execute(
            """
            INSERT INTO node_state (
                node_id, last_seen, battery_v, battery_percent, solar_v, charging,
                charge_done, recently_charged, sd_free_mb, recording_status, upload_status,
                wifi_rssi_dbm, mode, message, updated_at, raw_status_json, bridge_json, stats_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                last_seen=excluded.last_seen, battery_v=excluded.battery_v, battery_percent=excluded.battery_percent,
                solar_v=excluded.solar_v, charging=excluded.charging, charge_done=excluded.charge_done,
                recently_charged=excluded.recently_charged, sd_free_mb=excluded.sd_free_mb,
                recording_status=excluded.recording_status, upload_status=excluded.upload_status,
                wifi_rssi_dbm=excluded.wifi_rssi_dbm, mode=excluded.mode, message=excluded.message,
                updated_at=excluded.updated_at, raw_status_json=excluded.raw_status_json,
                bridge_json=excluded.bridge_json, stats_json=excluded.stats_json
            """,
            (node_id, t, data.get("battery_v"), data.get("battery_percent"), data.get("solar_v"), bool_int(data.get("charging")), bool_int(data.get("charge_done")), bool_int(data.get("recently_charged")), data.get("sd_free_mb"), data.get("recording_status"), upload_status, data.get("wifi_rssi_dbm"), data.get("mode"), upload_status, t, raw_json, json_compact(data.get("bridge")) if data.get("bridge") is not None else None, json_compact(data.get("stats")) if data.get("stats") is not None else None),
        )
        conn.commit()
    return {"ok": True}


@app.post("/v1/device/time_check")
async def contract_time_check(request: Request) -> Dict[str, bool]:
    body = await request.body()
    ident = await require_contract_auth(request, body)
    data = json.loads(body.decode("utf-8") or "{}")
    node_id = data.get("node_id") or ident["node_id"]
    if node_id != ident["node_id"]:
        raise HTTPException(status_code=403, detail="node_id mismatch")
    server_epoch = int(data.get("server_epoch") or now_epoch())
    esp_after = data.get("esp_epoch_after")
    moth_epoch = data.get("audiomoth_epoch")
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO time_checks (
                node_id, created_at, server_epoch, esp_epoch_before, esp_epoch_after,
                esp_offset_before_seconds, esp_offset_after_seconds, audiomoth_epoch,
                audiomoth_offset_seconds, rtt_ms, time_source, notes, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (node_id, now_epoch(), server_epoch, data.get("esp_epoch_before"), esp_after, (data.get("esp_epoch_before") - server_epoch) if data.get("esp_epoch_before") is not None else None, (esp_after - server_epoch) if esp_after is not None else None, moth_epoch, (moth_epoch - server_epoch) if moth_epoch is not None else None, data.get("rtt_ms"), data.get("time_source"), data.get("notes"), json_compact(data)),
        )
        conn.commit()
    return {"ok": True}


@app.get("/v1/device/{node_id}/commands")
async def contract_get_commands(node_id: str, request: Request) -> Dict[str, Any]:
    body = await request.body()
    ident = await require_contract_auth(request, body)
    if ident["node_id"] != node_id:
        raise HTTPException(status_code=403, detail="node_id mismatch")
    t = now_epoch()
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id, command_type, payload_json FROM commands WHERE node_id=? AND status IN ('PENDING', 'pending') AND (expires_at IS NULL OR expires_at > ?) ORDER BY created_at ASC, id ASC LIMIT 10",
            (node_id, t),
        ).fetchall()
    return {"commands": [{"id": int(row["id"]), "type": str(row["command_type"]).upper(), "payload": json.loads(row["payload_json"] or "{}")} for row in rows]}


@app.post("/v1/device/{node_id}/commands/{command_id}/ack")
async def contract_ack_command(node_id: str, command_id: int, request: Request) -> Dict[str, bool]:
    body = await request.body()
    ident = await require_contract_auth(request, body)
    if ident["node_id"] != node_id:
        raise HTTPException(status_code=403, detail="node_id mismatch")
    data = json.loads(body.decode("utf-8") or "{}")
    with db_connect() as conn:
        cur = conn.execute("UPDATE commands SET status='ACKED', acked_at=?, response_json=? WHERE id=? AND node_id=?", (now_epoch(), json_compact(data.get("response", {})), command_id, node_id))
        if cur.rowcount != 1:
            raise HTTPException(status_code=404, detail="command not found")
        conn.commit()
    return {"ok": True}


def legacy_manifest_id(node_id: str) -> str:
    return f"ESP32_UART_UPLOAD_{node_id}"


def upsert_legacy_file(conn: sqlite3.Connection, node_id: str, source_path: str, size: int, recording_epoch: Optional[int], t: int) -> int:
    manifest_id = legacy_manifest_id(node_id)
    conn.execute("INSERT INTO manifests (manifest_id, node_id, deployment_id, sd_card_id, created_at, updated_at, raw_json) VALUES (?, ?, 'ESP32_UART_UPLOAD', NULL, ?, ?, '{}') ON CONFLICT(manifest_id) DO UPDATE SET updated_at=excluded.updated_at", (manifest_id, node_id, t, t))
    row = conn.execute("SELECT id FROM files WHERE node_id=? AND filename=? AND file_size_bytes=?", (node_id, source_path, size)).fetchone()
    if row:
        file_id = int(row["id"])
        conn.execute("UPDATE files SET updated_at=?, upload_status='UPLOADING' WHERE id=?", (t, file_id))
        legacy.catalog_recording(conn, file_id, rename_files=False)
        return file_id
    recorded_at = iso_utc(recording_epoch) if recording_epoch is not None else None
    cur = conn.execute(
        """
        INSERT INTO files (node_id, deployment_id, manifest_id, local_file_id, filename, recorded_at_raw,
            recorded_at_corrected, file_size_bytes, upload_status, bytes_received, weather_status, created_at, updated_at)
        VALUES (?, 'ESP32_UART_UPLOAD', ?, NULL, ?, ?, ?, ?, 'UPLOADING', 0, 'pending', ?, ?)
        """,
        (node_id, manifest_id, source_path, recorded_at, recorded_at, size, t, t),
    )
    file_id = int(cur.lastrowid)
    legacy.catalog_recording(conn, file_id, rename_files=False)
    return file_id


@app.post("/v1/device/{node_id}/upload/start")
async def contract_upload_start(node_id: str, request: Request) -> Dict[str, Any]:
    body = await request.body()
    ident = await require_contract_auth(request, body)
    if ident["node_id"] != node_id:
        raise HTTPException(status_code=403, detail="node_id mismatch")
    data = json.loads(body.decode("utf-8") or "{}")
    if data.get("node_id") != node_id:
        raise HTTPException(status_code=403, detail="body node_id mismatch")
    source = sanitize_moth_path(data.get("path"))
    size = int(data.get("size") or 0)
    chunk_bytes = int(data.get("chunk_bytes") or 0)
    if size <= 0 or chunk_bytes <= 0:
        raise HTTPException(status_code=400, detail="size and chunk_bytes must be positive")
    temp_path, _ = upload_paths(node_id, source)
    temp_path.write_bytes(b"")
    t = now_epoch()
    with db_connect() as conn:
        conn.execute("UPDATE esp32_upload_sessions SET status='failed', updated_epoch=?, error_message='reset by new upload start' WHERE node_id=? AND source_path=? AND status IN ('started', 'uploading')", (t, node_id, source.as_posix()))
        legacy_file_id = upsert_legacy_file(conn, node_id, source.as_posix(), size, parse_recording_epoch(source.as_posix()), t)
        legacy_upload_id = "ESP32_" + uuid.uuid4().hex
        conn.execute("INSERT INTO upload_sessions (upload_id, file_id, node_id, status, chunk_size, total_chunks, bytes_received, temp_path, started_at, updated_at) VALUES (?, ?, ?, 'OPEN', ?, ?, 0, ?, ?, ?)", (legacy_upload_id, legacy_file_id, node_id, chunk_bytes, math.ceil(size / chunk_bytes), str(temp_path), data.get("started_epoch") or t, t))
        cur = conn.execute("INSERT INTO esp32_upload_sessions (node_id, source_path, expected_size, chunk_bytes, received_bytes, status, temp_path, final_path, started_epoch, created_epoch, updated_epoch, legacy_upload_id, legacy_file_id) VALUES (?, ?, ?, ?, 0, 'started', ?, NULL, ?, ?, ?, ?, ?)", (node_id, source.as_posix(), size, chunk_bytes, str(temp_path), data.get("started_epoch") or t, t, t, legacy_upload_id, legacy_file_id))
        session_id = int(cur.lastrowid)
        conn.commit()
    return {"ok": True, "upload_id": session_id, "path": source.as_posix(), "size": size}


@app.post("/v1/device/{node_id}/upload/chunk")
async def contract_upload_chunk(node_id: str, request: Request, path: str, offset: int, length: int, total: int, crc32: str) -> Dict[str, Any]:
    body = await request.body()
    ident = await require_contract_auth(request, body, include_query=True)
    if ident["node_id"] != node_id or request.query_params.get("node_id") != node_id:
        raise HTTPException(status_code=403, detail="node_id mismatch")
    source = sanitize_moth_path(path)
    if length <= 0 or len(body) != length:
        raise HTTPException(status_code=400, detail="body length mismatch")
    if offset < 0 or total <= 0 or offset + length > total:
        raise HTTPException(status_code=400, detail="invalid offset/length/total")
    expected_crc = f"{zlib.crc32(body) & 0xFFFFFFFF:08X}"
    if expected_crc.upper() != str(crc32).upper():
        raise HTTPException(status_code=400, detail="crc32 mismatch")
    t = now_epoch()
    with db_connect() as conn:
        session = conn.execute("SELECT * FROM esp32_upload_sessions WHERE node_id=? AND source_path=? AND status IN ('started', 'uploading') ORDER BY id DESC LIMIT 1", (node_id, source.as_posix())).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="upload session not found")
        if int(session["expected_size"]) != total:
            raise HTTPException(status_code=400, detail="total does not match upload session")
        overlap = conn.execute("SELECT offset_bytes, length_bytes, crc32 FROM esp32_upload_chunks WHERE session_id=? AND NOT (? + ? <= offset_bytes OR ? >= offset_bytes + length_bytes)", (session["id"], offset, length, offset)).fetchall()
        if overlap:
            if len(overlap) == 1 and int(overlap[0]["offset_bytes"]) == offset and int(overlap[0]["length_bytes"]) == length and str(overlap[0]["crc32"]).upper() == expected_crc.upper():
                return {"ok": True, "path": source.as_posix(), "offset": offset, "length": length, "received_bytes": int(session["received_bytes"] or 0)}
            raise HTTPException(status_code=409, detail="overlapping chunk rejected")
        temp_path = Path(str(session["temp_path"]))
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        with temp_path.open("r+b") as out:
            out.seek(offset)
            out.write(body)
            out.flush()
            os.fsync(out.fileno())
        conn.execute("INSERT INTO esp32_upload_chunks (session_id, offset_bytes, length_bytes, crc32, received_epoch) VALUES (?, ?, ?, ?, ?)", (session["id"], offset, length, expected_crc, t))
        received = int(conn.execute("SELECT COALESCE(SUM(length_bytes), 0) AS n FROM esp32_upload_chunks WHERE session_id=?", (session["id"],)).fetchone()["n"])
        conn.execute("UPDATE esp32_upload_sessions SET received_bytes=?, status='uploading', updated_epoch=?, error_message=NULL WHERE id=?", (received, t, session["id"]))
        if session["legacy_upload_id"]:
            conn.execute("UPDATE upload_sessions SET status='PARTIAL', bytes_received=?, updated_at=? WHERE upload_id=?", (received, t, session["legacy_upload_id"]))
        if session["legacy_file_id"]:
            conn.execute("UPDATE files SET bytes_received=?, updated_at=? WHERE id=?", (received, t, session["legacy_file_id"]))
        conn.commit()
    return {"ok": True, "path": source.as_posix(), "offset": offset, "length": length, "received_bytes": received}


@app.post("/v1/device/{node_id}/upload/finish")
async def contract_upload_finish(node_id: str, request: Request, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    body = await request.body()
    ident = await require_contract_auth(request, body)
    if ident["node_id"] != node_id:
        raise HTTPException(status_code=403, detail="node_id mismatch")
    data = json.loads(body.decode("utf-8") or "{}")
    if data.get("node_id") != node_id:
        raise HTTPException(status_code=403, detail="body node_id mismatch")
    source = sanitize_moth_path(data.get("path"))
    size = int(data.get("size") or 0)
    if size <= 0:
        raise HTTPException(status_code=400, detail="size must be positive")
    t = now_epoch()
    legacy_file_id: Optional[int] = None
    wav_status = "ERROR"
    wav_meta: Dict[str, Any] = {}
    with db_connect() as conn:
        session = conn.execute("SELECT * FROM esp32_upload_sessions WHERE node_id=? AND source_path=? AND status IN ('started', 'uploading') ORDER BY id DESC LIMIT 1", (node_id, source.as_posix())).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="upload session not found")
        if int(session["expected_size"]) != size:
            raise HTTPException(status_code=400, detail="size does not match upload session")
        received = int(conn.execute("SELECT COALESCE(SUM(length_bytes), 0) AS n FROM esp32_upload_chunks WHERE session_id=?", (session["id"],)).fetchone()["n"])
        if received != size:
            raise HTTPException(status_code=409, detail="upload incomplete")
        temp_path = Path(str(session["temp_path"]))
        if not temp_path.exists() or temp_path.stat().st_size != size:
            raise HTTPException(status_code=409, detail="temporary file size mismatch")
        legacy_file_id = int(session["legacy_file_id"]) if session["legacy_file_id"] else None
        if not legacy_file_id:
            raise HTTPException(status_code=409, detail="upload is missing its recording catalog entry")
        legacy.catalog_recording(conn, legacy_file_id, rename_files=False)
        file_row = conn.execute(
            "SELECT canonical_name FROM files WHERE id=?",
            (legacy_file_id,),
        ).fetchone()
        if not file_row or not file_row["canonical_name"]:
            raise HTTPException(status_code=409, detail="recording catalog name is unavailable")
        node_wav_dir = legacy.safe_join(legacy.WAV_DIR, node_id)
        node_wav_dir.mkdir(parents=True, exist_ok=True)
        final_path = legacy.safe_join(node_wav_dir, str(file_row["canonical_name"]))
        if final_path.exists():
            final_path.unlink()
        os.replace(temp_path, final_path)
        server_sha = file_sha256(final_path)
        recording_epoch = parse_recording_epoch(source.as_posix())

        try:
            wav_meta = legacy.parse_wav(final_path)
            wav_status = "OK"
        except Exception as exc:
            wav_status = f"ERROR: {exc}"

        conn.execute("UPDATE esp32_upload_sessions SET received_bytes=?, status='complete', final_path=?, finished_epoch=?, updated_epoch=?, error_message=NULL WHERE id=?", (size, str(final_path), data.get("finished_epoch") or t, t, session["id"]))
        if session["legacy_upload_id"]:
            session_status = "COMPLETE" if wav_status == "OK" else "COMPLETE_BUT_INVALID"
            conn.execute("UPDATE upload_sessions SET status=?, bytes_received=?, completed_at=?, updated_at=? WHERE upload_id=?", (session_status, size, t, t, session["legacy_upload_id"]))
        conn.execute(
            """
            UPDATE files
            SET upload_status=?, bytes_received=?, server_sha256=?, wav_parse_status=?,
                sample_rate=?, channels=?, bit_depth=?, duration_seconds=?,
                flac_status=?, original_wav_path=?, flac_path=NULL, updated_at=?
            WHERE id=?
            """,
            (
                "SERVER_COPY_VERIFIED" if wav_status == "OK" else "SERVER_COPY_FAILED_PARSE",
                size,
                server_sha,
                wav_status,
                wav_meta.get("sample_rate"),
                wav_meta.get("channels"),
                wav_meta.get("bit_depth"),
                wav_meta.get("duration_seconds"),
                "PENDING" if wav_status == "OK" else "NOT_RUN",
                str(final_path),
                t,
                legacy_file_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO recordings (node_id, source_path, stored_path, size, uploaded_epoch, recording_epoch, sha256, weather_status, weather_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL)
            ON CONFLICT(node_id, source_path) DO UPDATE SET
                stored_path=excluded.stored_path, size=excluded.size, uploaded_epoch=excluded.uploaded_epoch,
                recording_epoch=excluded.recording_epoch, sha256=excluded.sha256,
                weather_status=COALESCE(recordings.weather_status, 'pending')
            """,
            (node_id, source.as_posix(), str(final_path), size, t, recording_epoch, server_sha),
        )
        conn.execute("UPDATE node_state SET upload_status=?, updated_at=? WHERE node_id=?", (f"upload complete: {source.as_posix()}", t, node_id))
        conn.commit()
    if wav_status == "OK" and legacy_file_id is not None:
        background_tasks.add_task(legacy.reconcile_flac_files, 1, [legacy_file_id], False)
    return {
        "ok": wav_status == "OK",
        "path": source.as_posix(),
        "size": size,
        "stored_path": str(final_path),
        "wav_parse_status": wav_status,
        "flac_status": "PENDING" if wav_status == "OK" else "NOT_RUN",
    }


@app.post("/v1/admin/{node_id}/commands/{command_type}")
def contract_admin_queue_command(node_id: str, command_type: str) -> Dict[str, Any]:
    safe_node_id(node_id)
    command_type = command_type.upper()
    if command_type not in ALLOWED_COMMAND_TYPES:
        raise HTTPException(status_code=400, detail="unsupported command type")
    t = now_epoch()
    with db_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO nodes (node_id, node_name, created_at, updated_at) VALUES (?, ?, ?, ?)", (node_id, node_id, t, t))
        cur = conn.execute("INSERT INTO commands (node_id, command_type, payload_json, status, created_at, expires_at) VALUES (?, ?, '{}', 'PENDING', ?, ?)", (node_id, command_type, t, t + 24 * 3600))
        conn.commit()
    return {"ok": True, "command_id": int(cur.lastrowid), "type": command_type}
