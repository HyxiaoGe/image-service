"""REST/MCP 共用的身份限流与并发控制，上游全部模拟。"""

from __future__ import annotations

import asyncio
import json

import anyio
import httpx
import pytest

from src.config import ProductConfig
from src.models.errors import ErrorCode
from src.models.identity import CallerIdentity
from src.services.admission import PREFIX, AdmissionError, generation_admission, identity_keys
from tests.conftest import make_gemini_response


@pytest.fixture
def limited(app):
    s = app.state.settings
    s.rate_limit_global = s.rate_limit_product = 100
    s.rate_limit_user = 2
    return s


async def post(client, prompt="cup", user="user-1", **kwargs):
    return await client.post("/v1/generate", headers={"X-Image-User": user}, json={"prompt": prompt, **kwargs})


async def mcp_call(client, name="generate_image", arguments=None):
    if arguments is None:
        arguments = {"prompt": "cup"} if name == "generate_image" else {}
    response = await client.post(
        "/mcp",
        headers={
            "X-Image-User": "user-1",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith("data: ")))
    else:
        data = response.json()
    return data["result"]


async def test_rest_and_real_mcp_share_user_limit(client, app, fake_redis, limited, monkeypatch, mock_transport):
    monkeypatch.setattr("src.main.aioredis.from_url", lambda *a, **kw: fake_redis)
    async with app.router.lifespan_context(app):
        assert (await post(client)).status_code == 200
        assert (await mcp_call(client))["structuredContent"]["cached"] is True
        rejected = await post(client, no_cache=True)
        assert rejected.status_code == 429 and rejected.json()["retryable"] is True
        assert int(rejected.headers["Retry-After"]) == rejected.json()["retry_after"] > 0
        result = await mcp_call(client)
        assert result["structuredContent"]["error"]["code"] == "rate_limited"
        assert result["structuredContent"]["error"]["retry_after"] > 0
        assert result["isError"] is False
        assert "Available models:" in (await mcp_call(client, name="list_models"))["content"][0]["text"]
        assert (await client.get("/v1/models")).status_code == 200
        assert mock_transport.call_count == 1


async def test_user_limit_ignores_ip_scene_and_shares_product_cache(client, limited, mock_transport):
    assert (await post(client)).status_code == 200
    assert (await post(client)).status_code == 200
    rejected = await client.post(
        "/v1/generate",
        headers={"X-Image-User": "user-1", "X-Forwarded-For": "203.0.113.9", "X-Image-Scene": "validation"},
        json={"prompt": "other", "no_cache": True},
    )
    assert rejected.status_code == 429
    assert (await post(client, user="user-2")).status_code == 200
    assert mock_transport.call_count == 1


@pytest.mark.parametrize("dimension", ["global", "product"])
async def test_aggregate_limit_cannot_be_bypassed_by_user_changes(client, limited, dimension):
    setattr(limited, "rate_limit_" + dimension, 2)
    for user in ["one", "two"]:
        assert (await post(client, user=user)).status_code == 200
    assert (await post(client, user="three")).status_code == 429


async def test_rejected_user_does_not_consume_global_quota(client, limited):
    limited.rate_limit_global, limited.rate_limit_user = 3, 1
    assert (await post(client)).status_code == 200
    for _ in range(4):
        assert (await post(client)).status_code == 429
    assert (await post(client, user="two")).status_code == 200
    assert (await post(client, user="three")).status_code == 200
    assert (await post(client, user="four")).status_code == 429


async def test_product_quota_isolated_global_shared(client, limited, monkeypatch):
    limited.rate_limit_product, limited.rate_limit_global = 1, 2
    limited.products["other"] = ProductConfig(
        service_key_env="OTHER_SERVICE", ark_key_env="OTHER_ARK", ark_key_alias="other"
    )
    monkeypatch.setenv("OTHER_SERVICE", "other-service-key")
    assert (await post(client)).status_code == 200
    assert (await post(client, user="two")).status_code == 429
    other = await client.post(
        "/v1/generate", headers={"Authorization": "Bearer other-service-key"}, json={"prompt": "other"}
    )
    assert other.status_code == 200
    limited.rate_limit_product = 10
    assert (await post(client, user="three")).status_code == 429


async def test_windows_use_redis_clock_with_bounded_ttl(client, fake_redis, limited, monkeypatch):
    async def clock():
        return 120, 0

    monkeypatch.setattr(fake_redis, "time", clock)
    limited.rate_limit_user = 1
    assert (await post(client)).status_code == 200
    assert (await post(client)).status_code == 429

    async def next_window():
        return 180, 0

    monkeypatch.setattr(fake_redis, "time", next_window)
    assert (await post(client)).status_code == 200
    for key in await fake_redis.keys(PREFIX + "rate:*"):
        assert 0 < await fake_redis.ttl(key) <= limited.rate_limit_window * 2


