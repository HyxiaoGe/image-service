from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from src.services.usage import UsageStore, ark_estimate
from tests.contract import test_caller_auth as auth_fixtures

secured = auth_fixtures.secured
FUSION_KEY = auth_fixtures.FUSION_KEY
VALIDATION_KEY = auth_fixtures.VALIDATION_KEY


async def post(client, bearer=FUSION_KEY, **kwargs):
    return await client.post(
        "/v1/generate",
        headers={
            "Authorization": "Bearer " + bearer,
            "X-Image-User": "user-1",
            "X-Image-Scene": "chat_image",
            "X-Request-ID": "fusion-run-123",
        },
        json={"prompt": "a private cup prompt", "model": "doubao-seedream-5-0-flash", **kwargs},
    )


async def test_real_chain_persists_and_reports_without_credentials(client, app, secured):
    s = app.state.settings
    s.usage_enabled = True
    s.usage_db_path = s.storage_dir + "/usage.sqlite3"
    bearer = FUSION_KEY
    first = await post(client, bearer)
    assert first.status_code == 200, first.text
    second = await post(client, bearer)
    assert second.json()["cached"] is True
    report = await client.get("/v1/usage", headers={"Authorization": "Bearer " + bearer})
    assert report.status_code == 200, report.text
    data = report.json()
    assert data["requests"] == 2
    assert data["generated_images"] == data["upstream_attempts"] == data["cache_hits"] == 1
    assert data["estimated_cost_cny"] == "0.12"
    assert data["breakdown"][0]["scene"] == "chat_image"
    other_product_query = await client.get("/v1/usage?app_id=validation", headers={"Authorization": "Bearer " + bearer})
    assert other_product_query.json()["app_id"] == "fusion"
    with sqlite3.connect(s.usage_db_path) as db:
        request = json.loads(db.execute("SELECT data FROM requests ORDER BY started_at LIMIT 1").fetchone()[0])
        attempt = json.loads(db.execute("SELECT data FROM attempts").fetchone()[0])
        raw = str(db.execute("SELECT data FROM requests").fetchall()) + str(
            db.execute("SELECT data FROM attempts").fetchall()
        )
    assert request["subject"] == "user-1"
    assert request["principal_type"] == "user"
    assert request["correlation_id"] == "fusion-run-123"
    assert attempt["credential_alias"] == "fusion"
    for secret in (bearer, "fusion-upstream-secret", "a private cup prompt", "Bearer"):
        assert secret not in raw
    assert (await UsageStore(s.usage_db_path).summary("validation"))["requests"] == 0


async def test_source_headers_and_scene_cannot_change_billing_product(client, app, secured):
    app.state.settings.usage_enabled = True
    response = await client.post(
        "/v1/generate",
        headers={
            "Authorization": "Bearer " + FUSION_KEY,
            "X-App-ID": "validation",
            "X-Image-Scene": "validation",
        },
        json={"prompt": "spoofed source", "model": "doubao-seedream-5-0-flash", "app_id": "validation"},
    )
    assert response.status_code == 200
    assert secured.last_request.headers["authorization"] == "Bearer fusion-upstream-secret"
    report = await client.get("/v1/usage", headers={"Authorization": "Bearer " + FUSION_KEY})
    assert report.json()["app_id"] == "fusion"
    assert report.json()["breakdown"][0]["scene"] == "validation"


async def test_timeout_attempt_is_recorded_without_retrying_charge(client, app, secured):
    app.state.settings.usage_enabled = True
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("read timeout", request=request)

    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    response = await post(client, FUSION_KEY, fallback_model="none")
    assert response.status_code == 504
    assert len(calls) == 1
    report = await client.get("/v1/usage", headers={"Authorization": "Bearer " + FUSION_KEY})
    data = report.json()
    assert data["requests"] == data["errors"] == data["potentially_billable_attempts"] == 1
    assert data["generated_images"] == 0


