from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
import wave
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

# ============================================================
# Configuration
# ============================================================

APP_TITLE = "Bat Node Secure Ingest"
DB_PATH = Path(os.getenv("BAT_DB_PATH", "bat_nodes_v2.db"))
DATA_DIR = Path(os.getenv("BAT_DATA_DIR", "data"))
INCOMING_DIR = DATA_DIR / "incoming"
WAV_DIR = DATA_DIR / "original_wav"
FLAC_DIR = DATA_DIR / "flac"

AUTH_WINDOW_SECONDS = int(os.getenv("AUTH_WINDOW_SECONDS", "300"))
DEFAULT_CHUNK_SIZE = int(os.getenv("UPLOAD_CHUNK_SIZE", str(256 * 1024)))
MAX_CHUNK_SIZE = int(os.getenv("MAX_CHUNK_SIZE", str(1024 * 1024)))
REQUIRE_FLAC_BEFORE_DELETE = os.getenv("REQUIRE_FLAC_BEFORE_DELETE", "0") == "1"
REQUIRE_BACKUP_BEFORE_DELETE = os.getenv("REQUIRE_BACKUP_BEFORE_DELETE", "0") == "1"

ADMIN_USER = os.getenv("DASHBOARD_USER", "admin")
ADMIN_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "change-me-now")

security = HTTPBasic()
app = FastAPI(title=APP_TITLE)


# ============================================================
# Utilities
# ============================================================

def now_epoch() -> int:
    return int(time.time())


def ensure_dirs() -> None:
    for p in (DATA_DIR, INCOMING_DIR, WAV_DIR, FLAC_DIR):
        p.mkdir(parents=True, exist_ok=True)


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def execute_script(sql: str) -> None:
    with db_connect() as conn:
        conn.executescript(sql)
        conn.commit()


def qmarks(items: Iterable[Any]) -> str:
    return ",".join("?" for _ in items)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hmac_hex(secret: str, message: str) -> str:
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def safe_join(root: Path, *parts: str) -> Path:
    # Prevent path traversal. File names from nodes are not trusted.
    candidate = root.joinpath(*parts).resolve()
    root_resolved = root.resolve()
    if not str(candidate).startswith(str(root_resolved)):
        raise HTTPException(status_code=400, detail="Unsafe path")
    return candidate


def sanitize_filename(filename: str) -> str:
    # Keep directory structure out of node-provided names.
    name = Path(filename).name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Empty filename")
    bad = set('/\\:\0')
    if any(ch in bad for ch in name):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return name


# ============================================================
# Database schema
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    node_name TEXT NOT NULL,
    location_lat REAL,
    location_lon REAL,
    location_label TEXT,
    deployment_notes TEXT,
    firmware_version TEXT,
    hardware_version TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    compromised INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS node_credentials (
    id INTEGER PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(node_id),
    key_id TEXT NOT NULL,
    secret TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    revoked_at INTEGER,
    UNIQUE(node_id, key_id)
);

CREATE TABLE IF NOT EXISTS auth_nonces (
    node_id TEXT NOT NULL,
    nonce TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(node_id, nonce)
);

CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(node_id),
    created_at INTEGER NOT NULL,
    battery_v REAL,
    battery_percent REAL,
    solar_v REAL,
    charging INTEGER,
    charge_done INTEGER,
    recently_charged INTEGER,
    load_ma REAL,
    temperature_c REAL,
    humidity_percent REAL,
    sd_free_mb REAL,
    recording_status TEXT,
    upload_status TEXT,
    wifi_rssi_dbm REAL,
    mode TEXT,
    message TEXT
);

CREATE TABLE IF NOT EXISTS node_state (
    node_id TEXT PRIMARY KEY REFERENCES nodes(node_id),
    last_seen INTEGER,
    battery_v REAL,
    battery_percent REAL,
    solar_v REAL,
    charging INTEGER,
    charge_done INTEGER,
    recently_charged INTEGER,
    sd_free_mb REAL,
    recording_status TEXT,
    upload_status TEXT,
    wifi_rssi_dbm REAL,
    mode TEXT,
    message TEXT,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(node_id),
    command_type TEXT NOT NULL,
    payload_json TEXT,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    delivered_at INTEGER,
    acked_at INTEGER,
    expires_at INTEGER,
    response_json TEXT
);

CREATE TABLE IF NOT EXISTS manifests (
    manifest_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(node_id),
    deployment_id TEXT,
    sd_card_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(node_id),
    deployment_id TEXT,
    manifest_id TEXT NOT NULL REFERENCES manifests(manifest_id),
    local_file_id INTEGER,
    filename TEXT NOT NULL,
    recorded_at_raw TEXT,
    recorded_at_corrected TEXT,
    duration_seconds REAL,
    sample_rate INTEGER,
    channels INTEGER,
    bit_depth INTEGER,
    file_size_bytes INTEGER NOT NULL,
    upload_status TEXT NOT NULL DEFAULT 'ON_SD_ONLY',
    bytes_received INTEGER NOT NULL DEFAULT 0,
    server_sha256 TEXT,
    wav_parse_status TEXT,
    flac_status TEXT,
    backup_status TEXT,
    weather_status TEXT,
    original_wav_path TEXT,
    flac_path TEXT,
    delete_status TEXT NOT NULL DEFAULT 'NOT_AUTHORIZED',
    delete_authorization_id TEXT,
    delete_authorized_at INTEGER,
    delete_requested_at INTEGER,
    delete_confirmed_at INTEGER,
    delete_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(node_id, manifest_id, local_file_id),
    UNIQUE(node_id, filename, recorded_at_raw, file_size_bytes)
);

