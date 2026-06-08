from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path
from urllib import request as urlrequest

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")
NODE_ID = os.getenv("NODE_ID", "BATNODE_001")
KEY_ID = os.getenv("KEY_ID", "key-1")
DEVICE_SECRET = os.getenv("DEVICE_SECRET", "replace-me")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hmac_hex(secret: str, msg: str) -> str:
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def signed_request(method: str, path: str, body: bytes = b"", content_type: str = "application/json"):
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body_hash = sha256_hex(body)
    canonical = "\n".join([method.upper(), path, ts, nonce, body_hash])
    sig = hmac_hex(DEVICE_SECRET, canonical)
    req = urlrequest.Request(BASE_URL + path, data=body if method.upper() not in ("GET",) else None, method=method.upper())
    req.add_header("X-Node-ID", NODE_ID)
    req.add_header("X-Key-ID", KEY_ID)
    req.add_header("X-Timestamp", ts)
    req.add_header("X-Nonce", nonce)
    req.add_header("X-Body-SHA256", body_hash)
    req.add_header("X-Signature", sig)
    if body:
        req.add_header("Content-Type", content_type)
    with urlrequest.urlopen(req, timeout=20) as resp:
        raw = resp.read()
        try:
            return json.loads(raw.decode())
        except Exception:
            return raw.decode()


def heartbeat():
    body = json.dumps({
        "node_id": NODE_ID,
        "battery_v": 4.05,
        "battery_percent": 83,
        "solar_v": 5.3,
        "charging": True,
        "charge_done": False,
        "recently_charged": True,
        "sd_free_mb": 102400,
        "recording_status": "IDLE",
        "upload_status": "CHECKIN",
        "wifi_rssi_dbm": -58,
        "mode": "BENCH_TEST",
        "message": "hello from signed test client"
    }).encode()
    print(signed_request("POST", "/v1/device/heartbeat", body))


def manifest():
    body = json.dumps({
        "node_id": NODE_ID,
        "deployment_id": "BENCH",
        "manifest_id": "BENCH_MANIFEST_001",
        "sd_card_id": "SD_TEST_001",
        "files": [
            {
                "local_file_id": 1,
                "filename": "test.wav",
                "recorded_at": "2026-05-26T21:30:00",
                "duration_seconds": 1.0,
                "sample_rate": 384000,
                "channels": 1,
                "bit_depth": 16,
                "file_size_bytes": 44
            }
        ]
    }).encode()
    print(signed_request("POST", "/v1/files/manifest", body))


def commands():
    print(signed_request("GET", f"/v1/device/{NODE_ID}/commands", b""))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["heartbeat", "manifest", "commands"])
    args = p.parse_args()
    if args.action == "heartbeat":
        heartbeat()
    elif args.action == "manifest":
        manifest()
    elif args.action == "commands":
        commands()


if __name__ == "__main__":
    main()
