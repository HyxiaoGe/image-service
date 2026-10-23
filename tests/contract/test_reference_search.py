"""reference_subject: Tavily search → filter → vision judge → found image passed as a reference."""

from __future__ import annotations

import json
import struct

import httpx
import pytest
from fastmcp import Client

from src.mcp.server import init_mcp
from src.services import reference_search, references
from tests.conftest import make_gemini_response

JUDGE_MODEL = "gemini/gemini-3.8-flash"


def png(width: int, height: int, tag: bytes = b"") -> bytes:
    """PNG header with the given size (all the service reads), made unique by ``tag``."""
    ihdr = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + ihdr + b"\x00" * 4 + tag


BACK = "https://www.apple.com/img/back.png"
BACK_2 = "https://www.apple.com/img/back-2.png"
FRONT = "https://www.apple.com/img/front.png"
MEDIA_BACK = "https://cdn.example-media.com/back.png"
OLD = "https://www.apple.com/img/old-model.png"


class Web(httpx.AsyncBaseTransport):
    """Tavily, image hosts, the LiteLLM judge and the image model, routed by host / payload."""

    def __init__(self):
        self.files: dict[str, httpx.Response] = {
            BACK: httpx.Response(200, content=png(1306, 1741, b"back")),
            BACK_2: httpx.Response(200, content=png(1306, 1741, b"back2")),
            FRONT: httpx.Response(200, content=png(1306, 1829, b"front")),
            MEDIA_BACK: httpx.Response(200, content=png(2000, 1500, b"media")),
            OLD: httpx.Response(200, content=png(1960, 1306, b"old")),
        }
        self.tavily: list[httpx.Response] = []  # consumed in order; default: one page with all files
        self.tavily_default = {
            "results": [
                {
                    "url": "https://www.apple.com/iphone-18-pro/",
                    "title": "iPhone 18 Pro - Apple",
                    "images": [{"url": u} for u in (FRONT, OLD, BACK, BACK_2)],
                },
                {"url": "https://example-media.com/review", "title": "Review", "images": [{"url": MEDIA_BACK}]},
            ],
            "images": [],
        }
        # url -> (matches_consensus, view[, whole_device=True]); unlisted images fail.
        self.verdicts: dict[str, tuple[bool, str]] = {
            BACK: (True, "back"),
            BACK_2: (True, "back"),
            FRONT: (True, "front"),
            MEDIA_BACK: (True, "back"),
            OLD: (False, "back"),
        }
        self.judge_response: httpx.Response | None = None
        self.requests: list[httpx.Request] = []

    def tavily_calls(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "api.tavily.com"]

    def judge_calls(self) -> list[dict]:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.url.path.endswith("/chat/completions") and json.loads(r.content)["model"] == JUDGE_MODEL
        ]

    def generations(self) -> list[dict]:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.url.path.endswith("/chat/completions") and json.loads(r.content)["model"] != JUDGE_MODEL
        ]

    def judged_urls(self, call: int = 0) -> list[str]:
        payload = self.judge_calls()[call]
        uris = [p["image_url"]["url"] for p in payload["messages"][0]["content"] if p["type"] == "image_url"]
        by_uri = {references.ReferenceImage("image/png", r.content).data_uri: u for u, r in self.files.items()}
        return [by_uri[u] for u in uris]

    def _judge(self, request: httpx.Request) -> httpx.Response:
        if self.judge_response is not None:
            return self.judge_response
        images = []
        for i, url in enumerate(self.judged_urls(len(self.judge_calls()) - 1)):
            ok, view, whole, *_ = (*self.verdicts.get(url, (False, "other")), True)
            images.append(
                {
                    "index": i,
                    "matches_consensus": ok,
                    "view": view,
                    "whole_device": whole,
                    "kind": "photo",
                    "reason": "r",
                }
            )
        content = json.dumps({"consensus_appearance": "wide camera plateau", "images": images})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.tavily.com":
            return self.tavily.pop(0) if self.tavily else httpx.Response(200, json=self.tavily_default)
        if request.url.path.endswith("/chat/completions"):
            if json.loads(request.content)["model"] == JUDGE_MODEL:
                return self._judge(request)
            return httpx.Response(200, json=make_gemini_response())
        return self.files.get(str(request.url), httpx.Response(404, text="not found"))


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    async def resolve(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(references, "_resolve_host", resolve)
    reference_search._pools.clear()


@pytest.fixture
def web(app) -> Web:
    app.state.settings.tavily_api_keys = "key-a,key-b"
    transport = Web()
    app.state.http_client = httpx.AsyncClient(transport=transport)
    init_mcp(app.state.settings, app.state.cache, http_client=app.state.http_client)
    return transport


async def post(client, **body):
    return await client.post("/v1/generate", json={"prompt": "iPhone 18 Pro launch poster", **body})


def sent_images(web: Web, call: int = -1) -> list[str]:
    content = web.generations()[call]["messages"][0]["content"]
    return [] if isinstance(content, str) else [p["image_url"]["url"] for p in content if p["type"] == "image_url"]


def data_uri(url: str, web: Web) -> str:
    return references.ReferenceImage("image/png", web.files[url].content).data_uri


# --- found ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_found_back_view_is_passed_to_the_model(client, web, fake_redis):
    resp = await post(client, reference_subject="iPhone 18 Pro")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    # Default: one image, a back view (FRONT passed too and came first in the results).
    assert data["reference_images_used"] == [BACK]
    assert data["reference_search"] == {"status": "found", "cached": False, "candidates": 5, "accepted": 4}
    assert sent_images(web) == [data_uri(BACK, web)]
    # One Tavily search with our own query.
    [call] = web.tavily_calls()
    body = json.loads(call.content)
    assert body["query"] == "iPhone 18 Pro official product photo"
    assert body["include_images"] is True and body["search_depth"] == "basic"
    # Found results are cached for 7 days.
    key = reference_search._cache_key("iPhone 18 Pro")
    assert 7 * 24 * 3600 - 5 < await fake_redis.ttl(key) <= 7 * 24 * 3600


@pytest.mark.asyncio
async def test_judge_sees_all_candidates_with_domains_and_titles(client, web):
    await post(client, reference_subject="iPhone 18 Pro")

    [payload] = web.judge_calls()
    texts = [p["text"] for p in payload["messages"][0]["content"] if p["type"] == "text"]
    assert "has been officially released" in texts[0]
    assert "MAJORITY" in texts[0]
    assert "[0] domain=apple.com title=iPhone 18 Pro - Apple" in texts
    assert "[4] domain=example-media.com title=Review" in texts
    assert payload["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_minority_design_is_never_selected(client, web, app):
    app.state.settings.reference_search_images = 2
    web.verdicts[BACK] = web.verdicts[BACK_2] = (False, "back")  # say the judge rejects both apple backs

    data = (await post(client, reference_subject="iPhone 18 Pro")).json()

    # OLD (rejected) is never used even though it's on apple.com; domain only orders.
    assert data["reference_images_used"] == [MEDIA_BACK, FRONT]


@pytest.mark.asyncio
async def test_up_to_two_images_when_configured(client, web, app):
    app.state.settings.reference_search_images = 2

    data = (await post(client, reference_subject="iPhone 18 Pro")).json()

    # Back views first; official domain before media; then resolution.
    assert data["reference_images_used"] == [BACK, BACK_2]
    assert len(sent_images(web)) == 2


@pytest.mark.asyncio
async def test_subject_cache_skips_search(client, web):
    await post(client, reference_subject="iPhone 18 Pro")
    resp = await post(client, prompt="a different poster", reference_subject="  iphone 18   PRO ")

    data = resp.json()
    assert data["reference_search"]["status"] == "found"
    assert data["reference_search"]["cached"] is True
    assert data["reference_images_used"] == [BACK]
    assert len(web.tavily_calls()) == 1
    assert len(web.judge_calls()) == 1
    assert sent_images(web) == [data_uri(BACK, web)]  # re-downloaded from the cached URL


@pytest.mark.asyncio
async def test_no_cache_searches_again(client, web):
    await post(client, reference_subject="iPhone 18 Pro")
    await post(client, reference_subject="iPhone 18 Pro", no_cache=True)

    assert len(web.tavily_calls()) == 2


@pytest.mark.asyncio
async def test_cached_image_gone_drops_entry(client, web, fake_redis):
    await post(client, prompt="first", reference_subject="iPhone 18 Pro")
    web.files[BACK] = httpx.Response(404)

    data = (await post(client, prompt="second", reference_subject="iPhone 18 Pro")).json()

    assert data["reference_search"]["status"] == "search_failed"
    assert data["reference_images_used"] == []
    assert await fake_redis.get(reference_search._cache_key("iPhone 18 Pro")) is None


@pytest.mark.asyncio
async def test_generation_cache_includes_subject(client, web):
    first = (await post(client)).json()
    second = (await post(client, reference_subject="iPhone 18 Pro")).json()
    again = (await post(client, reference_subject="iPhone 18 Pro")).json()

    assert second["cached"] is False and second["id"] != first["id"]
    assert again["cached"] is True
    assert again["reference_images_used"] == [BACK]
    assert again["reference_search"]["status"] == "found"


# --- nothing usable ----------------------------------------------------------


@pytest.mark.asyncio
async def test_none_accepted_still_generates_and_caches_one_hour(client, web, fake_redis):
    web.verdicts = {}

    resp = await post(client, reference_subject="iPhone 18 Pro")

    assert resp.status_code == 200
    data = resp.json()
    assert data["reference_search"] == {"status": "none_accepted", "cached": False, "candidates": 5, "accepted": 0}
    assert data["reference_images_used"] == []
    assert sent_images(web) == []
    assert 3590 < await fake_redis.ttl(reference_search._cache_key("iPhone 18 Pro")) <= 3600


@pytest.mark.asyncio
async def test_no_images_in_results_is_none_accepted_without_judging(client, web):
    web.tavily_default = {"results": [{"url": "https://x.com", "title": "t", "images": []}], "images": []}

    data = (await post(client, reference_subject="iPhone 18 Pro")).json()

    assert data["reference_search"]["status"] == "none_accepted"
    assert web.judge_calls() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["tavily_500", "tavily_timeout", "judge_500", "judge_garbage", "judge_not_json", "no_keys"],
)
async def test_failures_degrade_to_search_failed_and_are_not_cached(client, web, app, fake_redis, case):
    if case == "tavily_500":
        web.tavily = [httpx.Response(500)]
    elif case == "tavily_timeout":

        async def boom(request):
            raise httpx.ReadTimeout("slow")

        original = web.handle_async_request

        async def route(request):
            if request.url.host == "api.tavily.com":
                return await boom(request)
            return await original(request)

        web.handle_async_request = route
    elif case == "judge_500":
        web.judge_response = httpx.Response(500)
    elif case == "judge_garbage":
        web.judge_response = httpx.Response(200, json={"choices": [{"message": {"content": '{"images": "nope"}'}}]})
    elif case == "judge_not_json":
        web.judge_response = httpx.Response(200, json={"choices": [{"message": {"content": "sure! here"}}]})
    elif case == "no_keys":
        app.state.settings.tavily_api_keys = ""

    resp = await post(client, reference_subject="iPhone 18 Pro")

    assert resp.status_code == 200, resp.text
    assert resp.json()["reference_search"]["status"] == "search_failed"
    assert len(web.generations()) == 1
    assert await fake_redis.get(reference_search._cache_key("iPhone 18 Pro")) is None


# --- filtering ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule_filters_before_judging(client, web):
    small = "https://www.apple.com/img/small.png"
    page = "https://www.apple.com/img/page"
    dup = "https://www.apple.com/img/back-copy.png"
    private = "http://127.0.0.1/secret.png"
    web.files[small] = httpx.Response(200, content=png(599, 900, b"small"))
    web.files[page] = httpx.Response(200, text="<html>instagram</html>")
    web.files[dup] = httpx.Response(200, content=web.files[BACK].content)
    web.verdicts[dup] = web.verdicts[BACK]  # the mock judge can't tell identical bytes apart
    web.tavily_default["results"][0]["images"] = [{"url": u} for u in (small, page, BACK, dup, private)]
    web.tavily_default["results"][1]["images"] = []

    data = (await post(client, reference_subject="iPhone 18 Pro")).json()

    # Only BACK survives (the copy has identical bytes, so compare counts, not URLs).
    assert len(web.judged_urls()) == 1
    assert data["reference_images_used"] == [BACK]
    assert all(str(r.url) != private for r in web.requests)


@pytest.mark.asyncio
async def test_candidates_are_capped_per_page_and_in_total(client, web):
    many = [f"https://site{i}.example.com/p{j}.png" for i in range(5) for j in range(10)]
    web.tavily_default = {
        "results": [
            {
                "url": f"https://site{i}.example.com/",
                "title": "t",
                "images": [{"url": u} for u in many[i * 10 : i * 10 + 10]],
            }
            for i in range(5)
        ]
    }

    await post(client, reference_subject="iPhone 18 Pro")

    hosts = [r.url.host for r in web.requests if str(r.url) in many]
    assert len(hosts) == reference_search.MAX_DOWNLOADS
    assert max(hosts.count(f"site{i}.example.com") for i in range(5)) <= reference_search.MAX_PER_PAGE


@pytest.mark.asyncio
async def test_official_domains_are_downloaded_first(client, web):
    others = [f"https://blog{i}.example.com/a.png" for i in range(30)]
    web.tavily_default = {
        "results": [
            {"url": f"https://blog{i}.example.com/", "title": "t", "images": [{"url": others[i]}]} for i in range(30)
        ]
        + [{"url": "https://www.apple.com/iphone/", "title": "Apple", "images": [{"url": BACK}]}]
    }

    data = (await post(client, reference_subject="iPhone 18 Pro")).json()

    assert data["reference_images_used"] == [BACK]


# --- with caller reference_images / key pool / no subject -------------------


@pytest.mark.asyncio
async def test_caller_images_come_first(client, web):
    caller = "https://img.example.com/caller.png"
    web.files[caller] = httpx.Response(200, content=png(1000, 1000, b"caller"))

    data = (await post(client, reference_images=[caller], reference_subject="iPhone 18 Pro")).json()

    assert data["reference_images_used"] == [caller, BACK]
    assert sent_images(web) == [data_uri(caller, web), data_uri(BACK, web)]


@pytest.mark.asyncio
async def test_no_free_slot_skips_search(client, web):
    urls = [f"https://img.example.com/{i}.png" for i in range(3)]
    for i, u in enumerate(urls):
        web.files[u] = httpx.Response(200, content=png(1000, 1000, bytes([i])))

    data = (await post(client, reference_images=urls, reference_subject="iPhone 18 Pro")).json()

    assert data["reference_search"]["status"] == "skipped"
    assert web.tavily_calls() == []


@pytest.mark.asyncio
async def test_without_subject_nothing_is_searched(client, web):
    data = (await post(client)).json()

    assert data["reference_search"] is None
    assert data["reference_images_used"] == []
    assert web.tavily_calls() == []


@pytest.mark.asyncio
async def test_exhausted_key_is_skipped_and_cooled_down(client, web):
    web.tavily = [httpx.Response(432, json={"detail": "quota"})]

    data = (await post(client, reference_subject="iPhone 18 Pro")).json()
    await post(client, reference_subject="Pixel 11", no_cache=True)

    assert data["reference_search"]["status"] == "found"
    keys = [c.headers["authorization"] for c in web.tavily_calls()]
    # key-a hit its quota, key-b answered; afterwards only key-b is used.
    assert keys == ["Bearer key-a", "Bearer key-b", "Bearer key-b"]


@pytest.mark.asyncio
async def test_all_keys_exhausted_is_search_failed(client, web):
    web.tavily = [httpx.Response(401), httpx.Response(429)]

    data = (await post(client, reference_subject="iPhone 18 Pro")).json()

    assert data["reference_search"]["status"] == "search_failed"


@pytest.mark.asyncio
async def test_subject_is_normalised_and_blank_ignored(client, web):
    data = (await post(client, reference_subject="   ")).json()
    assert data["reference_search"] is None

    resp = await post(client, reference_subject="x" * 201)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_summary_log_fields(client, web, monkeypatch):
    from src.services import generator

    events = []

    class Recorder:
        def __getattr__(self, _level):
            return lambda event, **kw: events.append((event, kw))

    monkeypatch.setattr(generator, "logger", Recorder())
    await post(client, reference_subject="iPhone 18 Pro")

    [summary] = [kw for event, kw in events if event == "generation_finished"]
    assert summary["reference_subject"] == "iPhone 18 Pro"
    assert summary["reference_search_status"] == "found"
    assert summary["reference_candidates"] == 5
    assert summary["reference_accepted"] == 4
    assert summary["tavily_searches"] == 1
    assert "reference_judge_ms" in summary


@pytest.mark.asyncio
async def test_mcp_reference_subject(app, web):
    async with Client(app.state.mcp) as mcp_client:
        result = await mcp_client.call_tool(
            "generate_image", {"prompt": "iPhone 18 Pro launch poster", "reference_subject": "iPhone 18 Pro"}
        )

    text = result.content[0].text
    assert f"Reference images used: {BACK}" in text
    assert "Reference search: found" in text
    assert result.structured_content["reference_images_used"] == [BACK]
    assert result.structured_content["reference_search"]["status"] == "found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdicts", "tavily", "expected"),
    [("found", None, 7 * 24 * 3600), ("none", None, 3600), ("found", 500, 300)],
)
async def test_generation_cache_ttl_follows_search_outcome(client, web, app, fake_redis, verdicts, tavily, expected):
    app.state.settings.cache_ttl_seconds = 7 * 24 * 3600
    app.state.cache.ttl = 7 * 24 * 3600
    if verdicts == "none":
        web.verdicts = {}
    if tavily:
        web.tavily = [httpx.Response(tavily)]

    await post(client, reference_subject="iPhone 18 Pro")

    [key] = [k async for k in fake_redis.scan_iter("image:*")]
    assert expected - 5 < await fake_redis.ttl(key) <= expected


