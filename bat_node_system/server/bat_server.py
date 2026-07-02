from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
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
FLAC_ENCODER = os.getenv("FLAC_ENCODER", "auto").strip().lower()
FLAC_ENCODER_PATH = os.getenv("FLAC_ENCODER_PATH", "").strip()
FLAC_COMPRESSION_LEVEL = os.getenv("FLAC_COMPRESSION_LEVEL", "5").strip()
FLAC_RECONCILE_INTERVAL_SECONDS = max(60, int(os.getenv("FLAC_RECONCILE_INTERVAL_SECONDS", "900")))
FLAC_RECONCILE_BATCH_SIZE = max(1, int(os.getenv("FLAC_RECONCILE_BATCH_SIZE", "5")))
FLAC_RECONCILE_START_DELAY_SECONDS = max(1, int(os.getenv("FLAC_RECONCILE_START_DELAY_SECONDS", "30")))
COMMAND_REDELIVER_AFTER_SECONDS = max(30, int(os.getenv("COMMAND_REDELIVER_AFTER_SECONDS", "120")))
PROVISIONING_TOKEN = os.getenv("PROVISIONING_TOKEN", "").strip()
ENROLLMENT_TTL_SECONDS = int(os.getenv("ENROLLMENT_TTL_SECONDS", "1800"))
ENROLLMENT_POLL_SECONDS = max(2, int(os.getenv("ENROLLMENT_POLL_SECONDS", "3")))
ALLOWED_COMMAND_TYPES = {
    "PING",
    "UPLOAD_NOW",
    "SYNC_MOTH_TIME",
    "MOTH_STATUS",
    "MOTH_LIST",
    "MOTH_TEST_STREAM",
    "OPEN_SETUP",
}

ADMIN_USER = os.getenv("DASHBOARD_USER", "admin")
ADMIN_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "change-me-now")

security = HTTPBasic()
app = FastAPI(title=APP_TITLE)
NODE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
HARDWARE_UID_PATTERN = re.compile(r"^[A-F0-9]{12,32}$")
_flac_reconcile_lock = threading.Lock()
_flac_reconcile_stop = threading.Event()
_flac_reconcile_thread: Optional[threading.Thread] = None


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
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unsafe path")
    return candidate


def remove_superseded_upload_temp(path_value: Any) -> None:
    if not path_value:
        return
    try:
        path = Path(str(path_value)).resolve()
        path.relative_to(INCOMING_DIR.resolve())
    except (OSError, ValueError):
        return
    if path.suffix != ".part":
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def sanitize_filename(filename: str) -> str:
    # Keep directory structure out of node-provided names.
    name = Path(filename).name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Empty filename")
    bad = set('/\\:\0')
    if any(ch in bad for ch in name):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return name


def parse_recording_datetime(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        epoch = int(value)
        return epoch if 946684800 <= epoch <= 4102444799 else None

    text = str(value).strip()
    if not text:
        return None
    if text.isdigit() and len(text) == 10:
        epoch = int(text)
        return epoch if 946684800 <= epoch <= 4102444799 else None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.astimezone(timezone.utc).timestamp())
    except ValueError:
        return None


def recording_time_from_filename(filename: str) -> Optional[int]:
    stem = Path(filename).stem
    match = re.search(
        r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])([0-2]\d|3[01])[_T-]?([01]\d|2[0-3])([0-5]\d)([0-5]\d)(?!\d)",
        stem,
    )
    if not match:
        return None
    try:
        parsed = datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
            int(match.group(5)),
            int(match.group(6)),
            tzinfo=timezone.utc,
        )
        return int(parsed.timestamp())
    except ValueError:
        return None


