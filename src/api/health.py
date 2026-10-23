from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Request

from src import __version__
from src.models.response import HealthResponse
from src.services.usage import usage_store

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

    if settings.usage_enabled:
        path = settings.usage_db_path or str(storage_path.parent / "usage.sqlite3")
        try:
            checks["usage"] = (
                "ok" if await usage_store(path, settings.usage_retention_days).ping() else "error: unavailable"
            )
        except Exception:
            checks["usage"] = "error: accounting storage unavailable"

    # Determine overall status
    all_ok = all(v == "ok" for v in checks.values())
    status = "healthy" if all_ok else "degraded"

    return HealthResponse(
        status=status,
        version=__version__,
        checks=checks,
    )
