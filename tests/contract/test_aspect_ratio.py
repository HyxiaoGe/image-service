"""Aspect ratio reaches Gemini, is validated per model, and LiteLLM's cache is bypassed."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from src.config import GEMINI_ASPECT_RATIOS, GEMINI_FLASH_31_ASPECT_RATIOS
from tests.conftest import MockTransport, make_seedream_response


def use(app, transport: MockTransport) -> MockTransport:
    app.state.http_client = httpx.AsyncClient(transport=transport)
    return transport


@pytest.mark.asyncio
async def test_gemini_payload_carries_aspect_ratio_and_bypasses_litellm_cache(client, app):
    transport = use(app, MockTransport())

    resp = await client.post("/v1/generate", json={"prompt": "a tower", "aspect_ratio": "3:4"})

    assert resp.status_code == 200
    sent = json.loads(transport.last_request.content)
    assert sent["imageConfig"] == {"aspectRatio": "3:4"}
    # LiteLLM's response cache ignores imageConfig, so it must never serve this call.
    assert sent["cache"] == {"no-cache": True, "no-store": True}


@pytest.mark.asyncio
async def test_seedream_payload_unchanged(monkeypatch, client, app):
    monkeypatch.setenv("TEST_ARK_API_KEY", "k")
    transport = use(app, MockTransport(response_data=make_seedream_response()))

    await client.post("/v1/generate", json={"prompt": "x", "model": "doubao-seedream-4-5", "aspect_ratio": "7:5"})

    sent = json.loads(transport.last_request.content)
    assert "imageConfig" not in sent and "cache" not in sent


@pytest.mark.asyncio
async def test_unsupported_ratio_rejected_before_upstream(client, app):
    transport = use(app, MockTransport())

    # Default test model is gemini-3-pro-image-preview, which lacks 1:8.
    resp = await client.post("/v1/generate", json={"prompt": "x", "aspect_ratio": "1:8"})

    assert resp.status_code == 422
    data = resp.json()
    assert data["error"] == "invalid_params"
    assert data["fields"] == ["aspect_ratio"]
    assert "21:9" in data["message"] and "1:8" in data["message"]
    assert transport.call_count == 0


@pytest.mark.asyncio
async def test_flash_31_accepts_extended_ratio(client, app):
    transport = use(app, MockTransport())

    resp = await client.post(
        "/v1/generate", json={"prompt": "x", "model": "gemini-3.1-flash-image-preview", "aspect_ratio": "1:8"}
    )

    assert resp.status_code == 200
    assert json.loads(transport.last_request.content)["imageConfig"] == {"aspectRatio": "1:8"}


@pytest.mark.asyncio
async def test_seedream_accepts_any_ratio(monkeypatch, client, app):
    monkeypatch.setenv("TEST_ARK_API_KEY", "k")
    use(app, MockTransport(response_data=make_seedream_response()))

    resp = await client.post(
        "/v1/generate", json={"prompt": "x", "model": "doubao-seedream-4-5", "aspect_ratio": "7:5"}
    )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_upstream_not_supported_message_maps_to_invalid_params(client, app):
    """Pro / 2.5 models reject with a different wording than 3.1 Flash."""
    msg = "GeminiException BadRequestError - Aspect ratio 2:3 is not supported for this model"
    use(app, MockTransport(response_data={"error": {"message": msg}}, status_code=400))

    resp = await client.post("/v1/generate", json={"prompt": "x", "aspect_ratio": "2:3"})

    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_params"


@pytest.mark.asyncio
async def test_models_endpoint_lists_aspect_ratios(client):
    models = {m["id"]: m for m in (await client.get("/v1/models")).json()["models"]}

    assert models["gemini-3-pro-image-preview"]["aspect_ratios"] == list(GEMINI_ASPECT_RATIOS)
    assert models["gemini-3.1-flash-image-preview"]["aspect_ratios"] == list(GEMINI_FLASH_31_ASPECT_RATIOS)
    assert models["doubao-seedream-4-5"]["aspect_ratios"] is None  # any ratio


@pytest.mark.asyncio
async def test_pre_ratio_cache_entries_are_not_served(client, app, fake_redis):
    """Entries cached before imageConfig was sent hold landscape images under any
    ratio's key; a cache-key version bump must make them unreachable."""
    legacy_raw = "a tower:gemini-3-pro-image-preview:3:4:1024x1024:"
    legacy_key = hashlib.sha256(legacy_raw.encode()).hexdigest()
    stale = {
        "id": "old",
        "image_url": "/static/images/old.jpeg",
        "model": "gemini-3-pro-image-preview",
        "created_at": "x",
    }
    await fake_redis.set(f"image:{legacy_key}", json.dumps(stale))

    resp = await client.post("/v1/generate", json={"prompt": "a tower", "aspect_ratio": "3:4"})

    assert resp.status_code == 200
    assert resp.json()["cached"] is False
    assert resp.json()["id"] != "old"