def canonical_recording_name(node_id: str, file_id: int, recorded_epoch: Optional[int], uploaded_epoch: int) -> str:
    safe_node = re.sub(r"[^A-Za-z0-9_.-]", "_", node_id)
    if recorded_epoch is not None:
        stamp = datetime.fromtimestamp(recorded_epoch, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{safe_node}_{stamp}_{file_id:06d}.WAV"
    upload_day = datetime.fromtimestamp(uploaded_epoch, timezone.utc).strftime("%Y%m%d")
    return f"{safe_node}_UPLOADED_{upload_day}_{file_id:06d}.WAV"


def catalog_recording(conn: sqlite3.Connection, file_id: int, rename_files: bool = False) -> None:
    row = conn.execute(
        """
        SELECT f.*, n.location_lat AS node_lat, n.location_lon AS node_lon,
               n.location_label AS node_location_label
        FROM files f
        JOIN nodes n ON n.node_id = f.node_id
        WHERE f.id=?
        """,
        (file_id,),
    ).fetchone()
    if not row:
        return

    recorded_epoch = (
        parse_recording_datetime(row["recorded_at_utc"])
        or parse_recording_datetime(row["recorded_at_corrected"])
        or parse_recording_datetime(row["recorded_at_raw"])
        or recording_time_from_filename(str(row["filename"]))
    )
    if row["recorded_at_source"]:
        time_source = str(row["recorded_at_source"])
    elif parse_recording_datetime(row["recorded_at_corrected"]) is not None:
        time_source = "corrected_manifest"
    elif parse_recording_datetime(row["recorded_at_raw"]) is not None:
        time_source = "manifest"
    elif recorded_epoch is not None:
        time_source = "filename_utc"
    else:
        time_source = "upload_day_fallback"

    uploaded_epoch = int(row["created_at"])
    canonical_name = canonical_recording_name(str(row["node_id"]), int(row["id"]), recorded_epoch, uploaded_epoch)
    recorded_iso = datetime.fromtimestamp(recorded_epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if recorded_epoch else None
    recording_lat = row["recording_lat"] if row["recording_lat"] is not None else row["node_lat"]
    recording_lon = row["recording_lon"] if row["recording_lon"] is not None else row["node_lon"]
    recording_label = row["recording_location_label"] or row["node_location_label"]
    if recorded_epoch is None:
        weather_status = "WAITING_FOR_RECORDING_TIME"
    elif recording_lat is None or recording_lon is None:
        weather_status = "WAITING_FOR_LOCATION"
    else:
        weather_status = row["weather_status"] or "PENDING"

    original_wav_path = row["original_wav_path"]
    flac_path = row["flac_path"]
    if rename_files and original_wav_path:
        current_wav = Path(str(original_wav_path))
        target_wav = current_wav.with_name(canonical_name)
        if current_wav.exists() and current_wav != target_wav:
            if target_wav.exists():
                original_wav_path = str(target_wav)
            else:
                current_wav.replace(target_wav)
                original_wav_path = str(target_wav)
        if original_wav_path == str(target_wav) and current_wav != target_wav:
            original_wav_path = str(target_wav)
            recordings_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recordings'"
            ).fetchone()
            if recordings_table:
                conn.execute(
                    "UPDATE recordings SET stored_path=? WHERE stored_path=?",
                    (str(target_wav), str(current_wav)),
                )
            sessions_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='esp32_upload_sessions'"
            ).fetchone()
            if sessions_table:
                conn.execute(
                    "UPDATE esp32_upload_sessions SET final_path=? WHERE final_path=?",
                    (str(target_wav), str(current_wav)),
                )
        if flac_path:
            current_flac = Path(str(flac_path))
            target_flac = current_flac.with_name(Path(canonical_name).with_suffix(".flac").name)
            if current_flac.exists() and current_flac != target_flac:
                if target_flac.exists():
                    flac_path = str(target_flac)
                else:
                    current_flac.replace(target_flac)
                    flac_path = str(target_flac)
            if flac_path == str(target_flac):
                flac_path = str(target_flac)

    conn.execute(
        """
        UPDATE files
        SET canonical_name=?, recorded_at_utc=?, recorded_at_source=?,
            recorded_at_corrected=COALESCE(recorded_at_corrected, ?),
            recording_lat=?, recording_lon=?, recording_location_label=?,
            weather_status=?, original_wav_path=?, flac_path=?
        WHERE id=?
        """,
        (
            canonical_name,
            recorded_epoch,
            time_source,
            recorded_iso,
            recording_lat,
            recording_lon,
            recording_label,
            weather_status,
            original_wav_path,
            flac_path,
            file_id,
        ),
    )


def backfill_recording_catalog() -> None:
    with db_connect() as conn:
        file_ids = [int(row["id"]) for row in conn.execute("SELECT id FROM files ORDER BY id").fetchall()]
        for file_id in file_ids:
            catalog_recording(conn, file_id, rename_files=True)
        conn.commit()


# ============================================================
# Database schema
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    hardware_uid TEXT,
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
    canonical_name TEXT,
    recorded_at_raw TEXT,
    recorded_at_corrected TEXT,
    recorded_at_utc INTEGER,
    recorded_at_source TEXT,
    recording_lat REAL,
    recording_lon REAL,
    recording_location_label TEXT,
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
    server_deleted_at INTEGER,
    server_delete_reason TEXT,
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

CREATE TABLE IF NOT EXISTS enrollment_requests (
    request_id TEXT PRIMARY KEY,
    hardware_uid TEXT NOT NULL,
    poll_token_hash TEXT NOT NULL,
    requested_node_name TEXT,
    firmware_version TEXT,
    hardware_version TEXT,
    request_ip TEXT,
    status TEXT NOT NULL,
    requested_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    approved_at INTEGER,
    delivered_at INTEGER,
    rejected_at INTEGER,
    node_id TEXT,
    key_id TEXT,
    device_secret TEXT
);

CREATE INDEX IF NOT EXISTS idx_enrollment_hardware
ON enrollment_requests(hardware_uid, requested_at DESC);

