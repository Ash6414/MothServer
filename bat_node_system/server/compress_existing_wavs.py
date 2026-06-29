from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
os.environ.setdefault("BAT_DB_PATH", str(SCRIPT_DIR / "bat_nodes_v2.db"))
os.environ.setdefault("BAT_DATA_DIR", str(SCRIPT_DIR / "data"))

import bat_server


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compress verified uploaded WAV files to FLAC.")
    parser.add_argument("--node-id", help="Only compress files for one node.")
    parser.add_argument("--limit", type=int, help="Maximum number of rows to process.")
    parser.add_argument("--force", action="store_true", help="Rebuild FLAC files even when flac_status is already OK.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be compressed without changing files or the database.")
    return parser.parse_args()


def candidate_rows(args: argparse.Namespace) -> list[Any]:
    where = [
        "upload_status='SERVER_COPY_VERIFIED'",
        "original_wav_path IS NOT NULL",
        "server_deleted_at IS NULL",
    ]
    params: list[Any] = []
    if args.node_id:
        where.append("node_id=?")
        params.append(args.node_id)
    if not args.force:
        where.append("COALESCE(flac_status, '')!='OK'")

    sql = f"""
        SELECT id, node_id, filename, original_wav_path, flac_status
        FROM files
        WHERE {" AND ".join(where)}
        ORDER BY id
    """
    if args.limit:
        sql += " LIMIT ?"
        params.append(args.limit)

    with bat_server.db_connect() as conn:
        return conn.execute(sql, params).fetchall()


def main() -> int:
    args = parse_args()
    bat_server.init_db()
    rows = candidate_rows(args)
    if not rows:
        print("No verified WAV files need compression.")
        return 0

    if args.dry_run:
        for row in rows:
            print(f"[{row['id']}] {row['node_id']} {row['filename']}: would verify and compress {row['original_wav_path']}")
        return 0

    result = bat_server.reconcile_flac_files(
        limit=args.limit or min(len(rows), 100),
        file_ids=[int(row["id"]) for row in rows],
        force=args.force,
    )
    print(json.dumps(result, indent=2))
    if result.get("error") and result.get("encoder") is None:
        print("Install 'flac' or 'ffmpeg' and ensure it is available on PATH.")
        return 2
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
