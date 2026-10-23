"""Fixtures shared by the fallback tests."""

from __future__ import annotations

import base64

import httpx
import pytest

from src.mcp.server import init_mcp
from tests.conftest import FAKE_PNG_B64, make_seedream_response

FILTERED = {"choices": [{"finish_reason": "content_filter", "message": {"content": None}}]}
QWEN_RESULT_URL = "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/out.png"
QWEN_OK = {"output": {"choices": [{"message": {"role": "assistant", "content": [{"image": QWEN_RESULT_URL}]}}]}}


class RoutingTransport(httpx.AsyncBaseTransport):
    """LiteLLM (chat), Ark (images) and DashScope (Qwen) answer independently:
    (status, body) per provider, or an exception to raise. Qwen's result URL serves a PNG."""

    def __init__(self, gemini=(200, FILTERED), ark=(200, None)):
        self.gemini = gemini
        self.ark = (ark[0], ark[1] if ark[1] is not None else make_seedream_response())
        self.qwen = (200, QWEN_OK)
        self.calls = {"gemini": 0, "ark": 0}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == QWEN_RESULT_URL:
            return httpx.Response(200, content=base64.b64decode(FAKE_PNG_B64))
        if request.url.host == "dashscope.aliyuncs.com":
            provider = "qwen"
        elif request.url.path.endswith("/images/generations"):
            provider = "ark"
        else:
            provider = "gemini"
        self.calls[provider] = self.calls.get(provider, 0) + 1
        answer = getattr(self, provider)
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx.Response(status, json=body)


@pytest.fixture
def routing(monkeypatch, app) -> RoutingTransport:
    monkeypatch.setenv("TEST_ARK_API_KEY", "k")
    app.state.settings.dashscope_api_key = "dk"
    transport = RoutingTransport()
    app.state.http_client = httpx.AsyncClient(transport=transport)
    init_mcp(app.state.settings, app.state.cache, http_client=app.state.http_client)
    return transport
