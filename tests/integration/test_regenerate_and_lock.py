"""no_cache regeneration, id-based filenames, and the generation lock."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from src.services import cache as cache_module
from src.services.cache import compute_cache_key
from tests.conftest import FAKE_PNG_B64, MockTransport, make_gemini_response

PAYLOAD = {"prompt": "a red kite over hills"}


class SlowTransport(MockTransport):
    """Gemini mock that takes ``delay`` seconds and can fail with a status code."""

    def __init__(self, delay: float, status_code: int = 200):
        body = make_gemini_response() if status_code == 200 else {"error": {"message": "down"}}
        super().__init__(response_data=body, status_code=status_code)
        self.delay = delay

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(self.delay)
        return await super().handle_async_request(request)


@pytest.fixture
def fast_lock_polling(monkeypatch):
    monkeypatch.setattr(cache_module, "LOCK_POLL_INTERVAL", 0.02)


def use(app, transport: httpx.AsyncBaseTransport):
    app.state.http_client = httpx.AsyncClient(transport=transport)
    return transport


def stored_path(settings, image_url: str) -> Path:
    return Path(settings.storage_dir) / image_url.rsplit("/", 1)[-1]


def key_for(settings) -> str:
    return compute_cache_key(PAYLOAD["prompt"], settings.default_model, "1:1", "1024x1024", None)


# --- no_cache ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_cache_regenerates_and_writes_back(client, mock_transport):
    first = (await client.post("/v1/generate", json=PAYLOAD)).json()

    regen = (await client.post("/v1/generate", json={**PAYLOAD, "no_cache": True})).json()
    assert regen["cached"] is False
    assert regen["id"] != first["id"]
    assert regen["image_url"] != first["image_url"]
    assert mock_transport.call_count == 2

    # The regenerated image replaces the cache entry for later normal requests.
    after = (await client.post("/v1/generate", json=PAYLOAD)).json()
    assert after["cached"] is True
    assert after["id"] == regen["id"]
    assert mock_transport.call_count == 2


@pytest.mark.asyncio
async def test_filename_is_generation_id_and_old_file_survives(client, settings, app):
    first = (await client.post("/v1/generate", json=PAYLOAD)).json()
    assert first["image_url"].endswith(f"/{first['id']}.png")
    old_path = stored_path(settings, first["image_url"])
    old_bytes = old_path.read_bytes()

    other = make_gemini_response(FAKE_PNG_B64[:-4] + "AAAA")  # different bytes, still decodable
    use(app, MockTransport(response_data=other))
    regen = (await client.post("/v1/generate", json={**PAYLOAD, "no_cache": True})).json()

    assert old_path.read_bytes() == old_bytes
    assert stored_path(settings, regen["image_url"]).exists()


@pytest.mark.asyncio
async def test_no_cache_does_not_wait_for_lock(client, app, settings, fake_redis):
    # Someone else holds the lock for these parameters.
    await fake_redis.set(f"lock:image:{key_for(settings)}", "other-token", ex=60)

    resp = await client.post("/v1/generate", json={**PAYLOAD, "no_cache": True})

    assert resp.status_code == 200
    # And it must not release the other request's lock.
    assert await fake_redis.get(f"lock:image:{key_for(settings)}") == "other-token"


# --- lock -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_identical_requests_share_one_generation(client, app, fast_lock_polling):
    """Generation takes longer than the old 10s-equivalent wait; the second caller still gets it."""
    transport = use(app, SlowTransport(delay=0.3))

    a, b = await asyncio.gather(
        client.post("/v1/generate", json=PAYLOAD),
        client.post("/v1/generate", json=PAYLOAD),
    )

    assert a.status_code == b.status_code == 200
    assert {a.json()["cached"], b.json()["cached"]} == {False, True}
    assert a.json()["id"] == b.json()["id"]
    assert transport.call_count == 1


@pytest.mark.asyncio
async def test_waiter_returns_promptly_when_holder_fails(client, app, fast_lock_polling, monkeypatch):
    monkeypatch.setattr(cache_module, "LOCK_MAX_WAIT", 30)
    use(app, SlowTransport(delay=0.2, status_code=500))

    loop = asyncio.get_running_loop()
    started = loop.time()
    a, b = await asyncio.gather(
        client.post("/v1/generate", json=PAYLOAD),
        client.post("/v1/generate", json=PAYLOAD),
    )

    codes = sorted([a.json()["error"], b.json()["error"]])
    assert codes == ["generation_in_progress", "upstream_unavailable"]
    assert loop.time() - started < 5  # did not sit out LOCK_MAX_WAIT


@pytest.mark.asyncio
async def test_waiter_times_out(client, app, settings, fake_redis, fast_lock_polling, monkeypatch):
    monkeypatch.setattr(cache_module, "LOCK_MAX_WAIT", 0.2)
    await fake_redis.set(f"lock:image:{key_for(settings)}", "other-token", ex=60)

    resp = await client.post("/v1/generate", json=PAYLOAD)

    assert resp.status_code == 503
    assert resp.json()["error"] == "generation_in_progress"
    assert resp.json()["retryable"] is True


@pytest.mark.asyncio
async def test_release_only_removes_own_lock(fake_redis):
    svc = cache_module.CacheService(fake_redis, ttl=60)
    token = await svc.acquire_lock("k")
    assert token
    assert await svc.acquire_lock("k") is None

    # Our lock expired and someone else took it; our late release must not drop theirs.
    await fake_redis.set("lock:image:k", "theirs")
    await svc.release_lock("k", token)
    assert await fake_redis.get("lock:image:k") == "theirs"

    await fake_redis.set("lock:image:k", token)
    await svc.release_lock("k", token)
    assert await fake_redis.get("lock:image:k") is None


# --- MCP --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_no_cache(app, mock_transport):
    async with Client(app.state.mcp) as mcp_client:
        first = await mcp_client.call_tool("generate_image", PAYLOAD)
        regen = await mcp_client.call_tool("generate_image", {**PAYLOAD, "no_cache": True})

    assert regen.structured_content["cached"] is False
    assert regen.structured_content["id"] != first.structured_content["id"]
    assert mock_transport.call_count == 2
