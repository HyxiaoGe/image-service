"""Contract tests for MCP tools."""

from __future__ import annotations

import httpx
import pytest
from fastmcp import Client

from src.mcp.server import init_mcp
from tests.conftest import MockTransport


@pytest.mark.asyncio
async def test_generate_image_tool(client: httpx.AsyncClient, app):
    """T023: generate_image tool returns image URL."""
    mcp_client = Client(app.state.mcp)

    async with mcp_client:
        result = await mcp_client.call_tool(
            "generate_image",
            {"prompt": "a sunset over mountains"},
        )

    text_parts = [p.text for p in result.content if hasattr(p, "text")]
    assert len(text_parts) > 0
    combined = "\n".join(text_parts)
    assert "/static/images/" in combined
    assert "gemini-3-pro-image-preview" in combined


@pytest.mark.asyncio
async def test_list_models_tool(app):
    """T024: list_models tool returns all configured models."""
    mcp_client = Client(app.state.mcp)

    async with mcp_client:
        result = await mcp_client.call_tool("list_models", {})

    text_parts = [p.text for p in result.content if hasattr(p, "text")]
    combined = "\n".join(text_parts)
    assert "Gemini 3 Pro Image" in combined
    assert "Gemini 3.1 Flash Image" in combined
    assert "Gemini 2.5 Flash Image" in combined


async def _call(app, args: dict):
    async with Client(app.state.mcp) as mcp_client:
        return await mcp_client.call_tool("generate_image", args)


def _text(result) -> str:
    return "\n".join(p.text for p in result.content if hasattr(p, "text"))


@pytest.mark.asyncio
async def test_generate_image_text_is_backward_compatible(app):
    """The first four lines keep the format Fusion parses with regexes."""
    result = await _call(app, {"prompt": "a lighthouse"})

    lines = _text(result).splitlines()
    assert lines[0].startswith("Generated image: ") and "/static/images/" in lines[0]
    assert lines[1] == "Model: gemini-3-pro-image-preview"
    assert lines[2] == "Cached: False"
    assert lines[3].startswith("ID: ")
    assert lines[4] == "Requested model: gemini-3-pro-image-preview"
    assert lines[5] == "Fallback used: False"
    assert result.is_error is False

    sc = result.structured_content
    assert sc["ok"] is True
    assert sc["image_url"] == lines[0].removeprefix("Generated image: ")
    assert sc["model"] == sc["requested_model"] == "gemini-3-pro-image-preview"
    assert sc["fallback_used"] is False
    assert sc["cached"] is False


@pytest.mark.asyncio
async def test_generate_image_error_has_code(app):
    content_filter = {"choices": [{"finish_reason": "content_filter", "message": {"content": None}}]}
    init_mcp(
        app.state.settings, app.state.cache, http_client=httpx.AsyncClient(transport=MockTransport(content_filter))
    )

    result = await _call(app, {"prompt": "a lighthouse"})

    assert _text(result).startswith("Error generating image [content_filtered]: ")
    assert result.is_error is False
    err = result.structured_content["error"]
    assert result.structured_content["ok"] is False
    assert err["code"] == "content_filtered"
    assert err["retryable"] is False
    assert err["model"] == "gemini-3-pro-image-preview"


@pytest.mark.asyncio
async def test_generate_image_validation_error(app):
    """pydantic ValidationError is reported as invalid_params, not leaked as a tool crash."""
    result = await _call(app, {"prompt": "x", "aspect_ratio": "wide"})

    assert _text(result).startswith("Error generating image [invalid_params]: ")
    assert result.is_error is False
    err = result.structured_content["error"]
    assert err["code"] == "invalid_params"
    assert err["fields"] == ["aspect_ratio"]


@pytest.mark.asyncio
async def test_generate_image_unexpected_exception(app, monkeypatch):
    async def boom(*_args, **_kwargs):
        raise RuntimeError("secret stack detail")

    monkeypatch.setattr("src.mcp.server.handle_generate", boom)
    result = await _call(app, {"prompt": "x"})

    text = _text(result)
    assert text.startswith("Error generating image [internal_error]: ")
    assert "secret" not in text
    assert result.structured_content["error"]["code"] == "internal_error"
