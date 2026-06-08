from __future__ import annotations

import hashlib
import json
import os
import re
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .db import get_conn, init_db
from .paths import final_path_for, safe_node_id, sanitize_moth_path, temp_path_for
from .security import require_allowed_command, verify_hmac

TIMESTAMP_RE = re.compile(r"(?:^|/)(?P<date>\d{8})_(?P<time>\d{6})\.wav$", re.IGNORECASE)


def utc_now() -> int:
    return int(time.time())


def iso_utc(epoch: int | None = None) -> str:
    dt = datetime.fromtimestamp(epoch if epoch is not None else utc_now(), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def bool_int(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0


def parse_recording_epoch(source_path: str) -> int | None:
    match = TIMESTAMP_RE.search(source_path)
    if not match:
        return None
    try:
        dt = datetime.strptime(match.group("date") + match.group("time"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def latest_upload_summary(raw: dict[str, Any]) -> str | None:
    direct = raw.get("upload_status")
    if direct:
        return str(direct)
    stats = raw.get("stats") or {}
    if stats:
        return f"sessions={stats.get('successful_upload_sessions', 0)} ok, {stats.get('failed_upload_sessions', 0)} failed"
    return None


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(title="AudioMoth Bat Node Server", version="1.0.0")

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": exc.detail})

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/v1/public/server_time")
    def server_time() -> dict[str, Any]:
        epoch = utc_now()
        return {"epoch_utc": epoch, "iso_utc": iso_utc(epoch)}

    @app.post("/v1/device/heartbeat")
    async def heartbeat(request: Request) -> dict[str, bool]:
        body = await request.body()
        auth = await verify_hmac(request, body)
        payload = json.loads(body.decode("utf-8") or "{}")
        node_id = payload.get("node_id")
        if node_id != auth["node_id"]:
            raise HTTPException(status_code=403, detail="node_id mismatch")
        safe_node_id(node_id)
        now = utc_now()
        raw_json = json_dumps(payload)
        upload_status = latest_upload_summary(payload)
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO nodes (
                    node_id, key_id, last_seen_epoch, battery_v, battery_percent, charging, charge_done,
                    wifi_rssi_dbm, recording_status, upload_status, mode, raw_status_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    key_id=excluded.key_id,
                    last_seen_epoch=excluded.last_seen_epoch,
                    battery_v=excluded.battery_v,
                    battery_percent=excluded.battery_percent,
                    charging=excluded.charging,
                    charge_done=excluded.charge_done,
                    wifi_rssi_dbm=excluded.wifi_rssi_dbm,
                    recording_status=excluded.recording_status,
                    upload_status=excluded.upload_status,
                    mode=excluded.mode,
                    raw_status_json=excluded.raw_status_json
                """,
                (
                    node_id,
                    auth["key_id"],
                    now,
                    payload.get("battery_v"),
                    payload.get("battery_percent"),
                    bool_int(payload.get("charging")),
                    bool_int(payload.get("charge_done")),
                    payload.get("wifi_rssi_dbm"),
                    payload.get("recording_status"),
                    upload_status,
                    payload.get("mode"),
                    raw_json,
                ),
            )
            conn.execute(
                """
                INSERT INTO heartbeats (
                    node_id, received_epoch, battery_v, charging, charge_done, wifi_rssi_dbm, upload_status, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    now,
                    payload.get("battery_v"),
                    bool_int(payload.get("charging")),
                    bool_int(payload.get("charge_done")),
                    payload.get("wifi_rssi_dbm"),
                    upload_status,
                    raw_json,
                ),
            )
        return {"ok": True}

    @app.post("/v1/device/time_check")
    async def time_check(request: Request) -> dict[str, bool]:
        body = await request.body()
        auth = await verify_hmac(request, body)
        payload = json.loads(body.decode("utf-8") or "{}")
        node_id = payload.get("node_id")
        if node_id != auth["node_id"]:
            raise HTTPException(status_code=403, detail="node_id mismatch")
        safe_node_id(node_id)
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO time_checks (
                    node_id, received_epoch, server_epoch, esp_epoch_after, audiomoth_epoch, rtt_ms, notes, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    utc_now(),
                    payload.get("server_epoch"),
                    payload.get("esp_epoch_after"),
                    payload.get("audiomoth_epoch"),
                    payload.get("rtt_ms"),
                    payload.get("notes"),
                    json_dumps(payload),
                ),
            )
        return {"ok": True}

    @app.get("/v1/device/{node_id}/commands")
    async def get_commands(node_id: str, request: Request) -> dict[str, list[dict[str, Any]]]:
        body = await request.body()
        auth = await verify_hmac(request, body)
        if node_id != auth["node_id"]:
            raise HTTPException(status_code=403, detail="node_id mismatch")
        safe_node_id(node_id)
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT id, type, payload_json
                FROM commands
                WHERE node_id = ? AND status = 'pending'
                ORDER BY created_epoch ASC, id ASC
                LIMIT 10
                """,
                (node_id,),
            ).fetchall()
        return {
            "commands": [
                {"id": int(row["id"]), "type": row["type"], "payload": json.loads(row["payload_json"] or "{}")}
                for row in rows
            ]
        }

    @app.post("/v1/device/{node_id}/commands/{command_id}/ack")
    async def ack_command(node_id: str, command_id: int, request: Request) -> dict[str, bool]:
        body = await request.body()
        auth = await verify_hmac(request, body)
        if node_id != auth["node_id"]:
            raise HTTPException(status_code=403, detail="node_id mismatch")
        payload = json.loads(body.decode("utf-8") or "{}")
        response = payload.get("response", {})
        with get_conn() as conn:
            cur = conn.execute(
                """
                UPDATE commands
                SET status = 'acknowledged', acknowledged_epoch = ?, response_json = ?
                WHERE id = ? AND node_id = ? AND status = 'pending'
                """,
                (utc_now(), json_dumps(response), command_id, node_id),
            )
            if cur.rowcount != 1:
                raise HTTPException(status_code=404, detail="pending command not found")
        return {"ok": True}

    @app.post("/v1/device/{node_id}/upload/start")
    async def upload_start(node_id: str, request: Request) -> dict[str, Any]:
        body = await request.body()
        auth = await verify_hmac(request, body)
        if node_id != auth["node_id"]:
            raise HTTPException(status_code=403, detail="node_id mismatch")
        payload = json.loads(body.decode("utf-8") or "{}")
        if payload.get("node_id") != node_id:
            raise HTTPException(status_code=403, detail="body node_id mismatch")
        source = sanitize_moth_path(payload.get("path"))
        size = int(payload.get("size"))
        chunk_bytes = int(payload.get("chunk_bytes") or 0) or None
        if size <= 0:
            raise HTTPException(status_code=400, detail="size must be positive")
        if chunk_bytes is not None and chunk_bytes <= 0:
            raise HTTPException(status_code=400, detail="chunk_bytes must be positive")
        temp_path = temp_path_for(node_id, source)
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(b"")
        now = utc_now()
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE upload_sessions
                SET status = 'failed', updated_epoch = ?, error_message = 'reset by new upload start'
                WHERE node_id = ? AND source_path = ? AND status IN ('started', 'uploading')
                """,
                (now, node_id, source.as_posix()),
            )
            cur = conn.execute(
                """
                INSERT INTO upload_sessions (
                    node_id, source_path, expected_size, chunk_bytes, received_bytes, status,
                    temp_path, final_path, started_epoch, created_epoch, updated_epoch
                ) VALUES (?, ?, ?, ?, 0, 'started', ?, NULL, ?, ?, ?)
                """,
                (
                    node_id,
                    source.as_posix(),
                    size,
                    chunk_bytes,
                    str(temp_path),
                    payload.get("started_epoch"),
                    now,
                    now,
                ),
            )
            upload_id = int(cur.lastrowid)
        return {"ok": True, "upload_id": upload_id, "path": source.as_posix(), "size": size}

    @app.post("/v1/device/{node_id}/upload/chunk")
    async def upload_chunk(
        node_id: str,
        request: Request,
        path: str,
        offset: int,
        length: int,
        total: int,
        crc32: str,
    ) -> dict[str, Any]:
        body = await request.body()
        auth = await verify_hmac(request, body, include_query=True)
        query_node_id = request.query_params.get("node_id")
        if node_id != auth["node_id"] or query_node_id != node_id:
            raise HTTPException(status_code=403, detail="node_id mismatch")
        source = sanitize_moth_path(path)
        if length <= 0:
            raise HTTPException(status_code=400, detail="length must be positive")
        if len(body) != length:
            raise HTTPException(status_code=400, detail="body length mismatch")
        if offset < 0 or total <= 0 or offset + length > total:
            raise HTTPException(status_code=400, detail="invalid offset/length/total")
        expected_crc = f"{zlib.crc32(body) & 0xFFFFFFFF:08X}"
        if expected_crc.upper() != str(crc32).upper():
            raise HTTPException(status_code=400, detail="crc32 mismatch")

        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM upload_sessions
                WHERE node_id = ? AND source_path = ? AND status IN ('started', 'uploading')
                ORDER BY id DESC
                LIMIT 1
                """,
                (node_id, source.as_posix()),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="upload session not found")
            if int(row["expected_size"]) != total:
                raise HTTPException(status_code=400, detail="total does not match upload session")
            temp_path = Path(row["temp_path"])
            if not temp_path.exists():
                raise HTTPException(status_code=404, detail="upload temp file missing")
            with temp_path.open("r+b") as handle:
                handle.seek(offset)
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            received = max(int(row["received_bytes"] or 0), offset + length)
            conn.execute(
                """
                UPDATE upload_sessions
                SET received_bytes = ?, status = 'uploading', updated_epoch = ?, error_message = NULL
                WHERE id = ?
                """,
                (received, utc_now(), row["id"]),
            )
        return {"ok": True, "path": source.as_posix(), "offset": offset, "length": length, "received_bytes": received}

    @app.post("/v1/device/{node_id}/upload/finish")
    async def upload_finish(node_id: str, request: Request) -> dict[str, Any]:
        body = await request.body()
        auth = await verify_hmac(request, body)
        if node_id != auth["node_id"]:
            raise HTTPException(status_code=403, detail="node_id mismatch")
        payload = json.loads(body.decode("utf-8") or "{}")
        if payload.get("node_id") != node_id:
            raise HTTPException(status_code=403, detail="body node_id mismatch")
        source = sanitize_moth_path(payload.get("path"))
        size = int(payload.get("size"))
        if size <= 0:
            raise HTTPException(status_code=400, detail="size must be positive")
        final_path = final_path_for(node_id, source)
        now = utc_now()
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM upload_sessions
                WHERE node_id = ? AND source_path = ? AND status IN ('started', 'uploading')
                ORDER BY id DESC
                LIMIT 1
                """,
                (node_id, source.as_posix()),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="upload session not found")
            if int(row["expected_size"]) != size:
                raise HTTPException(status_code=400, detail="size does not match upload session")
            if int(row["received_bytes"] or 0) < size:
                raise HTTPException(status_code=400, detail="upload incomplete")
            temp_path = Path(row["temp_path"])
            if not temp_path.exists():
                raise HTTPException(status_code=404, detail="upload temp file missing")
            actual_size = temp_path.stat().st_size
            if actual_size != size:
                raise HTTPException(status_code=400, detail="temp file size mismatch")
            os.replace(temp_path, final_path)
            file_sha = sha256_file(final_path)
            recording_epoch = parse_recording_epoch(source.as_posix())
            conn.execute(
                """
                UPDATE upload_sessions
                SET status = 'complete', final_path = ?, finished_epoch = ?, updated_epoch = ?, error_message = NULL
                WHERE id = ?
                """,
                (str(final_path), payload.get("finished_epoch") or now, now, row["id"]),
            )
            conn.execute(
                """
                INSERT INTO recordings (
                    node_id, source_path, stored_path, size, uploaded_epoch, recording_epoch,
                    sha256, weather_status, weather_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL)
                ON CONFLICT(node_id, source_path) DO UPDATE SET
                    stored_path=excluded.stored_path,
                    size=excluded.size,
                    uploaded_epoch=excluded.uploaded_epoch,
                    recording_epoch=excluded.recording_epoch,
                    sha256=excluded.sha256,
                    weather_status=COALESCE(recordings.weather_status, 'pending')
                """,
                (node_id, source.as_posix(), str(final_path), size, now, recording_epoch, file_sha),
            )
            conn.execute(
                """
                UPDATE nodes
                SET last_seen_epoch = ?, upload_status = ?
                WHERE node_id = ?
                """,
                (now, f"upload complete: {source.as_posix()}", node_id),
            )
        return {"ok": True, "path": source.as_posix(), "size": size, "stored_path": str(final_path)}

    @app.post("/v1/admin/{node_id}/commands/{command_type}")
    def queue_command(node_id: str, command_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        safe_node_id(node_id)
        command_type = require_allowed_command(command_type)
        now = utc_now()
        with get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO commands (node_id, type, payload_json, status, created_epoch)
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (node_id, command_type, json_dumps(payload or {}), now),
            )
        return {"ok": True, "id": int(cur.lastrowid), "type": command_type}

    return app


app = create_app()
