from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any, Optional

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
        "wav_parse_status='OK'",
        "original_wav_path IS NOT NULL",
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


def update_file(file_id: int, status: str, flac_path: Optional[Path], error: Optional[str]) -> None:
    db_status = status if not error else f"{status}: {error}"
    with bat_server.db_connect() as conn:
        conn.execute(
            """
            UPDATE files
            SET flac_status=?, flac_path=?, updated_at=?
            WHERE id=?
            """,
            (db_status, str(flac_path) if flac_path else None, int(time.time()), file_id),
        )
        conn.commit()


def main() -> int:
    args = parse_args()
    encoder_name, encoder_path = bat_server.find_flac_encoder()
    if not encoder_path:
        print("No FLAC encoder found. Install 'flac' or 'ffmpeg', restart the server, then run this again.")
        print("Raspberry Pi: sudo apt update && sudo apt install -y flac")
        print("Windows: install FLAC or FFmpeg and make sure 'flac' or 'ffmpeg' works in PowerShell.")
        return 2

    rows = candidate_rows(args)
    if not rows:
        print("No verified WAV files need compression.")
        return 0

    print(f"Using {encoder_name}: {encoder_path}")
    failures = 0
    for row in rows:
        wav_path = Path(row["original_wav_path"])
        label = f"[{row['id']}] {row['node_id']} {row['filename']}"
        if not wav_path.exists():
            failures += 1
            error = f"source WAV missing: {wav_path}"
            print(f"{label}: ERROR: {error}")
            if not args.dry_run:
                update_file(row["id"], "ERROR", None, error)
            continue

        if args.dry_run:
            print(f"{label}: would compress {wav_path}")
            continue

        status, flac_path, error = bat_server.make_flac(wav_path, row["node_id"])
        if status != "OK":
            failures += 1
            print(f"{label}: {status}{': ' + error if error else ''}")
        else:
            print(f"{label}: OK -> {flac_path}")
        update_file(row["id"], status, flac_path, error)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
