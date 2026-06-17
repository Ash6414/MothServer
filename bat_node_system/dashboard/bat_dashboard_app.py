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
import sqlite3
import time
import json
import html
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
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
ONLINE_TIMEOUT_SECONDS = int(os.getenv("BAT_ONLINE_TIMEOUT_SECONDS", "900"))

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="🦇",
    layout="wide",
    initial_sidebar_state="expanded",
)


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


def query_df(sql: str, params: Iterable[Any] = ()) -> pd.DataFrame:
    with db_connect() as conn:
        return pd.read_sql_query(sql, conn, params=list(params))


def execute(sql: str, params: Iterable[Any] = ()) -> None:
    with db_connect() as conn:
        conn.execute(sql, tuple(params))
        conn.commit()


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
    df = query_df(
        """
        SELECT
            n.node_id,
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
            f.recorded_at_corrected,
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

    cols = st.columns(7)
    cols[0].metric("Nodes", len(nodes))
    cols[1].metric("Online", online)
    cols[2].metric("Low battery", low_battery)
    cols[3].metric("Recording", recording)
    cols[4].metric("Uploading", uploading)
    cols[5].metric("Files pending", pending_files)
    cols[6].metric("Commands pending", pending_cmds, help=f"Delete-ready files: {delete_ready}")


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
    st.header("Overview")
    metric_row(nodes, files, commands)

    st.subheader("Node summary")
    filtered = apply_node_filters(nodes)
    display_cols = [
        "status", "node_id", "node_name", "location_label", "last_seen_age",
        "battery_v", "battery_percent", "solar_v", "charging", "charge_done",
        "recording_status", "upload_status", "sd_free_mb", "wifi_rssi_dbm", "message"
    ]
    existing = [c for c in display_cols if c in filtered.columns]
    st.dataframe(filtered[existing], width="stretch", hide_index=True)
    downloadable_csv(filtered, "Export filtered nodes CSV", "nodes.csv")

    st.subheader("Recent files needing attention")
    if files.empty:
        st.info("No files in database yet.")
    else:
        attention = files[
            files["upload_status"].fillna("").isin(["ON_SD_ONLY", "PARTIAL", "UPLOADING", "FAILED"])
            | files["delete_status"].fillna("").isin(["DELETE_FAILED", "MANUAL_REVIEW"])
        ].head(50)
        cols = ["node_id", "filename", "recorded_at_raw", "file_size_mb", "upload_status", "bytes_received", "wav_parse_status", "flac_status", "delete_status", "delete_error"]
        cols = [c for c in cols if c in attention.columns]
        st.dataframe(attention[cols], width="stretch", hide_index=True)


def page_nodes(nodes: pd.DataFrame) -> None:
    st.header("Nodes")
    if nodes.empty:
        st.info("No nodes exist yet. Use manage_node.py to create one.")
        return

    node_ids = nodes["node_id"].tolist()
    selected = pref_selectbox(
        "Select node",
        "nodes.selected_node",
        node_ids,
        default=node_ids[0],
        format_func=lambda x: f"{x} — {nodes.loc[nodes['node_id'] == x, 'node_name'].iloc[0]}",
    )
    row = nodes[nodes["node_id"] == selected].iloc[0]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Status", row.get("status", "unknown"))
    c2.metric("Last seen", row.get("last_seen_age", "never"))
    c3.metric("Battery", "—" if pd.isna(row.get("battery_v")) else f"{row.get('battery_v'):.2f} V")
    c4.metric("SD free", "—" if pd.isna(row.get("sd_free_mb")) else f"{row.get('sd_free_mb'):.1f} MB")

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

    st.subheader("Queue command")
    col_a, col_b, col_c = st.columns([1, 1, 2])
    with col_a:
        if st.button("Queue PING", width="stretch"):
            cid = queue_command(selected, "PING")
            st.success(f"Queued PING command #{cid}.")
    with col_b:
        if st.button("Queue STATUS", width="stretch"):
            cid = queue_command(selected, "STATUS")
            st.success(f"Queued STATUS command #{cid}.")
    with col_c:
        custom = st.text_input("Custom command type", placeholder="Example: FORCE_MANIFEST")
        if st.button("Queue custom command") and custom:
            cid = queue_command(selected, custom)
            st.success(f"Queued {custom.upper()} command #{cid}.")

    st.subheader("Raw current state")
    st.json(row.dropna().to_dict())


def page_map(nodes: pd.DataFrame) -> None:
    st.header("Map")
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
        st.info("No node coordinates saved yet. Add latitude and longitude in the Nodes page.")
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
    st.dataframe(mapped[["node_id", "node_name", "status", "location_label", "location_lat", "location_lon", "battery_v", "last_seen_age"]], width="stretch", hide_index=True)


def page_files(files: pd.DataFrame) -> None:
    st.header("Files")
    if files.empty:
        st.info("No file manifests yet.")
        return
    filtered = filter_files(files)
    cols = [
        "id", "node_id", "node_name", "location_label", "filename", "recorded_at_raw",
        "file_size_mb", "received_mb", "upload_status", "wav_parse_status", "flac_status",
        "backup_status", "weather_status", "delete_status", "delete_error", "server_sha256"
    ]
    cols = [c for c in cols if c in filtered.columns]
    st.dataframe(filtered[cols], width="stretch", hide_index=True)
    downloadable_csv(filtered, "Export filtered files CSV", "files.csv")

    st.subheader("File details")
    file_ids = filtered["id"].tolist()
    if file_ids:
        selected_id = pref_selectbox("Select file ID", "files.selected_file_id", file_ids, default=file_ids[0])
        row = filtered[filtered["id"] == selected_id].iloc[0]
        st.json(row.dropna().to_dict())


def page_sd_cleanup(files: pd.DataFrame) -> None:
    st.header("SD cleanup")
    st.caption("This page audits whether files are still only on SD, uploaded, safe to delete, or already deleted from the SD card.")
    if files.empty:
        st.info("No file records yet.")
        return

    status_counts = files.groupby(["upload_status", "delete_status"], dropna=False).size().reset_index(name="count")
    st.subheader("Status counts")
    st.dataframe(status_counts, width="stretch", hide_index=True)

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
        st.dataframe(attention[cols], width="stretch", hide_index=True)


def page_telemetry(nodes: pd.DataFrame) -> None:
    st.header("Telemetry")
    node_options = ["All"] + sorted(nodes["node_id"].dropna().unique().tolist()) if not nodes.empty else ["All"]
    selected = pref_selectbox("Node", "telemetry.node", node_options, default="All")
    limit = pref_slider("Rows", "telemetry.rows", 100, 5000, 1000, step=100)
    telemetry = load_telemetry(selected, limit)
    if telemetry.empty:
        st.info("No telemetry yet.")
        return
    telemetry = telemetry.sort_values("created_at")

    chart_cols = [c for c in ["battery_v", "battery_percent", "solar_v", "sd_free_mb", "wifi_rssi_dbm"] if c in telemetry.columns]
    if chart_cols:
        st.line_chart(telemetry.set_index("created_at")[chart_cols])

    st.dataframe(telemetry.sort_values("created_at", ascending=False), width="stretch", hide_index=True)
    downloadable_csv(telemetry, "Export telemetry CSV", "telemetry.csv")


def page_commands(commands: pd.DataFrame) -> None:
    st.header("Commands")
    if commands.empty:
        st.info("No commands yet.")
        return
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
    st.dataframe(out, width="stretch", hide_index=True)
    downloadable_csv(out, "Export commands CSV", "commands.csv")


def page_diagnostics() -> None:
    st.header("Diagnostics")
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
    st.header("Raw DB viewer")
    st.warning("Read-only viewer. Do not use this as the normal workflow; use the app pages above.")
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
    st.title(APP_TITLE)
    st.caption("Local database dashboard for ESP32 + AudioMoth bat monitoring nodes. Preferences persist across refreshes.")

    with st.sidebar:
        st.write("**Database**")
        st.code(str(DB_PATH))
        auto_refresh = pref_checkbox("Auto-refresh", "main.auto_refresh", False)
        refresh_seconds = pref_slider("Refresh seconds", "main.refresh_seconds", 5, 120, 15, disabled=not auto_refresh)
        if st.button("Refresh now"):
            st.rerun()
        if st.button("Reset saved preferences"):
            clear_preferences()
            st.rerun()
        st.divider()
        page_options = ["Overview", "Nodes", "Map", "Files", "SD cleanup", "Telemetry", "Commands", "Diagnostics", "Raw DB"]
        page = pref_radio("Page", "main.page", page_options, default="Overview")

    if auto_refresh:
        st.markdown(f"<meta http-equiv='refresh' content='{refresh_seconds}'>", unsafe_allow_html=True)

    if not require_db():
        return

    nodes = load_nodes()
    files = load_files()
    commands = load_commands()

    if page == "Overview":
        page_overview(nodes, files, commands)
    elif page == "Nodes":
        page_nodes(nodes)
    elif page == "Map":
        page_map(nodes)
    elif page == "Files":
        page_files(files)
    elif page == "SD cleanup":
        page_sd_cleanup(files)
    elif page == "Telemetry":
        page_telemetry(nodes)
    elif page == "Commands":
        page_commands(commands)
    elif page == "Diagnostics":
        page_diagnostics()
    elif page == "Raw DB":
        page_raw_db()


if __name__ == "__main__":
    main()
