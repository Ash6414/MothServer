from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from fastapi import HTTPException

from .config import settings

_SAFE_PART = re.compile(r"^[A-Za-z0-9._-]+$")


def sanitize_moth_path(path: str) -> PurePosixPath:
    if not isinstance(path, str):
        raise HTTPException(status_code=400, detail="path must be a string")
    value = path.strip()
    if not value:
        raise HTTPException(status_code=400, detail="empty path rejected")
    if "\\" in value:
        raise HTTPException(status_code=400, detail="backslashes are not allowed in paths")

    posix = PurePosixPath(value)
    if posix.is_absolute():
        raise HTTPException(status_code=400, detail="absolute paths are rejected")
    if ".." in posix.parts:
        raise HTTPException(status_code=400, detail="path traversal is rejected")
    if len(posix.parts) > 2:
        raise HTTPException(status_code=400, detail="only one folder level is allowed")
    if not posix.name.lower().endswith(".wav"):
        raise HTTPException(status_code=400, detail="only WAV files are accepted")
    for part in posix.parts:
        if part in ("", ".") or not _SAFE_PART.fullmatch(part) or part.startswith("."):
            raise HTTPException(status_code=400, detail="unsafe path component rejected")
    return posix


def safe_node_id(node_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", node_id or ""):
        raise HTTPException(status_code=400, detail="unsafe node_id")
    return node_id


def node_upload_dirs(node_id: str) -> tuple[Path, Path]:
    node = safe_node_id(node_id)
    incoming = settings.upload_root / node / "incoming"
    recordings = settings.upload_root / node / "recordings"
    incoming.mkdir(parents=True, exist_ok=True)
    recordings.mkdir(parents=True, exist_ok=True)
    return incoming, recordings


def temp_path_for(node_id: str, moth_path: PurePosixPath) -> Path:
    incoming, _ = node_upload_dirs(node_id)
    target = incoming.joinpath(*moth_path.parts).with_suffix(moth_path.suffix + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_inside(target, incoming)
    return target


def final_path_for(node_id: str, moth_path: PurePosixPath) -> Path:
    _, recordings = node_upload_dirs(node_id)
    target = recordings.joinpath(*moth_path.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_inside(target, recordings)
    return target


def _assert_inside(path: Path, base: Path) -> None:
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="resolved path escaped upload directory") from exc