@pytest.mark.parametrize("dimension", ["global", "product", "user"])
async def test_concurrency_rejects_and_releases_after_cancel(client, app, limited, dimension):
    setattr(limited, "max_concurrent_" + dimension, 1)
    started = asyncio.Event()

    async def blocking(request):
        started.set()
        await anyio.sleep_forever()

    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(blocking))
    task = asyncio.create_task(post(client, no_cache=True))
    try:
        await asyncio.wait_for(started.wait(), 1)
        overflow = await post(client, prompt="different", no_cache=True)
        assert overflow.status_code == 429 and overflow.json()["retry_after"] == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not await app.state.redis.keys(PREFIX + "active:*")
    app.state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=make_gemini_response()))
    )
    assert (await post(client, user="user-2")).status_code == 200


async def test_atomic_parallel_admissions_never_oversubscribe(fake_redis, limited):
    limited.max_concurrent_global = 2
    admitted, rejected = [], []
    release = asyncio.Event()

    async def attempt(index):
        try:
            async with generation_admission(fake_redis, limited, CallerIdentity.product("test", str(index))):
                admitted.append(index)
                await release.wait()
        except AdmissionError as e:
            rejected.append(e.code)

    tasks = [asyncio.create_task(attempt(i)) for i in range(10)]
    try:
        async with asyncio.timeout(2):
            while len(admitted) + len(rejected) < 10:
                await asyncio.sleep(0.005)
        assert len(admitted) == 2 and rejected == [ErrorCode.RATE_LIMITED] * 8
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert not await fake_redis.keys(PREFIX + "active:*")


async def test_crash_lease_expiry_and_rejected_call_preserves_current_holder(fake_redis, limited):
    limited.max_concurrent_global = 1
    seconds, _ = await fake_redis.time()
    key = PREFIX + "active:global"
    await fake_redis.zadd(key, {"crashed-owner": seconds - 1})
    caller = CallerIdentity.product("test", "user-1")
    async with generation_admission(fake_redis, limited, caller):
        assert await fake_redis.zcard(key) == 1
        with pytest.raises(AdmissionError):
            async with generation_admission(fake_redis, limited, caller):
                pytest.fail("Concurrent request must be rejected")
        assert await fake_redis.zcard(key) == 1
    assert not await fake_redis.exists(key)


@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_redis_failure_never_submits_upstream(client, limited, monkeypatch, mock_transport, failure):
    async def broken(*args, **kwargs):
        if failure == "timeout":
            await anyio.sleep_forever()
        raise OSError("secret redis connection details")

    monkeypatch.setattr("src.services.admission._acquire", broken)
    response = await post(client)
    assert response.status_code == 503 and response.json()["error"] == "admission_unavailable"
    assert "secret" not in response.text and mock_transport.call_count == 0


async def test_committed_lease_with_lost_reply_is_cleaned_up(fake_redis, limited, monkeypatch):
    from src.services import admission

    original = admission._acquire

    async def lost_reply(*args):
        await original(*args)
        raise OSError("Reply lost")

    monkeypatch.setattr(admission, "_acquire", lost_reply)
    with pytest.raises(AdmissionError):
        async with generation_admission(fake_redis, limited, CallerIdentity.product("test", "u")):
            pytest.fail("Uncertain admission must fail closed")
    assert not await fake_redis.keys(PREFIX + "active:*")


def test_service_and_user_subject_keys_do_not_collide():
    assert (
        identity_keys(CallerIdentity.product("app", None))[2] != identity_keys(CallerIdentity.product("app", "app"))[2]
    )
    assert (
        identity_keys(CallerIdentity.product("app", "app"))[2]
        != identity_keys(CallerIdentity.product("other", "app"))[2]
    )


async def test_admission_cleanup_shares_total_generation_deadline(fake_redis, limited, monkeypatch):
    from time import monotonic

    from src.services import admission
    from src.services.budget import generation_scope

    limited.generation_timeout_seconds = 0.1

    async def stuck_release(*args):
        await anyio.sleep_forever()

    monkeypatch.setattr(admission, "_release", stuck_release)
    started = monotonic()
    with pytest.raises(TimeoutError):
        async with generation_scope(limited, None):
            async with generation_admission(fake_redis, limited, CallerIdentity.product("test", "user")):
                await anyio.sleep_forever()
    assert monotonic() - started < 0.18
    for key in await fake_redis.keys(PREFIX + "active:*"):
        assert 0 < await fake_redis.ttl(key) <= 6


async def test_rest_active_request_blocks_real_mcp_before_upstream(client, app, fake_redis, limited, monkeypatch):
    limited.max_concurrent_user = 1
    monkeypatch.setattr("src.main.aioredis.from_url", lambda *a, **kw: fake_redis)
    started = asyncio.Event()
    calls = []

    async def blocking(request):
        calls.append(request)
        started.set()
        await anyio.sleep_forever()

    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(blocking))
    async with app.router.lifespan_context(app):
        task = asyncio.create_task(post(client, no_cache=True))
        try:
            await asyncio.wait_for(started.wait(), 1)
            result = await mcp_call(client, arguments={"prompt": "different", "no_cache": True})
            assert result["structuredContent"]["error"]["code"] == "rate_limited"
            assert len(calls) == 1
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not await fake_redis.keys(PREFIX + "active:*")
