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
    UNIÛM4âÚ$z{-®éÜj×tU$RCÓð¢"""À¢¢WÆöE÷7FGW5÷fÇVRÀ¢çB5²&fÆU÷6¦Uö'FW2%ÒÀ¢6W'fW%ö6À¢ve÷7FGW2À¢fÆ5÷7FGW2À¢7G"fæÅ÷FÀ¢æöæRÀ¢BÀ¢5²&fÆUöB%ÒÀ¢À¢¢6öæâæ6öÖÖB ¢bve÷7FGW2ÓÒ$ô²# ¢&6¶w&÷VæE÷F6·2æFE÷F6²&V6öæ6ÆUöfÆ5öfÆW2ÂÂ¶çB5²&fÆUöB%ÒÒÂfÇ6R ¢fæÆ¦Uö×2Ò&÷VæBFÖRçW&eö6÷VçFW"ÒfæÆ¦U÷7F'FVB¢Â ¢&WGW&â°¢&ö²#¢ve÷7FGW2ÓÒ$ô²"À¢'WÆöEöB#¢WÆöEöBÀ¢&fÆUöB#¢5²&fÆUöB%ÒÀ¢'6W'fW%÷6#Sb#¢6W'fW%ö6À¢'ve÷'6U÷7FGW2#¢ve÷7FGW2À¢'veöÖWFFF#¢veöÖWFÀ¢&fÆ5÷7FGW2#¢fÆ5÷7FGW2À¢&fÆ5öW'&÷"#¢æöæRÀ¢&fæÆ¦Uö×2#¢fæÆ¦Uö×2À¢Ð  ¢2ÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÐ¢2FVÆWFöâWF÷&¦Föà¢2ÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÐ ¦FVb5÷6fU÷FõöFVÆWFR&÷s¢7ÆFS2å&÷rÓâ&ööÃ ¢b&÷u²'WÆöE÷7FGW2%ÒÒ%4U%dU%ô4õõdU$dTB# ¢&WGW&âfÇ6P¢b&÷u²'ve÷'6U÷7FGW2%ÒÒ$ô²# ¢&WGW&âfÇ6P¢b$UT$UôdÄ5ô$Tdõ$UôDTÄUDRæB&÷u²&fÆ5÷7FGW2%ÒÒ$ô²# ¢&WGW&âfÇ6P¢b$UT$Uô$4µUô$Tdõ$UôDTÄUDRæB&÷u²&&6·W÷7FGW2%ÒÒ$ô²# ¢&WGW&âfÇ6P¢b&÷u²&FVÆWFU÷7FGW2%Òâ$DTÄUDTEôe$ôÕõ4B"Â$DTÄUDUõ$UTU5DTB" ¢&WGW&âfÇ6P¢&WGW&âG'VP  ¤ævWB"÷cöæöFW2÷¶æöFUöGÒöFVÆWFUöWF÷&¦Föâ"¦7æ2FVbFVÆWFUöWF÷&¦FöâæöFUöC¢7G"ÂÖæfW7EöC¢7G"Â&WVW7C¢&WVW7BÓâF7E·7G"ÂçÓ ¢&öGÒvB&WVW7Bæ&öG¢FVçBÒvB&WV&UöFWf6UöWF&WVW7BÂ&öG¢bFVçE²&æöFUöB%ÒÒæöFUöC ¢&6REEW6WFöâ7FGW5ö6öFSÓC2ÂFWFÃÒ&æöFUöBÖ6ÖF6" ¢BÒæ÷uöWö6¢W&W2ÒB²#B¢3c ¢vFF%ö6öææV7B26öæã ¢&÷w2Ò6öæâæWV7WFR¢%4TÄT5B¢e$ôÒfÆW2tU$RæöFUöCÓòäBÖæfW7EöCÓòõ$DU"%&V6÷&FVEöE÷&rÂfÆVæÖR"À¢æöFUöBÂÖæfW7EöBÀ¢æfWF6ÆÂ¢6fUöfÆW2ÒµÐ¢f÷""â&÷w3 ¢b5÷6fU÷FõöFVÆWFR" ¢6fUöfÆW2æVæB°¢&fÆUöB#¢%²&B%ÒÀ¢&Æö6ÅöfÆUöB#¢%²&Æö6ÅöfÆUöB%ÒÀ¢&fÆVæÖR#¢%²&fÆVæÖR%ÒÀ¢'&V6÷&FVEöB#¢%²'&V6÷&FVEöE÷&r%ÒÀ¢&fÆU÷6¦Uö'FW2#¢%²&fÆU÷6¦Uö'FW2%ÒÀ¢'6W'fW%÷7FGW2#¢%4dUõDõôDTÄUDR"À¢'6W'fW%÷6#Sb#¢%²'6W'fW%÷6#Sb%ÒÀ¢Ò¢WFöBÒ$DTÅò"²WVBçWVCBæW¢ÆöBÒ°¢&ö²#¢G'VRÀ¢&WF÷&¦FöåöB#¢WFöBÀ¢&æöFUöB#¢æöFUöBÀ¢&ÖæfW7EöB#¢ÖæfW7EöBÀ¢&FVÆWFUöÖöFR#¢'W%öfÆR"À¢&77VVEöB#¢BÀ¢&W&W5öB#¢W&W2À¢&fÆW2#¢6fUöfÆW2À¢Ð¢ÆöEö§6öâÒ6æöæ6Åö§6öâÆöB¢6væGW&RÒÖ5öWFVçE²'6V7&WB%ÒÂ$DTÄUDUôUDõ$¤DôåÆâ"²ÆöEö§6öâ¢6öæâæWV7WFR¢"" ¢å4U%BåDòFVÆWFUöWF÷&¦Föç2¢BÂæöFUöBÂÖæfW7EöBÂÖöFRÂ7FGW2Â77VVEöBÂW&W5öBÂ6væVE÷ÆöBÂ6væGW&P¢dÅTU2òÂòÂòÂwW%öfÆRrÂt55TTBrÂòÂòÂòÂò¢"""À¢WFöBÂæöFUöBÂÖæfW7EöBÂBÂW&W2ÂÆöEö§6öâÂ6væGW&RÀ¢¢fÆUöG2Ò¶e²&fÆUöB%Òf÷"bâ6fUöfÆW5Ð¢bfÆUöG3 ¢6öæâæWV7WFR¢b%UDDRfÆW24UBFVÆWFU÷7FGW3ÒtDTÄUDUôUDõ$¤TBrÂFVÆWFUöWF÷&¦FöåöCÓòÂFVÆWFUöWF÷&¦VEöCÓòÂWFFVEöCÓòtU$RBâ·Ö&·2fÆUöG2Ò"À¢WFöBÂBÂBÂ¦fÆUöG2À¢¢6öæâæ6öÖÖB¢&WGW&â²¢§ÆöBÂ'6væGW&R#¢6væGW&WÐ  ¤ç÷7B"÷cöæöFW2÷¶æöFUöGÒöFVÆWFUö6öæf&Ò"¦7æ2FVbFVÆWFUö6öæf&ÒæöFUöC¢7G"Â&WVW7C¢&WVW7BÓâF7E·7G"ÂçÓ ¢&öGÒvB&WVW7Bæ&öG¢FVçBÒvB&WV&UöFWf6UöWF&WVW7BÂ&öG¢bFVçE²&æöFUöB%ÒÒæöFUöC ¢&6REEW6WFöâ7FGW5ö6öFSÓC2ÂFWFÃÒ&æöFUöBÖ6ÖF6"¢FFÒ§6öâæÆöG2&öGæFV6öFR'WFbÓ"÷"'·Ò"¢WF÷&¦FöåöBÒFFævWB&WF÷&¦FöåöB"¢fÆW2ÒFFævWB&fÆW2"÷"µÐ¢bæ÷BWF÷&¦FöåöC ¢&6REEW6WFöâ7FGW5ö6öFSÓCÂFWFÃÒ&WF÷&¦FöåöB&WV&VB" ¢BÒæ÷uöWö6¢WFFVBÒ ¢vFF%ö6öææV7B26öæã ¢WFÒ6öæâæWV7WFR¢%4TÄT5B¢e$ôÒFVÆWFUöWF÷&¦Föç2tU$RCÓòäBæöFUöCÓò"À¢WF÷&¦FöåöBÂæöFUöBÀ¢æfWF6öæR¢bæ÷BWF ¢&6REEW6WFöâ7FGW5ö6öFSÓCBÂFWFÃÒ&FVÆWFRWF÷&¦Föâæ÷Bf÷VæB"¢f÷"FVÒâfÆW3 ¢fÆUöBÒFVÒævWB&fÆUöB"¢fÆVæÖRÒFVÒævWB&fÆVæÖR"¢&W7VÇBÒFVÒævWB'&W7VÇB"¢W'&÷"ÒFVÒævWB&W'&÷""¢b&W7VÇBÓÒ$DTÄUDTB# ¢6öæâæWV7WFR¢"" ¢UDDRfÆW0¢4UBFVÆWFU÷7FGW3ÒtDTÄUDTEôe$ôÕõ4BrÂFVÆWFUö6öæf&ÖVEöCÓòÂWFFVEöCÓð¢tU$RCÓòäBæöFUöCÓòäBFVÆWFUöWF÷&¦FöåöCÓð¢"""À¢BÂBÂfÆUöBÂæöFUöBÂWF÷&¦FöåöBÀ¢¢VÇ6S ¢6öæâæWV7WFR¢"" ¢UDDRfÆW0¢4UBFVÆWFU÷7FGW3ÒtDTÄUDUôdÄTBrÂFVÆWFUöW'&÷#ÓòÂWFFVEöCÓð¢tU$RCÓòäBæöFUöCÓòäBFVÆWFUöWF÷&¦FöåöCÓð¢"""À¢W'&÷"÷"&W7VÇB÷"'Væ¶æ÷vâ"ÂBÂfÆUöBÂæöFUöBÂWF÷&¦FöåöBÀ¢¢6öæâæWV7WFR¢"" ¢å4U%BåDò6EöFVÆWFöåöÆör¢æöFUöBÂWF÷&¦FöåöBÂfÆUöBÂfÆVæÖRÂ&WVW7FVEöBÂ6öæf&ÖVEöBÂ&W7VÇBÂW'&÷ ¢dÅTU2òÂòÂòÂòÂòÂòÂòÂò¢"""À¢æöFUöBÂWF÷&¦FöåöBÂfÆUöBÂfÆVæÖRÂWF²&77VVEöB%ÒÂBÂ&W7VÇB÷"%Tä´äõtâ"ÂW'&÷"À¢¢WFFVB³Ò¢6öæâæWV7WFR¢%UDDRFVÆWFUöWF÷&¦Föç24UB7FGW3Òt4´TBrtU$RCÓò"À¢WF÷&¦FöåöBÂÀ¢¢6öæâæ6öÖÖB¢&WGW&â²&ö²#¢G'VRÂ'WFFVB#¢WFFVGÐ  ¢2ÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÐ¢2FÖâöF6&ö&BVæGöçG0¢2ÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÐ ¤ævWB"öFÖâöVç&öÆÆÖVçB÷&WVW7G2"¦FVbFÖåöVç&öÆÆÖVçE÷&WVW7G2FÖã¢7G"ÒFWVæG2&WV&UöFÖâÓâF7E·7G"ÂçÓ ¢BÒæ÷uöWö6¢vFF%ö6öææV7B26öæã ¢6öæâæWV7WFR¢%UDDRVç&öÆÆÖVçE÷&WVW7G24UB7FGW3ÒtU$TBrtU$R7FGW3ÒuTäDärräBW&W5öBÃÒò"À¢BÂÀ¢¢6öæâæWV7WFR¢%UDDRVç&öÆÆÖVçE÷&WVW7G24UB7FGW3ÒtU$TBrÂFWf6U÷6V7&WCÔåTÄÂtU$R7FGW3Òt$õdTBräBW&W5öBÃÒò"À¢BÂÀ¢¢&÷w2Ò6öæâæWV7WFR¢"" ¢4TÄT5BW"â¢ÂâææöFUöB2ÖF6VEöæöFUöBÂâææöFUöæÖR2ÖF6VEöæöFUöæÖP¢e$ôÒVç&öÆÆÖVçE÷&WVW7G2W ¢ÄTeB¤ôâæöFW2âôââæ&Gv&U÷VBÒW"æ&Gv&U÷V@¢tU$RW"ç7FGW2âuTäDärrÂt$õdTBr¢õ$DU"%44RW"ç7FGW2tTâuTäDärrDTâTÅ4RTäBÂW"ç&WVW7FVEöBDU40¢"" ¢æfWF6ÆÂ¢6öæâæ6öÖÖB¢&WGW&â²&ö²#¢G'VRÂ'&WVW7G2#¢¶Vç&öÆÆÖVçE÷&÷r&÷rf÷"&÷râ&÷w5×Ð  ¤ç÷7B"öFÖâöVç&öÆÆÖVçB÷·&WVW7EöGÒö&÷fR"¦7æ2FVbFÖåö&÷fUöVç&öÆÆÖVçB¢&WVW7EöC¢7G"À¢&WVW7C¢&WVW7BÀ¢FÖã¢7G"ÒFWVæG2&WV&UöFÖâÀ¢ÓâF7E·7G"ÂçÓ ¢G' ¢FFÒ§6öâæÆöG2vB&WVW7Bæ&öGæFV6öFR'WFbÓ"÷"'·Ò"¢W6WB§6öâä¥4ôäFV6öFTW'&÷# ¢&6REEW6WFöâ7FGW5ö6öFSÓCÂFWFÃÒ$&B¥4ôâ"¢&WGW&â&÷fUöVç&öÆÆÖVçB¢&WVW7EöBÀ¢7G"FFævWB'F&vWEöæöFUöB"÷"""ç7G&÷"æöæRÀ¢FÖâÀ¢&WVW7Bæ6ÆVçBæ÷7Bb&WVW7Bæ6ÆVçBVÇ6R""À¢  ¤ç÷7B"öFÖâöVç&öÆÆÖVçB÷·&WVW7EöGÒ÷&V¦V7B"¦FVbFÖå÷&V¦V7EöVç&öÆÆÖVçB¢&WVW7EöC¢7G"À¢&WVW7C¢&WVW7BÀ¢FÖã¢7G"ÒFWVæG2&WV&UöFÖâÀ¢ÓâF7E·7G"ÂçÓ ¢BÒæ÷uöWö6¢vFF%ö6öææV7B26öæã ¢&÷rÒ6öæâæWV7WFR¢%4TÄT5B&Gv&U÷VBÂ7FGW2e$ôÒVç&öÆÆÖVçE÷&WVW7G2tU$R&WVW7EöCÓò"À¢&WVW7EöBÂÀ¢æfWF6öæR¢bæ÷B&÷s ¢&6REEW6WFöâ7FGW5ö6öFSÓCBÂFWFÃÒ$Vç&öÆÆÖVçB&WVW7Bæ÷Bf÷VæB"¢b&÷u²'7FGW2%ÒÒ%TäDär# ¢&6REEW6WFöâ7FGW5ö6öFSÓCÂFWFÃÖb$Vç&öÆÆÖVçB&WVW7B2·&÷u²w7FGW2u×Ò"¢6öæâæWV7WFR¢%UDDRVç&öÆÆÖVçE÷&WVW7G24UB7FGW3Òu$T¤T5DTBrÂ&V¦V7FVEöCÓòÂFWf6U÷6V7&WCÔåTÄÂtU$R&WVW7EöCÓò"À¢BÂ&WVW7EöBÀ¢¢6öæâæ6öÖÖB¢VFB¢FÖâÀ¢'&V¦V7EöVç&öÆÆÖVçB"À¢&&Gv&R"À¢7G"&÷u²&&Gv&U÷VB%ÒÀ¢&WVW7Bæ6ÆVçBæ÷7Bb&WVW7Bæ6ÆVçBVÇ6R""À¢²'&WVW7EöB#¢&WVW7EöGÒÀ¢¢&WGW&â²&ö²#¢G'VRÂ'&WVW7EöB#¢&WVW7EöBÂ'7FGW2#¢%$T¤T5DTB'Ð  ¤ç÷7B"öFÖâ÷7F÷&vRö6ö×&W72"¦7æ2FVbFÖåö6ö×&W75÷&V6÷&Fæw2¢&WVW7C¢&WVW7BÀ¢FÖã¢7G"ÒFWVæG2&WV&UöFÖâÀ¢ÓâF7E·7G"ÂçÓ ¢G' ¢FFÒ§6öâæÆöG2vB&WVW7Bæ&öGæFV6öFR'WFbÓ"÷"'·Ò"¢W6WB§6öâä¥4ôäFV6öFTW'&÷# ¢&6REEW6WFöâ7FGW5ö6öFSÓCÂFWFÃÒ$&B¥4ôâ"¢ÆÖBÒÖÂÖâçBFFævWB&ÆÖB"÷"#RÂ¢f÷&6RÒ&ööÂFFævWB&f÷&6R"ÂfÇ6R¢&W7VÇBÒ&V6öæ6ÆUöfÆ5öfÆW2ÆÖCÖÆÖBÂf÷&6SÖf÷&6R¢VFB¢FÖâÀ¢&fÆ5÷&V6öæ6ÆFöâ"À¢'&V6÷&Fæw2"À¢&ÆÂ"À¢&WVW7Bæ6ÆVçBæ÷7Bb&WVW7Bæ6ÆVçBVÇ6R""À¢&W7VÇBÀ¢¢&WGW&â&W7VÇ@  ¤ç÷7B"öFÖâö6öÖÖæG2÷¶æöFUöGÒ÷¶6öÖÖæE÷GWÒ"¦FVbFÖå÷VWVUö6öÖÖæBæöFUöC¢7G"Â6öÖÖæE÷GS¢7G"Â&WVW7C¢&WVW7BÂFÖã¢7G"ÒFWVæG2&WV&UöFÖâÓâF7E·7G"ÂçÓ ¢BÒæ÷uöWö6¢ÆöBÒ·Ð¢vFF%ö6öææV7B26öæã ¢æöFRÒ6öæâæWV7WFR%4TÄT5BæöFUöBe$ôÒæöFW2tU$RæöFUöCÓò"ÂæöFUöBÂæfWF6öæR¢bæ÷BæöFS ¢&6REEW6WFöâ7FGW5ö6öFSÓCBÂFWFÃÒ%Væ¶æ÷vâæöFR"¢7W"Ò6öæâæWV7WFR¢"" ¢å4U%BåDò6öÖÖæG2æöFUöBÂ6öÖÖæE÷GRÂÆöEö§6öâÂ7FGW2Â7&VFVEöBÂW&W5öB¢dÅTU2òÂòÂòÂuTäDärrÂòÂò¢"""À¢æöFUöBÂ6öÖÖæE÷GRçWW"Â§6öâæGV×2ÆöBÂBÂB²#B¢3cÀ¢¢6öæâæ6öÖÖB¢6öÖÖæEöBÒ7W"æÆ7G&÷v@¢VFBFÖâÂ'VWVUö6öÖÖæB"Â&æöFR"ÂæöFUöBÂFWFÇ3×²&6öÖÖæE÷GR#¢6öÖÖæE÷GRÂ&6öÖÖæEöB#¢6öÖÖæEöGÒ¢&WGW&â²&ö²#¢G'VRÂ&6öÖÖæEöB#¢6öÖÖæEöGÐ  ¤ævWB"öF6&ö&B"Â&W7öç6Uö6Æ73ÔDÔÅ&W7öç6R¦FVbF6&ö&BFÖã¢7G"ÒFWVæG2&WV&UöFÖâÓâ7G# ¢BÒæ÷uöWö6¢vFF%ö6öææV7B26öæã ¢æöFW2Ò6öæâæWV7WFR¢"" ¢4TÄT5BâææöFUöBÂâææöFUöæÖRÂâæÆö6FöåöÆ&VÂÂ2â ¢e$ôÒæöFW2à¢ÄTeB¤ôâæöFU÷7FFR2ôâ2ææöFUöBÒâææöFUö@¢õ$DU"%âææöFUö@¢"" ¢æfWF6ÆÂ¢fÆW2Ò6öæâæWV7WFR¢"" ¢4TÄT5BæöFUöBÂfÆVæÖRÂ&V6÷&FVEöE÷&rÂfÆU÷6¦Uö'FW2ÂWÆöE÷7FGW2À¢ve÷'6U÷7FGW2ÂfÆ5÷7FGW2ÂFVÆWFU÷7FGW2Â'FW5÷&V6VfV@¢e$ôÒfÆW0¢õ$DU"%7&VFVEöBDU40¢ÄÔB ¢"" ¢æfWF6ÆÂ¢6öÖÖæG2Ò6öæâæWV7WFR¢"" ¢4TÄT5BBÂæöFUöBÂ6öÖÖæE÷GRÂ7FGW2Â7&VFVEöBÂFVÆfW&VEöBÂ6¶VEö@¢e$ôÒ6öÖÖæG0¢õ$DU"%7&VFVEöBDU40¢ÄÔBS ¢"" ¢æfWF6ÆÂ ¢FVbW62¢çÓâ7G# ¢b2æöæS ¢&WGW&â" ¢&WGW&â7G"ç&WÆ6R"b"Â"f×²"ç&WÆ6R#Â"Â"fÇC²"ç&WÆ6R#â"Â"fwC²" ¢æöFU÷&÷w2Ò" ¢f÷"ââæöFW3 ¢vRÒ&æWfW""bå²&Æ7E÷6VVâ%Ò2æöæRVÇ6Rb'·BÒçBå²vÆ7E÷6VVâuÒÒ2vò ¢æöFU÷&÷w2³Òb"" ¢ÇG#à¢ÇFCç¶W62å²væöFUöBuÒÓÂ÷FCãÇFCç¶W62å²væöFUöæÖRuÒÓÂ÷FCãÇFCç¶W62å²vÆö6FöåöÆ&VÂuÒÓÂ÷FCà¢ÇFCç¶vWÓÂ÷FCãÇFCç¶W62å²v&GFW'÷buÒÓÂ÷FCãÇFCç¶W62å²v&GFW'÷W&6VçBuÒÓÂ÷FCà¢ÇFCç¶W62å²v6&væruÒÓÂ÷FCãÇFCç¶W62å²w&V6÷&Fæu÷7FGW2uÒÓÂ÷FCãÇFCç¶W62å²wWÆöE÷7FGW2uÒÓÂ÷FCà¢ÇFCç¶W62å²w6Eög&VUöÖ"uÒÓÂ÷FCãÇFCç¶W62å²wvf÷'76öF&ÒuÒÓÂ÷FCà¢ÇFCãÆf÷&ÒÖWFöCÒ'÷7B"7FöãÒ"öFÖâö6öÖÖæG2÷¶W62å²væöFUöBuÒÒõär#ãÆ'WGFöãåVWVRæsÂö'WGFöããÂöf÷&ÓãÂ÷FCà¢Â÷G#à¢""  ¢fÆU÷&÷w2Ò" ¢f÷"bâfÆW3 ¢Ö"Ò""be²&fÆU÷6¦Uö'FW2%Ò2æöæRVÇ6Rb'¶çBe²vfÆU÷6¦Uö'FW2uÒóóó¢ã&gÒ ¢fÆU÷&÷w2³Òb"" ¢ÇG#ãÇFCç¶W62e²væöFUöBuÒÓÂ÷FCãÇFCç¶W62e²vfÆVæÖRuÒÓÂ÷FCãÇFCç¶W62e²w&V6÷&FVEöE÷&ruÒÓÂ÷FCà¢ÇFCç¶Ö'ÓÂ÷FCãÇFCç¶W62e²wWÆöE÷7FGW2uÒÓÂ÷FCãÇFCç¶W62e²v'FW5÷&V6VfVBuÒÓÂ÷FCà¢ÇFCç¶W62e²wve÷'6U÷7FGW2uÒÓÂ÷FCãÇFCç¶W62e²vfÆ5÷7FGW2uÒÓÂ÷FCãÇFCç¶W62e²vFVÆWFU÷7FGW2uÒÓÂ÷FCãÂ÷G#à¢""  ¢6ÖE÷&÷w2Ò" ¢f÷"2â6öÖÖæG3 ¢6ÖE÷&÷w2³Òb"" ¢ÇG#ãÇFCç¶W625²vBuÒÓÂ÷FCãÇFCç¶W625²væöFUöBuÒÓÂ÷FCãÇFCç¶W625²v6öÖÖæE÷GRuÒÓÂ÷FCà¢ÇFCç¶W625²w7FGW2uÒÓÂ÷FCãÇFCç¶W625²v7&VFVEöBuÒÓÂ÷FCãÇFCç¶W625²vFVÆfW&VEöBuÒÓÂ÷FCãÇFCç¶W625²v6¶VEöBuÒÓÂ÷FCãÂ÷G#à¢""  ¢&WGW&âb"" ¢ÂFö7GRFÖÃà¢ÆFÖÃãÆVCãÇFFÆSä&BæöFRF6&ö&CÂ÷FFÆSà¢ÆÖWFæÖSÒ'fWw÷'B"6öçFVçCÒ'vGFÖFWf6R×vGFÂæFÂ×66ÆSÓ#à¢Ç7GÆSà¢&öG·²föçBÖfÖÇ¢&ÂÂ6ç2×6W&c²&6¶w&÷VæC¢3²6öÆ÷#¢6VVS²FFæs£#²×Ð¢F&ÆR·²vGF£S²&÷&FW"Ö6öÆÆ6S¦6öÆÆ6S²Ö&vâÖ&÷GFöÓ£#²föçB×6¦S£G²×Ð¢FÂFB·²&÷&FW"Ö&÷GFöÓ£6öÆB3333²FFæs£g²FWBÖÆvã¦ÆVgC²×Ð¢'WGFöâ·²FFæs£g²×Ð¢ç6ÖÆÂ·²6öÆ÷#¢6²×Ð¢Â÷7GÆSãÂöVCãÆ&öGà¢Æä&BæöFRF6&ö&CÂöà¢Ç6Æ73Ò'6ÖÆÂ#å6W'fW"FÖS¢·GÓÂ÷à ¢Æ#äæöFW3Âö#à¢ÇF&ÆSãÇG#ãÇFäCÂ÷FãÇFäæÖSÂ÷FãÇFäÆö6FöãÂ÷FãÇFäÆ7B6VVãÂ÷FãÇFä&GFW'cÂ÷FãÇFä&GFW'SÂ÷FãÇFä6&væsÂ÷FãÇFå&V6÷&FæsÂ÷FãÇFåWÆöCÂ÷FãÇFå4BÔ#Â÷FãÇFå%54Â÷FãÇFä7FöãÂ÷FãÂ÷G#ç¶æöFU÷&÷w7ÓÂ÷F&ÆSà ¢Æ#å&V6VçBfÆW3Âö#à¢ÇF&ÆSãÇG#ãÇFäæöFSÂ÷FãÇFäfÆVæÖSÂ÷FãÇFå&V6÷&FVCÂ÷FãÇFäÔ#Â÷FãÇFåWÆöCÂ÷FãÇFä'FW3Â÷FãÇFåtcÂ÷FãÇFädÄ3Â÷FãÇFäFVÆWFSÂ÷FãÂ÷G#ç¶fÆU÷&÷w7ÓÂ÷F&ÆSà ¢Æ#å&V6VçB6öÖÖæG3Âö#à¢ÇF&ÆSãÇG#ãÇFäCÂ÷FãÇFäæöFSÂ÷FãÇFåGSÂ÷FãÇFå7FGW3Â÷FãÇFä7&VFVCÂ÷FãÇFäFVÆfW&VCÂ÷FãÇFä6¶VCÂ÷FãÂ÷G#ç¶6ÖE÷&÷w7ÓÂ÷F&ÆSà¢Âö&öGãÂöFÖÃà¢""   ¤ævWB"ò"Â&W7öç6Uö6Æ73ÔDÔÅ&W7öç6R¦FVb&ö÷BÓâ7G# ¢&WGW&âsÆÖWFGGÖWVcÒ'&Vg&W6"6öçFVçCÒ#²W&ÃÒöF6&ö&B#âp