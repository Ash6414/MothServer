"""
Bat Node Database Dashboard

A local, app-like dashboard for the AudioMoth/ESP32 bat monitoring system.
It reads the same SQLite database used by bat_server.py and provides safer,
user-friendly access to nodes, telemetry, files, commands, deletion status,
and exports.

Run from this folder:
    streamlit run bat_dashboard_app.py

Environment variables:
    BAT_DB_PATH=/absolute/path/to/bat_nodes_v2.db
    MAPBOX_TOKEN=optional_token_for_mapbox_satellite
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
import json
import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests
import streamlit as st

try:
    import folium
    from streamlit_folium import st_folium
    FOLIUM_AVAILABLE = True
except Exception:
    FOLIUM_AVAILABLE = False


# ============================================================
# Configuration
# ============================================================

APP_TITLE = "Bat Node Dashboard"
DEFAULT_DB = Path(__file__).resolve().parents[1] / "server" / "bat_nodes_v2.db"
DB_PATH = Path(os.getenv("BAT_DB_PATH", str(DEFAULT_DB))).expanduser().resolve()
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "server" / "data"
DATA_DIR = Path(os.getenv("BAT_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser().resolve()
ONLINE_TIMEOUT_SECONDS = int(os.getenv("BAT_ONLINE_TIMEOUT_SECONDS", "900"))
SERVER_URL = os.getenv("BAT_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
SERVER_ADMIN_USER = os.getenv("DASHBOARD_USER", "admin")
SERVER_ADMIN_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "change-me-now")

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="B",
    layout="wide",
    initial_sidebar_state="expanded",
)

STATUS_COLORS = {
    "ONLINE": "green",
    "RECORDING": "purple",
    "UPLOADING": "blue",
    "CHARGING": "orange",
    "CRITICAL BATTERY": "red",
    "OFFLINE / ASLEEP": "gray",
}

PAGE_RENAMES = {
    "Nodes": "Fleet",
    "Files": "Recordings",
    "Commands": "Command Queue",
    "SD cleanup": "SD Cleanup",
    "Raw DB": "Raw Database",
}


# ============================================================
# Low-level database helpers
# ============================================================

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def table_exists(table_name: str) -> bool:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
    return row is not None


def table_columns(table_name: str) -> set[str]:
    if not table_exists(table_name):
        return set()
    with db_connect() as conn:
        return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def query_df(sql: str, params: Iterable[Any] = ()) -> pd.DataFrame:
    with db_connect() as conn:
        return pd.read_sql_query(sql, conn, params=list(params))


def admin_api(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.request(
        method,
        f"{SERVER_URL}{path}",
        json=payload,
        auth=(SERVER_ADMIN_USER, SERVER_ADMIN_PASSWORD),
        timeout=10,
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise RuntimeError(f"Server returned HTTP {response.status_code}: {detail}")
    return response.json()


def execute(sql: str, params: Iterable[Any] = ()) -> None:
    with db_connect() as conn:
        conn.execute(sql, tuple(params))
        conn.commit()


def resolve_data_path(path_value: Any) -> Path:
    if not path_value:
        raise ValueError("No stored file path")
    candidate = Path(str(path_value)).expanduser()
    if not candidate.is_absolute():
        candidate = DB_PATH.parent / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(DATA_DIR)
    except ValueError as exc:
        raise ValueError(f"Refusing path outside the server data directory: {candidate}") from exc
    return candidate


def database_backup_to(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(DB_PATH, timeout=10)
    destination = sqlite3.connect(path)
    try:
        source.execute("PRAGMA busy_timeout = 5000")
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return path


def database_backup_bytes() -> bytes:
    handle = tempfile.NamedTemporaryFile(prefix="bat-node-", suffix=".sqlite3", delete=False)
    temp_path = Path(handle.name)
    handle.close()
    try:
        database_backup_to(temp_path)
        return temp_path.read_bytes()
    finally:
        temp_path.unlink(missing_ok=True)


def create_safety_backup(label: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)
    path = DATA_DIR / "backups" / f"bat_nodes_{timestamp}_{safe_label}.sqlite3"
    sequence = 1
    while path.exists():
        path = DATA_DIR / "backups" / f"bat_nodes_{timestamp}_{safe_label}_{sequence}.sqlite3"
        sequence += 1
    return database_backup_to(path)


def delete_server_recording(file_id: int, reason: str = "Deleted from dashboard") -> dict[str, Any]:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT original_wav_path, flac_path FROM files WHERE id=?",
            (int(file_id),),
        ).fetchone()
        if not row:
            raise ValueError(f"Recording {file_id} was not found")

    deleted: list[str] = []
    errors: list[str] = []
    for path_value in (row["original_wav_path"], row["flac_path"]):
        if not path_value:
            continue
        try:
            path = resolve_data_path(path_value)
            if path.is_file():
                path.unlink()
                deleted.append(str(path))
        except (OSError, ValueError) as exc:
            errors.append(str(exc))

    with db_connect() as conn:
        conn.execute(
            """
            UPDATE files
            SET upload_status='DELETED_FROM_SERVER', original_wav_path=NULL, flac_path=NULL,
                server_deleted_at=?, server_delete_reason=?, updated_at=?
            WHERE id=?
            """,
            (now_epoch(), reason[:500], now_epoch(), int(file_id)),
        )
        conn.commit()
    return {"deleted": deleted, "errors": errors}


def _delete_existing_tables(conn: sqlite3.Connection, table_names: Iterable[str]) -> None:
    existing = {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    for table_name in table_names:
        if table_name in existing:
            conn.execute(f'DELETE FROM "{table_name}"')


def _clear_directory_contents(directory: Path) -> int:
    directory = directory.resolve()
    try:
        directory.relative_to(DATA_DIR)
    except ValueError as exc:
        raise ValueError(f"Refusing to clear path outside data directory: {directory}") from exc
    if directory == DATA_DIR or not directory.exists():
        return 0
    removed = 0
    for child in directory.iterdir():
        if child.is_dir() and not child.is_symlink():
            removed += sum(1 for item in child.rglob("*") if item.is_file())
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)
            removed += 1
    return removed


def clear_operational_history() -> dict[str, Any]:
    backup = create_safety_backup("before_history_clear")
    with db_connect() as conn:
        _delete_existing_tables(
            conn,
            ["auth_nonces", "telemetry", "heartbeats", "commands", "time_checks", "node_errors", "audit_log", "node_state"],
        )
        conn.commit()
    return {"backup": str(backup)}


def clear_recording_storage() -> dict[str, Any]:
    backup = create_safety_backup("before_recording_clear")
    removed = 0
    for folder in ("incoming", "original_wav", "flac", "uploads"):
        removed += _clear_directory_contents(DATA_DIR / folder)
    with db_connect() as conn:
        _delete_existing_tables(
            conn,
            [
                "esp32_upload_chunks", "esp32_upload_sessions", "upload_chunks", "upload_sessions",
                "sd_deletion_log", "delete_authorizations", "recordings", "files", "manifests",
            ],
        )
        conn.commit()
    return {"backup": str(backup), "removed_files": removed}


def factory_clear_server() -> dict[str, Any]:
    backup = create_safety_backup("before_server_reset")
    removed = 0
    for folder in ("incoming", "original_wav", "flac", "uploads"):
        removed += _clear_directory_contents(DATA_DIR / folder)
    with db_connect() as conn:
        _delete_existing_tables(
            conn,
            [
                "esp32_upload_chunks", "esp32_upload_sessions", "upload_chunks", "upload_sessions",
                "sd_deletion_log", "delete_authorizations", "recordings", "files", "manifests",
                "auth_nonces", "telemetry", "heartbeats", "commands", "time_checks", "node_errors",
                "audit_log", "node_state", "enrollment_requests", "node_credentials", "nodes",
            ],
        )
        conn.commit()
    return {"backup": str(backup), "removed_files": removed}


def now_epoch() -> int:
    return int(time.time())


def fmt_age(epoch_value: Any) -> str:
    if epoch_value is None or pd.isna(epoch_value):
        return "never"
    try:
        seconds = max(0, now_epoch() - int(epoch_value))
    except Exception:
        return "unknown"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def file_size_mb(value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    try:
        return round(float(value) / 1_000_000, 2)
    except Exception:
        return None


def status_badge_text(row: pd.Series) -> str:
    last_seen = row.get("last_seen")
    battery_v = row.get("battery_v")
    charging = row.get("charging")
    recording = str(row.get("recording_status") or "").upper()
    uploading = str(row.get("upload_status") or "").upper()

    online = False
    if last_seen is not None and not pd.isna(last_seen):
        online = (now_epoch() - int(last_seen)) <= ONLINE_TIMEOUT_SECONDS

    if battery_v is not None and not pd.isna(battery_v) and float(battery_v) < 3.50:
        return "CRITICAL BATTERY"
    if not online:
        return "OFFLINE / ASLEEP"
    if "RECORD" in recording:
        return "RECORDING"
    if "UPLOAD" in uploading:
        return "UPLOADING"
    if charging in (1, True, "1", "true", "True"):
        return "CHARGING"
    return "ONLINE"


def require_db() -> bool:
    if not DB_PATH.exists():
        st.error(f"Database not found: {DB_PATH}")
        st.info("Start the FastAPI server once, or set BAT_DB_PATH to the correct bat_nodes_v2.db path.")
        return False
    required = ["nodes", "node_state", "telemetry", "files", "commands"]
    missing = [name for name in required if not table_exists(name)]
    if missing:
        st.error(f"Database is missing expected tables: {', '.join(missing)}")
        return False
    return True


# ============================================================
# Data access
# ============================================================

def load_nodes() -> pd.DataFrame:
    hardware_column = "n.hardware_uid" if "hardware_uid" in table_columns("nodes") else "NULL AS hardware_uid"
    df = query_df(
        f"""
        SELECT
            n.node_id,
            {hardware_column},
            n.node_name,
            n.location_lat,
            n.location_lon,
            n.location_label,
            n.deployment_notes,
            n.firmware_version,
            n.hardware_version,
            n.active,
            n.compromised,
            s.last_seen,
            s.battery_v,
            s.battery_percent,
            s.charging,
            s.charge_done,
            s.recently_charged,
            s.sd_free_mb,
            s.recording_status,
            s.upload_status,
            s.wifi_rssi_dbm,
            s.mode,
            s.message,
            s.updated_at
        FROM nodes n
        LEFT JOIN node_state s ON s.node_id = n.node_id
        ORDER BY n.node_id
        """
    )
    if not df.empty:
        df["last_seen_age"] = df["last_seen"].apply(fmt_age)
        df["status"] = df.apply(status_badge_text, axis=1)
    return df


def load_files(limit: int = 1000) -> pd.DataFrame:
    df = query_df(
        """
        SELECT
            f.id,
            f.node_id,
            n.node_name,
            n.location_label,
            f.deployment_id,
            f.manifest_id,
            f.local_file_id,
            f.filename,
            f.canonical_name,
            f.recorded_at_raw,
            f.recorded_at_corrected,
            f.recorded_at_utc,
            f.recorded_at_source,
            f.recording_lat,
            f.recording_lon,
            f.recording_location_label,
            f.duration_seconds,
            f.sample_rate,
            f.channels,
            f.bit_depth,
            f.file_size_bytes,
            f.upload_status,
            f.bytes_received,
            f.server_sha256,
            f.wav_parse_status,
            f.flac_status,
            f.backup_status,
            f.weather_status,
            f.original_wav_path,
            f.flac_path,
            f.delete_status,
            f.delete_authorization_id,
            f.delete_authorized_at,
            f.delete_requested_at,
            f.delete_confirmed_at,
            f.delete_error,
            f.server_deleted_at,
            f.server_delete_reason,
            f.created_at,
            f.updated_at
        FROM files f
        LEFT JOIN nodes n ON n.node_id = f.node_id
        ORDER BY f.created_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    if not df.empty:
        df["file_size_mb"] = df["file_size_bytes"].apply(file_size_mb)
        df["received_mb"] = df["bytes_received"].apply(file_size_mb)
        df["created_age"] = df["created_at"].apply(fmt_age)
        df["recorded_time"] = pd.to_datetime(df["recorded_at_utc"], unit="s", utc=True, errors="coerce")
        df["server_deleted_time"] = pd.to_datetime(df["server_deleted_at"], unit="s", utc=True, errors="coerce")
        df["display_name"] = df["canonical_name"].fillna(df["filename"])
    return df


