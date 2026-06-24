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
PROVISIONING_TOKEN = os.getenv("PROVISIONING_TOKEN", "").strip()
ENROLLMENT_TTL_SECONDS = int(os.getenv("ENROLLMENT_TTL_SECONDS", "1800"))
ENROLLMENT_POLL_SECONDS = max(2, int(os.getenv("ENROLLMENT_POLL_SECONDS", "3")))

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
                raise RuntimeError(f"Canonical recording already exists: {target_wav}")
            current_wav.replace(target_wav)
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
                    raise RuntimeError(f"Canonical FLAC already exists: {target_flac}")
                current_flac.replace(target_flac)
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


def ge…12427 tokens truncated…th):
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
