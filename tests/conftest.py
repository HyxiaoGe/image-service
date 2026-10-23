from __future__ import annotations

from pathlib import Path

import fakeredis.aioredis
import httpx
import pytest
from fastapi import FastAPI

from src.config import Settings
from src.main import create_app
from src.mcp.server import init_mcp, mcp
from src.services.cache import CacheService

# A minimal 1x1 red PNG for test fixtures
FAKE_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8BQDwAEgAF/pooBPQAAAABJRU5ErkJggg=="


def make_gemini_response(image_b64: str = FAKE_PNG_B64) -> dict:
    """Build a fake LiteLLM Proxy OpenAI-compatible response with an image."""
    return {
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "images": [
                        {
                            "image_url": {
                                "url": f"data:image/png;base64,{image_b64}",
                            },
                            "index": 0,
                            "type": "image_url",
                        }
                    ],
                },
            }
        ],
    }


def make_seedream_response(image_b64: str = FAKE_PNG_B64) -> dict:
    """Build a fake Ark (OpenAI Images) response with a b64 image."""
    return {
        "data": [{"b64_json": image_b64}],
        "model": "doubao-seedream-4-5-251128",
    }


class MockTransport(httpx.AsyncBaseTransport):
    """Mock httpx transport that returns canned Gemini responses."""

    def __init__(self, response_data: dict | None = None, status_code: int = 200):
        self._response_data = response_data or make_gemini_response()
        self._status_code = status_code
        self.call_count = 0
        self.last_request: httpx.Request | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        self.last_request = request
        return httpx.Response(
            status_code=self._status_code,
            json=self._response_data,
        )


@pytest.fixture
def tmp_storage(tmp_path: Path) -> Path:
    storage = tmp_path / "images"
    storage.mkdir()
    return storage


@pytest.fixture
def fake_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def settings(tmp_storage: Path) -> Settings:
    return Settings(
        litellm_api_key="test-api-key",
        redis_url="redis://localhost:6379/0",
        storage_dir=str(tmp_storage),
        static_url_prefix="/static/images",
        default_model="gemini-3-pro-image-preview",
        default_fallback_model=None,  # tests opt in to fallback explicitly
        rate_limit_global=20,
        rate_limit_per_ip=5,
        rate_limit_window=60,
        cache_ttl_seconds=3600,
        usage_enabled=False,  # 计费记录在专门的集成测试中启用，故障预算用例不引入磁盘时序。
    )


@pytest.fixture
def mock_transport() -> MockTransport:
    return MockTransport()


@pytest.fixture
def app(settings: Settings, fake_redis, mock_transport: MockTransport) -> FastAPI:
    application = create_app(settings=settings)

    cache_service = CacheService(fake_redis, ttl=settings.cache_ttl_seconds)
    application.state.settings = settings
    application.state.redis = fake_redis
    application.state.cache = cache_service
    mock_http = httpx.AsyncClient(transport=mock_transport)
    application.state.http_client = mock_http
    application.state.mcp = mcp
    init_mcp(settings, cache_service, http_client=mock_http)

    return application


@pytest.fixture
async def client(app: FastAPI) -> httpx.AsyncClient:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
