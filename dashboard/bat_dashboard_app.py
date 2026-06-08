from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path(os.getenv("MOTHSERVER_DB_PATH", "data/mothserver.sqlite3"))
COMMAND_TYPES = ("PING", "UPLOAD_NOW", "SYNC_MOTH_TIME", "MOTH_STATUS")


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def load_df(query: str, params: tuple = ()) -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()
    with conn() as c:
        return pd.read_sql_query(query, c, params=params)


def fmt_epoch(value):
    if value is None or value == "":
        return ""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(value)


def queue_command(node_id: str, command_type: str) -> int:
    now = int(time.time())
    with conn() as c:
        cur = c.execute(
            """
            INSERT INTO commands (node_id, type, payload_json, status, created_epoch)
            VALUES (?, ?, '{}', 'pending', ?)
            """,
            (node_id, command_type, now),
        )
        return int(cur.lastrowid)


st.set_page_config(page_title="AudioMoth Bat Nodes", layout="wide")
st.title("AudioMoth Bat Node Dashboard")
st.caption(f"SQLite database: `{DB_PATH}`")

nodes = load_df(
    """
    SELECT
        n.node_id,
        n.name,
        n.last_seen_epoch,
        n.battery_v,
        n.battery_percent,
        n.charging,
        n.charge_done,
        n.wifi_rssi_dbm,
        n.recording_status,
        n.upload_status,
        n.mode,
        COALESCE(r.recording_count, 0) AS uploaded_recordings
    FROM nodes n
    LEFT JOIN (
        SELECT node_id, COUNT(*) AS recording_count
        FROM recordings
        GROUP BY node_id
    ) r ON r.node_id = n.node_id
    ORDER BY n.last_seen_epoch DESC
    """
)

if nodes.empty:
    st.warning("No nodes have checked in yet.")
else:
    nodes_display = nodes.copy()
    nodes_display["last_seen"] = nodes_display["last_seen_epoch"].apply(fmt_epoch)
    nodes_display = nodes_display.drop(columns=["last_seen_epoch"])
    st.subheader("Nodes")
    st.dataframe(nodes_display, width="stretch", hide_index=True)

    selected_node = st.selectbox("Selected node", nodes["node_id"].tolist())

    st.subheader("Commands")
    cols = st.columns(len(COMMAND_TYPES))
    for col, command_type in zip(cols, COMMAND_TYPES):
        with col:
            if st.button(command_type, key=f"cmd_{command_type}"):
                command_id = queue_command(selected_node, command_type)
                st.success(f"Queued {command_type} as command {command_id}")

    pending = load_df(
        """
        SELECT id, node_id, type, status, created_epoch, acknowledged_epoch, response_json
        FROM commands
        WHERE node_id = ?
        ORDER BY id DESC
        LIMIT 25
        """,
        (selected_node,),
    )
    if not pending.empty:
        pending["created"] = pending["created_epoch"].apply(fmt_epoch)
        pending["acknowledged"] = pending["acknowledged_epoch"].apply(fmt_epoch)
        pending = pending.drop(columns=["created_epoch", "acknowledged_epoch"])
        st.dataframe(pending, width="stretch", hide_index=True)

    st.subheader("Recordings")
    recs = load_df(
        """
        SELECT source_path, stored_path, size, uploaded_epoch, recording_epoch, sha256, weather_status
        FROM recordings
        WHERE node_id = ?
        ORDER BY uploaded_epoch DESC
        LIMIT 100
        """,
        (selected_node,),
    )
    if recs.empty:
        st.info("No recordings stored for this node yet.")
    else:
        recs["uploaded"] = recs["uploaded_epoch"].apply(fmt_epoch)
        recs["recorded"] = recs["recording_epoch"].apply(fmt_epoch)
        recs = recs.drop(columns=["uploaded_epoch", "recording_epoch"])
        st.dataframe(recs, width="stretch", hide_index=True)

    with st.expander("Latest raw heartbeat JSON"):
        raw = load_df("SELECT raw_status_json FROM nodes WHERE node_id = ?", (selected_node,))
        if not raw.empty and raw.iloc[0]["raw_status_json"]:
            try:
                st.json(json.loads(raw.iloc[0]["raw_status_json"]))
            except Exception:
                st.code(raw.iloc[0]["raw_status_json"])
