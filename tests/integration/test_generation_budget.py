"""Real deadlines, cancellation and cleanup, without submitting paid generations."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

import anyio
import httpx
import pytest
from fastmcp import Client
from mcp.server.lowlevel.server import request_ctx

from mcp import types
from src.mcp.server import init_mcp
from src.models.identity import CallerIdentity
from src.models.lifetime import RequestLifetime
from src.models.request import GenerateRequest
from src.services import generator
from src.services.cache import compute_cache_key
from tests.conftest import make_gemini_response, make_seedream_response
from tests.contract.conftest import QWEN_OK, QWEN_RESULT_URL
from tests.contract.test_generation_log import RecordingLogger

FLASH = "doubao-seedream-5-0-flash"
PRO = "doubao-seedream-5-0-pro"
GEMINI = "gemini-3-pro-image-preview"
BODY = {"prompt": "a blue cup"}


class BlockingTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, block_all=False, download=False, connect_error=False):
        self.block_all = block_all
        self.download = download
        self.connect_error = connect_error
        self.calls: list[httpx.Request] = []
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def handle_async_request(self, request):
        self.calls.append(request)
        if self.connect_error and len(self.calls) == 1:
            raise httpx.ConnectError("cannot connect")
        if self.download and request.method == "POST":
            return httpx.Response(200, json=QWEN_OK)
        if len(self.calls) == 1 or self.block_all or str(request.url) == QWEN_RESULT_URL:
            with suppress(LookupError):
                self.mcp_request_id = request_ctx.get().request_id
            self.started.set()
            try:
                await anyio.sleep_forever()
            finally:
                self.cancelled.set()
        body = make_seedream_response() if request.url.path.endswith("/images/generations") else make_gemini_response()
        return httpx.Response(200, json=body)


@pytest.fixture
def budget_app(app, monkeypatch):
    settings = app.state.settings
    settings.generation_timeout_seconds = 0.6
    settings.model_timeout_seconds = 0.1
    settings.seedream_pro_timeout_seconds = 0.4
    settings.fallback_reserve_seconds = 0.15
    settings.min_model_budget_seconds = 0.02
    monkeypatch.setenv("TEST_ARK_API_KEY", "k")
    settings.dashscope_api_key = "dk"
    recorder = RecordingLogger()
    monkeypatch.setattr(generator, "logger", recorder)
    app.state.recorder = recorder
    return app


def install(app, transport):
    app.state.http_client = httpx.AsyncClient(transport=transport)
    init_mcp(app.state.settings, app.state.cache, http_client=app.state.http_client)


async def no_outputs(app):
    assert await app.state.cache.redis.keys("lock:image:*") == []
    assert await app.state.cache.redis.keys("admission:image:v1:active:*") == []
    assert await app.state.cache.redis.keys("image:*") == []
    assert list(Path(app.state.settings.storage_dir).iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["rest", "mcp"])
async def test_hanging_primary_is_cancelled_and_backup_finishes(client, budget_app, entry):
    app = budget_app
    transport = BlockingTransport()
    install(app, transport)
    body = {**BODY, "fallback_model": FLASH}
    started = time.monotonic()
    if entry == "rest":
        response = await client.post("/v1/generate", json=body)
        assert response.status_code == 200
        data = response.json()
    else:
        async with Client(app.state.mcp) as mcp_client:
            result = await mcp_client.call_tool("generate_image", body)
        data = result.structured_content
        assert data["ok"] is True
    assert data["requested_model"] == GEMINI
    assert data["model"] == FLASH and data["fallback_used"] is True
    assert len(transport.calls) == 2 and transport.cancelled.is_set()
    assert time.monotonic() - started < 0.6
    assert transport.calls[0].extensions["timeout"]["read"] <= 0.1
    assert await app.state.cache.redis.keys("lock:image:*") == []


@pytest.mark.asyncio
async def test_total_budget_stops_backup_chain(client, budget_app):
    app = budget_app
    app.state.settings.generation_timeout_seconds = 0.3
    app.state.settings.model_timeout_seconds = 0.2
    app.state.settings.fallback_reserve_seconds = 0.08
    transport = BlockingTransport(block_all=True)
    install(app, transport)
    started = time.monotonic()
    response = await client.post("/v1/generate", json={**BODY, "fallback_model": FLASH})
    assert response.status_code == 504
    assert response.json()["error"] == "upstream_timeout"
    assert response.json()["fallback_from"] == {"model": GEMINI, "code": "upstream_timeout"}
    assert len(transport.calls) == 2
    assert 0.2 < time.monotonic() - started < 0.6
    await no_outputs(app)


@pytest.mark.asyncio
async def test_pro_budget_is_clipped_to_reserve_a_backup(client, budget_app):
    app = budget_app
    app.state.settings.generation_timeout_seconds = 0.4
    app.state.settings.seedream_pro_timeout_seconds = 1
    app.state.settings.fallback_reserve_seconds = 0.15
    transport = BlockingTransport()
    install(app, transport)
    response = await client.post("/v1/generate", json={**BODY, "model": PRO, "fallback_model": GEMINI})
    assert response.status_code == 200
    assert response.json()["model"] == GEMINI
    assert 0.1 < transport.calls[0].extensions["timeout"]["read"] < 0.25
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_pro_reserves_the_backups_full_model_budget(client, budget_app):
    app = budget_app
    app.state.settings.generation_timeout_seconds = 0.6
    app.state.settings.seedream_pro_timeout_seconds = 1
    app.state.settings.model_timeout_seconds = 0.25
    app.state.settings.fallback_reserve_seconds = 0.05
    transport = BlockingTransport(block_all=True)
    install(app, transport)
    response = await client.post("/v1/generate", json={**BODY, "model": PRO, "fallback_model": GEMINI})
    assert response.status_code == 504
    pro_read = transport.calls[0].extensions["timeout"]["read"]
    backup_read = transport.calls[1].extensions["timeout"]["read"]
    # Pro stops early enough that the backup still gets its whole 0.25s model budget.
    assert pro_read < 0.6 - 0.25
    assert backup_read > 0.2
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_reference_search_is_inside_total_budget(client, budget_app, monkeypatch):
    app = budget_app
    app.state.settings.generation_timeout_seconds = 0.1
    transport = BlockingTransport()
    install(app, transport)

    async def stuck(*args):
        await anyio.sleep_forever()

    monkeypatch.setattr(generator, "_find_references", stuck)
    response = await client.post("/v1/generate", json={**BODY, "reference_subject": "a cup"})
    assert response.status_code == 504 and not transport.calls
    assert app.state.recorder.finished()[0][1]["stage"] == "references"
    await no_outputs(app)


@pytest.mark.asyncio
async def test_cache_wait_uses_total_budget_without_releasing_another_requests_lock(client, budget_app):
    app = budget_app
    app.state.settings.generation_timeout_seconds = 0.1
    key = compute_cache_key(BODY["prompt"], GEMINI, "1:1", "1024x1024", None, namespace="test")
    await app.state.cache.redis.set(f"lock:image:{key}", "someone-else", ex=60)
    transport = BlockingTransport()
    install(app, transport)
    response = await client.post("/v1/generate", json=BODY)
    assert response.status_code == 504 and not transport.calls
    assert await app.state.cache.redis.get(f"lock:image:{key}") == "someone-else"


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", [False, True])
async def test_cancellation_stops_generation_and_releases_lock(budget_app, disconnect):
    app = budget_app
    transport = BlockingTransport()
    install(app, transport)
    lifetime = RequestLifetime()
    task = asyncio.create_task(
        generator.handle_generate(
            GenerateRequest(**BODY, fallback_model=FLASH),
            app.state.settings,
            app.state.cache,
            http_client=app.state.http_client,
            lifetime=lifetime,
            caller=CallerIdentity.product("test", "test-user"),
        )
    )
    await asyncio.wait_for(transport.started.wait(), 1)
    ttl = await app.state.cache.redis.ttl((await app.state.cache.redis.keys("lock:image:*"))[0])
    assert ttl > app.state.settings.generation_timeout_seconds
    if disconnect:
        lifetime.disconnected.set()
    else:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(transport.calls) == 1 and transport.cancelled.is_set()
    [(_, event)] = app.state.recorder.finished()
    assert event["outcome"] == "cancelled"
    assert event["cancel_reason"] == ("client_disconnected" if disconnect else "caller_cancelled")
    await no_outputs(app)


@pytest.mark.asyncio
async def test_connect_retry_sleep_is_bounded_by_model_deadline(client, budget_app):
    app = budget_app
    transport = BlockingTransport(connect_error=True)
    install(app, transport)
    response = await client.post("/v1/generate", json={**BODY, "fallback_model": FLASH})
    assert response.status_code == 200 and response.json()["model"] == FLASH
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_download_timeout_after_qwen_generated_does_not_generate_on_backup(client, budget_app):
    app = budget_app
    transport = BlockingTransport(download=True)
    install(app, transport)
    response = await client.post("/v1/generate", json={**BODY, "model": "qwen-image-3.0", "fallback_model": FLASH})
    assert response.status_code == 504
    assert [r.method for r in transport.calls] == ["POST", "GET"]
    await no_outputs(app)


@pytest.mark.asyncio
async def test_anyio_cancellation_shields_bounded_lock_cleanup(budget_app):
    app = budget_app
    transport = BlockingTransport()
    install(app, transport)
    with anyio.move_on_after(0.04):
        await generator.handle_generate(
            GenerateRequest(**BODY),
            app.state.settings,
            app.state.cache,
            http_client=app.state.http_client,
            caller=CallerIdentity.product("test", "test-user"),
        )
    assert transport.cancelled.is_set()
    await no_outputs(app)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/generate", "/mcp"])
async def test_actual_http_disconnect_cancels_rest_and_stateless_mcp(budget_app, fake_redis, monkeypatch, path):
    app = budget_app
    transport = BlockingTransport()
    install(app, transport)
    # Run the real MCP lifespan, retaining the fixture's Redis and upstream mocks.
    monkeypatch.setattr("src.main.aioredis.from_url", lambda *a, **kw: fake_redis)
    payload = (
        BODY
        if path == "/v1/generate"
        else {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "generate_image", "arguments": {**BODY, "fallback_model": FLASH}},
        }
    )
    queue = asyncio.Queue()
    raw = json.dumps(payload).encode()
    # A chunked request also verifies that observing disconnects preserves the body.
    await queue.put({"type": "http.request", "body": raw[:10], "more_body": True})
    await queue.put({"type": "http.request", "body": raw[10:], "more_body": False})
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "server": ("127.0.0.1", 8090),
        "client": ("127.0.0.1", 12345),
        "headers": [
            (b"host", b"localhost:8090"),
            (b"authorization", b"Bearer test-service-key"),
            (b"content-type", b"application/json"),
            (b"accept", b"application/json, text/event-stream"),
            (b"mcp-protocol-version", b"2025-06-18"),
        ],
    }

    async def send(message):
        pass

    async with app.router.lifespan_context(app):
        task = asyncio.create_task(app(scope, queue.get, send))
        try:
            await asyncio.wait_for(transport.started.wait(), 1)
            await queue.put({"type": "http.disconnect"})
            await asyncio.wait_for(transport.cancelled.wait(), 0.5)
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert len(transport.calls) == 1
            assert any(
                e[1]["outcome"] == "cancelled" and e[1]["cancel_reason"] == "client_disconnected"
                for e in app.state.recorder.finished()
            )
            await no_outputs(app)
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


@pytest.mark.asyncio
async def test_mcp_cancellation_notification_stops_tool_and_cleans_lock(budget_app):
    app = budget_app
    transport = BlockingTransport()
    install(app, transport)
    async with Client(app.state.mcp) as client:
        task = asyncio.create_task(client.call_tool("generate_image", {**BODY, "fallback_model": FLASH}))
        try:
            await asyncio.wait_for(transport.started.wait(), 1)
            await client.session.send_notification(
                types.ClientNotification(
                    types.CancelledNotification(
                        method="notifications/cancelled",
                        params=types.CancelledNotificationParams(
                            requestId=transport.mcp_request_id, reason="user stopped"
                        ),
                    )
                )
            )
            await asyncio.wait_for(transport.cancelled.wait(), 0.5)
            assert len(transport.calls) == 1
            assert app.state.recorder.finished()[0][1]["outcome"] == "cancelled"
            await no_outputs(app)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task


@pytest.mark.asyncio
async def test_cancel_during_retry_backoff_does_not_try_again_or_fall_back(budget_app):
    app = budget_app
    backoff = asyncio.Event()
    transport = BlockingTransport(connect_error=True)
    install(app, transport)

    async def stop_in_backoff(*args):
        backoff.set()
        await anyio.sleep_forever()

    with patch.object(generator.asyncio, "sleep", stop_in_backoff):
        task = asyncio.create_task(
            generator.handle_generate(
                GenerateRequest(**BODY, fallback_model=FLASH),
                app.state.settings,
                app.state.cache,
                http_client=app.state.http_client,
                caller=CallerIdentity.product("test", "test-user"),
            )
        )
        await asyncio.wait_for(backoff.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(transport.calls) == 1
    await no_outputs(app)


@pytest.mark.asyncio
async def test_too_little_time_left_does_not_start_backup(client, budget_app):
    app = budget_app
    app.state.settings.generation_timeout_seconds = 0.2
    app.state.settings.model_timeout_seconds = 0.15
    app.state.settings.fallback_reserve_seconds = 0
    app.state.settings.min_model_budget_seconds = 0.06
    transport = BlockingTransport()
    install(app, transport)
    response = await client.post("/v1/generate", json={**BODY, "fallback_model": FLASH})
    assert response.status_code == 504
    assert response.json()["message"] == "Not enough time remains to start a backup model."
    assert len(transport.calls) == 1
    await no_outputs(app)


@pytest.mark.asyncio
async def test_disconnect_at_primary_failure_does_not_start_backup(budget_app):
    app = budget_app
    lifetime = RequestLifetime()
    calls = []

    async def disconnected(request):
        calls.append(request)
        lifetime.disconnected.set()
        raise httpx.ReadTimeout("primary failed as caller disconnected")

    install(app, httpx.MockTransport(disconnected))
    with pytest.raises(asyncio.CancelledError):
        await generator.handle_generate(
            GenerateRequest(**BODY, fallback_model=FLASH),
            app.state.settings,
            app.state.cache,
            http_client=app.state.http_client,
            lifetime=lifetime,
            caller=CallerIdentity.product("test", "test-user"),
        )
    assert len(calls) == 1
    await no_outputs(app)


@pytest.mark.asyncio
async def test_expired_request_never_submits_a_generation(budget_app):
    app = budget_app
    transport = BlockingTransport()
    install(app, transport)
    with pytest.raises(generator.GenerationError) as error:
        await generator.handle_generate(
            GenerateRequest(**BODY),
            app.state.settings,
            app.state.cache,
            http_client=app.state.http_client,
            lifetime=RequestLifetime(started_at=time.monotonic() - 1),
            caller=CallerIdentity.product("test", "test-user"),
        )
    assert error.value.code.value == "upstream_timeout"
    assert not transport.calls


@pytest.mark.asyncio
async def test_lock_cleanup_is_bounded_when_redis_stalls(client, budget_app, monkeypatch):
    app = budget_app
    install(app, httpx.MockTransport(lambda request: httpx.Response(200, json=make_gemini_response())))

    async def stuck(*args):
        await anyio.sleep_forever()

    monkeypatch.setattr(app.state.cache, "release_lock", stuck)
    started = time.monotonic()
    response = await client.post("/v1/generate", json=BODY)
    assert response.status_code == 200
    assert time.monotonic() - started < 0.3
    assert any(event == "generation_lock_release_timed_out" for _, event, _ in app.state.recorder.events)
    assert await app.state.cache.redis.ttl((await app.state.cache.redis.keys("lock:image:*"))[0]) < 10
