"""Contract tests for POST /v1/generate."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.conftest import MockTransport, make_seedream_response


@pytest.mark.asyncio
async def test_generate_success(client: httpx.AsyncClient):
    """T014: Success path — returns image URL matching schema."""
    resp = await client.post("/v1/generate", json={"prompt": "a sunset over mountains"})

    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert "image_url" in data
    assert data["model"] == "gemini-3-pro-image-preview"
    assert data["cached"] is False
    assert "created_at" in data
    # Image should be accessible via static files
    img_resp = await client.get(data["image_url"].replace("http://test", ""))
    assert img_resp.status_code == 200


@pytest.mark.asyncio
async def test_generate_missing_prompt(client: httpx.AsyncClient):
    """T015: Missing prompt → 422 validation error."""
    resp = await client.post("/v1/generate", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_generate_invalid_model(client: httpx.AsyncClient):
    """T015: Invalid model → 400."""
    resp = await client.post(
        "/v1/generate",
        json={"prompt": "test", "model": "nonexistent/model"},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"] == "model_not_found"


@pytest.mark.asyncio
async def test_generate_invalid_aspect_ratio(client: httpx.AsyncClient):
    """T015: Invalid aspect_ratio → 422."""
    resp = await client.post(
        "/v1/generate",
        json={"prompt": "test", "aspect_ratio": "invalid"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        {"aspect_ratio": "1" * 5000 + ":1"},
        {"size": "1" * 5000 + "x1"},
    ],
)
async def test_generate_oversized_dimension_digits(client: httpx.AsyncClient, field: dict):
    """Over-long digit runs → 422, not a 500 from int() hitting the 4300-digit limit."""
    resp = await client.post("/v1/generate", json={"prompt": "test", **field})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_generate_upstream_error(
    client: httpx.AsyncClient,
    app,
):
    """T016: Upstream 500 → 502 upstream_unavailable."""
    # Replace the http_client with one that returns errors
    error_transport = MockTransport(
        response_data={"error": {"message": "upstream error"}},
        status_code=500,
    )
    app.state.http_client = httpx.AsyncClient(transport=error_transport)

    resp = await client.post("/v1/generate", json={"prompt": "a sunset"})

    assert resp.status_code == 502
    data = resp.json()
    assert data["error"] == "upstream_unavailable"
    assert data["retryable"] is True
    assert "message" in data


@pytest.mark.asyncio
async def test_generate_seedream_success(client: httpx.AsyncClient, app):
    """Seedream 路径：返回图片 URL，model 为 seedream 别名。"""
    app.state.settings.ark_api_key = "test-ark-key"
    app.state.http_client = httpx.AsyncClient(transport=MockTransport(response_data=make_seedream_response()))

    resp = await client.post(
        "/v1/generate",
        json={"prompt": "a timeline infographic", "model": "doubao-seedream-4-0"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "doubao-seedream-4-0"
    assert data["cached"] is False
    img_resp = await client.get(data["image_url"].replace("http://test", ""))
    assert img_resp.status_code == 200


@pytest.mark.asyncio
async def test_generate_seedream_request_body(client: httpx.AsyncClient, app):
    """Seedream 请求体含 watermark=false / b64_json / 鉴权头，且无 modalities。"""
    app.state.settings.ark_api_key = "test-ark-key"
    transport = MockTransport(response_data=make_seedream_response())
    app.state.http_client = httpx.AsyncClient(transport=transport)

    await client.post(
        "/v1/generate",
        json={"prompt": "x", "model": "doubao-seedream-4-0"},
    )

    assert str(transport.last_request.url).endswith("/images/generations")
    assert transport.last_request.headers["authorization"] == "Bearer test-ark-key"
    sent = json.loads(transport.last_request.content)
    assert sent["watermark"] is False
    assert sent["response_format"] == "b64_json"
    assert sent["sequential_image_generation"] == "disabled"
    # No aspect ratio in the request → default 1:1 → square at the pixel floor.
    assert sent["size"] == "1920x1920"
    # Every Volcano alias runs whichever version ARK_MODEL_ID pins (default 4.5).
    assert sent["model"] == "doubao-seedream-4-5-251128"
    assert "modalities" not in sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "wire_id"),
    [
        ("doubao-seedream-5-0-pro", "doubao-seedream-5-0-pro-260628"),
        ("doubao-seedream-5-0-flash", "doubao-seedream-5-0-flash-260915"),
        ("doubao-seedream-5-0-lite", "doubao-seedream-5-0-260128"),
    ],
)
async def test_generate_seedream_50_request_body(client: httpx.AsyncClient, app, model: str, wire_id: str):
    """5.0 models send their pinned wire id and omit sequential_image_generation (Ark 400s on it)."""
    app.state.settings.ark_api_key = "test-ark-key"
    transport = MockTransport(response_data=make_seedream_response())
    app.state.http_client = httpx.AsyncClient(transport=transport)

    resp = await client.post("/v1/generate", json={"prompt": "x", "model": model, "aspect_ratio": "16:9"})

    assert resp.status_code == 200
    assert resp.json()["model"] == model
    sent = json.loads(transport.last_request.content)
    assert sent["model"] == wire_id
    assert "sequential_image_generation" not in sent
    assert sent["watermark"] is False
    assert sent["size"] == "2560x1440"


@pytest.mark.asyncio
async def test_generate_seedream_explicit_landscape_size(client: httpx.AsyncClient, app):
    """A 16:9 request locks a landscape size instead of letting Seedream pick."""
    app.state.settings.ark_api_key = "test-ark-key"
    transport = MockTransport(response_data=make_seedream_response())
    app.state.http_client = httpx.AsyncClient(transport=transport)

    await client.post(
        "/v1/generate",
        json={"prompt": "x", "model": "doubao-seedream-4-5", "aspect_ratio": "16:9"},
    )

    sent = json.loads(transport.last_request.content)
    assert sent["size"] == "2560x1440"


@pytest.mark.asyncio
async def test_generate_seedream_missing_key(client: httpx.AsyncClient, app):
    """未配置 ARK_API_KEY → 503 config_missing。"""
    app.state.settings.ark_api_key = ""
    app.state.http_client = httpx.AsyncClient(transport=MockTransport(response_data=make_seedream_response()))

    resp = await client.post(
        "/v1/generate",
        json={"prompt": "x", "model": "doubao-seedream-4-0"},
    )

    assert resp.status_code == 503
    assert resp.json()["error"] == "config_missing"


@pytest.mark.asyncio
async def test_generate_seedream_upstream_error(client: httpx.AsyncClient, app):
    """Ark 5xx → 502 upstream_unavailable，上游原文不回给调用方。"""
    app.state.settings.ark_api_key = "test-ark-key"
    app.state.http_client = httpx.AsyncClient(
        transport=MockTransport(
            response_data={"error": {"message": "quota exceeded"}},
            status_code=500,
        )
    )

    resp = await client.post(
        "/v1/generate",
        json={"prompt": "x", "model": "doubao-seedream-4-0"},
    )

    assert resp.status_code == 502
    data = resp.json()
    assert data["error"] == "upstream_unavailable"
    assert "quota exceeded" not in data["message"]
