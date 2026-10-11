"""reference_images: caller-supplied image URLs passed to the model as input images."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from src.mcp.server import init_mcp
from src.models.request import MAX_REFERENCE_IMAGES
from src.services import references
from tests.conftest import FAKE_PNG_B64, make_gemini_response, make_seedream_response

SEEDREAM = "doubao-seedream-4-5"
PNG = base64.b64decode(FAKE_PNG_B64)
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100
DOH_HOST = httpx.URL(references.DOH_URL).host
FILTERED = {"choices": [{"finish_reason": "content_filter", "message": {"content": None}}]}


class Web(httpx.AsyncBaseTransport):
    """Image hosts plus LiteLLM / Ark, routed by host and path."""

    def __init__(self):
        self.files: dict[str, httpx.Response] = {
            "https://img.example.com/a.png": httpx.Response(200, content=PNG, headers={"content-type": "image/png"}),
            "https://img.example.com/b.jpg": httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}),
        }
        self.gemini = (200, make_gemini_response())
        # DNS-over-HTTPS answers by host name; a Response / Exception replaces the answer.
        self.doh: dict[str, list[str] | httpx.Response | Exception] = {}
        self.requests: list[httpx.Request] = []

    def sent(self, kind: str) -> list[dict]:
        match = {"gemini": "/chat/completions", "ark": "/images/generations"}[kind]
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith(match)]

    def fetched(self) -> list[str]:
        return [str(r.url) for r in self.requests if r.method == "GET" and r.url.host != DOH_HOST]

    def doh_queries(self) -> list[tuple[str, str]]:
        return [(r.url.params["name"], r.url.params["type"]) for r in self.requests if r.url.host == DOH_HOST]

    def _doh(self, request: httpx.Request) -> httpx.Response:
        answer = self.doh.get(request.url.params["name"], httpx.Response(200, json={"Status": 3}))
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        rtype = request.url.params["type"]
        records = [{"type": 28 if ":" in ip else 1, "data": ip} for ip in answer if (":" in ip) == (rtype == "AAAA")]
        return httpx.Response(200, json={"Status": 0, "Answer": [{"type": 5, "data": "cdn.example.net."}, *records]})

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == DOH_HOST:
            return self._doh(request)
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(200, json=make_seedream_response())
        if request.url.path.endswith("/chat/completions"):
            status, body = self.gemini
            return httpx.Response(status, json=body)
        return self.files.get(str(request.url), httpx.Response(404, text="not found"))


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Every hostname resolves to a public address unless a test says otherwise."""

    async def resolve(host: str) -> list[str]:
        return {"intranet.example.com": ["10.0.0.5"]}.get(host, ["93.184.216.34"])

    monkeypatch.setattr(references, "_resolve_host", resolve)


@pytest.fixture
def web(app) -> Web:
    app.state.settings.ark_api_key = "k"
    transport = Web()
    app.state.http_client = httpx.AsyncClient(transport=transport)
    init_mcp(app.state.settings, app.state.cache, http_client=app.state.http_client)
    return transport


async def post(client, **body):
    return await client.post("/v1/generate", json={"prompt": "iPhone 18 Pro launch poster", **body})


