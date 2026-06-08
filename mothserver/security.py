from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time

from fastapi import HTTPException, Request

from .config import settings
from .db import get_conn


def _header(request: Request, name: str) -> str:
    value = request.headers.get(name)
    if value is None or value == "":
        raise HTTPException(status_code=401, detail=f"missing {name}")
    return value.strip()


def _secret_for(node_id: str, key_id: str) -> str:
    try:
        return settings.node_secrets[node_id][key_id]
    except KeyError as exc:
        raise HTTPException(status_code=403, detail="unknown node/key") from exc


def _store_nonce(node_id: str, nonce: str, timestamp_value: int) -> None:
    now = int(time.time())
    cutoff = now - settings.nonce_retention_seconds
    with get_conn() as conn:
        conn.execute("DELETE FROM auth_nonces WHERE created_epoch < ?", (cutoff,))
        try:
            conn.execute(
                "INSERT INTO auth_nonces (node_id, nonce, timestamp, created_epoch) VALUES (?, ?, ?, ?)",
                (node_id, nonce, timestamp_value, now),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=401, detail="duplicate nonce rejected") from exc


async def verify_hmac(request: Request, body: bytes, include_query: bool = False) -> dict[str, str]:
    node_id = _header(request, "X-Node-ID")
    key_id = _header(request, "X-Key-ID")
    timestamp = _header(request, "X-Timestamp")
    nonce = _header(request, "X-Nonce")
    body_sha_header = _header(request, "X-Body-SHA256").lower()
    supplied_sig = _header(request, "X-Signature").lower()

    try:
        timestamp_int = int(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid timestamp") from exc

    if abs(int(time.time()) - timestamp_int) > settings.auth_max_clock_drift_seconds:
        raise HTTPException(status_code=401, detail="timestamp drift rejected")

    body_sha = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(body_sha, body_sha_header):
        raise HTTPException(status_code=401, detail="body hash mismatch")

    path = request.url.path + ("?" + request.url.query if include_query else "")
    canonical = "\n".join([request.method.upper(), path, timestamp, nonce, body_sha])
    expected = hmac.new(_secret_for(node_id, key_id).encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_sig):
        raise HTTPException(status_code=403, detail="signature mismatch")

    _store_nonce(node_id, nonce, timestamp_int)
    return {"node_id": node_id, "key_id": key_id, "timestamp": timestamp, "nonce": nonce}


def require_allowed_command(command_type: str) -> str:
    allowed = {"PING", "UPLOAD_NOW", "SYNC_MOTH_TIME", "MOTH_STATUS"}
    if command_type not in allowed:
        raise HTTPException(status_code=400, detail=f"unsupported command type: {command_type}")
    return command_type
