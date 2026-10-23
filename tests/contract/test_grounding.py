"""grounding: Google Search tool for Gemini models that support it, ignored elsewhere."""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client

from src.mcp.server import init_mcp
from src.services import generator
from tests.conftest import MockTransport, make_gemini_response, make_seedream_response

FLASH_31 = "gemini-3.1-flash-image-preview"
FLASH_25 = "gemini-2.5-flash-image"  # rejects the search tool upstream
SEEDREAM = "doubao-seedream-4-5"
FILTERED = {"choices": [{"finish_reason": "content_filter", "message": {"content": None}}]}


def grounded_response() -> dict:
    body = make_gemini_response()
    body["vertex_ai_grounding_metadata"] = [
        {
            "searchEntryPoint": {"renderedContent": "<style>...</style>"},
            "groundingChunks": [
                {"web": {"uri": "https://a", "title": "apple.com"}},
                {"image": {"sourceUri": "https://b", "imageUri": "https://b.jpg"}},
                {"image": {"sourceUri": "https://c", "imageUri": "https://c.jpg"}},
            ],
            "webSearchQueries": ["iPhone 18 Pro colors"],
            "imageSearchQueries": ["iPhone 18 Pro marketing photo", "iPhone 18 Pro back camera"],
        }
    ]
    return body


WEB_ONLY = {"googleSearch": {}}
WEB_AND_IMAGE = {"googleSearch": {"searchTypes": {"webSearch": {}, "imageSearch": {}}}}


def use(app, transport: httpx.AsyncBaseTransport):
    app.state.http_client = httpx.AsyncClient(transport=transport)
    init_mcp(app.state.settings, app.state.cache, http_client=app.state.http_client)
    return transport


async def post(client, **body):
    return await client.post("/v1/generate", json={"prompt": "iPhone 18 Pro product photo", **body})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "tool"),
    [
        (FLASH_31, WEB_AND_IMAGE),
        # These two answer 400 "Image search as tool is not enabled": web search only.
        ("gemini-3-pro-image-preview", WEB_ONLY),
        ("nano-banana-pro-preview", WEB_ONLY),
    ],
)
async def test_grounding_sends_search_tool(client, app, model, tool):
    transport = use(app, MockTransport(response_data=grounded_response()))

    resp = await post(client, model=model, grounding=True)

    assert resp.status_code == 200
    assert resp.json()["grounding_used"] is True
    assert json.loads(transport.last_request.content)["tools"] == [tool]


@pytest.mark.asyncio
async def test_grounding_off_by_default(client, app):
    transport = use(app, MockTransport())

    resp = await post(client, model=FLASH_31)

    assert resp.json()["grounding_used"] is False
    assert "tools" not in json.loads(transport.last_request.content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "response"),
    [(FLASH_25, make_gemini_response()), (SEEDREAM, make_seedream_response())],
    ids=["gemini-2.5", "seedream"],
)
async def test_grounding_ignored_by_unsupported_models(client, app, model, response):
    transport = use(app, MockTransport(response_data=response))

    resp = await post(client, model=model, grounding=True)

    assert resp.status_code == 200
    assert resp.json()["grounding_used"] is False
    assert "tools" not in json.loads(transport.last_request.content)


@pytest.mark.asyncio
async def test_grounding_is_part_of_cache_key(client, app):
    transport = use(app, MockTransport(response_data=grounded_response()))

    plain = (await post(client, model=FLASH_31)).json()
    grounded = (await post(client, model=FLASH_31, grounding=True)).json()
    assert grounded["cached"] is False
    assert grounded["id"] != plain["id"]

    again = (await post(client, model=FLASH_31, grounding=True)).json()
    assert again["cached"] is True
    assert again["grounding_used"] is True
    assert (await post(client, model=FLASH_31)).json()["grounding_used"] is False
    assert transport.call_count == 2


class RoutingTransport(httpx.AsyncBaseTransport):
    """Gemini blocks the prompt; Ark draws it."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(200, json=make_seedream_response())
        return httpx.Response(200, json=FILTERED)


@pytest.mark.asyncio
async def test_fallback_to_seedream_reports_no_grounding(client, app):
    transport = use(app, RoutingTransport())

    resp = await post(client, model=FLASH_31, grounding=True, fallback_model=SEEDREAM)

    data = resp.json()
    assert data["fallback_used"] is True
    assert data["grounding_used"] is False
    gemini_req, ark_req = transport.requests
    assert json.loads(gemini_req.content)["tools"] == [WEB_AND_IMAGE]
    assert "tools" not in json.loads(ark_req.content)


@pytest.mark.asyncio
async def test_grounding_summary_log(client, app, monkeypatch):
    use(app, MockTransport(response_data=grounded_response()))
    events = []

    class Recorder:
        def __getattr__(self, _level):
            return lambda event, **kw: events.append((event, kw))

    monkeypatch.setattr(generator, "logger", Recorder())

    await post(client, model=FLASH_31, grounding=True)

    [finished] = [kw for event, kw in events if event == "generation_finished"]
    assert finished["grounding"] is True
    assert finished["grounding_used"] is True
    assert finished["search_queries"] == ["iPhone 18 Pro colors"]
    assert finished["image_search_queries"] == ["iPhone 18 Pro marketing photo", "iPhone 18 Pro back camera"]
    assert finished["search_sources"] == 1
    assert finished["image_search_sources"] == 2


@pytest.mark.asyncio
async def test_models_endpoint_lists_grounding_support(client):
    models = {m["id"]: m for m in (await client.get("/v1/models")).json()["models"]}
    assert models[FLASH_31]["grounding"] is True
    assert models[FLASH_31]["search_types"] == ["web", "image"]
    assert models["gemini-3-pro-image-preview"]["search_types"] == ["web"]
    assert models[FLASH_25]["grounding"] is False
    assert models[FLASH_25]["search_types"] == []
    assert models[SEEDREAM]["grounding"] is False


@pytest.mark.asyncio
async def test_mcp_grounding(app):
    use(app, MockTransport(response_data=grounded_response()))
    async with Client(app.state.mcp) as mcp_client:
        result = await mcp_client.call_tool(
            "generate_image", {"prompt": "iPhone", "model": FLASH_31, "grounding": True}
        )

    assert result.structured_content["grounding_used"] is True
    assert result.content[0].text.splitlines()[6] == "Grounding used: True"