def data_uri(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


# --- passing images to the models --------------------------------------------


@pytest.mark.asyncio
async def test_gemini_gets_multipart_content(client, web):
    resp = await post(client, reference_images=["https://img.example.com/a.png", "https://img.example.com/b.jpg"])

    assert resp.status_code == 200, resp.text
    [sent] = web.sent("gemini")
    assert sent["messages"][0]["content"] == [
        {"type": "text", "text": "iPhone 18 Pro launch poster"},
        {"type": "image_url", "image_url": {"url": data_uri("image/png", PNG)}},
        {"type": "image_url", "image_url": {"url": data_uri("image/jpeg", JPEG)}},
    ]


@pytest.mark.asyncio
async def test_gemini_without_references_keeps_plain_content(client, web):
    await post(client)
    assert web.sent("gemini")[0]["messages"][0]["content"] == "iPhone 18 Pro launch poster"


@pytest.mark.asyncio
async def test_seedream_single_reference_is_a_string(client, web):
    await post(client, model=SEEDREAM, reference_images=["https://img.example.com/a.png"])
    assert web.sent("ark")[0]["image"] == data_uri("image/png", PNG)


@pytest.mark.asyncio
async def test_seedream_multiple_references_are_a_list(client, web):
    await post(
        client, model=SEEDREAM, reference_images=["https://img.example.com/a.png", "https://img.example.com/b.jpg"]
    )
    assert web.sent("ark")[0]["image"] == [data_uri("image/png", PNG), data_uri("image/jpeg", JPEG)]


@pytest.mark.asyncio
async def test_seedream_without_references_has_no_image_field(client, web):
    await post(client, model=SEEDREAM)
    assert "image" not in web.sent("ark")[0]


@pytest.mark.asyncio
async def test_fallback_reuses_downloaded_references(client, web):
    web.gemini = (200, FILTERED)

    data = (await post(client, fallback_model=SEEDREAM, reference_images=["https://img.example.com/a.png"])).json()

    assert data["fallback_used"] is True
    assert web.sent("ark")[0]["image"] == data_uri("image/png", PNG)
    assert web.fetched() == ["https://img.example.com/a.png"]  # downloaded once


@pytest.mark.asyncio
async def test_own_generated_image_is_read_from_storage(client, web, settings):
    first = (await post(client)).json()
    own_url = f"{settings.service_base_url}{first['image_url']}"
    stored = (Path(settings.storage_dir) / first["image_url"].rsplit("/", 1)[-1]).read_bytes()

    resp = await post(client, prompt="make it blue", reference_images=[own_url])

    assert resp.status_code == 200, resp.text
    assert web.fetched() == []
    parts = web.sent("gemini")[-1]["messages"][0]["content"]
    assert parts[1]["image_url"]["url"] == data_uri("image/png", stored)


# --- caching -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_references_are_part_of_cache_key(client, web):
    plain = (await post(client)).json()
    with_ref = (await post(client, reference_images=["https://img.example.com/a.png"])).json()
    assert with_ref["cached"] is False
    assert with_ref["id"] != plain["id"]

    again = (await post(client, reference_images=["https://img.example.com/a.png"])).json()
    assert again["cached"] is True
    assert len(web.sent("gemini")) == 2
    assert web.fetched() == ["https://img.example.com/a.png"]  # cache hits don't download


# --- rejected inputs -----------------------------------------------------------


def assert_invalid(resp: httpx.Response, fragment: str) -> None:
    assert resp.status_code == 422, resp.text
    data = resp.json()
    assert data["error"] == "invalid_params"
    assert data["fields"] == ["reference_images"]
    assert fragment in data["message"]


@pytest.mark.asyncio
async def test_too_many_references(client, web):
    urls = [f"https://img.example.com/{i}.png" for i in range(MAX_REFERENCE_IMAGES + 1)]
    resp = await post(client, reference_images=urls)
    assert resp.status_code == 422
    assert resp.json()["fields"] == ["reference_images"]
    assert web.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://img.example.com/a.png", "not a url"])
async def test_only_http_urls(client, web, url):
    resp = await post(client, reference_images=[url])
    assert resp.status_code == 422
    assert resp.json()["fields"] == ["reference_images"]
    assert web.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.png",
        "http://localhost/a.png",
        "http://192.168.1.10:8090/static/images/x.png",
        "http://[::1]/a.png",
        "http://169.254.169.254/latest/meta-data",
        "https://intranet.example.com/a.png",  # resolves to 10.0.0.5
    ],
)
async def test_private_addresses_are_refused(client, web, url):
    assert_invalid(await post(client, reference_images=[url]), "reference_images[0]")
    assert web.fetched() == []


@pytest.mark.asyncio
async def test_redirect_to_private_address_is_refused(client, web):
    web.files["https://img.example.com/r"] = httpx.Response(302, headers={"location": "http://127.0.0.1/secret.png"})

    assert_invalid(await post(client, reference_images=["https://img.example.com/r"]), "reference_images[0]")
    assert web.fetched() == ["https://img.example.com/r"]


@pytest.mark.asyncio
async def test_public_redirect_is_followed(client, web):
    web.files["https://img.example.com/r"] = httpx.Response(302, headers={"location": "https://img.example.com/a.png"})

    resp = await post(client, reference_images=["https://img.example.com/r"])

    assert resp.status_code == 200, resp.text


# --- fake-IP DNS proxy (local resolver answers 198.18.0.0/15) ----------------


@pytest.fixture
def fake_ip_dns(monkeypatch):
    """Like Clash fake-ip mode: every host resolves locally to 198.18.x.x."""

    async def resolve(host: str) -> list[str]:
        return ["198.18.1.206"]

    monkeypatch.setattr(references, "_resolve_host", resolve)


@pytest.mark.asyncio
async def test_fake_ip_host_with_public_doh_answer_passes(client, web, fake_ip_dns):
    # The case PR #17 missed: on dev every external host is a fake IP.
    web.doh["img.example.com"] = ["93.184.216.34", "2606:2800:220:1::1"]

    resp = await post(client, reference_images=["https://img.example.com/a.png"])

    assert resp.status_code == 200, resp.text
    assert sorted(web.doh_queries()) == [("img.example.com", "A"), ("img.example.com", "AAAA")]
    assert web.fetched() == ["https://img.example.com/a.png"]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [["10.0.0.5"], ["127.0.0.1"], ["93.184.216.34", "192.168.1.10"], ["::1"]])