@pytest.mark.asyncio
async def test_whole_back_view_beats_close_up(client, web):
    # Both apple backs are cropped close-ups: the complete media back wins despite its domain.
    web.verdicts[BACK] = web.verdicts[BACK_2] = (True, "back", False)

    data = (await post(client, reference_subject="iPhone 18 Pro")).json()

    assert data["reference_images_used"] == [MEDIA_BACK]
    [payload] = web.judge_calls()
    assert '"whole_device": bool' in payload["messages"][0]["content"][0]["text"]


@pytest.mark.asyncio
async def test_og_sized_official_images_are_judged(client, web):
    og = "https://www.apple.com/img/og.png"
    web.files[og] = httpx.Response(200, content=png(1200, 630, b"og"))
    web.verdicts[og] = (True, "back")
    web.tavily_default["results"][0]["images"] = [{"url": og}]
    web.tavily_default["results"][1]["images"] = []

    data = (await post(client, reference_subject="iPhone 18 Pro")).json()

    assert data["reference_images_used"] == [og]


# --- reference hint appended to the prompt ------------------------------------

HINT = "参考图是 iPhone 18 Pro 的真实照片，外观以它为准。"


def sent_text(web: Web, call: int = -1) -> str:
    content = web.generations()[call]["messages"][0]["content"]
    return content if isinstance(content, str) else content[0]["text"]


