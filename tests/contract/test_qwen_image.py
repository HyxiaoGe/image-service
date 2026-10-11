"""Qwen-Image via Bailian's OpenAI-compatible images API (URL-only results)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from src.mcp.server import init_mcp
from src.services import references
from src.services.generator import QWEN_MAX_PIXELS, QWEN_MAX_RATIO, _resolve_qwen_size
from tests.conftest import FAKE_PNG_B64

QWEN = "qwen-image-3.0"
PNG = base64.b64decode(FAKE_PNG_B64)
RESULT_URL = "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/out.png?Expires=1"
DASHSCOPE_HOST = "dashscope.aliyuncs.com"


class Bailian(httpx.AsyncBaseTransport):
    """DashScope generation, its result URL, and caller reference images."""

    def __init__(self):
        self.generation: tuple[int, dict] = (200, {"data": [{"url": RESULT_URL}], "usage": {"output_image_count": 1}})
        self.result = httpx.Response(200, content=PNG)
        self.requests: list[httpx.Request] = []

    def sent(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.host == DASHSCOPE_HOST]

    def downloads(self) -> int:
        return sum(1 for r in self.requests if str(r.url) == RESULT_URL)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == DASHSCOPE_HOST:
            assert request.url.path == "/compatible-mode/v1/images/generations"
            status, body = self.generation
            return httpx.Response(status, json=body)
        if str(request.url) == RESULT_URL:
            return self.result
        if str(request.url) == "https://img.example.com/a.png":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return httpx.Response(404, text="not found")


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    async def resolve(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(references, "_resolve_host", resolve)


@pytest.fixture
def bailian(app) -> Bailian:
    app.state.settings.dashscope_api_key = "dk"
    transport = Bailian()
    app.state.http_client = httpx.AsyncClient(transport=transport)
    init_mcp(app.state.settings, app.state.cache, http_client=app.state.http_client)
    return transport


async def post(client, **body):
    return await client.post("/v1/generate", json={"prompt": "新品发布海报", "model": QWEN, **body})


@pytest.mark.asyncio
async def test_generates_downloads_and_stores(client, bailian, settings):
    resp = await post(client, aspect_ratio="16:9")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["model"] == QWEN
    sent = bailian.sent()[0]
    assert sent == {
        "model": QWEN,
        "prompt": "新品发布海报",
        "size": "1360x768",
        "n": 1,
        "watermark": False,
        "prompt_extend": False,
    }
    assert bailian.requests[0].headers["authorization"] == "Bearer dk"
    assert bailian.downloads() == 1
    # Stored locally under our own URL, never the 24-hour Bailian link.
    assert data["image_url"].startswith("/static/images/") and data["image_url"].endswith(".png")
    stored = Path(settings.storage_dir) / data["image_url"].rsplit("/", 1)[1]
    assert stored.read_bytes() == PNG


@pytest.mark.asyncio
async def test_reference_image_is_a_data_uri(client, bailian):
    resp = await post(client, reference_images=["https://img.example.com/a.png"])
    assert resp.status_code == 200, resp.text
    assert bailian.sent()[0]["image"] == f"data:image/png;base64,{FAKE_PNG_B64}"


@pytest.mark.asyncio
async def test_missing_key_is_config_missing(client, app, bailian):
    app.state.settings.dashscope_api_key = ""
    resp = await post(client)
    assert resp.status_code >= 500
    assert resp.json()["error"] == "config_missing"
    assert bailian.sent() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"error": {"code": "DataInspectionFailed", "message": "Input data may contain inappropriate content."}},
        {"code": "IPInfringementSuspect", "message": "Input data is suspected of being involved in IP infringement."},
    ],
)
async def test_moderation_is_content_filtered(client, bailian, body):
    bailian.generation = (400, body)
    resp = await post(client)
    assert resp.json()["error"] == "content_filtered"
    assert bailian.downloads() == 0


@pytest.mark.asyncio
async def test_other_400_is_upstream_error(client, bailian):
    bailian.generation = (400, {"error": {"code": "InvalidParameter", "message": "size is invalid"}})
    resp = await post(client)
    assert resp.json()["error"] == "upstream_error"


@pytest.mark.asyncio
async def test_missing_url_is_no_image(client, bailian):
    bailian.generation = (200, {"data": []})
    resp = await post(client)
    assert resp.json()["error"] == "no_image_returned"


@pytest.mark.asyncio
async def test_failed_download_is_not_regenerated(client, bailian):
    bailian.result = httpx.Response(403, text="expired")
    resp = await post(client)
    assert resp.json()["error"] == "no_image_returned"
    assert len(bailian.sent()) == 1


@pytest.mark.asyncio
async def test_listed_in_models(client):
    models = {m["id"]: m for m in (await client.get("/v1/models")).json()["models"]}
    assert models["qwen-image-3.0-pro"]["provider"] == "Alibaba"
    assert QWEN in models


@pytest.mark.parametrize("ratio", ["1:1", "16:9", "9:16", "4:3", "21:9", "8:1", "1:8", "20:1"])
def test_size_stays_in_1k_budget_and_ratio_limit(ratio):
    w, h = map(int, _resolve_qwen_size(ratio, None).split("x"))
    assert 512 * 512 <= w * h <= QWEN_MAX_PIXELS
    assert max(w / h, h / w) <= QWEN_MAX_RATIO
    assert w % 16 == 0 and h % 16 == 0


def test_size_falls_back_to_size_then_square():
    assert _resolve_qwen_size(None, "1536x1024") == _resolve_qwen_size("3:2", None)
    assert _resolve_qwen_size(None, None) == "1024x1024"