CREATE INDEX IF NOT EXISTS idx_enrollment_status
ON enrollment_requests(status, requested_at DESC);
"""


def init_db() -> None:
    ensure_dirs()
    execute_script(SCHEMA)
    with db_connect() as conn:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(nodes)").fetchall()}
        if "hardware_uid" not in columns:
            conn.execute("ALTER TABLE nodes ADD COLUMN hardware_uid TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_hardware_uid "
            "ON nodes(hardware_uid) WHERE hardware_uid IS NOT NULL"
        )
        file_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(files)").fetchall()}
        recording_columns = {
            "canonical_name": "TEXT",
            "recorded_at_utc": "INTEGER",
            "recorded_at_source": "TEXT",
            "recording_lat": "REAL",
            "recording_lon": "REAL",
            "recording_location_label": "TEXT",
            "server_deleted_at": "INTEGER",
            "server_delete_reason": "TEXT",
        }
        for name, column_type in recording_columns.items():
            if name not in file_columns:
                conn.execute(f"ALTER TABLE files ADD COLUMN {name} {column_type}")
        conn.commit()
    backfill_recording_catalog()
    with db_connect() as conn:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_files_canonical_name "
            "ON files(canonical_name) WHERE canonical_name IS NOT NULL"
        )
        conn.commit()


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    start_flac_reconcile_worker()


@app.on_event("shutdown")
def on_shutdown() -> None:
    stop_flac_reconcile_worker()


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


def mark_enrollment_claimed(node_id: str, key_id: str) -> None:
    t = now_epoch()
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE enrollment_requests
            SET status='CLAIMED', delivered_at=COALESCE(delivered_at, ?), device_secret=NULL
            WHERE node_id=? AND key_id=? AND status='APPROVED'
            """,
            (t, node_id, key_id),
        )
        conn.commit()


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
    mark_enrollment_claimed(node_id, key_id)
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


def normalize_hardware_uid(value: Any) -> str:
    hardware_uid = re.sub(r"[^A-Fa-f0-9]", "", str(value or "")).upper()
    if not HARDWARE_UID_PATTERN.fullmatch(hardware_uid):
        raise HTTPException(status_code=400, detail="Invalid hardware_uid")
    return hardware_uid


def allocate_node_id(conn: sqlite3.Connection, hardware_uid: str) -> str:
    base = f"BATNODE_{hardware_uid[-8:]}"
    if not conn.execute("SELECT 1 FROM nodes WHERE node_id=?", (base,)).fetchone():
        return base
    for _ in range(20):
        candidate = f"{base}_{secrets.token_hex(2).upper()}"
        if not conn.execute("SELECT 1 FROM nodes WHERE node_id=?", (candidate,)).fetchone():
            return candidate
    raise HTTPException(status_code=500, detail="Could not allocate node_id")


def enrollment_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "request_id": row["request_id"],
        "hardware_uid": row["hardware_uid"],
        "requested_node_name": row["requested_node_name"],
        "firmware_version": row["firmware_version"],
        "hardware_version": row["hardware_version"],
        "request_ip": row["request_ip"],
        "status": row["status"],
        "requested_at": row["requested_at"],
        "expires_at": row["expires_at"],
        "approved_at": row["approved_at"],
        "delivered_at": row["delivered_at"],
        "rejected_at": row["rejected_at"],
        "node_id": row["node_id"],
        "matched_node_id": row["matched_node_id"] if "matched_node_id" in row.keys() else None,
        "matched_node_name": row["matched_node_name"] if "matched_node_name" in row.keys() else None,
    }