def load_commands(limit: int = 500) -> pd.DataFrame:
    df = query_df(
        """
        SELECT
            c.id,
            c.node_id,
            n.node_name,
            c.command_type,
            c.payload_json,
            c.status,
            c.created_at,
            c.delivered_at,
            c.acked_at,
            c.expires_at,
            c.response_json
        FROM commands c
        LEFT JOIN nodes n ON n.node_id = c.node_id
        ORDER BY c.created_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    if not df.empty:
        df["created_age"] = df["created_at"].apply(fmt_age)
        df["delivered_age"] = df["delivered_at"].apply(fmt_age)
        df["acked_age"] = df["acked_at"].apply(fmt_age)
    return df


def load_telemetry(node_id: str | None, limit: int = 2000) -> pd.DataFrame:
    if node_id and node_id != "All":
        return query_df(
            """
            SELECT * FROM telemetry
            WHERE node_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (node_id, limit),
        )
    return query_df(
        """
        SELECT * FROM telemetry
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    )


def load_time_checks(limit: int = 500) -> pd.DataFrame:
    if not table_exists("time_checks"):
        return pd.DataFrame()
    return query_df(
        """
        SELECT * FROM time_checks
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    )


def load_errors(limit: int = 500) -> pd.DataFrame:
    if not table_exists("node_errors"):
        return pd.DataFrame()
    return query_df(
        """
        SELECT * FROM node_errors
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    )


def queue_command(node_id: str, command_type: str, payload: dict[str, Any] | None = None) -> int:
    t = now_epoch()
    payload_json = json.dumps(payload or {})
    with db_connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO commands (node_id, command_type, payload_json, status, created_at, expires_at)
            VALUES (?, ?, ?, 'PENDING', ?, ?)
            """,
            (node_id, command_type.upper(), payload_json, t, t + 24 * 3600),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_node_metadata(node_id: str, node_name: str, location_label: str, lat: Any, lon: Any, notes: str) -> None:
    t = now_epoch()
    lat_value = None if lat in (None, "") else float(lat)
    lon_value = None if lon in (None, "") else float(lon)
    execute(
        """
        UPDATE nodes
        SET node_name=?, location_label=?, location_lat=?, location_lon=?, deployment_notes=?, updated_at=?
        WHERE node_id=?
        """,
        (node_name, location_label, lat_value, lon_value, notes, t, node_id),
    )


# ============================================================
# UI helpers
# ============================================================

def metric_row(nodes: pd.DataFrame, files: pd.DataFrame, commands: pd.DataFrame) -> None:
    now = now_epoch()
    if nodes.empty:
        online = low_battery = recording = uploading = 0
    else:
        online = int(nodes["last_seen"].fillna(0).apply(lambda x: (now - int(x)) <= ONLINE_TIMEOUT_SECONDS if x else False).sum())
        low_battery = int(nodes["battery_v"].fillna(99).apply(lambda x: float(x) < 3.50).sum())
        recording = int(nodes["recording_status"].fillna("").str.upper().str.contains("RECORD").sum())
        uploading = int(nodes["upload_status"].fillna("").str.upper().str.contains("UPLOAD").sum())

    pending_files = 0 if files.empty else int(files["upload_status"].fillna("").isin(["ON_SD_ONLY", "PARTIAL", "UPLOADING"]).sum())
    delete_ready = 0 if files.empty else int(files["delete_status"].fillna("").isin(["SAFE_TO_DELETE", "DELETE_AUTHORIZED"]).sum())
    pending_cmds = 0 if commands.empty else int(commands["status"].fillna("").eq("PENDING").sum())

    cols = st.columns(6)
    cols[0].metric("Nodes", len(nodes))
    cols[1].metric("Online", online)
    cols[2].metric("Low battery", low_battery)
    cols[3].metric("Recording", recording)
    cols[4].metric("Uploading", uploading)
    cols[5].metric("Pending", pending_files, help=f"Files pending transfer: {pending_files}. Commands pending: {pending_cmds}. Delete-ready files: {delete_ready}.")


def apply_node_filters(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    with st.sidebar:
        st.subheader("Filters")
        text = pref_text_input("Search node/location/status", "overview.search", "")
        status_values = sorted([x for x in df["status"].dropna().unique().tolist()])
        selected_status = pref_multiselect("Node status", "overview.status", status_values, default=[])
        show_inactive = pref_checkbox("Show inactive nodes", "overview.show_inactive", True)
        low_battery_only = pref_checkbox("Low battery only", "overview.low_battery_only", False)

    out = df.copy()
    if text:
        hay = out.fillna("").astype(str).agg(" ".join, axis=1).str.lower()
        out = out[hay.str.contains(text.lower(), regex=False)]
    if selected_status:
        out = out[out["status"].isin(selected_status)]
    if not show_inactive and "active" in out:
        out = out[out["active"] == 1]
    if low_battery_only:
        out = out[out["battery_v"].fillna(99).astype(float) < 3.50]
    return out


def filter_files(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        text = pref_text_input("Search files", "files.search", "")
    with c2:
        node_options = ["All"] + sorted(df["node_id"].dropna().unique().tolist())
        node = pref_selectbox("Node", "files.node", node_options, default="All")
    with c3:
        upload_options = ["All"] + sorted(df["upload_status"].fillna("unknown").unique().tolist())
        upload = pref_selectbox("Upload status", "files.upload_status", upload_options, default="All")
    with c4:
        delete_options = ["All"] + sorted(df["delete_status"].fillna("unknown").unique().tolist())
        delete = pref_selectbox("Delete status", "files.delete_status", delete_options, default="All")

    out = df.copy()
    if text:
        hay = out.fillna("").astype(str).agg(" ".join, axis=1).str.lower()
        out = out[hay.str.contains(text.lower(), regex=False)]
    if node != "All":
        out = out[out["node_id"] == node]
    if upload != "All":
        out = out[out["upload_status"].fillna("unknown") == upload]
    if delete != "All":
        out = out[out["delete_status"].fillna("unknown") == delete]
    return out


def downloadable_csv(df: pd.DataFrame, label: str, filename: str) -> None:
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button(label, data=csv, file_name=filename, mime="text/csv")


def inject_theme() -> None:
    st.markdown(
        """
        <style>
        :root {
            --bat-panel: rgba(127, 127, 127, 0.08);
            --bat-panel-strong: rgba(127, 127, 127, 0.12);
            --bat-border: rgba(127, 127, 127, 0.26);
            --bat-shadow: rgba(0, 0, 0, 0.06);
            --bat-accent: #2c6f5b;
            --bat-accent-hover: #245d4c;
            --bat-accent-text: #ffffff;
        }

        @media (prefers-color-scheme: dark) {
            :root {
                --bat-panel: rgba(255, 255, 255, 0.055);
                --bat-panel-strong: rgba(255, 255, 255, 0.09);
                --bat-border: rgba(255, 255, 255, 0.16);
                --bat-shadow: rgba(0, 0, 0, 0.22);
                --bat-accent: #42a889;
                --bat-accent-hover: #57bd9e;
                --bat-accent-text: #071612;
            }
        }

        .block-container {
            max-width: 1520px;
            padding-top: 1.35rem;
            padding-bottom: 2.5rem;
        }

        [data-testid="stSidebar"] {
            border-right: 1px solid var(--bat-border);
        }

        [data-testid="stHeader"] {
            background: transparent;
        }

        [data-testid="stToolbar"] {
            opacity: 0.72;
        }

        h1 {
            letter-spacing: 0;
            font-size: 2.15rem;
            line-height: 1.1;
            margin-bottom: 0.35rem;
        }

        h2, h3 {
            letter-spacing: 0;
        }

        div[data-testid="stMetric"] {
            background: var(--bat-panel);
            border: 1px solid var(--bat-border);
            border-radius: 8px;
            padding: 0.85rem 0.95rem;
            box-shadow: 0 1px 2px var(--bat-shadow);
        }

        [data-testid="stMetricLabel"] {
            font-size: 0.78rem;
            opacity: 0.72;
        }

        [data-testid="stMetricLabel"] *,
        [data-testid="stMetricDelta"] * {
            opacity: 0.92;
        }

        [data-testid="stMetricValue"] {
            font-size: 1.55rem;
        }

        div[data-testid="stDataFrame"] {
            border: 1px solid var(--bat-border);
            border-radius: 8px;
            overflow: hidden;
        }

        .bat-kicker {
            color: var(--bat-accent);
            font-size: 0.78rem;
            font-weight: 700;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            margin-bottom: 0.25rem;
        }

        .bat-subtitle {
            opacity: 0.72;
            max-width: 920px;
            margin: 0 0 1.15rem 0;
        }

        .bat-status-strip {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            margin: 0.25rem 0 1rem 0;
        }

        .bat-pill {
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            border: 1px solid var(--bat-border);
            border-radius: 999px;
            background: var(--bat-panel);
            color: inherit;
            padding: 0.28rem 0.62rem;
            font-size: 0.82rem;
            line-height: 1.2;
            white-space: nowrap;
        }

        .bat-dot {
            width: 0.52rem;
            height: 0.52rem;
            border-radius: 99px;
            display: inline-block;
            background: currentColor;
            opacity: 0.62;
        }

        .bat-dot.green { background: #237a57; }
        .bat-dot.blue { background: #315f9f; }
        .bat-dot.purple { background: #7351a5; }
        .bat-dot.orange { background: #b46528; }
        .bat-dot.red { background: #ba3a3a; }
        .bat-dot.gray { background: #778079; }

        .stButton > button,
        .stDownloadButton > button,
        button[kind="primary"] {
            border-radius: 8px;
            border-color: var(--bat-border);
        }

        .stButton > button[kind="primary"] {
            background: var(--bat-accent);
            border-color: var(--bat-accent);
            color: var(--bat-accent-text);
        }

        .stButton > button[kind="primary"]:hover {
            background: var(--bat-accent-hover);
            border-color: var(--bat-accent-hover);
        }

        .stButton > button[kind="primary"] p {
            color: var(--bat-accent-text);
        }

        hr {
            border-color: var(--bat-border);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def page_heading(title: str, subtitle: str | None = None, kicker: str = "Bat Node Operations") -> None:
    st.markdown(f"<div class='bat-kicker'>{html.escape(kicker)}</div>", unsafe_allow_html=True)
    st.header(title)
    if subtitle:
        st.markdown(f"<p class='bat-subtitle'>{html.escape(subtitle)}</p>", unsafe_allow_html=True)


def status_strip(nodes: pd.DataFrame) -> None:
    if nodes.empty or "status" not in nodes.columns:
        return
    counts = nodes["status"].fillna("unknown").value_counts().to_dict()
    pills = []
    for status, count in counts.items():
        color = STATUS_COLORS.get(str(status), "gray")
        safe_status = html.escape(str(status).title())
        pills.append(
            f"<span class='bat-pill'><span class='bat-dot {color}'></span>{safe_status}: {int(count)}</span>"
        )
    st.markdown(f"<div class='bat-status-strip'>{''.join(pills)}</div>", unsafe_allow_html=True)


def friendly_page_name(page: str) -> str:
    return PAGE_RENAMES.get(page, page)


def fmt_value(value: Any, suffix: str = "", decimals: int = 1, empty: str = "-") -> str:
    if value is None or pd.isna(value):
        return empty
    try:
        number = float(value)
    except Exception:
        return str(value)
    return f"{number:.{decimals}f}{suffix}"


def add_upload_progress(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "file_size_bytes" not in df.columns or "bytes_received" not in df.columns:
        return df
    out = df.copy()
    sizes = pd.to_numeric(out["file_size_bytes"], errors="coerce").fillna(0)
    received = pd.to_numeric(out["bytes_received"], errors="coerce").fillna(0)
    out["upload_progress"] = [
        0 if size <= 0 else min(100, round((got / size) * 100, 1))
        for got, size in zip(received, sizes)
    ]
    return out


# ============================================================
# Persistent dashboard preferences
# ============================================================

PREF_SCOPE = os.getenv("BAT_DASHBOARD_PREF_SCOPE", "default")
PREF_TABLE = "dashboard_preferences"


def prefs_available() -> bool:
    return DB_PATH.exists()


def ensure_preferences_table() -> None:
    if not prefs_available():
        return
    with db_connect() as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {PREF_TABLE} (
                scope TEXT NOT NULL,
                pref_key TEXT NOT NULL,
                pref_value TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (scope, pref_key)
            )
            """
        )
        conn.commit()


def get_pref(pref_key: str, default: Any) -> Any:
    if not prefs_available():
        return default
    try:
        ensure_preferences_table()
        with db_connect() as conn:
            row = conn.execute(
                f"SELECT pref_value FROM {PREF_TABLE} WHERE scope=? AND pref_key=?",
                (PREF_SCOPE, pref_key),
            ).fetchone()
        if row is None:
            return default
        return json.loads(row["pref_value"])
    except Exception:
        return default


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def set_pref(pref_key: str, value: Any) -> None:
    if not prefs_available():
        return
    try:
        ensure_preferences_table()
        with db_connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {PREF_TABLE} (scope, pref_key, pref_value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope, pref_key) DO UPDATE SET
                    pref_value=excluded.pref_value,
                    updated_at=excluded.updated_at
                """,
                (PREF_SCOPE, pref_key, json.dumps(json_safe(value)), now_epoch()),
            )
            conn.commit()
    except Exception:
        # Preferences are convenience state. Never break the dashboard if saving fails.
        pass


def clear_preferences() -> None:
    if prefs_available():
        try:
            ensure_preferences_table()
            with db_connect() as conn:
                conn.execute(f"DELETE FROM {PREF_TABLE} WHERE scope=?", (PREF_SCOPE,))
                conn.commit()
        except Exception:
            pass
    for key in list(st.session_state.keys()):
        if str(key).startswith("pref::"):
            del st.session_state[key]


def pref_session_key(pref_key: str) -> str:
    return f"pref::{pref_key}"


def is_advanced_mode() -> bool:
    return bool(st.session_state.get(pref_session_key("main.advanced_mode"), False))


def init_pref_state(pref_key: str, default: Any, valid_options: list[Any] | None = None, multiple: bool = False) -> str:
    ss_key = pref_session_key(pref_key)
    if ss_key not in st.session_state:
        value = get_pref(pref_key, default)
        if multiple:
            if not isinstance(value, list):
                value = list(default or [])
            if valid_options is not None:
                value = [v for v in value if v in valid_options]
        elif valid_options is not None and value not in valid_options:
            value = default
        st.session_state[ss_key] = value
    else:
        if multiple and valid_options is not None:
            value = st.session_state[ss_key]
            if not isinstance(value, list):
                value = list(default or [])
            st.session_state[ss_key] = [v for v in value if v in valid_options]
        elif valid_options is not None and st.session_state[ss_key] not in valid_options:
            st.session_state[ss_key] = default
    return ss_key


def pref_checkbox(label: str, pref_key: str, default: bool = False, **kwargs: Any) -> bool:
    ss_key = init_pref_state(pref_key, bool(default))
    value = st.checkbox(label, key=ss_key, **kwargs)
    set_pref(pref_key, bool(value))
    return bool(value)


def pref_text_input(label: str, pref_key: str, default: str = "", **kwargs: Any) -> str:
    ss_key = init_pref_state(pref_key, str(default))
    value = st.text_input(label, key=ss_key, **kwargs)
    set_pref(pref_key, value)
    return value


def pref_slider(label: str, pref_key: str, min_value: int, max_value: int, default: int, **kwargs: Any) -> int:
    saved = get_pref(pref_key, default)
    try:
        saved = int(saved)
    except Exception:
        saved = default
    saved = max(min_value, min(max_value, saved))
    ss_key = init_pref_state(pref_key, saved)
    value = st.slider(label, min_value, max_value, key=ss_key, **kwargs)
    set_pref(pref_key, int(value))
    return int(value)


def pref_selectbox(
    label: str,
    pref_key: str,
    options: list[Any],
    default: Any | None = None,
    **kwargs: Any,
) -> Any:
    if not options:
        return None
    if default is None or default not in options:
        default = options[0]
    ss_key = init_pref_state(pref_key, default, valid_options=options)
    value = st.selectbox(label, options, key=ss_key, **kwargs)
    set_pref(pref_key, value)
    return value


def pref_multiselect(
    label: str,
    pref_key: str,
    options: list[Any],
    default: list[Any] | None = None,
    **kwargs: Any,
) -> list[Any]:
    default = default or []
    ss_key = init_pref_state(pref_key, default, valid_options=options, multiple=True)
    value = st.multiselect(label, options, key=ss_key, **kwargs)
    set_pref(pref_key, list(value))
    return list(value)


def pref_radio(label: str, pref_key: str, options: list[Any], default: Any | None = None, **kwargs: Any) -> Any:
    if not options:
        return None
    if default is None or default not in options:
        default = options[0]
    ss_key = init_pref_state(pref_key, default, valid_options=options)
    value = st.radio(label, options, key=ss_key, **kwargs)
    set_pref(pref_key, value)
    return value


# ============================================================
# Pages
# ============================================================

def page_overview(nodes: pd.DataFrame, files: pd.DataFrame, commands: pd.DataFrame) -> None:
    page_heading("Overview", "Current fleet health, transfer status, and items that need attention.")
    metric_row(nodes, files, commands)
    status_strip(nodes)

    st.subheader("Fleet health")
    filtered = apply_node_filters(nodes)
    display_cols = [
        "status", "node_id", "node_name", "location_label", "last_seen_age",
        "battery_v", "battery_percent", "sd_free_mb", "wifi_rssi_dbm",
        "recording_status", "upload_status", "message"
    ]
    existing = [c for c in display_cols if c in filtered.columns]
    st.dataframe(
        filtered[existing],
        width="stretch",
        hide_index=True,
        column_config={
            "status": "Status",
            "node_id": "Node ID",
            "node_name": "Name",
            "location_label": "Location",
            "last_seen_age": "Last seen",
            "battery_v": st.column_config.NumberColumn("Battery", format="%.2f V"),
            "battery_percent": st.column_config.ProgressColumn("Charge", min_value=0, max_value=100, format="%.0f%%"),
            "sd_free_mb": st.column_config.NumberColumn("SD free", format="%.1f MB"),
            "wifi_rssi_dbm": st.column_config.NumberColumn("Wi-Fi", format="%.0f dBm"),
            "recording_status": "Recorder",
            "upload_status": "Transfer",
            "message": "Latest message",
        },
    )
    downloadable_csv(filtered, "Export filtered nodes CSV", "nodes.csv")

    st.subheader("Transfer queue")
    if files.empty:
        st.info("No files in database yet.")
    else:
        attention = add_upload_progress(files[
            files["upload_status"].fillna("").isin(["ON_SD_ONLY", "PARTIAL", "UPLOADING", "FAILED"])
            | files["delete_status"].fillna("").isin(["DELETE_FAILED", "MANUAL_REVIEW"])
        ].head(50))
        cols = ["node_id", "filename", "file_size_mb", "received_mb", "upload_progress", "upload_status", "wav_parse_status", "delete_status", "delete_error"]
        cols = [c for c in cols if c in attention.columns]
        if attention.empty:
            st.success("No files need attention right now.")
        else:
            st.dataframe(
                attention[cols],
                width="stretch",
                hide_index=True,
                column_config={
                    "node_id": "Node",
                    "filename": "File",
                    "file_size_mb": st.column_config.NumberColumn("Size", format="%.2f MB"),
                    "received_mb": st.column_config.NumberColumn("Received", format="%.2f MB"),
                    "upload_progress": st.column_config.ProgressColumn("Progress", min_value=0, max_value=100, format="%.0f%%"),
                    "upload_status": "Transfer",
                    "wav_parse_status": "WAV check",
                    "delete_status": "SD Cleanup",
                    "delete_error": "Cleanup error",
                },
            )


def page_nodes(nodes: pd.DataFrame) -> None:
    page_heading("Fleet", "Edit node details, check state, and queue safe service commands.")
    if nodes.empty:
        st.info("No nodes exist yet. Use manage_node.py to create one.")
        return

    node_ids = nodes["node_id"].tolist()
    selected = pref_selectbox(
        "Select node",
        "nodes.selected_node",
        node_ids,
        default=node_ids[0],
        format_func=lambda x: f"{x} - {nodes.loc[nodes['node_id'] == x, 'node_name'].iloc[0]}",
    )
    row = nodes[nodes["node_id"] == selected].iloc[0]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Status", row.get("status", "unknown"))
    c2.metric("Last seen", row.get("last_seen_age", "never"))
    c3.metric("Battery", fmt_value(row.get("battery_v"), " V", 2))
    c4.metric("SD free", fmt_value(row.get("sd_free_mb"), " MB", 1))

    st.subheader("Node metadata")
    with st.form("node_edit_form"):
        node_name = st.text_input("Node name", value=str(row.get("node_name") or ""))
        location_label = st.text_input("Location label", value=str(row.get("location_label") or ""))
        col_lat, col_lon = st.columns(2)
        with col_lat:
            lat = st.text_input("Latitude", value="" if pd.isna(row.get("location_lat")) else str(row.get("location_lat")))
        with col_lon:
            lon = st.text_input("Longitude", value="" if pd.isna(row.get("location_lon")) else str(row.get("location_lon")))
        notes = st.text_area("Deployment notes", value=str(row.get("deployment_notes") or ""), height=120)
        saved = st.form_submit_button("Save metadata")
        if saved:
            try:
                update_node_metadata(selected, node_name, location_label, lat, lon, notes)
                st.success("Node metadata saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Save failed: {exc}")

    st.subheader("Node actions")
    st.caption("Commands are queued immediately. A sleeping node receives them the next time it wakes and checks in.")
    col_a, col_b, col_c, col_d, col_e, col_f, col_g = st.columns(7)
    with col_a:
        if st.button("Ping", width="stretch", type="primary", help="Ask the node to post a fresh heartbeat"):
            cid = queue_command(selected, "PING")
            st.success(f"Ping queued as command #{cid}.")
    with col_b:
        if st.button("Upload recordings", width="stretch", help="Start a transfer even when automatic charging policy would wait"):
            cid = queue_command(selected, "UPLOAD_NOW")
            st.success(f"Upload queued as command #{cid}.")
    with col_c:
        if st.button("Check bridge", width="stretch", help="Read current AudioMoth bridge status"):
            cid = queue_command(selected, "MOTH_STATUS")
            st.success(f"Bridge check queued as command #{cid}.")
    with col_d:
        if st.button("List SD", width="stretch", help="List AudioMoth SD card files and free space"):
            cid = queue_command(selected, "MOTH_LIST")
            st.success(f"SD list queued as command #{cid}.")
    with col_e:
        if st.button("Test UART", width="stretch", help="Measure the 921600-baud AudioMoth-to-ESP stream path"):
            cid = queue_command(selected, "MOTH_TEST_STREAM")
            st.success(f"UART test queued as command #{cid}.")
    with col_f:
        if st.button("Sync clock", width="stretch", help="Send current server time to AudioMoth"):
            cid = queue_command(selected, "SYNC_MOTH_TIME")
            st.success(f"Clock sync queued as command #{cid}.")
    with col_g:
        if st.button("Change network", width="stretch", help="Restart into the local Wi-Fi setup portal"):
            cid = queue_command(selected, "OPEN_SETUP")
            st.success(f"Setup restart queued as command #{cid}.")

    recent_commands = load_commands(100)
    if not recent_commands.empty:
        recent_commands = recent_commands[recent_commands["node_id"] == selected].head(8)
        if not recent_commands.empty:
            st.dataframe(
                recent_commands[["id", "command_type", "status", "created_age", "acked_age", "response_json"]],
                width="stretch",
                hide_index=True,
                column_config={
                    "id": "ID",
                    "command_type": "Action",
                    "status": "State",
                    "created_age": "Queued",
                    "acked_age": "Acknowledged",
                    "response_json": "Result",
                },
            )

    if is_advanced_mode():
        st.divider()
        custom = st.text_input("Custom command type", placeholder="Example: FORCE_MANIFEST")
        if st.button("Queue custom command", disabled=not bool(custom)):
            cid = queue_command(selected, custom)
            st.success(f"Queued {custom.upper()} command #{cid}.")

        with st.expander("Raw current state"):
            st.json(row.dropna().to_dict())


def page_enrollment(nodes: pd.DataFrame) -> None:
    page_heading("Add Nodes", "Approve new or reflashed ESP32 nodes without copying tokens or device secrets.")
    st.info(
        "Start setup on the ESP32, connect it to field Wi-Fi, and submit its enrollment request. "
        "It will appear here for approval."
    )
    try:
        payload = admin_api("GET", "/admin/enrollment/requests")
    except Exception as exc:
        st.error(f"Could not reach the local server enrollment API: {exc}")
        st.caption(f"Server API: {SERVER_URL}")
        return

    requests_data = payload.get("requests") or []
    pending = [item for item in requests_data if item.get("status") == "PENDING"]
    approved = [item for item in requests_data if item.get("status") == "APPROVED"]
    node_ids = nodes["node_id"].tolist() if not nodes.empty else []

    if not pending:
        st.success("No nodes are waiting for approval.")
    for item in pending:
        request_id = str(item["request_id"])
        hardware_uid = str(item.get("hardware_uid") or "")
        matched_node_id = item.get("matched_node_id")
        with st.container(border=True):
            title = str(item.get("requested_node_name") or f"Bat Node {hardware_uid[-6:]}")
            st.subheader(title)
            c1, c2, c3 = st.columns(3)
            c1.metric("Hardware ID", hardware_uid)
            c2.metric("Requested", fmt_age(item.get("requested_at")))
            c3.metric("Firmware", str(item.get("firmware_version") or "unknown"))

            options = ["Create a new node"] + node_ids
            default_option = matched_node_id if matched_node_id in node_ids else "Create a new node"
            target = st.selectbox(
                "Keep existing identity",
                options,
                index=options.index(default_option),
                key=f"enrollment-target-{request_id}",
                format_func=lambda value: (
                    "Create a new node ID"
                    if value == "Create a new node"
                    else f"{value} - preserve this node's history"
                ),
                help="Choose the old node after a reflash. Future reflashes will match automatically by hardware ID.",
            )
            if matched_node_id:
                st.caption(f"Recognized hardware: this request is already linked to {matched_node_id}.")

            approve_col, reject_col, _ = st.columns([1, 1, 2])
            with approve_col:
                if st.button("Approve", key=f"approve-{request_id}", type="primary", width="stretch"):
                    try:
                        result = admin_api(
                            "POST",
                            f"/admin/enrollment/{request_id}/approve",
                            {"target_node_id": "" if target == "Create a new node" else target},
                        )
                        st.success(f"Approved as {result['node_id']}. The ESP32 can now finish setup.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Approval failed: {exc}")
            with reject_col:
                if st.button("Reject", key=f"reject-{request_id}", width="stretch"):
                    try:
                        admin_api("POST", f"/admin/enrollment/{request_id}/reject", {})
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Rejection failed: {exc}")

    if approved:
        st.subheader("Waiting for pickup")
        st.caption("These nodes are approved and will save credentials as soon as their setup page polls again.")
        st.dataframe(
            pd.DataFrame(approved)[["node_id", "requested_node_name", "hardware_uid", "approved_at", "delivered_at"]],
            width="stretch",
            hide_index=True,
            column_config={
                "node_id": "Node",
                "requested_node_name": "Name",
                "hardware_uid": "Hardware ID",
                "approved_at": "Approved epoch",
                "delivered_at": "Credential pickup epoch",
            },
        )


def page_map(nodes: pd.DataFrame) -> None:
    page_heading("Map", "Live node locations with status-aware markers.")
    mapped = nodes.dropna(subset=["location_lat", "location_lon"]).copy()
    if not mapped.empty:
        mapped["location_lat"] = pd.to_numeric(mapped["location_lat"], errors="coerce")
        mapped["location_lon"] = pd.to_numeric(mapped["location_lon"], errors="coerce")
        mapped = mapped.dropna(subset=["location_lat", "location_lon"])
        mapped = mapped[
            mapped["location_lat"].between(-90, 90)
            & mapped["location_lon"].between(-180, 180)
        ].copy()

    if mapped.empty:
        st.info("No node coordinates saved yet. Add latitude and longitude in the Fleet page.")
        return

    if FOLIUM_AVAILABLE:
        center = [float(mapped["location_lat"].mean()), float(mapped["location_lon"].mean())]
        zoom_start = 13 if len(mapped) == 1 else 11
        m = folium.Map(location=center, zoom_start=zoom_start, tiles=None, prefer_canvas=True)
        folium.TileLayer(
            tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            attr="Esri World Imagery",
            name="Satellite",
            overlay=False,
            control=True,
        ).add_to(m)
        folium.TileLayer("OpenStreetMap", name="Street map", control=True).add_to(m)

        for _, r in mapped.iterrows():
            status = str(r.get("status") or "unknown")
            color = "green"
            if status == "CRITICAL BATTERY":
                color = "red"
            elif status == "OFFLINE / ASLEEP":
                color = "gray"
            elif status == "RECORDING":
                color = "purple"
            elif status == "UPLOADING":
                color = "blue"
            elif status == "CHARGING":
                color = "orange"
            node_label = html.escape(str(r.get("node_name") or r.get("node_id") or "Node"))
            node_id = html.escape(str(r.get("node_id") or ""))
            location_label = html.escape(str(r.get("location_label") or ""))
            battery = "" if pd.isna(r.get("battery_v")) else html.escape(str(r.get("battery_v")))
            last_seen = html.escape(str(r.get("last_seen_age") or ""))
            safe_status = html.escape(status)
            popup = (
                f"<b>{node_label}</b><br>"
                f"ID: {node_id}<br>"
                f"Status: {safe_status}<br>"
                f"Battery: {battery} V<br>"
                f"Last seen: {last_seen}<br>"
                f"Location: {location_label}"
            )
            folium.Marker(
                location=[float(r["location_lat"]), float(r["location_lon"])],
                popup=popup,
                tooltip=f"{node_label} - {safe_status}",
                icon=folium.Icon(color=color, icon="info-sign"),
            ).add_to(m)
        if len(mapped) > 1:
            bounds = mapped[["location_lat", "location_lon"]].astype(float).values.tolist()
            m.fit_bounds(bounds, padding=(30, 30))
        folium.LayerControl(collapsed=False).add_to(m)
        st_folium(
            m,
            key="bat_node_map",
            height=650,
            use_container_width=True,
            returned_objects=[],
        )
    else:
        st.warning("Install folium and streamlit-folium for satellite map support. Falling back to Streamlit map.")
        st.map(mapped.rename(columns={"location_lat": "lat", "location_lon": "lon"})[["lat", "lon"]])

    st.subheader("Mapped nodes")
    st.dataframe(
        mapped[["node_id", "node_name", "status", "location_label", "location_lat", "location_lon", "battery_v", "last_seen_age"]],
        width="stretch",
        hide_index=True,
        column_config={
            "node_id": "Node",
            "node_name": "Name",
            "status": "Status",
            "location_label": "Location",
            "location_lat": st.column_config.NumberColumn("Latitude", format="%.6f"),
            "location_lon": st.column_config.NumberColumn("Longitude", format="%.6f"),
            "battery_v": st.column_config.NumberColumn("Battery", format="%.2f V"),
            "last_seen_age": "Last seen",
        },
    )


def page_files(files: pd.DataFrame) -> None:
    page_heading("Recordings", "Browse, download, and manage recordings stored by node and recording time.")
    notice = st.session_state.pop("recording_notice", None)
    if notice:
        st.success(notice)
    if files.empty:
        st.info("No file manifests yet.")
        return
    filtered = add_upload_progress(filter_files(files))
    cols = [
        "node_id", "recording_location_label", "display_name", "filename", "recorded_time",
        "recorded_at_source", "file_size_mb", "received_mb",
        "upload_progress", "upload_status", "wav_parse_status", "flac_status",
        "weather_status", "server_deleted_time"
    ]
    cols = [c for c in cols if c in filtered.columns]
    st.dataframe(
        filtered[cols],
        width="stretch",
        hide_index=True,
        column_config={
            "node_id": "Node",
            "recording_location_label": "Recording location",
            "display_name": "Stored name",
            "filename": "Original name",
            "recorded_time": st.column_config.DatetimeColumn("Recorded UTC", format="YYYY-MM-DD HH:mm:ss"),
            "recorded_at_source": "Time source",
            "file_size_mb": st.column_config.NumberColumn("Size", format="%.2f MB"),
            "received_mb": st.column_config.NumberColumn("Received", format="%.2f MB"),
            "upload_progress": st.column_config.ProgressColumn("Progress", min_value=0, max_value=100, format="%.0f%%"),
            "upload_status": "Transfer",
            "wav_parse_status": "WAV check",
            "flac_status": "FLAC",
            "backup_status": "Backup",
            "weather_status": "Weather",
            "server_deleted_time": st.column_config.DatetimeColumn("Server deleted", format="YYYY-MM-DD HH:mm:ss"),
        },
    )
    downloadable_csv(filtered, "Export filtered files CSV", "files.csv")

    st.subheader("Recording manager")
    file_ids = [int(value) for value in filtered["id"].tolist()]
    if file_ids:
        labels = {
            int(row["id"]): f"#{int(row['id'])}  {row['display_name']}"
            for _, row in filtered.iterrows()
        }
        selected_id = pref_selectbox(
            "Recording",
            "files.selected_file_id",
            file_ids,
            default=file_ids[0],
            format_func=lambda value: labels.get(int(value), str(value)),
        )
        row = filtered[filtered["id"] == selected_id].iloc[0]
        info_col, action_col = st.columns([2, 1])
        with info_col:
            recorded_value = row.get("recorded_time")
            recorded_text = "Unknown"
            if recorded_value is not None and not pd.isna(recorded_value):
                recorded_text = pd.Timestamp(recorded_value).strftime("%Y-%m-%d %H:%M:%S UTC")
            st.write(f"**Stored as:** {row.get('display_name') or row.get('filename')}")
            st.write(f"**Original name:** {row.get('filename')}")
            st.write(f"**Recorded:** {recorded_text} ({row.get('recorded_at_source') or 'unknown source'})")
            location = row.get("recording_location_label") or "No location assigned"
            st.write(f"**Location:** {location}")
        with action_col:
            for kind, path_column, mime in (
                ("WAV", "original_wav_path", "audio/wav"),
                ("FLAC", "flac_path", "audio/flac"),
            ):
                path_value = row.get(path_column)
                if path_value is None or pd.isna(path_value):
                    continue
                try:
                    download_path = resolve_data_path(path_value)
                    if download_path.is_file():
                        st.download_button(
                            f"Download {kind}",
                            data=download_path.read_bytes(),
                            file_name=download_path.name,
                            mime=mime,
                            key=f"download_{kind.lower()}_{selected_id}",
                            width="stretch",
                        )
                except (OSError, ValueError) as exc:
                    st.warning(str(exc))

        with st.expander("Delete server copy"):
            st.warning("This removes the WAV and FLAC from the server. The catalog row is kept as a deletion record.")
            confirmation = st.text_input(
                f"Type DELETE {selected_id} to confirm",
                key=f"delete_recording_confirmation_{selected_id}",
            )
            if st.button(
                "Delete WAV and FLAC",
                type="primary",
                disabled=confirmation != f"DELETE {selected_id}",
                key=f"delete_recording_{selected_id}",
            ):
                result = delete_server_recording(selected_id)
                if result["errors"]:
                    st.warning("; ".join(result["errors"]))
                st.session_state["recording_notice"] = f"Recording {selected_id} was removed from server storage."
                st.rerun()

        if is_advanced_mode():
            with st.expander("Raw catalog details"):
                st.json(row.dropna().to_dict())


def page_sd_cleanup(files: pd.DataFrame) -> None:
    page_heading("SD Cleanup", "Review server-verified files before AudioMoth SD deletion.")
    if files.empty:
        st.info("No file records yet.")
        return

    status_counts = files.groupby(["upload_status", "delete_status"], dropna=False).size().reset_index(name="count")
    st.subheader("Status counts")
    st.dataframe(
        status_counts,
        width="stretch",
        hide_index=True,
        column_config={
            "upload_status": "Transfer",
            "delete_status": "SD Cleanup",
            "count": "Files",
        },
    )

    st.subheader("Delete pipeline")
    pipeline_order = [
        "NOT_AUTHORIZED",
        "SAFE_TO_DELETE",
        "DELETE_AUTHORIZED",
        "DELETE_REQUESTED",
        "DELETED_FROM_SD",
        "DELETE_FAILED",
    ]
    cols = st.columns(len(pipeline_order))
    for col, status in zip(cols, pipeline_order):
        count = int(files["delete_status"].fillna("").eq(status).sum())
        col.metric(status.replace("_", " ").title(), count)

    attention = files[files["delete_status"].fillna("").isin(["SAFE_TO_DELETE", "DELETE_AUTHORIZED", "DELETE_FAILED"])]
    st.subheader("Needs cleanup attention")
    if attention.empty:
        st.success("No cleanup issues currently flagged.")
    else:
        cols = ["id", "node_id", "filename", "file_size_mb", "upload_status", "wav_parse_status", "flac_status", "backup_status", "delete_status", "delete_error"]
        cols = [c for c in cols if c in attention.columns]
        st.dataframe(
            attention[cols],
            width="stretch",
            hide_index=True,
            column_config={
                "id": "ID",
                "node_id": "Node",
                "filename": "File",
                "file_size_mb": st.column_config.NumberColumn("Size", format="%.2f MB"),
                "upload_status": "Transfer",
                "wav_parse_status": "WAV check",
                "flac_status": "FLAC",
                "backup_status": "Backup",
                "delete_status": "SD Cleanup",
                "delete_error": "Cleanup error",
            },
        )


def page_telemetry(nodes: pd.DataFrame) -> None:
    page_heading("Telemetry", "Battery, storage, and Wi-Fi trends from recent heartbeats.")
    node_options = ["All"] + sorted(nodes["node_id"].dropna().unique().tolist()) if not nodes.empty else ["All"]
    selected = pref_selectbox("Node", "telemetry.node", node_options, default="All")
    limit = pref_slider("Rows", "telemetry.rows", 100, 5000, 1000, step=100)
    telemetry = load_telemetry(selected, limit)
    if telemetry.empty:
        st.info("No telemetry yet.")
        return
    telemetry = telemetry.drop(columns=["solar_v"], errors="ignore")
    telemetry = telemetry.sort_values("created_at")
    if "created_at" in telemetry.columns:
        telemetry["created_time"] = pd.to_datetime(telemetry["created_at"], unit="s", errors="coerce")

    chart_cols = [c for c in ["battery_v", "battery_percent", "sd_free_mb", "wifi_rssi_dbm"] if c in telemetry.columns]
    if chart_cols:
        chart_index = "created_time" if "created_time" in telemetry.columns else "created_at"
        st.line_chart(telemetry.set_index(chart_index)[chart_cols])

    preferred_cols = [
        "node_id", "created_time", "battery_v", "battery_percent", "sd_free_mb",
        "wifi_rssi_dbm", "charging", "charge_done", "recording_status", "upload_status"
    ]
    table_cols = [c for c in preferred_cols if c in telemetry.columns]
    if not table_cols:
        table_cols = telemetry.columns.tolist()
    st.dataframe(
        telemetry.sort_values("created_at", ascending=False)[table_cols],
        width="stretch",
        hide_index=True,
        column_config={
            "node_id": "Node",
            "created_time": st.column_config.DatetimeColumn("Time"),
            "battery_v": st.column_config.NumberColumn("Battery", format="%.2f V"),
            "battery_percent": st.column_config.ProgressColumn("Charge", min_value=0, max_value=100, format="%.0f%%"),
            "sd_free_mb": st.column_config.NumberColumn("SD free", format="%.1f MB"),
            "wifi_rssi_dbm": st.column_config.NumberColumn("Wi-Fi", format="%.0f dBm"),
            "charging": "Charging",
            "charge_done": "Charge done",
            "recording_status": "Recorder",
            "upload_status": "Transfer",
        },
    )
    downloadable_csv(telemetry, "Export telemetry CSV", "telemetry.csv")


def page_commands(commands: pd.DataFrame) -> None:
    page_heading("Command Queue", "Monitor pending, delivered, and acknowledged node commands.")
    if commands.empty:
        st.info("No commands yet.")
        return
    counts = commands["status"].fillna("unknown").value_counts()
    c1m, c2m, c3m, c4m = st.columns(4)
    c1m.metric("Pending", int(counts.get("PENDING", 0)))
    c2m.metric("Delivered", int(counts.get("DELIVERED", 0)))
    c3m.metric("Acknowledged", int(counts.get("ACKED", 0)))
    c4m.metric("Total", len(commands))

    c1, c2 = st.columns(2)
    with c1:
        status_options = ["All"] + sorted(commands["status"].fillna("unknown").unique().tolist())
        status = pref_selectbox("Status", "commands.status", status_options, default="All")
    with c2:
        node_options = ["All"] + sorted(commands["node_id"].dropna().unique().tolist())
        node = pref_selectbox("Node", "commands.node", node_options, default="All")
    out = commands.copy()
    if status != "All":
        out = out[out["status"].fillna("unknown") == status]
    if node != "All":
        out = out[out["node_id"] == node]
    cols = [
        "id", "node_id", "command_type", "status", "created_age",
        "delivered_age", "acked_age", "expires_at", "response_json"
    ]
    if is_advanced_mode():
        cols.insert(4, "payload_json")
    cols = [c for c in cols if c in out.columns]
    st.dataframe(
        out[cols],
        width="stretch",
        hide_index=True,
        column_config={
            "id": "ID",
            "node_id": "Node",
            "command_type": "Command",
            "status": "Status",
            "payload_json": "Payload",
            "created_age": "Queued",
            "delivered_age": "Delivered",
            "acked_age": "Acked",
            "expires_at": "Expires",
            "response_json": "Response",
        },
    )
    downloadable_csv(out, "Export commands CSV", "commands.csv")


def page_data_management() -> None:
    page_heading("Data Management", "Back up, compress, download, or clear server data with explicit safeguards.")
    notice = st.session_state.pop("data_management_notice", None)
    if notice:
        st.success(notice)

    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    recording_stats = query_df(
        """
        SELECT COUNT(*) AS recordings,
               SUM(CASE WHEN flac_status='OK' AND flac_path IS NOT NULL THEN 1 ELSE 0 END) AS compressed,
               SUM(CASE WHEN upload_status='SERVER_COPY_VERIFIED' AND COALESCE(flac_status, '')!='OK' THEN 1 ELSE 0 END) AS awaiting_flac
        FROM files
        """
    ).iloc[0]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Database", f"{db_size / 1_000_000:.2f} MB")
    c2.metric("Recordings", int(recording_stats.get("recordings") or 0))
    c3.metric("Compressed", int(recording_stats.get("compressed") or 0))
    c4.metric("Awaiting FLAC", int(recording_stats.get("awaiting_flac") or 0))

    st.subheader("Backup and compression")
    left, right = st.columns(2)
    with left:
        backup_name = datetime.now(timezone.utc).strftime("bat_nodes_backup_%Y%m%dT%H%M%SZ.sqlite3")
        st.download_button(
            "Download database backup",
            data=database_backup_bytes(),
            file_name=backup_name,
            mime="application/vnd.sqlite3",
            width="stretch",
        )
        st.caption(f"Database: {DB_PATH}")
    with right:
        if st.button("Check and compress WAV files", type="primary", width="stretch"):
            try:
                result = admin_api("POST", "/admin/storage/compress", {"limit": 25})
                if result.get("busy"):
                    st.info("A compression pass is already running.")
                elif result.get("ok"):
                    st.success(
                        f"Checked {result.get('checked', 0)}; compressed {result.get('compressed', 0)}; "
                        f"already healthy {result.get('already_ok', 0)}."
                    )
                else:
                    st.warning(result.get("error") or f"Compression finished with {result.get('failed', 0)} failure(s).")
                if result.get("errors"):
                    st.json(result["errors"])
            except Exception as exc:
                st.error(str(exc))
        st.caption("The server also runs this check automatically on a schedule.")

    st.divider()
    st.subheader("Clear operational history")
    st.write("Removes telemetry, command history, errors, time checks, and current status. Nodes and recordings remain.")
    history_confirmation = st.text_input("Type CLEAR HISTORY", key="clear_history_confirmation")
    if st.button(
        "Clear history",
        disabled=history_confirmation != "CLEAR HISTORY",
        key="clear_history_button",
    ):
        result = clear_operational_history()
        st.session_state["data_management_notice"] = f"Operational history cleared. Backup: {result['backup']}"
        st.rerun()

    st.divider()
    st.subheader("Delete all recordings")
    st.warning("Removes all server WAV/FLAC files, manifests, upload sessions, and recording rows. Nodes remain enrolled.")
    recordings_confirmation = st.text_input("Type DELETE RECORDINGS", key="clear_recordings_confirmation")
    if st.button(
        "Delete all recordings",
        type="primary",
        disabled=recordings_confirmation != "DELETE RECORDINGS",
        key="clear_recordings_button",
    ):
        result = clear_recording_storage()
        st.session_state["data_management_notice"] = (
            f"Recording storage cleared ({result['removed_files']} files removed). Backup: {result['backup']}"
        )
        st.rerun()

    st.divider()
    st.subheader("Reset server data")
    st.error("Removes nodes, credentials, enrollment requests, history, and recordings. Every ESP32 must enroll again.")
    reset_confirmation = st.text_input("Type RESET SERVER", key="reset_server_confirmation")
    if st.button(
        "Reset all server data",
        type="primary",
        disabled=reset_confirmation != "RESET SERVER",
        key="reset_server_button",
    ):
        result = factory_clear_server()
        st.session_state["data_management_notice"] = (
            f"Server data reset ({result['removed_files']} files removed). Backup: {result['backup']}"
        )
        st.rerun()


def page_diagnostics() -> None:
    page_heading("Diagnostics", "Server timing, node errors, and local database health.", kicker="Advanced")
    st.subheader("Time checks")
    time_checks = load_time_checks()
    if time_checks.empty:
        st.info("No time checks yet.")
    else:
        st.dataframe(time_checks, width="stretch", hide_index=True)

    st.subheader("Node errors")
    errors = load_errors()
    if errors.empty:
        st.info("No node errors logged.")
    else:
        st.dataframe(errors, width="stretch", hide_index=True)

    st.subheader("Database info")
    st.code(str(DB_PATH))
    if DB_PATH.exists():
        st.write(f"Database size: {DB_PATH.stat().st_size / 1_000_000:.2f} MB")


def page_raw_db() -> None:
    page_heading("Raw Database", "Read-only table access.", kicker="Advanced")
    with db_connect() as conn:
        tables = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name", conn)["name"].tolist()
    table = pref_selectbox("Table", "raw_db.table", tables, default=tables[0])
    limit = pref_slider("Rows", "raw_db.rows", 10, 5000, 500, step=10)
    df = query_df(f"SELECT * FROM {table} LIMIT ?", (limit,))
    st.dataframe(df, width="stretch", hide_index=True)
    downloadable_csv(df, f"Export {table} CSV", f"{table}.csv")


# ============================================================
# Main
# ============================================================

def main() -> None:
    inject_theme()
    st.title("Bat Node Monitor")

    with st.sidebar:
        st.markdown("### Bat Node")
        st.caption("ESP32 + AudioMoth operations")
        if st.button("Refresh now", width="stretch", type="primary"):
            st.rerun()

        auto_refresh = pref_checkbox("Auto-refresh", "main.auto_refresh", False)
        refresh_seconds = pref_slider("Refresh seconds", "main.refresh_seconds", 5, 120, 15, disabled=not auto_refresh)

        st.divider()
        advanced = pref_checkbox("Advanced mode", "main.advanced_mode", False)
        page_options = ["Overview", "Add Nodes", "Fleet", "Map", "Recordings", "Telemetry", "Command Queue", "SD Cleanup", "Data Management"]
        if advanced:
            page_options.extend(["Diagnostics", "Raw Database"])
        saved_page = friendly_page_name(get_pref("main.page", "Overview"))
        if saved_page not in page_options:
            saved_page = "Overview"
        page = pref_radio("Navigation", "main.page", page_options, default=saved_page)

        with st.expander("Database"):
            st.code(str(DB_PATH))
            if DB_PATH.exists():
                st.write(f"{DB_PATH.stat().st_size / 1_000_000:.2f} MB")

        with st.expander("Preferences"):
            if st.button("Reset saved preferences"):
                clear_preferences()
                st.rerun()

    if auto_refresh:
        st.markdown(f"<meta http-equiv='refresh' content='{refresh_seconds}'>", unsafe_allow_html=True)

    if not require_db():
        return

    nodes = load_nodes()
    files = load_files()
    commands = load_commands()

    if page == "Overview":
        page_overview(nodes, files, commands)
    elif page == "Add Nodes":
        page_enrollment(nodes)
    elif page == "Fleet":
        page_nodes(nodes)
    elif page == "Map":
        page_map(nodes)
    elif page == "Recordings":
        page_files(files)
    elif page == "Telemetry":
        page_telemetry(nodes)
    elif page == "Command Queue":
        page_commands(commands)
    elif page == "SD Cleanup":
        page_sd_cleanup(files)
    elif page == "Data Management":
        page_data_management()
    elif page == "Diagnostics":
        page_diagnostics()
    elif page == "Raw Database":
        page_raw_db()


if __name__ == "__main__":
    main()