@pytest.mark.asyncio
async def test_found_reference_appends_hint(client, web):
    await post(client, reference_subject="iPhone 18 Pro")

    assert sent_text(web) == "iPhone 18 Pro launch poster\n" + HINT


@pytest.mark.asyncio
async def test_hint_on_subject_cache_hit_and_not_in_generation_cache_key(client, web):
    await post(client, reference_subject="iPhone 18 Pro")
    await post(client, prompt="another poster", reference_subject="iPhone 18 Pro")
    again = (await post(client, reference_subject="iPhone 18 Pro")).json()

    assert sent_text(web) == "another poster\n" + HINT
    assert again["cached"] is True  # same request → same cache entry; the hint isn't keyed


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["none_accepted", "search_failed", "caller_refs_only", "no_subject"])
async def test_no_hint_without_found_images(client, web, app, case):
    body = {"reference_subject": "iPhone 18 Pro"}
    if case == "none_accepted":
        web.verdicts = {}
    elif case == "search_failed":
        web.tavily = [httpx.Response(500)]
    else:
        caller = "https://img.example.com/caller.png"
        web.files[caller] = httpx.Response(200, content=png(1000, 1000, b"caller"))
        body = {"reference_images": [caller]} if case == "caller_refs_only" else {}

    await post(client, **body)

    assert sent_text(web) == "iPhone 18 Pro launch poster"


