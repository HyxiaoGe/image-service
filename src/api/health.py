from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Request

from src import __version__
from src.models.response import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    settings = request.app.state.settings
    cache = request.app.state.cache

    checks: dict[str, str] = {}

    # Check Redis
    try:
        redis_ok = await cache.ping()
        checks["redis"] = "ok" if redis_ok else "error: ping returned false"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    # Check storage directory
    storage_path = Path(settings.storage_dir)
    if storage_path.is_dir() and os.access(str(storage_path), os.W_OK):
        checks["storage"] = "ok"
    else:
        checks["storage"] = "error: storage directory not writable"

    # Determine overall status
    all_ok = all(v == "ok" for v in checks.values())
    status = "healthy" if all_ok else "degraded"

    return HealthResponse(
        status=status,
        version=__version__,
        checks=checks,
    )
