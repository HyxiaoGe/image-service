"""Fixtures shared by the fallback tests."""

from __future__ import annotations

import httpx
import pytest

from src.mcp.server import init_mcp
from tests.conftest import make_seedream_response

FILTERED = {"choices": [{"finish_reason": "content_filter", "message": {"content": None}}]}


class RoutingTransport(httpx.AsyncBaseTransport):
    """LiteLLM (chat) and Ark (images) answer independently: (status, body) per provider."""

    def __init__(self, gemini=(200, FILTERED), ark=(200, None)):
        self.gemini = gemini
        self.ark = (ark[0], ark[1] if ark[1] is not None else make_seedream_response())
        self.calls = {"gemini": 0, "ark": 0}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        provider = "ark" if request.url.path.endswith("/images/generations") else "gemini"
        self.calls[provider] += 1
        status, body = getattr(self, provider)
        return httpx.Response(status, json=body)


@pytest.fixture
def routing(app) -> RoutingTransport:
    app.state.settings.ark_api_key = "k"
    transport = RoutingTransport()
    app.state.http_client = httpx.AsyncClient(transport=transport)
    init_mcp(app.state.settings, app.state.cache, http_client=app.state.http_client)
    return transport