def approve_enrollment(request_id: str, target_node_id: Optional[str], actor: str, ip: str = "") -> Dict[str, Any]:
    t = now_epoch()
    with db_connect() as conn:
        request_row = conn.execute(
            "SELECT * FROM enrollment_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if not request_row:
            raise HTTPException(status_code=404, detail="Enrollment request not found")
        if request_row["status"] != "PENDING":
            raise HTTPException(status_code=409, detail=f"Enrollment request is {request_row['status']}")
        if int(request_row["expires_at"]) <= t:
            conn.execute("UPDATE enrollment_requests SET status='EXPIRED' WHERE request_id=?", (request_id,))
            conn.commit()
            raise HTTPException(status_code=410, detail="Enrollment request expired")

        hardware_uid = str(request_row["hardware_uid"])
        matched = conn.execute(
            "SELECT node_id FROM nodes WHERE hardware_uid=?",
            (hardware_uid,),
        ).fetchone()
        requested_target = str(target_node_id or "").strip()
        if matched:
            node_id = str(matched["node_id"])
            if requested_target and requested_target != node_id:
                raise HTTPException(status_code=409, detail=f"Hardware is already linked to {node_id}")
        elif requested_target:
            if not NODE_ID_PATTERN.fullmatch(requested_target):
                raise HTTPException(status_code=400, detail="Invalid target_node_id")
            target = conn.execute(
                "SELECT node_id, hardware_uid FROM nodes WHERE node_id=?",
                (requested_target,),
            ).fetchone()
            if not target:
                raise HTTPException(status_code=404, detail="Target node does not exist")
            if target["hardware_uid"] and str(target["hardware_uid"]) != hardware_uid:
                raise HTTPException(status_code=409, detail="Target node is linked to different hardware")
            node_id = requested_target
        else:
            node_id = allocate_node_id(conn, hardware_uid)

        node = conn.execute("SELECT node_id FROM nodes WHERE node_id=?", (node_id,)).fetchone()
        requested_name = str(request_row["requested_node_name"] or f"Bat Node {hardware_uid[-6:]}")[:96]
        if node:
            conn.execute(
                """
                UPDATE nodes
                SET hardware_uid=?, firmware_version=?, hardware_version=?, active=1,
                    compromised=0, updated_at=?
                WHERE node_id=?
                """,
                (
                    hardware_uid,
                    request_row["firmware_version"],
                    request_row["hardware_version"],
                    t,
                    node_id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO nodes (
                    node_id, hardware_uid, node_name, deployment_notes,
                    firmware_version, hardware_version, active, compromised,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
                """,
                (
                    node_id,
                    hardware_uid,
                    requested_name,
                    "approved ESP32 enrollment",
                    request_row["firmware_version"],
                    request_row["hardware_version"],
                    t,
                    t,
                ),
            )

        key_id = f"key-{request_id[:12]}"
        device_secret = secrets.token_hex(32)
        conn.execute(
            "UPDATE node_credentials SET revoked_at=? WHERE node_id=? AND revoked_at IS NULL",
            (t, node_id),
        )
        conn.execute(
            """
            INSERT INTO node_credentials (node_id, key_id, secret, created_at, expires_at, revoked_at)
            VALUES (?, ?, ?, ?, NULL, NULL)
            """,
            (node_id, key_id, device_secret, t),
        )
        conn.execute(
            """
            UPDATE enrollment_requests
            SET status='APPROVED', approved_at=?, node_id=?, key_id=?, device_secret=?
            WHERE request_id=?
            """,
            (t, node_id, key_id, device_secret, request_id),
        )
        conn.commit()

    audit(actor, "approve_enrollment", "node", node_id, ip, {"request_id": request_id, "hardware_uid": hardware_uid})
    return {"ok": True, "request_id": request_id, "node_id": node_id, "re_enrolled": bool(node)}


@app.post("/v1/enrollment/request")
async def request_enrollment(request: Request) -> Dict[str, Any]:
    try:
        data = json.loads((await request.body()).decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Bad JSON")

    hardware_uid = normalize_hardware_uid(data.get("hardware_uid"))
    node_name = str(data.get("node_name") or f"Bat Node {hardware_uid[-6:]}").strip()[:96]
    firmware = str(data.get("firmware_version") or "")[:96] or None
    hardware = str(data.get("hardware_version") or "")[:96] or None
    request_id = uuid.uuid4().hex
    poll_token = secrets.token_urlsafe(32)
    poll_token_hash = sha256_hex(poll_token.encode("utf-8"))
    t = now_epoch()
    expires_at = t + ENROLLMENT_TTL_SECONDS
    request_ip = request.client.host if request.client else ""

    with db_connect() as conn:
        conn.execute(
            """
            UPDATE enrollment_requests
            SET status='REPLACED', device_secret=NULL
            WHERE hardware_uid=? AND status IN ('PENDING', 'APPROVED')
            """,
            (hardware_uid,),
        )
        conn.execute(
            """
            INSERT INTO enrollment_requests (
                request_id, hardware_uid, poll_token_hash, requested_node_name,
                firmware_version, hardware_version, request_ip, status,
                requested_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
            """,
            (
                request_id,
                hardware_uid,
                poll_token_hash,
                node_name,
                firmware,
                hardware,
                request_ip,
                t,
                expires_at,
            ),
        )
        matched = conn.execute("SELECT node_id FROM nodes WHERE hardware_uid=?", (hardware_uid,)).fetchone()
        conn.commit()

    return {
        "ok": True,
        "status": "PENDING",
        "request_id": request_id,
        "poll_token": poll_token,
        "poll_after_seconds": ENROLLMENT_POLL_SECONDS,
        "expires_at": expires_at,
        "recognized_node": str(matched["node_id"]) if matched else None,
    }


@app.post("/v1/enrollment/status/{request_id}")
async def enrollment_status(request_id: str, request: Request) -> Dict[str, Any]:
    try:
        data = json.loads((await request.body()).decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Bad JSON")
    poll_token = str(data.get("poll_token") or "")
    if not poll_token:
        raise HTTPException(status_code=401, detail="poll_token required")

    t = now_epoch()
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM enrollment_requests WHERE request_id=?", (request_id,)).fetchone()
        if not row or not constant_time_eq(sha256_hex(poll_token.encode("utf-8")), str(row["poll_token_hash"])):
            raise HTTPException(status_code=404, detail="Enrollment request not found")
        status = str(row["status"])
        if int(row["expires_at"]) <= t and status == "PENDING":
            status = "EXPIRED"
            conn.execute("UPDATE enrollment_requests SET status='EXPIRED' WHERE request_id=?", (request_id,))
            conn.commit()
        if status == "APPROVED":
            conn.execute(
                "UPDATE enrollment_requests SET delivered_at=COALESCE(delivered_at, ?) WHERE request_id=?",
                (t, request_id),
            )
            conn.commit()
            return {
                "ok": True,
                "status": status,
                "node_id": row["node_id"],
                "key_id": row["key_id"],
                "device_secret": row["device_secret"],
            }
    return {"ok": True, "status": status, "poll_after_seconds": ENROLLMENT_POLL_SECONDS}


@app.post("/v1/provision/node")
async def provision_node(request: Request) -> Dict[str, Any]:
    if not PROVISIONING_TOKEN:
        raise HTTPException(status_code=503, detail="Provisioning is not enabled on this server")

    try:
        data = json.loads((await request.body()).decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Bad JSON")

    token = str(data.get("provisioning_token") or "")
    if not constant_time_eq(token, PROVISIONING_TOKEN):
        raise HTTPException(status_code=401, detail="Bad provisioning token")

    requested_node_id = str(data.get("node_id") or "").strip()
    if requested_node_id and not NODE_ID_PATTERN.fullmatch(requested_node_id):
        raise HTTPException(status_code=400, detail="Invalid node_id")

    node_name = str(data.get("node_name") or requested_node_id or "Bat Node").strip()[:96]
    firmware = str(data.get("firmware_version") or "")[:96] or None
    hardware = str(data.get("hardware_version") or "")[:96] or None
    notes = str(data.get("deployment_notes") or "self-provisioned ESP32 node")[:512]
    key_id = str(data.get("key_id") or "key-1").strip()[:48] or "key-1"
    secret = secrets.token_hex(32)
    t = now_epoch()

    with db_connect() as conn:
        if requested_node_id:
            node_id = requested_node_id
        else:
            for _ in range(10):
                candidate = f"BATNODE_{secrets.token_hex(4).upper()}"
                exists = conn.execute("SELECT 1 FROM nodes WHERE node_id=?", (candidate,)).fetchone()
                if not exists:
                    node_id = candidate
                    break
            else:
                raise HTTPException(status_code=500, detail="Could not allocate node_id")

        conn.execute(
            """
            INSERT INTO nodes (
                node_id, node_name, location_lat, location_lon, location_label,
                deployment_notes, firmware_version, hardware_version, active,
                compromised, created_at, updated_at
            ) VALUES (?, ?, NULL, NULL, NULL, ?, ?, ?, 1, 0, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                node_name=excluded.node_name,
                deployment_notes=excluded.deployment_notes,
                firmware_version=excluded.firmware_version,
                hardware_version=excluded.hardware_version,
                active=1,
                compromised=0,
                updated_at=excluded.updated_at
            """,
            (node_id, node_name, notes, firmware, hardware, t, t),
        )
        conn.execute(
            """
            INSERT INTO node_credentials (node_id, key_id, secret, created_at, expires_at, revoked_at)
            VALUES (?, ?, ?, ?, NULL, NULL)
            ON CONFLICT(node_id, key_id) DO UPDATE SET
                secret=excluded.secret,
                revoked_at=NULL
            """,
            (node_id, key_id, secret, t),
        )
        conn.commit()

    audit(
        "provisioning",
        "node_provisioned",
        "node",
        node_id,
        request.client.host if request.client else "",
        {"node_name": node_name, "key_id": key_id},
    )
    return {
        "ok": True,
        "node_id": node_id,
        "key_id": key_id,
        "device_secret": secret,
        "server_time": t,
    }


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
    retry_before = t - COMMAND_REDELIVER_AFTER_SECONDS
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, command_type, payload_json
            FROM commands
            WHERE node_id = ?
              AND (
                status = 'PENDING'
                OR (status = 'DELIVERED' AND acked_at IS NULL AND COALESCE(delivered_at, 0) <= ?)
              )
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY created_at ASC
            LIMIT 5
            """,
            (node_id, retry_before, t),
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
    sd_free_mb = data.get("sd_free_mb")
    if sd_free_mb is None and data.get("sd_free_kb") is not None:
        sd_free_mb = float(data.get("sd_free_kb")) / 1024.0
    elif sd_free_mb is not None:
        sd_free_mb = float(sd_free_mb)

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
        if sd_free_mb is not None:
            conn.execute(
                """
                INSERT INTO node_state (node_id, last_seen, sd_free_mb, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    last_seen=excluded.last_seen,
                    sd_free_mb=excluded.sd_free_mb,
                    updated_at=excluded.updated_at
                """,
                (node_id, t, sd_free_mb, t),
            )
        for item in files:
            filename = sanitize_filename(str(item.get("filename", "")))
            file_size = int(item.get("file_size_bytes") or 0)
            if file_size <= 0:
                raise HTTPException(status_code=400, detail=f"Bad file_size_bytes for {filename}")
            local_file_id = item.get("local_file_id")
            recorded_at = item.get("recorded_at")

            try:
                cur = conn.execute(
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
                file_id = int(cur.lastrowid)
            except sqlite3.IntegrityError:
                # Existing manifest/file row. Do not reset completed upload status.
                conn.execute(
                    """
                    UPDATE files
                    SET updated_at=?, duration_seconds=COALESCE(?, duration_seconds),
                        sample_rate=COALESCE(?, sample_rate), channels=COALESCE(?, channels),
                        bit_depth=COALESCE(?, bit_depth),
                        recorded_at_raw=COALESCE(recorded_at_raw, ?),
                        recorded_at_corrected=COALESCE(recorded_at_corrected, ?)
                    WHERE node_id=? AND filename=? AND recorded_at_raw IS ? AND file_size_bytes=?
                    """,
                    (
                        t,
                        item.get("duration_seconds"),
                        item.get("sample_rate"),
                        item.get("channels"),
                        item.get("bit_depth"),
                        recorded_at,
                        item.get("recorded_at_corrected"),
                        node_id,
                        filename,
                        recorded_at,
                        file_size,
                    ),
                )
                existing_file = conn.execute(
                    """
                    SELECT id FROM files
                    WHERE node_id=? AND filename=? AND recorded_at_raw IS ? AND file_size_bytes=?
                    """,
                    (node_id, filename, recorded_at, file_size),
                ).fetchone()
                if not existing_file:
                    raise
                file_id = int(existing_file["id"])
            catalog_recording(conn, file_id, rename_files=False)

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
                remove_superseded_upload_temp(existing["temp_path"])
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
    request_started = time.perf_counter()
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
            return {
                "ok": True,
                "duplicate": True,
                "chunk_index": chunk_index,
                "server_ms": round((time.perf_counter() - request_started) * 1000),
            }

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

    return {
        "ok": True,
        "duplicate": False,
        "chunk_index": chunk_index,
        "bytes_received": total_received,
        "server_ms": round((time.perf_counter() - request_started) * 1000),
    }


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
        if frames <= 0:
            raise ValueError("WAV contains no audio frames")
        if rate <= 0 or channels <= 0 or sampwidth <= 0:
            raise ValueError("WAV audio format is incomplete")
        duration = frames / rate if rate else None
        return {
            "sample_rate": rate,
            "channels": channels,
            "bit_depth": sampwidth * 8,
            "duration_seconds": duration,
            "frames": frames,
        }


def clamp_int(value: str, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def find_flac_encoder() -> Tuple[Optional[str], Optional[str]]:
    if FLAC_ENCODER in ("none", "off", "disabled"):
        return None, None
    if FLAC_ENCODER == "flac":
        candidates = ("flac",)
    elif FLAC_ENCODER == "ffmpeg":
        candidates = ("ffmpeg",)
    else:
        candidates = ("flac", "ffmpeg")

    if FLAC_ENCODER_PATH:
        explicit = Path(FLAC_ENCODER_PATH).expanduser()
        if explicit.is_file():
            name = "ffmpeg" if "ffmpeg" in explicit.stem.lower() else "flac"
            return name, str(explicit)

    for name in candidates:
        path = shutil.which(name)
        if path:
            return name, path
    if os.name == "nt" and "flac" in candidates:
        package_root = Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
        matches = sorted(package_root.glob("Xiph.FLAC_*/flac-*-win/Win64/flac.exe"), reverse=True)
        if matches:
            return "flac", str(matches[0])
    return None, None


def flac_command(encoder_name: str, encoder_path: str, wav_path: Path, flac_path: Path) -> list[str]:
    if encoder_name == "flac":
        level = clamp_int(FLAC_COMPRESSION_LEVEL, 5, 0, 8)
        return [encoder_path, f"-{level}", "-f", "-s", "-o", str(flac_path), str(wav_path)]

    level = clamp_int(FLAC_COMPRESSION_LEVEL, 5, 0, 12)
    return [
        encoder_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(wav_path),
        "-compression_level",
        str(level),
        str(flac_path),
    ]


def make_flac(wav_path: Path, node_id: str) -> Tuple[str, Optional[Path], Optional[str]]:
    encoder_name, encoder_path = find_flac_encoder()
    if not encoder_name or not encoder_path:
        return "SKIPPED_NO_ENCODER", None, None

    out_dir = safe_join(FLAC_DIR, node_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    flac_path = safe_join(out_dir, wav_path.with_suffix(".flac").name)
    result = subprocess.run(
        flac_command(encoder_name, encoder_path, wav_path, flac_path),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        try:
            flac_path.unlink(missing_ok=True)
        except OSError:
            pass
        return "ERROR", None, result.stderr.strip() or f"{encoder_name} failed"
    return "OK", flac_path, None


def maybe_make_flac(wav_path: Path, node_id: str) -> Optional[Path]:
    status, flac_path, error = make_flac(wav_path, node_id)
    if status == "ERROR":
        raise RuntimeError(error or "FLAC conversion failed")
    return flac_path


def valid_flac_file(path_value: Any) -> bool:
    if not path_value:
        return False
    try:
        path = Path(str(path_value))
        if not path.is_file() or path.stat().st_size <= 4:
            return False
        with path.open("rb") as handle:
            return handle.read(4) == b"fLaC"
    except OSError:
        return False


def reconcile_flac_files(
    limit: int = FLAC_RECONCILE_BATCH_SIZE,
    file_ids: Optional[Iterable[int]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    if not _flac_reconcile_lock.acquire(blocking=False):
        return {"ok": False, "busy": True, "checked": 0, "compressed": 0, "failed": 0}

    summary: Dict[str, Any] = {
        "ok": True,
        "busy": False,
        "checked": 0,
        "compressed": 0,
        "already_ok": 0,
        "failed": 0,
        "missing": 0,
        "encoder": None,
        "errors": [],
    }
    try:
        where = [
            "upload_status='SERVER_COPY_VERIFIED'",
            "original_wav_path IS NOT NULL",
            "server_deleted_at IS NULL",
        ]
        params: list[Any] = []
        if file_ids is not None:
            ids = [int(file_id) for file_id in file_ids]
            if not ids:
                return summary
            where.append(f"id IN ({qmarks(ids)})")
            params.extend(ids)

        with db_connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, node_id, filename, original_wav_path, wav_parse_status,
                       flac_status, flac_path
                FROM files
                WHERE {' AND '.join(where)}
                ORDER BY CASE WHEN COALESCE(flac_status, '')='OK' THEN 1 ELSE 0 END,
                         updated_at, id
                """,
                params,
            ).fetchall()

        candidates = []
        for row in rows:
            if not force and row["flac_status"] == "OK" and valid_flac_file(row["flac_path"]):
                summary["already_ok"] += 1
                continue
            candidates.append(row)
            if len(candidates) >= limit:
                break

        if not candidates:
            return summary

        encoder_name, encoder_path = find_flac_encoder()
        summary["encoder"] = encoder_name
        if not encoder_name or not encoder_path:
            with db_connect() as conn:
                conn.executemany(
                    "UPDATE files SET flac_status='SKIPPED_NO_ENCODER', updated_at=? WHERE id=?",
                    [(now_epoch(), int(row["id"])) for row in candidates],
                )
                conn.commit()
            summary["checked"] = len(candidates)
            summary["ok"] = False
            summary["error"] = "No FLAC encoder is installed or available on PATH"
            return summary

        for row in candidates:
            file_id = int(row["id"])
            wav_path = Path(str(row["original_wav_path"]))
            summary["checked"] += 1
            if not wav_path.is_file():
                error = f"Source WAV missing: {wav_path}"
                with db_connect() as conn:
                    conn.execute(
                        "UPDATE files SET flac_status=?, updated_at=? WHERE id=?",
                        (f"ERROR: {error}", now_epoch(), file_id),
                    )
                    conn.commit()
                summary["missing"] += 1
                summary["failed"] += 1
                summary["errors"].append({"file_id": file_id, "error": error})
                continue

            try:
                wav_meta = parse_wav(wav_path)
            except Exception as exc:
                error = f"Invalid WAV: {exc}"
                with db_connect() as conn:
                    conn.execute(
                        """
                        UPDATE files
                        SET upload_status='SERVER_COPY_FAILED_PARSE', wav_parse_status=?,
                            flac_status='NOT_RUN', updated_at=?
                        WHERE id=?
                        """,
                        (f"ERROR: {exc}", now_epoch(), file_id),
                    )
                    conn.commit()
                summary["failed"] += 1
                summary["errors"].append({"file_id": file_id, "error": error})
                continue

            with db_connect() as conn:
                conn.execute(
                    """
                    UPDATE files
                    SET wav_parse_status='OK', sample_rate=?, channels=?, bit_depth=?,
                        duration_seconds=?, flac_status='COMPRESSING', updated_at=?
                    WHERE id=?
                    """,
                    (
                        wav_meta.get("sample_rate"),
                        wav_meta.get("channels"),
                        wav_meta.get("bit_depth"),
                        wav_meta.get("duration_seconds"),
                        now_epoch(),
                        file_id,
                    ),
                )
                conn.commit()

            try:
                status, flac_path, error = make_flac(wav_path, str(row["node_id"]))
                if status != "OK" or not valid_flac_file(flac_path):
                    raise RuntimeError(error or f"Encoder returned {status} without a valid FLAC file")
            except Exception as exc:
                error = str(exc)
                with db_connect() as conn:
                    conn.execute(
                        "UPDATE files SET flac_status=?, flac_path=NULL, updated_at=? WHERE id=?",
                        (f"ERROR: {error}"[:1000], now_epoch(), file_id),
                    )
                    conn.commit()
                summary["failed"] += 1
                summary["errors"].append({"file_id": file_id, "error": error})
                continue

            with db_connect() as conn:
                conn.execute(
                    "UPDATE files SET flac_status='OK', flac_path=?, updated_at=? WHERE id=?",
                    (str(flac_path), now_epoch(), file_id),
                )
                conn.commit()
            summary["compressed"] += 1

        summary["ok"] = summary["failed"] == 0
        return summary
    finally:
        _flac_reconcile_lock.release()


def _flac_reconcile_loop() -> None:
    if _flac_reconcile_stop.wait(FLAC_RECONCILE_START_DELAY_SECONDS):
        return
    while not _flac_reconcile_stop.is_set():
        result = reconcile_flac_files()
        if result.get("checked") or result.get("failed"):
            print(f"FLAC reconciliation: {json.dumps(result, separators=(',', ':'))}", flush=True)
        if _flac_reconcile_stop.wait(FLAC_RECONCILE_INTERVAL_SECONDS):
            return


def start_flac_reconcile_worker() -> None:
    global _flac_reconcile_thread
    if _flac_reconcile_thread and _flac_reconcile_thread.is_alive():
        return
    _flac_reconcile_stop.clear()
    _flac_reconcile_thread = threading.Thread(
        target=_flac_reconcile_loop,
        name="flac-reconcile",
        daemon=True,
    )
    _flac_reconcile_thread.start()


def stop_flac_reconcile_worker() -> None:
    _flac_reconcile_stop.set()
    thread = _flac_reconcile_thread
    if thread and thread.is_alive():
        thread.join(timeout=2)


@app.post("/v1/uploads/{upload_id}/complete")
async def complete_upload(upload_id: str, request: Request, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    finalize_started = time.perf_counter()
    body = await request.body()
    ident = await require_device_auth(request, body)
    node_id = ident["node_id"]
    t = now_epoch()

    with db_connect() as conn:
        s = conn.execute(
            """
            SELECT s.*, f.filename, f.canonical_name, f.file_size_bytes, f.id AS file_id
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
    final_name = sanitize_filename(str(s["canonical_name"] or s["filename"]))
    final_path = safe_join(node_wav_dir, final_name)
    if final_path.exists():
        # Avoid clobbering an earlier file with the same name.
        final_path = safe_join(node_wav_dir, f"{Path(final_name).stem}_{upload_id}{Path(final_name).suffix}")
    temp_path.replace(final_path)

    wav_status = "ERROR"
    wav_meta: Dict[str, Any] = {}
    flac_status = "NOT_RUN"

    try:
        wav_meta = parse_wav(final_path)
        wav_status = "OK"
    except Exception as e:
        wav_status = f"ERROR: {e}"

    server_hash = file_sha256(final_path)

    if wav_status == "OK":
        flac_status = "PENDING"

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
                flac_status,
                str(final_path),
                None,
                t,
                s["file_id"],
            ),
        )
        conn.commit()

    if wav_status == "OK":
        background_tasks.add_task(reconcile_flac_files, 1, [int(s["file_id"])], False)

    finalize_ms = round((time.perf_counter() - finalize_started) * 1000, 1)

    return {
        "ok": wav_status == "OK",
        "upload_id": upload_id,
        "file_id": s["file_id"],
        "server_sha256": server_hash,
        "wav_parse_status": wav_status,
        "wav_metadata": wav_meta,
        "flac_status": flac_status,
        "flac_error": None,
        "finalize_ms": finalize_ms,
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

@app.get("/admin/enrollment/requests")
def admin_enrollment_requests(admin: str = Depends(require_admin)) -> Dict[str, Any]:
    t = now_epoch()
    with db_connect() as conn:
        conn.execute(
            "UPDATE enrollment_requests SET status='EXPIRED' WHERE status='PENDING' AND expires_at <= ?",
            (t,),
        )
        conn.execute(
            "UPDATE enrollment_requests SET status='EXPIRED', device_secret=NULL WHERE status='APPROVED' AND expires_at <= ?",
            (t,),
        )
        rows = conn.execute(
            """
            SELECT er.*, n.node_id AS matched_node_id, n.node_name AS matched_node_name
            FROM enrollment_requests er
            LEFT JOIN nodes n ON n.hardware_uid = er.hardware_uid
            WHERE er.status IN ('PENDING', 'APPROVED')
            ORDER BY CASE er.status WHEN 'PENDING' THEN 0 ELSE 1 END, er.requested_at DESC
            """
        ).fetchall()
        conn.commit()
    return {"ok": True, "requests": [enrollment_row(row) for row in rows]}


@app.post("/admin/enrollment/{request_id}/approve")
async def admin_approve_enrollment(
    request_id: str,
    request: Request,
    admin: str = Depends(require_admin),
) -> Dict[str, Any]:
    try:
        data = json.loads((await request.body()).decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Bad JSON")
    return approve_enrollment(
        request_id,
        str(data.get("target_node_id") or "").strip() or None,
        admin,
        request.client.host if request.client else "",
    )


@app.post("/admin/enrollment/{request_id}/reject")
def admin_reject_enrollment(
    request_id: str,
    request: Request,
    admin: str = Depends(require_admin),
) -> Dict[str, Any]:
    t = now_epoch()
    with db_connect() as conn:
        row = conn.execute(
            "SELECT hardware_uid, status FROM enrollment_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Enrollment request not found")
        if row["status"] != "PENDING":
            raise HTTPException(status_code=409, detail=f"Enrollment request is {row['status']}")
        conn.execute(
            "UPDATE enrollment_requests SET status='REJECTED', rejected_at=?, device_secret=NULL WHERE request_id=?",
            (t, request_id),
        )
        conn.commit()
    audit(
        admin,
        "reject_enrollment",
        "hardware",
        str(row["hardware_uid"]),
        request.client.host if request.client else "",
        {"request_id": request_id},
    )
    return {"ok": True, "request_id": request_id, "status": "REJECTED"}


@app.post("/admin/storage/compress")
async def admin_compress_recordings(
    request: Request,
    admin: str = Depends(require_admin),
) -> Dict[str, Any]:
    try:
        data = json.loads((await request.body()).decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Bad JSON")
    limit = max(1, min(int(data.get("limit") or 25), 100))
    force = bool(data.get("force", False))
    result = reconcile_flac_files(limit=limit, force=force)
    audit(
        admin,
        "flac_reconciliation",
        "recordings",
        "all",
        request.client.host if request.client else "",
        result,
    )
    return result


@app.post("/admin/commands/{node_id}/{command_type}")
def admin_queue_command(node_id: str, command_type: str, request: Request, admin: str = Depends(require_admin)) -> Dict[str, Any]:
    t = now_epoch()
    payload = {}
    command_type = command_type.upper()
    if command_type not in ALLOWED_COMMAND_TYPES:
        raise HTTPException(status_code=400, detail="unsupported command type")
    with db_connect() as conn:
        node = conn.execute("SELECT node_id FROM nodes WHERE node_id=?", (node_id,)).fetchone()
        if not node:
            raise HTTPException(status_code=404, detail="Unknown node")
        cur = conn.execute(
            """
            INSERT INTO commands (node_id, command_type, payload_json, status, created_at, expires_at)
            VALUES (?, ?, ?, 'PENDING', ?, ?)
            """,
            (node_id, command_type, json.dumps(payload), t, t + 24 * 3600),
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