CREATE TABLE IF NOT EXISTS upload_sessions (
    upload_id TEXT PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id),
    node_id TEXT NOT NULL REFERENCES nodes(node_id),
    status TEXT NOT NULL,
    chunk_size INTEGER NOT NULL,
    total_chunks INTEGER NOT NULL,
    bytes_received INTEGER NOT NULL DEFAULT 0,
    temp_path TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    completed_at INTEGER
);

CREATE TABLE IF NOT EXISTS upload_chunks (
    upload_id TEXT NOT NULL REFERENCES upload_sessions(upload_id),
    chunk_index INTEGER NOT NULL,
    offset_bytes INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    received_at INTEGER NOT NULL,
    PRIMARY KEY(upload_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS delete_authorizations (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(node_id),
    manifest_id TEXT NOT NULL REFERENCES manifests(manifest_id),
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    issued_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    signed_payload TEXT NOT NULL,
    signature TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sd_deletion_log (
    id INTEGER PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(node_id),
    authorization_id TEXT,
    file_id INTEGER,
    filename TEXT,
    requested_at INTEGER,
    confirmed_at INTEGER,
    result TEXT NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS time_checks (
    id INTEGER PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(node_id),
    created_at INTEGER NOT NULL,
    server_epoch INTEGER NOT NULL,
    esp_epoch_before INTEGER,
    esp_epoch_after INTEGER,
    esp_offset_before_seconds REAL,
    esp_offset_after_seconds REAL,
    audiomoth_epoch INTEGER,
    audiomoth_offset_seconds REAL,
    rtt_ms INTEGER,
    time_source TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS node_errors (
    id INTEGER PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(node_id),
    created_at INTEGER NOT NULL,
    severity TEXT NOT NULL,
    subsystem TEXT NOT NULL,
    error_code TEXT NOT NULL,
    message TEXT,
    context_json TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    ip TEXT,
    details_json TEXT
);
"""


def init_db() -> None:
    ensure_dirs()
    execute_script(SCHEMA)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


# ============================================================
# Authentication
# ============================================================

class DeviceIdentity(Dict[str, Any]):
    pass


def get_node_secret(node_id: str, key_id: str) -> Optional[str]:
    now = now_epoch()
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT c.secret, n.active, n.compromised
            FROM node_credentials c
            JOIN nodes n ON n.node_id = c.node_id
            WHERE c.node_id = ?
              AND c.key_id = ?
              AND c.revoked_at IS NULL
              AND (c.expires_at IS NULL OR c.expires_at > ?)
            """,
            (node_id, key_id, now),
        ).fetchone()
    if not row:
        return None
    if not row["active"] or row["compromised"]:
        return None
    return str(row["secret"])


def record_nonce(node_id: str, nonce: str, created_at: int) -> None:
    cutoff = now_epoch() - AUTH_WINDOW_SECONDS * 4
    with db_connect() as conn:
        conn.execute("DELETE FROM auth_nonces WHERE created_at < ?", (cutoff,))
        try:
            conn.execute(
                "INSERT INTO auth_nonces (node_id, nonce, created_at) VALUES (?, ?, ?)",
                (node_id, nonce, created_at),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=401, detail="Replay nonce rejected")


async def require_device_auth(request: Request, body: Optional[bytes] = None) -> DeviceIdentity:
    if body is None:
        body = await request.body()

    node_id = request.headers.get("X-Node-ID", "")
    key_id = request.headers.get("X-Key-ID", "")
    ts_raw = request.headers.get("X-Timestamp", "")
    nonce = request.headers.get("X-Nonce", "")
    body_hash = request.headers.get("X-Body-SHA256", "")
    signature = request.headers.get("X-Signature", "")

    if not all([node_id, key_id, ts_raw, nonce, body_hash, signature]):
        raise HTTPException(status_code=401, detail="Missing device authentication headers")

    try:
        ts = int(ts_raw)
    except ValueError:
        raise HTTPException(status_code=401, detail="Bad timestamp")

    server_now = now_epoch()
    if abs(server_now - ts) > AUTH_WINDOW_SECONDS:
        raise HTTPException(status_code=401, detail="Timestamp outside allowed window")

    actual_body_hash = sha256_hex(body)
    if not constant_time_eq(actual_body_hash, body_hash):
        raise HTTPException(status_code=401, detail="Body hash mismatch")

    secret = get_node_secret(node_id, key_id)
    if secret is None:
        raise HTTPException(status_code=401, detail="Unknown, revoked, or inactive device key")

    canonical = "\n".join([
        request.method.upper(),
        request.url.path,
        ts_raw,
        nonce,
        body_hash,
    ])
    expected = hmac_hex(secret, canonical)
    if not constant_time_eq(expected, signature):
        raise HTTPException(status_code=401, detail="Bad signature")

    record_nonce(node_id, nonce, ts)
    return DeviceIdentity(node_id=node_id, key_id=key_id, secret=secret)


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    user_ok = hmac.compare_digest(credentials.username, ADMIN_USER)
    pass_ok = hmac.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Bad dashboard credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def audit(actor: str, action: str, target_type: str = "", target_id: str = "", ip: str = "", details: Any = None) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO audit_log (created_at, actor, action, target_type, target_id, ip, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (now_epoch(), actor, action, target_type, target_id, ip, json.dumps(details) if details is not None else None),
        )
        conn.commit()


# ============================================================
# Public utility endpoints
# ============================================================

@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "server_time": now_epoch()}


@app.get("/v1/public/server_time")
def public_server_time() -> Dict[str, Any]:
    # Public by design: lets low-power nodes obtain a timestamp before HMAC requests.
    return {"ok": True, "epoch_utc": now_epoch()}


# ============================================================
# Device endpoints
# ============================================================

@app.post("/v1/device/heartbeat")
async def heartbeat(request: Request) -> Dict[str, Any]:
    body = await request.body()
    ident = await require_device_auth(request, body)
    data = json.loads(body.decode("utf-8") or "{}")
    node_id = ident["node_id"]
    if data.get("node_id") not in (None, node_id):
        raise HTTPException(status_code=400, detail="node_id mismatch")

    t = now_epoch()
    fields = {
        "battery_v": data.get("battery_v"),
        "battery_percent": data.get("battery_percent"),
        "solar_v": data.get("solar_v"),
        "charging": int(bool(data.get("charging"))) if data.get("charging") is not None else None,
        "charge_done": int(bool(data.get("charge_done"))) if data.get("charge_done") is not None else None,
        "recently_charged": int(bool(data.get("recently_charged"))) if data.get("recently_charged") is not None else None,
        "load_ma": data.get("load_ma"),
        "temperature_c": data.get("temperature_c"),
        "humidity_percent": data.get("humidity_percent"),
        "sd_free_mb": data.get("sd_free_mb"),
        "recording_status": data.get("recording_status"),
        "upload_status": data.get("upload_status"),
        "wifi_rssi_dbm": data.get("wifi_rssi_dbm"),
        "mode": data.get("mode"),
        "message": data.get("message"),
    }

    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO telemetry (
                node_id, created_at, battery_v, battery_percent, solar_v, charging,
                charge_done, recently_charged, load_ma, temperature_c, humidity_percent,
                sd_free_mb, recording_status, upload_status, wifi_rssi_dbm, mode, message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (node_id, t, *fields.values()),
        )
        conn.execute(
            """
            INSERT INTO node_state (
                node_id, last_seen, battery_v, battery_percent, solar_v, charging,
                charge_done, recently_charged, sd_free_mb, recording_status, upload_status,
                wifi_rssi_dbm, mode, message, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                last_seen=excluded.last_seen,
                battery_v=excluded.battery_v,
                battery_percent=excluded.battery_percent,
                solar_v=excluded.solar_v,
                charging=excluded.charging,
                charge_done=excluded.charge_done,
                recently_charged=excluded.recently_charged,
                sd_free_mb=excluded.sd_free_mb,
                recording_status=excluded.recording_status,
                upload_status=excluded.upload_status,
                wifi_rssi_dbm=excluded.wifi_rssi_dbm,
                mode=excluded.mode,
                message=excluded.message,
                updated_at=excluded.updated_at
            """,
            (
                node_id,
                t,
                fields["battery_v"],
                fields["battery_percent"],
                fields["solar_v"],
                fields["charging"],
                fields["charge_done"],
                fields["recently_charged"],
                fields["sd_free_mb"],
                fields["recording_status"],
                fields["upload_status"],
                fields["wifi_rssi_dbm"],
                fields["mode"],
                fields["message"],
                t,
            ),
        )
        conn.commit()
    return {"ok": True, "server_time": t}


@app.get("/v1/device/{node_id}/commands")
async def get_commands(node_id: str, request: Request) -> Dict[str, Any]:
    body = await request.body()
    ident = await require_device_auth(request, body)
    if ident["node_id"] != node_id:
        raise HTTPException(status_code=403, detail="node_id mismatch")

    t = now_epoch()
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, command_type, payload_json
            FROM commands
            WHERE node_id = ?
              AND status = 'PENDING'
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY created_at ASC
            LIMIT 5
            """,
            (node_id, t),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            conn.execute(
                f"UPDATE commands SET status='DELIVERED', delivered_at=? WHERE id IN ({qmarks(ids)})",
                (t, *ids),
            )
        conn.commit()

    commands = []
    for row in rows:
        commands.append({
            "id": row["id"],
            "type": row["command_type"],
            "payload": json.loads(row["payload_json"] or "{}"),
        })
    return {"ok": True, "server_time": t, "commands": commands}


@app.post("/v1/device/{node_id}/commands/{command_id}/ack")
async def ack_command(node_id: str, command_id: int, request: Request) -> Dict[str, Any]:
    body = await request.body()
    ident = await require_device_auth(request, body)
    if ident["node_id"] != node_id:
        raise HTTPException(status_code=403, detail="node_id mismatch")
    data = json.loads(body.decode("utf-8") or "{}")
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE commands
            SET status='ACKED', acked_at=?, response_json=?
            WHERE id=? AND node_id=?
            """,
            (now_epoch(), json.dumps(data.get("response", {})), command_id, node_id),
        )
        conn.commit()
    return {"ok": True}


@app.post("/v1/device/time_check")
async def time_check(request: Request) -> Dict[str, Any]:
    body = await request.body()
    ident = await require_device_auth(request, body)
    data = json.loads(body.decode("utf-8") or "{}")
    node_id = ident["node_id"]
    server_epoch = int(data.get("server_epoch", now_epoch()))
    esp_before = data.get("esp_epoch_before")
    esp_after = data.get("esp_epoch_after")
    moth_epoch = data.get("audiomoth_epoch")
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO time_checks (
                node_id, created_at, server_epoch, esp_epoch_before, esp_epoch_after,
                esp_offset_before_seconds, esp_offset_after_seconds,
                audiomoth_epoch, audiomoth_offset_seconds, rtt_ms, time_source, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node_id,
                now_epoch(),
                server_epoch,
                esp_before,
                esp_after,
                (esp_before - server_epoch) if esp_before is not None else None,
                (esp_after - server_epoch) if esp_after is not None else None,
                moth_epoch,
                (moth_epoch - server_epoch) if moth_epoch is not None else None,
                data.get("rtt_ms"),
                data.get("time_source"),
                data.get("notes"),
            ),
        )
        conn.commit()
    return {"ok": True, "server_time": now_epoch()}


# ============================================================
# Manifest and upload endpoints
# ============================================================

@app.post("/v1/files/manifest")
async def post_manifest(request: Request) -> Dict[str, Any]:
    body = await request.body()
    ident = await require_device_auth(request, body)
    data = json.loads(body.decode("utf-8") or "{}")
    node_id = ident["node_id"]
    if data.get("node_id") not in (None, node_id):
        raise HTTPException(status_code=400, detail="node_id mismatch")

    manifest_id = str(data.get("manifest_id") or "").strip()
    if not manifest_id:
        raise HTTPException(status_code=400, detail="manifest_id required")
    deployment_id = data.get("deployment_id")
    sd_card_id = data.get("sd_card_id")
    files = data.get("files") or []
    if not isinstance(files, list):
        raise HTTPException(status_code=400, detail="files must be a list")

    t = now_epoch()
    wanted = []
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO manifests (manifest_id, node_id, deployment_id, sd_card_id, created_at, updated_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(manifest_id) DO UPDATE SET
                updated_at=excluded.updated_at,
                raw_json=excluded.raw_json
            """,
            (manifest_id, node_id, deployment_id, sd_card_id, t, t, json.dumps(data)),
        )
        for item in files:
            filename = sanitize_filename(str(item.get("filename", "")))
            file_size = int(item.get("file_size_bytes") or 0)
            if file_size <= 0:
                raise HTTPException(status_code=400, detail=f"Bad file_size_bytes for {filename}")
            local_file_id = item.get("local_file_id")
            recorded_at = item.get("recorded_at")

            try:
                conn.execute(
                    """
                    INSERT INTO files (
                        node_id, deployment_id, manifest_id, local_file_id, filename,
                        recorded_at_raw, recorded_at_corrected, duration_seconds,
                        sample_rate, channels, bit_depth, file_size_bytes,
                        upload_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ON_SD_ONLY', ?, ?)
                    """,
                    (
                        node_id,
                        deployment_id,
                        manifest_id,
                        local_file_id,
                        filename,
                        recorded_at,
                        item.get("recorded_at_corrected"),
                        item.get("duration_seconds"),
                        item.get("sample_rate"),
                        item.get("channels"),
                        item.get("bit_depth"),
                        file_size,
                        t,
                        t,
                    ),
                )
            except sqlite3.IntegrityError:
                # Existing manifest/file row. Do not reset completed upload status.
                conn.execute(
                    """
                    UPDATE files
                    SET updated_at=?, duration_seconds=COALESCE(?, duration_seconds),
                        sample_rate=COALESCE(?, sample_rate), channels=COALESCE(?, channels),
                        bit_depth=COALESCE(?, bit_depth)
                    WHERE node_id=? AND filename=? AND recorded_at_raw IS ? AND file_size_bytes=?
                    """,
                    (
                        t,
                        item.get("duration_seconds"),
                        item.get("sample_rate"),
                        item.get("channels"),
                        item.get("bit_depth"),
                        node_id,
                        filename,
                        recorded_at,
                        file_size,
                    ),
                )

        wanted_rows = conn.execute(
            """
            SELECT id, local_file_id, filename, file_size_bytes, upload_status, bytes_received
            FROM files
            WHERE node_id=? AND manifest_id=?
              AND upload_status NOT IN ('SERVER_COPY_VERIFIED', 'SAFE_TO_DELETE', 'DELETED_FROM_SD')
            ORDER BY recorded_at_raw, filename
            """,
            (node_id, manifest_id),
        ).fetchall()
        conn.commit()

    for r in wanted_rows:
        wanted.append(dict(r))
    return {"ok": True, "manifest_id": manifest_id, "wanted_files": wanted}


@app.post("/v1/uploads/init")
async def upload_init(request: Request) -> Dict[str, Any]:
    body = await request.body()
    ident = await require_device_auth(request, body)
    data = json.loads(body.decode("utf-8") or "{}")
    node_id = ident["node_id"]
    manifest_id = data.get("manifest_id")
    filename = sanitize_filename(str(data.get("filename", "")))
    file_size = int(data.get("file_size_bytes") or 0)
    local_file_id = data.get("local_file_id")
    requested_chunk_size = int(data.get("chunk_size") or DEFAULT_CHUNK_SIZE)
    if file_size <= 0:
        raise HTTPException(status_code=400, detail="file_size_bytes required")
    if requested_chunk_size <= 0 or requested_chunk_size > MAX_CHUNK_SIZE:
        raise HTTPException(status_code=400, detail="Bad chunk_size")

    t = now_epoch()
    with db_connect() as conn:
        if local_file_id is not None:
            f = conn.execute(
                """
                SELECT * FROM files
                WHERE node_id=? AND manifest_id=? AND local_file_id=?
                """,
                (node_id, manifest_id, local_file_id),
            ).fetchone()
        else:
            f = conn.execute(
                """
                SELECT * FROM files
                WHERE node_id=? AND manifest_id=? AND filename=? AND file_size_bytes=?
                """,
                (node_id, manifest_id, filename, file_size),
            ).fetchone()
        if not f:
            raise HTTPException(status_code=404, detail="File is not in manifest")
        if int(f["file_size_bytes"]) != file_size:
            raise HTTPException(status_code=400, detail="File size mismatch")
        if f["upload_status"] in ("SERVER_COPY_VERIFIED", "SAFE_TO_DELETE", "DELETED_FROM_SD"):
            return {
                "ok": True,
                "already_complete": True,
                "file_id": f["id"],
                "upload_status": f["upload_status"],
            }

        existing = conn.execute(
            """
            SELECT * FROM upload_sessions
            WHERE file_id=? AND status IN ('OPEN', 'PARTIAL')
            ORDER BY started_at DESC LIMIT 1
            """,
            (f["id"],),
        ).fetchone()
        if existing and int(existing["chunk_size"]) == requested_chunk_size:
            upload_id = existing["upload_id"]
            chunk_size = int(existing["chunk_size"])
            total_chunks = int(existing["total_chunks"])
        else:
            if existing:
                conn.execute(
                    "UPDATE upload_sessions SET status='SUPERSEDED', updated_at=? WHERE upload_id=?",
                    (t, existing["upload_id"]),
                )
            upload_id = "UPL_" + uuid.uuid4().hex
            chunk_size = min(requested_chunk_size, MAX_CHUNK_SIZE)
            total_chunks = math.ceil(file_size / chunk_size)
            node_dir = safe_join(INCOMING_DIR, node_id)
            node_dir.mkdir(parents=True, exist_ok=True)
            temp_path = safe_join(node_dir, upload_id + ".part")
            with temp_path.open("wb") as out:
                out.truncate(file_size)
            conn.execute(
                """
                INSERT INTO upload_sessions (
                    upload_id, file_id, node_id, status, chunk_size, total_chunks,
                    bytes_received, temp_path, started_at, updated_at
                ) VALUES (?, ?, ?, 'OPEN', ?, ?, 0, ?, ?, ?)
                """,
                (upload_id, f["id"], node_id, chunk_size, total_chunks, str(temp_path), t, t),
            )
            conn.execute(
                "UPDATE files SET upload_status='UPLOADING', updated_at=? WHERE id=?",
                (t, f["id"]),
            )
        chunks = conn.execute(
            "SELECT chunk_index FROM upload_chunks WHERE upload_id=? ORDER BY chunk_index",
            (upload_id,),
        ).fetchall()
        received_chunks = [int(c["chunk_index"]) for c in chunks]
        conn.commit()

    received_set = set(received_chunks)
    next_missing_chunk = total_chunks
    for chunk_index in range(total_chunks):
        if chunk_index not in received_set:
            next_missing_chunk = chunk_index
            break
    next_missing_offset = min(next_missing_chunk * chunk_size, file_size)

    return {
        "ok": True,
        "already_complete": False,
        "upload_id": upload_id,
        "file_id": f["id"],
        "chunk_size": chunk_size,
        "total_chunks": total_chunks,
        "next_missing_chunk": next_missing_chunk,
        "next_missing_offset": next_missing_offset,
        "received_chunk_count": len(received_chunks),
    }



@app.put("/v1/uploads/{upload_id}/chunks/{chunk_index}")
async def put_chunk(upload_id: str, chunk_index: int, request: Request) -> Dict[str, Any]:
    body = await request.body()
    ident = await require_device_auth(request, body)
    node_id = ident["node_id"]
    if chunk_index < 0:
        raise HTTPException(status_code=400, detail="Bad chunk index")
    if len(body) == 0 or len(body) > MAX_CHUNK_SIZE:
        raise HTTPException(status_code=400, detail="Bad chunk size")

    t = now_epoch()
    with db_connect() as conn:
        s = conn.execute(
            """
            SELECT s.*, f.file_size_bytes, f.id AS file_id
            FROM upload_sessions s
            JOIN files f ON f.id = s.file_id
            WHERE s.upload_id=? AND s.node_id=?
            """,
            (upload_id, node_id),
        ).fetchone()
        if not s:
            raise HTTPException(status_code=404, detail="Upload session not found")
        if s["status"] not in ("OPEN", "PARTIAL"):
            raise HTTPException(status_code=409, detail=f"Upload is {s['status']}")
        total_chunks = int(s["total_chunks"])
        chunk_size = int(s["chunk_size"])
        file_size = int(s["file_size_bytes"])
        if chunk_index >= total_chunks:
            raise HTTPException(status_code=400, detail="Chunk index outside file")
        expected_offset = chunk_index * chunk_size
        expected_size = min(chunk_size, file_size - expected_offset)
        if len(body) != expected_size:
            raise HTTPException(status_code=400, detail=f"Expected {expected_size} bytes for chunk")

        duplicate = conn.execute(
            "SELECT size_bytes FROM upload_chunks WHERE upload_id=? AND chunk_index=?",
            (upload_id, chunk_index),
        ).fetchone()
        if duplicate:
            # Idempotent retry. We do not overwrite existing bytes.
            return {"ok": True, "duplicate": True, "chunk_index": chunk_index}

        path = Path(str(s["temp_path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("r+b") as out:
            out.seek(expected_offset)
            out.write(body)
            out.flush()

        conn.execute(
            """
            INSERT INTO upload_chunks (upload_id, chunk_index, offset_bytes, size_bytes, received_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (upload_id, chunk_index, expected_offset, len(body), t),
        )
        total_received = conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) AS n FROM upload_chunks WHERE upload_id=?",
            (upload_id,),
        ).fetchone()["n"]
        conn.execute(
            "UPDATE upload_sessions SET status='PARTIAL', bytes_received=?, updated_at=? WHERE upload_id=?",
            (total_received, t, upload_id),
        )
        conn.execute(
            "UPDATE files SET bytes_received=?, updated_at=? WHERE id=?",
            (total_received, t, s["file_id"]),
        )
        conn.commit()

    return {"ok": True, "duplicate": False, "chunk_index": chunk_index, "bytes_received": total_received}


@app.get("/v1/uploads/{upload_id}/status")
async def upload_status(upload_id: str, request: Request) -> Dict[str, Any]:
    body = await request.body()
    ident = await require_device_auth(request, body)
    node_id = ident["node_id"]
    with db_connect() as conn:
        s = conn.execute(
            """
            SELECT s.*, f.file_size_bytes
            FROM upload_sessions s
            JOIN files f ON f.id = s.file_id
            WHERE s.upload_id=? AND s.node_id=?
            """,
            (upload_id, node_id),
        ).fetchone()
        if not s:
            raise HTTPException(status_code=404, detail="Upload session not found")
        chunks = conn.execute(
            "SELECT chunk_index FROM upload_chunks WHERE upload_id=? ORDER BY chunk_index",
            (upload_id,),
        ).fetchall()
    received = {int(c["chunk_index"]) for c in chunks}
    missing = [i for i in range(int(s["total_chunks"])) if i not in received]
    next_missing_chunk = missing[0] if missing else int(s["total_chunks"])
    next_missing_offset = min(next_missing_chunk * int(s["chunk_size"]), int(s["file_size_bytes"]))
    return {
        "ok": True,
        "upload_id": upload_id,
        "status": s["status"],
        "bytes_received": s["bytes_received"],
        "total_chunks": s["total_chunks"],
        "chunk_size": s["chunk_size"],
        "next_missing_chunk": next_missing_chunk,
        "next_missing_offset": next_missing_offset,
        "received_chunk_count": len(received),
    }


def parse_wav(path: Path) -> Dict[str, Any]:
    with wave.open(str(path), "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate()
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        duration = frames / rate if rate else None
        return {
            "sample_rate": rate,
            "channels": channels,
            "bit_depth": sampwidth * 8,
            "duration_seconds": duration,
            "frames": frames,
        }


def maybe_make_flac(wav_path: Path, node_id: str) -> Optional[Path]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    out_dir = safe_join(FLAC_DIR, node_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    flac_path = safe_join(out_dir, wav_path.with_suffix(".flac").name)
    result = subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav_path), str(flac_path)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffmpeg failed")
    return flac_path


@app.post("/v1/uploads/{upload_id}/complete")
async def complete_upload(upload_id: str, request: Request) -> Dict[str, Any]:
    body = await request.body()
    ident = await require_device_auth(request, body)
    node_id = ident["node_id"]
    t = now_epoch()

    with db_connect() as conn:
        s = conn.execute(
            """
            SELECT s.*, f.filename, f.file_size_bytes, f.id AS file_id
            FROM upload_sessions s
            JOIN files f ON f.id = s.file_id
            WHERE s.upload_id=? AND s.node_id=?
            """,
            (upload_id, node_id),
        ).fetchone()
        if not s:
            raise HTTPException(status_code=404, detail="Upload session not found")
        chunks = conn.execute("SELECT COUNT(*) AS c, COALESCE(SUM(size_bytes), 0) AS n FROM upload_chunks WHERE upload_id=?", (upload_id,)).fetchone()
        if int(chunks["c"]) != int(s["total_chunks"]):
            raise HTTPException(status_code=409, detail="Missing chunks")
        if int(chunks["n"]) != int(s["file_size_bytes"]):
            raise HTTPException(status_code=409, detail="Byte count mismatch")

    temp_path = Path(str(s["temp_path"]))
    if not temp_path.exists() or temp_path.stat().st_size != int(s["file_size_bytes"]):
        raise HTTPException(status_code=409, detail="Temporary file size mismatch")

    node_wav_dir = safe_join(WAV_DIR, node_id)
    node_wav_dir.mkdir(parents=True, exist_ok=True)
    final_name = sanitize_filename(str(s["filename"]))
    final_path = safe_join(node_wav_dir, final_name)
    if final_path.exists():
        # Avoid clobbering an earlier file with the same name.
        final_path = safe_join(node_wav_dir, f"{Path(final_name).stem}_{upload_id}{Path(final_name).suffix}")
    temp_path.replace(final_path)

    wav_status = "ERROR"
    wav_meta: Dict[str, Any] = {}
    flac_status = "NOT_RUN"
    flac_path: Optional[Path] = None
    flac_error: Optional[str] = None

    try:
        wav_meta = parse_wav(final_path)
        wav_status = "OK"
    except Exception as e:
        wav_status = f"ERROR: {e}"

    server_hash = file_sha256(final_path)

    if wav_status == "OK":
        try:
            flac_path = maybe_make_flac(final_path, node_id)
            flac_status = "OK" if flac_path else "SKIPPED_NO_FFMPEG"
        except Exception as e:
            flac_status = "ERROR"
            flac_error = str(e)

    with db_connect() as conn:
        upload_status_value = "SERVER_COPY_VERIFIED" if wav_status == "OK" else "SERVER_COPY_FAILED_PARSE"
        conn.execute(
            """
            UPDATE upload_sessions
            SET status=?, bytes_received=?, updated_at=?, completed_at=?
            WHERE upload_id=?
            """,
            ("COMPLETE" if wav_status == "OK" else "COMPLETE_BUT_INVALID", int(s["file_size_bytes"]), t, t, upload_id),
        )
        conn.execute(
            """
            UPDATE files
            SET upload_status=?, bytes_received=?, server_sha256=?, wav_parse_status=?,
                flac_status=?, original_wav_path=?, flac_path=?, updated_at=?
            WHERE id=?
            """,
            (
                upload_status_value,
                int(s["file_size_bytes"]),
                server_hash,
                wav_status,
                flac_status if not flac_error else f"{flac_status}: {flac_error}",
                str(final_path),
                str(flac_path) if flac_path else None,
                t,
                s["file_id"],
            ),
        )
        conn.commit()

    return {
        "ok": wav_status == "OK",
        "upload_id": upload_id,
        "file_id": s["file_id"],
        "server_sha256": server_hash,
        "wav_parse_status": wav_status,
        "wav_metadata": wav_meta,
        "flac_status": flac_status,
        "flac_error": flac_error,
    }


# ============================================================
# Deletion authorization
# ============================================================

def is_safe_to_delete(row: sqlite3.Row) -> bool:
    if row["upload_status"] != "SERVER_COPY_VERIFIED":
        return False
    if row["wav_parse_status"] != "OK":
        return False
    if REQUIRE_FLAC_BEFORE_DELETE and row["flac_status"] != "OK":
        return False
    if REQUIRE_BACKUP_BEFORE_DELETE and row["backup_status"] != "OK":
        return False
    if row["delete_status"] in ("DELETED_FROM_SD", "DELETE_REQUESTED"):
        return False
    return True


@app.get("/v1/nodes/{node_id}/delete_authorization")
async def delete_authorization(node_id: str, manifest_id: str, request: Request) -> Dict[str, Any]:
    body = await request.body()
    ident = await require_device_auth(request, body)
    if ident["node_id"] != node_id:
        raise HTTPException(status_code=403, detail="node_id mismatch")

    t = now_epoch()
    expires = t + 24 * 3600
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM files WHERE node_id=? AND manifest_id=? ORDER BY recorded_at_raw, filename",
            (node_id, manifest_id),
        ).fetchall()
        safe_files = []
        for r in rows:
            if is_safe_to_delete(r):
                safe_files.append({
                    "file_id": r["id"],
                    "local_file_id": r["local_file_id"],
                    "filename": r["filename"],
                    "recorded_at": r["recorded_at_raw"],
                    "file_size_bytes": r["file_size_bytes"],
                    "server_status": "SAFE_TO_DELETE",
                    "server_sha256": r["server_sha256"],
                })
        auth_id = "DEL_" + uuid.uuid4().hex
        payload = {
            "ok": True,
            "authorization_id": auth_id,
            "node_id": node_id,
            "manifest_id": manifest_id,
            "delete_mode": "per_file",
            "issued_at": t,
            "expires_at": expires,
            "files": safe_files,
        }
        payload_json = canonical_json(payload)
        signature = hmac_hex(ident["secret"], "DELETE_AUTHORIZATION\n" + payload_json)
        conn.execute(
            """
            INSERT INTO delete_authorizations (
                id, node_id, manifest_id, mode, status, issued_at, expires_at, signed_payload, signature
            ) VALUES (?, ?, ?, 'per_file', 'ISSUED', ?, ?, ?, ?)
            """,
            (auth_id, node_id, manifest_id, t, expires, payload_json, signature),
        )
        file_ids = [f["file_id"] for f in safe_files]
        if file_ids:
            conn.execute(
                f"UPDATE files SET delete_status='DELETE_AUTHORIZED', delete_authorization_id=?, delete_authorized_at=?, updated_at=? WHERE id IN ({qmarks(file_ids)})",
                (auth_id, t, t, *file_ids),
            )
        conn.commit()
    return {**payload, "signature": signature}


@app.post("/v1/nodes/{node_id}/delete_confirm")
async def delete_confirm(node_id: str, request: Request) -> Dict[str, Any]:
    body = await request.body()
    ident = await require_device_auth(request, body)
    if ident["node_id"] != node_id:
        raise HTTPException(status_code=403, detail="node_id mismatch")
    data = json.loads(body.decode("utf-8") or "{}")
    authorization_id = data.get("authorization_id")
    files = data.get("files") or []
    if not authorization_id:
        raise HTTPException(status_code=400, detail="authorization_id required")

    t = now_epoch()
    updated = 0
    with db_connect() as conn:
        auth = conn.execute(
            "SELECT * FROM delete_authorizations WHERE id=? AND node_id=?",
            (authorization_id, node_id),
        ).fetchone()
        if not auth:
            raise HTTPException(status_code=404, detail="delete authorization not found")
        for item in files:
            file_id = item.get("file_id")
            filename = item.get("filename")
            result = item.get("result")
            error = item.get("error")
            if result == "DELETED":
                conn.execute(
                    """
                    UPDATE files
                    SET delete_status='DELETED_FROM_SD', delete_confirmed_at=?, updated_at=?
                    WHERE id=? AND node_id=? AND delete_authorization_id=?
                    """,
                    (t, t, file_id, node_id, authorization_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE files
                    SET delete_status='DELETE_FAILED', delete_error=?, updated_at=?
                    WHERE id=? AND node_id=? AND delete_authorization_id=?
                    """,
                    (error or result or "unknown", t, file_id, node_id, authorization_id),
                )
            conn.execute(
                """
                INSERT INTO sd_deletion_log (
                    node_id, authorization_id, file_id, filename, requested_at, confirmed_at, result, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (node_id, authorization_id, file_id, filename, auth["issued_at"], t, result or "UNKNOWN", error),
            )
            updated += 1
        conn.execute(
            "UPDATE delete_authorizations SET status='ACKED' WHERE id=?",
            (authorization_id,),
        )
        conn.commit()
    return {"ok": True, "updated": updated}


# ============================================================
# Admin/dashboard endpoints
# ============================================================

@app.post("/admin/commands/{node_id}/{command_type}")
def admin_queue_command(node_id: str, command_type: str, request: Request, admin: str = Depends(require_admin)) -> Dict[str, Any]:
    t = now_epoch()
    payload = {}
    with db_connect() as conn:
        node = conn.execute("SELECT node_id FROM nodes WHERE node_id=?", (node_id,)).fetchone()
        if not node:
            raise HTTPException(status_code=404, detail="Unknown node")
        cur = conn.execute(
            """
            INSERT INTO commands (node_id, command_type, payload_json, status, created_at, expires_at)
            VALUES (?, ?, ?, 'PENDING', ?, ?)
            """,
            (node_id, command_type.upper(), json.dumps(payload), t, t + 24 * 3600),
        )
        conn.commit()
        command_id = cur.lastrowid
    audit(admin, "queue_command", "node", node_id, details={"command_type": command_type, "command_id": command_id})
    return {"ok": True, "command_id": command_id}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(admin: str = Depends(require_admin)) -> str:
    t = now_epoch()
    with db_connect() as conn:
        nodes = conn.execute(
            """
            SELECT n.node_id, n.node_name, n.location_label, s.*
            FROM nodes n
            LEFT JOIN node_state s ON s.node_id = n.node_id
            ORDER BY n.node_id
            """
        ).fetchall()
        files = conn.execute(
            """
            SELECT node_id, filename, recorded_at_raw, file_size_bytes, upload_status,
                   wav_parse_status, flac_status, delete_status, bytes_received
            FROM files
            ORDER BY created_at DESC
            LIMIT 100
            """
        ).fetchall()
        commands = conn.execute(
            """
            SELECT id, node_id, command_type, status, created_at, delivered_at, acked_at
            FROM commands
            ORDER BY created_at DESC
            LIMIT 50
            """
        ).fetchall()

    def esc(x: Any) -> str:
        if x is None:
            return ""
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    node_rows = ""
    for n in nodes:
        age = "never" if n["last_seen"] is None else f"{t - int(n['last_seen'])} s ago"
        node_rows += f"""
        <tr>
          <td>{esc(n['node_id'])}</td><td>{esc(n['node_name'])}</td><td>{esc(n['location_label'])}</td>
          <td>{age}</td><td>{esc(n['battery_v'])}</td><td>{esc(n['battery_percent'])}</td>
          <td>{esc(n['charging'])}</td><td>{esc(n['recording_status'])}</td><td>{esc(n['upload_status'])}</td>
          <td>{esc(n['sd_free_mb'])}</td><td>{esc(n['wifi_rssi_dbm'])}</td>
          <td><form method="post" action="/admin/commands/{esc(n['node_id'])}/PING"><button>Queue ping</button></form></td>
        </tr>
        """

    file_rows = ""
    for f in files:
        mb = "" if f["file_size_bytes"] is None else f"{int(f['file_size_bytes'])/1_000_000:.2f}"
        file_rows += f"""
        <tr><td>{esc(f['node_id'])}</td><td>{esc(f['filename'])}</td><td>{esc(f['recorded_at_raw'])}</td>
        <td>{mb}</td><td>{esc(f['upload_status'])}</td><td>{esc(f['bytes_received'])}</td>
        <td>{esc(f['wav_parse_status'])}</td><td>{esc(f['flac_status'])}</td><td>{esc(f['delete_status'])}</td></tr>
        """

    cmd_rows = ""
    for c in commands:
        cmd_rows += f"""
        <tr><td>{esc(c['id'])}</td><td>{esc(c['node_id'])}</td><td>{esc(c['command_type'])}</td>
        <td>{esc(c['status'])}</td><td>{esc(c['created_at'])}</td><td>{esc(c['delivered_at'])}</td><td>{esc(c['acked_at'])}</td></tr>
        """

    return f"""
    <!doctype html>
    <html><head><title>Bat Node Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
    body {{ font-family: Arial, sans-serif; background:#111; color:#eee; padding:20px; }}
    table {{ width:100%; border-collapse:collapse; margin-bottom:28px; font-size:14px; }}
    th, td {{ border-bottom:1px solid #333; padding:6px; text-align:left; }}
    button {{ padding:6px 10px; }}
    .small {{ color:#aaa; }}
    </style></head><body>
    <h1>Bat Node Dashboard</h1>
    <p class="small">Server time: {t}</p>

    <h2>Nodes</h2>
    <table><tr><th>ID</th><th>Name</th><th>Location</th><th>Last seen</th><th>Battery V</th><th>Battery %</th><th>Charging</th><th>Recording</th><th>Upload</th><th>SD MB</th><th>RSSI</th><th>Action</th></tr>{node_rows}</table>

    <h2>Recent files</h2>
    <table><tr><th>Node</th><th>Filename</th><th>Recorded</th><th>MB</th><th>Upload</th><th>Bytes</th><th>WAV</th><th>FLAC</th><th>Delete</th></tr>{file_rows}</table>

    <h2>Recent commands</h2>
    <table><tr><th>ID</th><th>Node</th><th>Type</th><th>Status</th><th>Created</th><th>Delivered</th><th>Acked</th></tr>{cmd_rows}</table>
    </body></html>
    """


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    return '<meta http-equiv="refresh" content="0; url=/dashboard">'
