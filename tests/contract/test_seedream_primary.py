"""Seedream primary generation, fallback, and pixel-budget cache behavior."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from fastmcp import Client

from tests.conftest import MockTransport, make_gemini_response, make_seedream_response

FLASH = "doubao-seedream-5-0-flash"
PRO = "doubao-seedream-5-0-pro"
GEMINI = "gemini-nano-banana-2.1"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [FLASH, PRO])
@pytest.mark.parametrize("code", ["AccountOverdueError", "OperationDenied.ServiceOverdue"])
async def test_seedream_primary_overdue_falls_back(client, app, routing, model, code):
    app.state.settings.default_model = model
    app.state.settings.default_fallback_model = GEMINI
    routing.ark = (403, {"error": {"code": code, "message": "overdue"}})
    routing.gemini = (200, make_gemini_response())

    response = await client.post("/v1/generate", json={"prompt": "poster"})

    assert response.status_code == 200
    data = response.json()
    assert data["requested_model"] == model
    assert data["model"] == GEMINI
    assert data["fallback_used"] is True
    assert routing.calls == {"gemini": 1, "ark": 1}


@pytest.mark.asyncio
async def test_seedream_submitted_timeout_uses_backup_once(client, app, routing):
    app.state.settings.default_model = FLASH
    app.state.settings.default_fallback_model = GEMINI
    routing.ark = httpx.ReadTimeout("already submitted")
    routing.gemini = (200, make_gemini_response())

    response = await client.post("/v1/generate", json={"prompt": "poster"})

    assert response.status_code == 200
    assert response.json()["requested_model"] == FLASH
    assert response.json()["model"] == GEMINI
    assert response.json()["fallback_used"] is True
    assert routing.calls == {"gemini": 1, "ark": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("ratio", ["17:1", "1:17", "0:1", "1:0"])
async def test_seedream_invalid_ratio_rejected_before_upstream(client, app, routing, ratio):
    response = await client.post("/v1/generate", json={"prompt": "poster", "model": FLASH, "aspect_ratio": ratio})
    assert response.status_code == 422
    assert response.json()["fields"] == ["aspect_ratio"]
    assert routing.calls == {"gemini": 0, "ark": 0}


@pytest.mark.asyncio
async def test_pixel_budget_changes_generation_and_cache(client, app):
    app.state.settings.ark_api_key = "k"
    transport = MockTransport(response_data=make_seedream_response())
    app.state.http_client = httpx.AsyncClient(transport=transport)
    body = {"prompt": "poster", "model": PRO, "aspect_ratio": "16:9"}

    first = (await client.post("/v1/generate", json=body)).json()
    assert json.loads(transport.last_request.content)["size"] == "1376x768"
    larger = (await client.post("/v1/generate", json={**body, "size": "1536x1536"})).json()
    assert json.loads(transport.last_request.content)["size"] == "2048x1152"
    again = (await client.post("/v1/generate", json=body)).json()

    assert first["id"] != larger["id"]
    assert again["id"] == first["id"]
    assert again["cached"] is True
    assert transport.call_count == 2
    image = await client.get(first["image_url"])
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"


@pytest.mark.asyncio
@pytest.mark.parametrize("model,fallback", [(FLASH, None), (GEMINI, FLASH)])
async def test_old_seedream_sizes_are_not_served_from_cache(client, app, routing, fake_redis, model, fallback):
    raw = f"v2:poster:{model}:1:1:1024x1024:"
    if fallback:
        raw += f":fallback={fallback}"
    key = hashlib.sha256(raw.encode()).hexdigest()
    await fake_redis.set(
        f"image:{key}",
        json.dumps({"id": "old-size", "image_url": "/static/images/old.jpeg", "model": FLASH, "created_at": "x"}),
    )
    body = {"prompt": "poster", "model": model}
    if fallback:
        body["fallback_model"] = fallback

    response = await client.post("/v1/generate", json=body)

    assert response.status_code == 200
    assert response.json()["id"] != "old-size"
    assert response.json()["cached"] is False
    assert routing.calls["ark"] == 1


@pytest.mark.asyncio
async def test_mcp_uses_flash_primary_and_same_error_fallback(app, routing):
    app.state.settings.default_model = FLASH
    app.state.settings.default_fallback_model = GEMINI
    routing.gemini = (200, make_gemini_response())
    async with Client(app.state.mcp) as client:
        result = await client.call_tool("generate_image", {"prompt": "poster", "size": "1536x1536"})
        assert result.structured_content["model"] == FLASH
        assert result.structured_content["image_url"].endswith(".png")
        routing.ark = (403, {"error": {"code": "AccountOverdueError", "message": "overdue"}})
        fallback = await client.call_tool("generate_image", {"prompt": "another poster"})
        assert fallback.structured_content["requested_model"] == FLASH
        assert fallback.structured_content["model"] == GEMINI
        assert fallback.structured_content["fallback_used"] is True
