from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from bat_server_runtime import app
from gateway_policy import is_public_device_path


@app.middleware("http")
async def device_gateway_only(request: Request, call_next):
    if not is_public_device_path(request.url.path):
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    return await call_next(request)
