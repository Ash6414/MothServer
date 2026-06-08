from __future__ import annotations

import sqlite3

from .config import settings


def get_conn() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def dict_row(row: sqlite3.Row | None) -> dict | None:
    return None if row is None else dict(row)


def init_db() -> None:
    settings.upload_root.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                node_id TEXT PRIMARY KEY,
                key_id TEXT,
                name TEXT,
                latitude REAL,
                longitude REAL,
                last_seen_epoch INTEGER,
                battery_v REAL,
                battery_percent INTEGER,
                charging INTEGER,
                charge_done INTEGER,
                wifi_rssi_dbm INTEGER,
                recording_status TEXT,
                upload_status TEXT,
                mode TEXT,
                raw_status_json TEXT
            );

            CREATE TABLE IF NOT EXISTS heartbeats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT NOT NULL,
                received_epoch INTEGER NOT NULL,
                battery_v REAL,
                charging INTEGER,
                charge_done INTEGER,
                wifi_rssi_dbm INTEGER,
                upload_status TEXT,
                raw_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS time_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT NOT NULL,
                received_epoch INTEGER NOT NULL,
                server_epoch INTEGER,
                esp_epoch_after INTEGER,
                audiomoth_epoch INTEGER,
                rtt_ms INTEGER,
                notes TEXT,
                raw_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT NOT NULL,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                created_epoch INTEGER NOT NULL,
                acknowledged_epoch INTEGER,
                response_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_commands_pending
                ON commands (node_id, status, created_epoch);

            CREATE TABLE IF NOT EXISTS upload_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                error_message TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_upload_sessions_active
                ON upload_sessions (node_id, source_path, status, updated_epoch);

            CREATE TABLE IF NOT EXISTS recordings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                size INTEGER NOT NULL,
                uploaded_epoch INTEGER NOT NULL,
                recording_epoch INTEGER,
                sha256 TEXT,
                weather_status TEXT,
                weather_json TEXT,
                UNIQUE (node_id, source_path)
            );
            CREATE INDEX IF NOT EXISTS idx_recordings_node_uploaded
                ON recordings (node_id, uploaded_epoch DESC);

            CREATE TABLE IF NOT EXISTS auth_nonces (
                node_id TEXT NOT NULL,
                nonce TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                created_epoch INTEGER NOT NULL,
                PRIMARY KEY (node_id, nonce)
            );
            CREATE INDEX IF NOT EXISTS idx_auth_nonces_created
                ON auth_nonces (created_epoch);
            """
        )


# Best effort migration for repos that already had a partial schema.
def ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
