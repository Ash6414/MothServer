from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

DEFAULT_DEVICE_SECRET = "REPLACE_WITH_64_HEX_OR_SERVER_SECRET"
DEFAULT_NODE_ID = "BATNODE_001"
DEFAULT_KEY_ID = "key-1"


def _load_node_secrets() -> Dict[str, Dict[str, str]]:
    """Load node secrets from JSON or simple environment variables.

    HMAC keys are used as literal UTF-8 bytes. They are not hex-decoded.
    """
    raw_json = os.getenv("NODE_SECRETS_JSON", "").strip()
    if raw_json:
        parsed = json.loads(raw_json)
        if not isinstance(parsed, dict):
            raise ValueError("NODE_SECRETS_JSON must be an object")
        return {
            str(node_id): {str(key_id): str(secret) for key_id, secret in keys.items()}
            for node_id, keys in parsed.items()
        }

    node_id = os.getenv("MOTH_NODE_ID", DEFAULT_NODE_ID)
    key_id = os.getenv("MOTH_KEY_ID", DEFAULT_KEY_ID)
    secret = os.getenv("MOTH_DEVICE_SECRET", DEFAULT_DEVICE_SECRET)
    return {node_id: {key_id: secret}}


@dataclass(frozen=True)
class Settings:
    db_path: Path
    upload_root: Path
    auth_max_clock_drift_seconds: int
    nonce_retention_seconds: int
    node_secrets: Dict[str, Dict[str, str]]


settings = Settings(
    db_path=Path(os.getenv("MOTHSERVER_DB_PATH", "data/mothserver.sqlite3")),
    upload_root=Path(os.getenv("MOTHSERVER_UPLOAD_ROOT", "data/uploads")),
    auth_max_clock_drift_seconds=int(os.getenv("AUTH_MAX_CLOCK_DRIFT_SECONDS", "900")),
    nonce_retention_seconds=int(os.getenv("AUTH_NONCE_RETENTION_SECONDS", "86400")),
    node_secrets=_load_node_secrets(),
)
