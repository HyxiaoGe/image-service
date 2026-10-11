"""fallback_model: opt-in, on content_filtered / upstream_unavailable, reported in the result."""

from __future__ import annotations

import httpx
import pytest
from fastmcp import Client

from src.services import generator

GEMINI = "gemini-3-pro-image-preview"  # default model in tests
SEEDREAM = "doubao-seedream-4-5"


async def _no_sleep(_seconds):
    return None


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
@pytest.mark.parametrize("status", [429, 503])
async def test_unavailable_primary_falls_back(client, routing, status):
    routing.gemini = (status, {"error": {"message": "overloaded"}})

    data = (await post(client, fallback_model=SEEDREAM)).json()

    assert data["model"] == SEEDREAM
    assert data["fallback_used"] is True


@pytest.mark.asyncio
async def test_unreachable_primary_falls_back(client, routing, monkeypatch):
    monkeypatch.setattr(generator.asyncio, "sleep", _no_sleep)
    routing.gemini = httpx.ConnectError("refused")

    data = (await post(client, fallback_model=SEEDREAM)).json()

    assert data["model"] == SEEDREAM
    assert routing.calls["gemini"] == 3  # its own connect retries first


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (httpx.ReadTimeout("slow"), "upstream_timeout"),
        # Dropped mid-response: the image may already be billed.
        (httpx.RemoteProtocolError("dropped"), "upstream_unavailable"),
        ((400, {"error": {"message": "bad request"}}), "upstream_error"),
    ],
    ids=["timeout", "transport-error", "400"],
)
async def test_other_errors_do_not_fall_back(client, routing, monkeypatch, answer, code):
    monkeypatch.setattr(generator.asyncio, "sleep", _no_sleep)
    routing.gemini = answer

    resp = await post(client, fallback_model=SEEDREAM)

    assert resp.json()["error"] == code
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
