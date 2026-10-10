"""fallback_model: opt-in, only on content_filtered, reported in the result."""

from __future__ import annotations

import httpx
import pytest
from fastmcp import Client

from src.mcp.server import init_mcp
from src.services import generator
from tests.conftest import make_seedream_response

GEMINI = "gemini-3-pro-image-preview"  # default model in tests
SEEDREAM = "doubao-seedream-4-5"
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


async def post(client, **body):
    return await client.post("/v1/generate", json={"prompt": "an avengers poster", **body})


@pytest.mark.asyncio
async def test_content_filtered_falls_back(client, routing):
    resp = await post(client, fallback_model=SEEDREAM, aspect_ratio="3:4")

    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == SEEDREAM
    assert data["requested_model"] == GEMINI
    assert data["fallback_used"] is True
    assert routing.calls == {"gemini": 1, "ark": 1}


@pytest.mark.asyncio
async def test_no_fallback_without_opt_in(client, routing):
    resp = await post(client)

    assert resp.json()["error"] == "content_filtered"
    assert routing.calls["ark"] == 0


@pytest.mark.asyncio
async def test_other_errors_do_not_fall_back(client, routing, monkeypatch):
    routing.gemini = (503, {"error": {"message": "overloaded"}})

    resp = await post(client, fallback_model=SEEDREAM)

    assert resp.json()["error"] == "upstream_unavailable"
    assert resp.json()["model"] == GEMINI
    assert routing.calls["ark"] == 0


@pytest.mark.asyncio
async def test_fallback_failure_reports_both(client, routing):
    routing.ark = (500, {"error": {"message": "ark down"}})

    resp = await post(client, fallback_model=SEEDREAM)

    assert resp.status_code == 502
    data = resp.json()
    assert data["error"] == "upstream_unavailable"
    assert data["model"] == SEEDREAM
    assert data["fallback_from"] == {"model": GEMINI, "code": "content_filtered"}
    assert routing.calls == {"gemini": 1, "ark": 1}  # single fallback attempt, no chaining


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"fallback_model": "nope"}, "nope"),
        ({"fallback_model": GEMINI}, "differ"),
        # Seedream takes any ratio, but the Gemini fallback can't do 7:5.
        ({"model": SEEDREAM, "fallback_model": GEMINI, "aspect_ratio": "7:5"}, "7:5"),
    ],
    ids=["unknown", "same-as-model", "ratio-unsupported-by-fallback"],
)
async def test_invalid_fallback_rejected_upfront(client, routing, body, fragment):
    resp = await post(client, **body)

    assert resp.status_code == 422
    data = resp.json()
    assert data["error"] == "invalid_params"
    assert data["fields"] == ["fallback_model"]
    assert fragment in data["message"]
    assert routing.calls == {"gemini": 0, "ark": 0}


@pytest.mark.asyncio
async def test_fallback_results_are_cached_separately(client, routing):
    first = (await post(client, fallback_model=SEEDREAM)).json()
    assert first["fallback_used"] is True

    # A caller that did not opt in must not receive the Seedream image from cache.
    plain = await post(client)
    assert plain.json()["error"] == "content_filtered"

    again = (await post(client, fallback_model=SEEDREAM)).json()
    assert again["cached"] is True
    assert again["id"] == first["id"]
    assert again["model"] == SEEDREAM
    assert again["requested_model"] == GEMINI
    assert again["fallback_used"] is True


@pytest.mark.asyncio
async def test_fallback_summary_log(client, routing, monkeypatch):
    events = []

    class Recorder:
        def __getattr__(self, _level):
            return lambda event, **kw: events.append((event, kw))

    monkeypatch.setattr(generator, "logger", Recorder())

    await post(client, fallback_model=SEEDREAM)

    [finished] = [kw for event, kw in events if event == "generation_finished"]
    assert finished["fallback_model"] == SEEDREAM
    assert finished["fallback_used"] is True
    assert finished["model"] == SEEDREAM
    assert finished["attempts"] == 2


@pytest.mark.asyncio
async def test_mcp_fallback(app, routing):
    async with Client(app.state.mcp) as mcp_client:
        ok = await mcp_client.call_tool("generate_image", {"prompt": "poster", "fallback_model": SEEDREAM})
        routing.ark = (500, {"error": {"message": "down"}})
        bad = await mcp_client.call_tool("generate_image", {"prompt": "another poster", "fallback_model": SEEDREAM})

    lines = ok.content[0].text.splitlines()
    assert lines[1] == f"Model: {SEEDREAM}"
    assert lines[4] == f"Requested model: {GEMINI}"
    assert lines[5] == "Fallback used: True"
    assert ok.structured_content["fallback_used"] is True

    err = bad.structured_content["error"]
    assert err["model"] == SEEDREAM
    assert err["fallback_from"] == {"model": GEMINI, "code": "content_filtered"}
