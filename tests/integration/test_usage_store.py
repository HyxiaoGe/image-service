from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from src.services.usage import UsageStore


async def test_record_survives_reopen_and_summary_is_product_scoped(tmp_path):
    path = str(tmp_path / "usage.sqlite3")
    store = UsageStore(path)
    await store.begin(
        {
            "request_id": "r1",
            "app_id": "fusion",
            "subject": "u1",
            "scene": "chat_image",
            "entry": "mcp",
            "requested_model": "doubao-seedream-5-0-flash",
        }
    )
    attempt = await store.start_attempt(
        "r1", {"model": "doubao-seedream-5-0-flash", "provider": "ark", "credential_alias": "fusion"}
    )
    await store.finish_attempt(
        attempt,
        {"outcome": "response", "http_status": 200, "upstream_request_id": "ark-1", "estimated_cost_cny": "0.12"},
    )
    await store.finish(
        "r1",
        {"outcome": "success", "generation_id": "image-1", "model": "doubao-seedream-5-0-flash", "duration_ms": 50},
    )
    reopened = UsageStore(path)
    fusion = await reopened.summary("fusion")
    assert fusion["requests"] == fusion["generated_images"] == fusion["upstream_attempts"] == 1
    assert fusion["estimated_cost_cny"] == "0.12"
    assert (await reopened.summary("validation"))["requests"] == 0


async def test_unfinished_attempt_is_potentially_billable_after_restart(tmp_path):
    path = str(tmp_path / "usage.sqlite3")
    store = UsageStore(path)
    await store.begin({"request_id": "r1", "app_id": "fusion", "subject": "u1"})
    await store.start_attempt(
        "r1", {"model": "doubao-seedream-5-0-pro", "provider": "ark", "credential_alias": "fusion"}
    )
    result = await UsageStore(path).summary("fusion")
    assert result["potentially_billable_attempts"] == 1
    assert result["estimated_cost_cny"] == "0.00"
    assert result["incomplete_requests"] == 1


async def test_retention_keeps_recent_records_and_prunes_old_attempts(tmp_path):
    path = str(tmp_path / "usage.sqlite3")
    store = UsageStore(path)
    for request_id in ("old", "recent"):
        await store.begin({"request_id": request_id, "app_id": "fusion"})
        await store.start_attempt(request_id, {"provider": "ark"})
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE requests SET started_at=? WHERE request_id='old'",
            ((datetime.now(UTC) - timedelta(days=91)).isoformat(),),
        )
    report = await UsageStore(path).summary("fusion")
    assert report["requests"] == report["upstream_attempts"] == 1
    assert report["estimate_complete"] is False