@pytest.mark.asyncio
async def test_hint_reaches_seedream_fallback(monkeypatch, client, web, app):
    from tests.conftest import make_seedream_response

    monkeypatch.setenv("TEST_ARK_API_KEY", "k")
    original = web.handle_async_request

    async def route(request):
        if request.url.path.endswith("/images/generations"):
            web.requests.append(request)
            return httpx.Response(200, json=make_seedream_response())
        if request.url.path.endswith("/chat/completions") and json.loads(request.content)["model"] != JUDGE_MODEL:
            web.requests.append(request)
            return httpx.Response(
                200, json={"choices": [{"finish_reason": "content_filter", "message": {"content": None}}]}
            )
        return await original(request)

    web.handle_async_request = route
    data = (await post(client, reference_subject="iPhone 18 Pro", fallback_model="doubao-seedream-5-0-pro")).json()

    assert data["fallback_used"] is True
    [ark] = [json.loads(r.content) for r in web.requests if r.url.path.endswith("/images/generations")]
    assert ark["prompt"].endswith(HINT)


def test_cache_keys_are_versioned():
    from src.services.cache import compute_cache_key

    assert reference_search._cache_key("iPhone 18 Pro").startswith("refsearch:v2:")
    old = compute_cache_key("p", "m", "1:1", "1024x1024", None, None, False, None, None)
    assert compute_cache_key("p", "m", "1:1", "1024x1024", None, None, False, None, "iPhone 18 Pro") != old
