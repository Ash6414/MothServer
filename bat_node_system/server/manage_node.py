from __future__ import annotations

import argparse
import secrets
import sqlite3
import time
from pathlib import Path
import os

# Keep this file in the same directory as bat_server.py.
from bat_server import DB_PATH, init_db


def create_node(args: argparse.Namespace) -> None:
    init_db()
    now = int(time.time())
    node_id = args.node_id
    key_id = args.key_id or "key-1"
    secret = args.secret or secrets.token_hex(32)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            INSERT INTO nodes (
                node_id, node_name, location_lat, location_lon, location_label,
                deployment_notes, firmware_version, hardware_version, active,
                compromised, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                node_name=excluded.node_name,
                location_lat=excluded.location_lat,
                location_lon=excluded.location_lon,
                location_label=excluded.location_label,
                deployment_notes=excluded.deployment_notes,
                updated_at=excluded.updated_at
            """,
            (
                node_id,
                args.name,
                args.lat,
                args.lon,
                args.location_label,
                args.notes,
                args.firmware,
                args.hardware,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO node_credentials (node_id, key_id, secret, created_at, expires_at, revoked_at)
            VALUES (?, ?, ?, ?, NULL, NULL)
            ON CONFLICT(node_id, key_id) DO UPDATE SET
                secret=excluded.secret,
                revoked_at=NULL
            """,
            (node_id, key_id, secret, now),
        )
        conn.commit()

    print("Node created/updated.")
    print(f"NODE_ID={node_id}")
    print(f"KEY_ID={key_id}")
    print(f"DEVICE_SECRET={secret}")
    print("Put these three values into the ESP32 sketch. Treat DEVICE_SECRET like a password.")


def revoke_node(args: argparse.Namespace) -> None:
    init_db()
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE nodes SET active=0, compromised=? WHERE node_id=?", (1 if args.compromised else 0, args.node_id))
        conn.execute("UPDATE node_credentials SET revoked_at=? WHERE node_id=?", (now, args.node_id))
        conn.commit()
    print(f"Revoked {args.node_id}.")


def list_nodes(args: argparse.Namespace) -> None:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT n.node_id, n.node_name, n.active, n.compromised, c.key_id, c.revoked_at
            FROM nodes n
            LEFT JOIN node_credentials c ON c.node_id = n.node_id
            ORDER BY n.node_id, c.key_id
            """
        ).fetchall()
    for r in rows:
        print(dict(r))


def main() -> None:
    p = argparse.ArgumentParser(description="Manage bat-node server records")
    sub = p.add_subparsers(required=True)

    c = sub.add_parser("create", help="Create or update a node and credential")
    c.add_argument("node_id")
    c.add_argument("name")
    c.add_argument("--key-id", default="key-1")
    c.add_argument("--secret", default=None)
    c.add_argument("--lat", type=float, default=None)
    c.add_argument("--lon", type=float, default=None)
    c.add_argument("--location-label", default=None)
    c.add_argument("--notes", default=None)
    c.add_argument("--firmware", default=None)
    c.add_argument("--hardware", default=None)
    c.set_defaults(func=create_node)

    r = sub.add_parser("revoke", help="Revoke all credentials for a node")
    r.add_argument("node_id")
    r.add_argument("--compromised", action="store_true")
    r.set_defaults(func=revoke_node)

    l = sub.add_parser("list", help="List nodes")
    l.set_defaults(func=list_nodes)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
