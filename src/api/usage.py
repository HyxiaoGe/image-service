from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.services.usage import usage_store

router = APIRouter()


@router.get("/usage")
async def usage(request: Request, since: datetime | None = None, until: datetime | None = None):
    """仅供获授 usage:read 的调用方读取自身产品统计，until 为开区间。"""
    caller = request.state.image_caller
    settings = request.app.state.settings
    if not settings.usage_enabled:
        return JSONResponse({"error": "usage_unavailable"}, status_code=503)
    if any(value is not None and value.tzinfo is None for value in (since, until)) or (
        since and until and since >= until
    ):
        return JSONResponse(
            {"error": "invalid_params", "message": "Use timezone-aware timestamps and since < until."}, status_code=422
        )
    path = settings.usage_db_path or str(Path(settings.storage_dir).parent / "usage.sqlite3")
    try:
        return await usage_store(path, settings.usage_retention_days).summary(
            caller.app_id,
            since=since.astimezone(UTC).isoformat() if since else None,
            until=until.astimezone(UTC).isoformat() if until else None,
        )
    except Exception:
        return JSONResponse({"error": "usage_unavailable"}, status_code=503)
