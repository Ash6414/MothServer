from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Optional, Tuple

from fastapi import HTTPException, Request

import bat_server

app = bat_server.app

NODE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
PROVISIONING_TOKEN = os.getenv("PROVISIONING_TOKEN", "").strip()
FLAC_ENCODER = os.getenv("FLAC_ENCODER", "auto").strip().lower()
FLAC_COMPRESSION_LEVEL = os.getenv("FLAC_COMPRESSION_LEVEL", "5").strip()


def _route_exists(path: str, method: str) -> bool:
    wanted = method.upper()
    for route in app.routes:
        if getattr(route, "path", None) == path and wanted in getattr(route, "methods", set()):
            return True
    return False


def _clamp_int(value: str, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def find_flac_encoder() -> Tuple[Optional[str], Optional[str]]:
    if FLAC_ENCODER in ("none", "off", "disabled"):
        return None, None
    if FLAC_ENCODER == "flac":
        candidates = ("flac",)
    elif FLAC_ENCODER == "ffmpeg":
        candidates = ("ffmpeg",)
    else:
        candidates = ("flac", "ffmpeg")

    for name in candidates:
        path = shutil.which(name)
        if path:
            return name, path
    return None, None


def flac_command(encoder_name: str, encoder_path: str, wav_path: Path, flac_path: Path) -> list[str]:
    if encoder_name == "flac":
        level = _clamp_int(FLAC_COMPRESSION_LEVEL, 5, 0, 8)
        return [encoder_path, f"-{level}", "-f", "-s", "-o", str(flac_path), str(wav_path)]

    level = _clamp_int(FLAC_COMPRESSION_LEVEL, 5, 0, 12)
    return [
        encoder_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(wav_path),
        "-compression_level",
        str(level),
        str(flac_path),
    ]


def make_flac(wav_path: Path, node_id: str) -> Tuple[str, Optional[Path], Optional[str]]:
    encoder_name, encoder_path = find_flac_encoder()
    if not encoder_name or not encoder_path:
        return "SKIPPED_NO_ENCODER", None, None

    out_dir = bat_server.safe_join(bat_server.FLAC_DIR, node_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    flac_path = bat_server.safe_join(out_dir, wav_path.with_suffix(".flac").name)
    result = subprocess.run(
        flac_command(encoder_name, encoder_path, wav_path, flac_path),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        try:
            flac_path.unlink(missing_ok=True)
        except OSError:
            pass
        return "ERROR", None, result.stderr.strip() or f"{encoder_name} failed"
    return "OK", flac_path, None


def maybe_make_flac(wav_path: Path, node_id: str) -> Optional[Path]:
    status, flac_path, error = make_flac(wav_path, node_id)
    if status == "ERROR":
        raise RuntimeError(error or "FLAC conversion failed")
    return flac_path


bat_server.find_flac_encoder = find_flac_encoder
bat_server.make_flac = make_flac
bat_server.maybe_make_flac = maybe_make_flac


if not _route_exists("/v1/provision/node", "POST"):

    @app.post("/v1/provision/node")
    async def provision_node(request: Request) -> dict[str, Any]:
        if not PROVISIONING_TOKEN:
            raise HTTPException(status_code=503, detail="Provisioning is not enabled on this server")

        try:
            data = json.loads((await request.body()).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Bad JSON")

        token = str(data.get("provisioning_token") or "")
        if not bat_server.constant_time_eq(token, PROVISIONING_TOKEN):
            raise HTTPException(status_code=401, detail="Bad provisioning token")

        requested_node_id = str(data.get("node_id") or "").strip()
        if requested_node_id and not NODE_ID_PATTERN.fullmatch(requested_node_id):
            raise HTTPException(status_code=400, detail="Invalid node_id")

        node_name = str(data.get("node_name") or requested_node_id or "Bat Node").strip()[:96]
        firmware = str(data.get("firmware_version") or "")[:96] or None
        hardware = str(data.get("hardware_version") or "")[:96] or None
        notes = str(data.get("deployment_notes") or "self-provisioned ESP32 node")[:512]
        key_id = str(data.get("key_id") or "key-1").strip()[:48] or "key-1"
        secret = secrets.token_hex(32)
        t = bat_server.now_epoch()

        with bat_server.db_connect() as conn:
            if requested_node_id:
                node_id = requested_node_id
            else:
                for _ in range(10):
                    candidate = f"BATNODE_{secrets.token_hex(4).upper()}"
                    exists = conn.execute("SELECT 1 FROM nodes WHERE node_id=?", (candidate,)).fetchone()
                    if not exists:
                        node_id = candidate
                        break
                else:
                    raise HTTPException(status_code=500, detail="Could not allocate node_id")

            conn.execute(
                """
                INSERT INTO nodes (
                    node_id, node_name, location_lat, location_lon, location_label,
                    deployment_notes, firmware_version, hardware_version, active,
                    compromised, created_at, updated_at
                ) VALUES (?, ?, NULL, NULL, NULL, ?, ?, ?, 1, 0, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    node_name=excluded.node_name,
                    deployment_notes=excluded.deployment_notes,
                    firmware_version=excluded.firmware_version,
                    hardware_version=excluded.hardware_version,
                    active=1,
                    compromised=0,
                    updated_at=excluded.updated_at
                """,
                (node_id, node_name, notes, firmware, hardware, t, t),
            )
            conn.execute(
                """
                INSERT INTO node_credentials (node_id, key_id, secret, created_at, expires_at, revoked_at)
                VALUES (?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(node_id, key_id) DO UPDATE SET
                    secret=excluded.secret,
                    revoked_at=NULL
                """,
                (node_id, key_id, secret, t),
            )
            conn.commit()

        try:
            bat_server.audit(
                "provisioning",
                "node_provisioned",
                "node",
                node_id,
                request.client.host if request.client else "",
                {"node_name": node_name, "key_id": key_id},
            )
        except (sqlite3.Error, AttributeError):
            pass

        return {
            "ok": True,
            "node_id": node_id,
            "key_id": key_id,
            "device_secret": secret,
            "server_time": t,
        }