async def test_fake_ip_host_with_private_doh_answer_is_refused(client, web, fake_ip_dns, answer):
    web.doh["img.example.com"] = answer

    assert_invalid(await post(client, reference_images=["https://img.example.com/a.png"]), "private or local")
    assert web.fetched() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "fragment"),
    [
        (httpx.Response(503, text="unavailable"), "could not be verified"),
        (httpx.ReadTimeout("timed out"), "could not be verified"),
        (httpx.ConnectError("refused"), "could not be verified"),
        (httpx.Response(200, text="<html>not json</html>"), "could not be verified"),
        (httpx.Response(200, json=["not", "an", "object"]), "could not be verified"),
        (httpx.Response(200, json={"Status": 0, "Answer": [{"type": 1, "data": "garbage"}]}), "could not be verified"),
        (httpx.Response(200, json={"Status": 3}), "could not be resolved"),  # NXDOMAIN
        (httpx.Response(200, json={"Status": 0, "Answer": []}), "private or local"),  # no addresses
    ],
)
async def test_doh_failure_refuses(client, web, fake_ip_dns, answer, fragment):
    web.doh["img.example.com"] = answer

    assert_invalid(await post(client, reference_images=["https://img.example.com/a.png"]), fragment)
    assert web.fetched() == []
    assert web.sent("gemini") == []


@pytest.mark.asyncio
async def test_fake_ip_redirect_to_private_host_is_refused(client, web, fake_ip_dns):
    web.doh["img.example.com"] = ["93.184.216.34"]
    web.doh["evil.example.com"] = ["169.254.169.254"]
    web.files["https://img.example.com/r"] = httpx.Response(302, headers={"location": "https://evil.example.com/x.png"})

    assert_invalid(await post(client, reference_images=["https://img.example.com/r"]), "private or local")
    assert web.fetched() == ["https://img.example.com/r"]


@pytest.mark.asyncio
async def test_real_dns_answer_skips_doh(client, web):
    # Local resolver gives a real public IP (autouse public_dns): no extra lookup.
    resp = await post(client, reference_images=["https://img.example.com/a.png"])

    assert resp.status_code == 200, resp.text
    assert web.doh_queries() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://198.18.1.206/a.png", "http://10.0.0.5/a.png", "http://127.0.0.1/a.png"])
async def test_ip_literals_are_never_doh_resolved(client, web, fake_ip_dns, url):
    assert_invalid(await post(client, reference_images=[url]), "private or local")
    assert web.doh_queries() == []
    assert web.fetched() == []


@pytest.mark.asyncio
async def test_download_failure_names_the_image(client, web):
    resp = await post(client, reference_images=["https://img.example.com/a.png", "https://img.example.com/missing.png"])
    assert_invalid(resp, "reference_images[1]")
    assert web.sent("gemini") == []


@pytest.mark.asyncio
async def test_html_page_is_not_an_image(client, web):
    web.files["https://img.example.com/post"] = httpx.Response(
        200, text="<html>instagram</html>", headers={"content-type": "text/html"}
    )
    assert_invalid(await post(client, reference_images=["https://img.example.com/post"]), "not a supported image")


@pytest.mark.asyncio
async def test_oversized_image(client, web):
    big = b"\x89PNG\r\n\x1a\n" + b"0" * references.MAX_REFERENCE_BYTES
    web.files["https://img.example.com/big.png"] = httpx.Response(
        200, content=big, headers={"content-type": "image/png"}
    )
    assert_invalid(await post(client, reference_images=["https://img.example.com/big.png"]), "MB")


@pytest.mark.asyncio
async def test_storage_path_traversal_is_refused(client, web, settings):
    url = f"{settings.service_base_url}{settings.static_url_prefix}/../../etc/passwd"
    assert_invalid(await post(client, reference_images=[url]), "reference_images[0]")


# --- logging & MCP -----------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_log_counts_references(client, web, monkeypatch):
    from src.services import generator

    events = []

    class Recorder:
        def __getattr__(self, _level):
            return lambda event, **kw: events.append((event, kw))

    monkeypatch.setattr(generator, "logger", Recorder())
    await post(client, reference_images=["https://img.example.com/a.png", "https://img.example.com/b.jpg"])

    [finished] = [kw for event, kw in events if event == "generation_finished"]
    assert finished["reference_count"] == 2
    assert finished["reference_bytes"] == len(PNG) + len(JPEG)


@pytest.mark.asyncio
async def test_mcp_reference_images(app, web):
    async with Client(app.state.mcp) as mcp_client:
        ok = await mcp_client.call_tool(
            "generate_image", {"prompt": "poster", "reference_images": ["https://img.example.com/a.png"]}
        )
        bad = await mcp_client.call_tool(
            "generate_image", {"prompt": "poster", "reference_images": ["http://127.0.0.1/a.png"]}
        )

    assert ok.structured_content["ok"] is True
    assert web.sent("gemini")[0]["messages"][0]["content"][1]["image_url"]["url"] == data_uri("image/png", PNG)
    assert bad.structured_content["error"]["code"] == "invalid_params"
    assert bad.structured_content["error"]["fields"] == ["reference_images"]