async def test_ledger_start_failure_prevents_paid_request(client, app, secured, monkeypatch):
    app.state.settings.usage_enabled = True

    async def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(UsageStore, "start_attempt", fail)
    response = await post(client)
    assert response.status_code == 503
    assert response.json()["error"] == "usage_unavailable"
    assert secured.call_count == 0


async def test_ledger_update_failure_does_not_resubmit(client, app, secured, monkeypatch):
    app.state.settings.usage_enabled = True

    async def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(UsageStore, "finish_attempt", fail)
    response = await post(client)
    assert response.status_code == 200
    assert secured.call_count == 1
    s = app.state.settings
    report = await UsageStore(str(Path(s.storage_dir).parent / "usage.sqlite3")).summary("fusion")
    assert report["potentially_billable_attempts"] == 1


async def test_hung_final_accounting_does_not_hang_successful_response(client, app, secured, monkeypatch):
    app.state.settings.usage_enabled = True

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(UsageStore, "finish", hang)
    response = await asyncio.wait_for(post(client), 1.5)
    assert response.status_code == 200
    assert secured.call_count == 1


async def test_usage_storage_is_in_health_check(client, app, secured):
    app.state.settings.usage_enabled = True
    app.state.settings.usage_db_path = app.state.settings.storage_dir  # 目录不能作为数据库。
    health = await client.get("/v1/health")
    assert health.json()["status"] == "degraded"
    assert "error" in health.json()["checks"]["usage"]
    response = await post(client)
    assert response.status_code == 503
    assert secured.call_count == 0


async def test_concurrent_products_do_not_swap_keys(client, app, secured):
    seen = {}

    async def respond(request):
        body = json.loads(request.content)
        seen[body["prompt"]] = request.headers["authorization"]
        await asyncio.sleep(0.01)
        from tests.conftest import make_seedream_response

        return httpx.Response(200, json=make_seedream_response())

    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    a, b = FUSION_KEY, VALIDATION_KEY
    responses = await asyncio.gather(
        post(client, a, prompt="fusion prompt"), post(client, b, prompt="validation prompt")
    )
    assert all(r.status_code == 200 for r in responses)
    assert seen == {
        "fusion prompt": "Bearer fusion-upstream-secret",
        "validation prompt": "Bearer validation-upstream-secret",
    }


@pytest.mark.parametrize(
    ("model", "size", "refs", "expected"),
    [
        ("doubao-seedream-5-0-flash-260915", "1024x1024", 3, "0.12"),
        ("doubao-seedream-5-0-260128", "1920x1920", 3, "0.22"),
        ("doubao-seedream-5-0-pro-260628", "1536x1536", 1, "0.30"),
        ("doubao-seedream-5-0-pro-260628", "2048x2048", 3, "0.64"),
    ],
)
def test_cost_uses_wire_model_pixel_tier_and_reference_count(model, size, refs, expected):
    estimate = ark_estimate(
        {"model": model, "size": size, "image": ["redacted"] * refs}, {"data": [{"b64_json": "redacted"}]}
    )
    assert estimate["estimated_cost_cny"] == expected


async def test_mcp_http_uses_request_identity_per_product(client, app, fake_redis, secured, monkeypatch):
    # 启用真实 FastMCP HTTP lifespan，保留测试 Redis 和假上游。
    monkeypatch.setattr("src.main.aioredis.from_url", lambda *a, **kw: fake_redis)
    headers = {
        "Authorization": "Bearer " + FUSION_KEY,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    async with app.router.lifespan_context(app):
        response = await client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "generate_image",
                    "arguments": {"prompt": "mcp cup", "model": "doubao-seedream-5-0-flash"},
                },
            },
        )
        assert response.status_code == 200, response.text
        assert "fusion-upstream-secret" not in response.text
        assert "Generated image:" in response.text
        assert secured.last_request.headers["authorization"] == "Bearer fusion-upstream-secret"
        headers["Authorization"] = "Bearer " + VALIDATION_KEY
        discovery = await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert discovery.status_code == 200, discovery.text
        assert "generate_image" in discovery.text
