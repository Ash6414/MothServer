from __future__ import annotations


PUBLIC_EXACT_PATHS = {
    "/health",
    "/v1/public/server_time",
    "/v1/files/manifest",
    "/v1/uploads/init",
}

PUBLIC_PATH_PREFIXES = (
    "/v1/enrollment/",
    "/v1/device/",
    "/v1/uploads/",
    "/v1/nodes/",
)


def is_public_device_path(path: str) -> bool:
    return path in PUBLIC_EXACT_PATHS or path.startswith(PUBLIC_PATH_PREFIXES)
