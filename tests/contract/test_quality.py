"""Quality selects the initial model consistently across REST and MCP."""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client

from src.config import SUPPORTED_MODELS
from src.mcp.server import init_mcp
from tests.conftest import MockTransport, make_gemini_response, make_seedream_response

FLASH = "doubao-seedream-5-0-flash"
PRO = "doubao-seedream-5-0-pro"
LITE = "doubao-seedream-5-0-lite"
DEFAULT = "gemini-3-pro-image-preview"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["rest", "mcp"])
@pytest.mark.parametrize(
    "args,expected",
    [
        ({}, DEFAULT),
        ({"quality": None}, DEFAULT),
        ({"quality": "standard"}, FLASH),
        ({"quality": "high"}, PRO),
        ({"quality": "high", "model": FLASH}, FLASH),
        ({"quality": "standard", "model": PRO}, PRO),
        ({"quality": "high", "model": LITE}, LITE),
    ],
)
async def test_quality_selects_initial_model(client, app, entry, args, expected):
    app.state.settings.ark_api_key = "k"
    transport = MockTransport(response_data=make_gemini_response() if expected == DEFAULT else make_seedream_response())
    async with httpx.AsyncClient(transport=transport) as upstream:
        app.state.http_client = upstream
        init_mcp(app.state.settings, app.state.cache, http_client=upstream)
        body = {"prompt": "a coffee poster", **args}
        if entry == "rest":
            response = await client.post("/v1/generate", json=body)
            assert response.status_code == 200
            data = response.json()
        else:
            async with Client(app.state.mcp) as mcp_client:
                result = await mcp_client.call_tool("generate_image", body)
            data = result.structured_content
            assert data["ok"] is True

    assert data["requested_model"] == data["model"] == expected
    assert data["fallback_used"] is False
    wire = json.loads(transport.last_request.content)
    assert wire["model"] == (SUPPORTED_MODELS[expected].ark_model_id or f"gemini/{expected}")
    assert "quality" not in wire  # A service routing preference, not an Ark parameter.


@pytest.mark.asyncio
async def test_quality_cache_is_separate_by_model_and_shared_with_explicit_model(client, app, routing):
    body = {"prompt": "a coffee poster"}
    standard = await client.post("/v1/generate", json={**body, "quality": "standard"})
    high = await client.post("/v1/generate", json={**body, "quality": "high"})
    explicit = await client.post("/v1/generate", json={**body, "model": PRO})
    again = await client.post("/v1/generate", json={**body, "quality": "standard"})

    assert all(r.status_code == 200 for r in (standard, high, explicit, again))
    assert standard.json()["id"] != high.json()["id"]
    assert explicit.json()["id"] == high.json()["id"]
    assert again.json()["id"] == standard.json()["id"]
    assert explicit.json()["cached"] is again.json()["cached"] is True
    assert routing.calls == {"gemini": 0, "ark": 2}


@pytest.mark.asyncio
async def test_invalid_quality_is_rejected_before_upstream(client, app, routing):
    response = await client.post("/v1/generate", json={"prompt": "poster", "quality": "lite"})
    assert response.status_code == 422
    assert response.json()["fields"] == ["quality"]
    async with Client(app.state.mcp) as mcp_client:
        # FastMCP validates its input schema before entering the tool function.
        result = await mcp_client.call_tool(
            "generate_image", {"prompt": "poster", "quality": "lite"}, raise_on_error=False
        )
    assert result.is_error is True
    assert "quality" in "\n".join(p.text for p in result.content if hasattr(p, "text"))
    assert routing.calls == {"gemini": 0, "ark": 0}


@pytest.mark.asyncio
async def test_explicit_invalid_model_is_not_replaced_by_quality(client, app, routing):
    response = await client.post("/v1/generate", json={"prompt": "poster", "model": "unknown", "quality": "high"})
    assert response.status_code == 400
    assert response.json()["error"] == "model_not_found"
    assert routing.calls == {"gemini": 0, "ark": 0}


@pytest.mark.asyncio
async def test_quality_routing_keeps_existing_fallback_and_actual_model_reporting(client, app, routing):
    app.state.settings.default_fallback_model = DEFAULT
    routing.ark = (403, {"error": {"code": "AccountOverdueError", "message": "overdue"}})
    routing.gemini = (200, make_gemini_response())

    response = await client.post("/v1/generate", json={"prompt": "poster", "quality": "high"})

    assert response.status_code == 200
    assert response.json()["requested_model"] == PRO
    assert response.json()["model"] == DEFAULT
    assert response.json()["fallback_used"] is True
    assert routing.calls == {"gemini": 1, "ark": 1}


@pytest.mark.asyncio
async def test_mcp_exposes_optional_quality_enum_and_selection_guidance(app):
    async with Client(app.state.mcp) as mcp_client:
        tools = await mcp_client.list_tools()
    tool = next(t for t in tools if t.name == "generate_image")
    assert "quality" not in tool.inputSchema["required"]
    quality_schema = tool.inputSchema["properties"]["quality"]
    assert any(s.get("enum") == ["standard", "high"] for s in quality_schema["anyOf"])
    description = " ".join(quality_schema["description"].split())
    assert "Explicit model takes priority" in description
    assert "Resolution is set" in description
