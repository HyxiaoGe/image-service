"""Automatic reference images for ``reference_subject``.

Flow (design: fusion-workspace/workspace-docs/image-service-reference-search-design-2026-10-11.md):
per-subject cache → one Tavily search (always runs, query built by us) → rule
filtering (download, not an image, too small, duplicates) → one vision-model call
that finds the appearance most candidates agree on and checks each image against
it → pick back views first, then by source domain and resolution.

Source domains only order candidates; they never make an image pass. Any failure
here degrades to "no found image" — reference search never fails a generation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
import redis.asyncio as aioredis
import structlog

from src.config import Settings
from src.services.metrics import REFERENCE_SEARCHES
from src.services.references import (
    ReferenceImage,
    ReferenceImageError,
    _download,
    _sniff_mime,
    image_size,
    load_reference_images,
)

logger = structlog.get_logger()

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_TIMEOUT = 15.0
# Account-level failures cool a key down (same policy as reader-service's pool):
# invalid key until restart, quota exhausted for a day, rate limited for a minute.
TAVILY_KEY_COOLDOWNS: dict[int, float | None] = {401: None, 432: 86400.0, 433: 86400.0, 429: 60.0}

MAX_PER_PAGE = 8  # one product page can carry 100+ images; keep sources mixed
MAX_DOWNLOADS = 24
MIN_SHORT_SIDE = 768
MAX_JUDGED = 12
# Images go to the judge base64-inline (~4/3 size); stay well under Gemini's 20 MB.
JUDGE_MAX_BYTES = 12 * 1024 * 1024
JUDGE_TIMEOUT = 90.0

FOUND_TTL = 7 * 24 * 3600
# A miss is retried soon: one bad search must not block a subject for a week.
NONE_ACCEPTED_TTL = 3600
CACHE_PREFIX = "refsearch:v1:"
# How long an image generated under each outcome may be served from the image
# cache: an image made without the reference must not outlive the search miss.
# search_failed keeps it just long enough for requests waiting on the same lock.
GENERATION_TTL = {"none_accepted": NONE_ACCEPTED_TTL, "search_failed": 300}

# Ordering only (see module docstring). Official brand sites first, then major tech media.
OFFICIAL_DOMAINS = (
    "apple.com",
    "samsung.com",
    "store.google.com",
    "huawei.com",
    "mi.com",
    "xiaomi.com",
    "oppo.com",
    "vivo.com",
    "oneplus.com",
    "sony.com",
    "microsoft.com",
    "nintendo.com",
)
MEDIA_DOMAINS = (
    "theverge.com",
    "engadget.com",
    "macrumors.com",
    "9to5mac.com",
    "macworld.com",
    "gsmarena.com",
    "cnet.com",
    "techradar.com",
    "arstechnica.com",
    "pcmag.com",
    "wired.com",
    "mashable.com",
)

JUDGE_PROMPT = """You are checking candidate reference photos for the product "{subject}".
IMPORTANT: "{subject}" has been officially released. Your training data may predate it. Do NOT use your own
knowledge to decide whether it exists or what it looks like, and do not reject an image because you think
the product is unreleased. Decide only from the images below.

Below are {n} candidate images, each labelled with an index, source domain and page title. The labels are
context only; base every decision on what is visible.

Step 1: Compare all candidates that show this kind of product and find the exterior design that the MAJORITY
of them show consistently (camera module shape and extent, lens layout, flash position, back panel). Describe
it in `consensus_appearance`. Images showing a different design are a minority and must not shape the consensus.
Step 2: For each image, decide whether it shows the product with that majority design ("matches_consensus").
Reject: a different design (older/other models), concept or fan renders, product mostly hidden by a case or
accessory, collages of several different products, text-dominated images, screenshots, unrelated products.

Return JSON only:
{{"consensus_appearance": str,
  "images": [{{"index": int, "matches_consensus": bool, "view": "back|front|side|other",
              "kind": "photo|official_render|concept|unrelated", "reason": str}}]}}"""


@dataclass
class Candidate:
    url: str
    page: str | None = None
    title: str | None = None
    image: ReferenceImage | None = None
    size: tuple[int, int] | None = None
    status: str = "pending"
    verdict: dict = field(default_factory=dict)

    @property
    def domain(self) -> str:
        return _domain(self.page or self.url)


@dataclass
class SearchOutcome:
    status: str  # found | none_accepted | search_failed | skipped
    images: list[ReferenceImage] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    cached: bool = False
    candidates: int = 0
    accepted: int = 0
    log: dict[str, object] = field(default_factory=dict)


class SearchFailed(Exception):
    """Search or judging failed; the outcome is search_failed and is not cached."""


def _domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _matches(domain: str, suffixes: tuple[str, ...]) -> bool:
    return any(domain == s or domain.endswith("." + s) for s in suffixes)


def _tier(domain: str) -> int:
    if _matches(domain, OFFICIAL_DOMAINS):
        return 0
    if _matches(domain, MEDIA_DOMAINS):
        return 1
    return 2


def _cache_key(subject: str) -> str:
    return CACHE_PREFIX + hashlib.sha256(subject.strip().lower().encode()).hexdigest()


class TavilyKeyPool:
    """Round-robin over Tavily keys; a key with an account-level failure cools down alone."""

    def __init__(self, keys: list[str]):
        self.keys = keys
        self._next = 0
        self._unavailable_until: dict[int, float] = {}

    def candidates(self) -> list[tuple[int, str]]:
        now = time.monotonic()
        order = [(self._next + i) % len(self.keys) for i in range(len(self.keys))]
        if self.keys:
            self._next = (self._next + 1) % len(self.keys)
        return [(i, self.keys[i]) for i in order if self._unavailable_until.get(i, 0) <= now]

    def mark_unavailable(self, index: int, status: int) -> None:
        cooldown = TAVILY_KEY_COOLDOWNS[status]
        self._unavailable_until[index] = float("inf") if cooldown is None else time.monotonic() + cooldown
        logger.warning("tavily_key_unavailable", key_index=index, upstream_status=status, cooldown=cooldown)


_pools: dict[str, TavilyKeyPool] = {}


def _pool(settings: Settings) -> TavilyKeyPool:
    raw = settings.tavily_api_keys
    if raw not in _pools:
        _pools[raw] = TavilyKeyPool([k.strip() for k in raw.split(",") if k.strip()])
    return _pools[raw]


async def _tavily_search(query: str, settings: Settings, client: httpx.AsyncClient) -> dict:
    pool = _pool(settings)
    if not pool.keys:
        raise SearchFailed("tavily_not_configured")
    body = {
        "query": query,
        "search_depth": "basic",  # 1 credit; advanced (2) found no better images in testing
        "max_results": 10,
        "include_images": True,
        "include_image_descriptions": True,
    }
    last = "tavily_no_available_key"
    for index, key in pool.candidates():
        try:
            resp = await client.post(
                TAVILY_SEARCH_URL, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=TAVILY_TIMEOUT
            )
        except httpx.HTTPError as e:
            raise SearchFailed(f"tavily_{type(e).__name__}") from None
        if resp.status_code in TAVILY_KEY_COOLDOWNS:
            pool.mark_unavailable(index, resp.status_code)
            last = f"tavily_http_{resp.status_code}"
            continue
        if resp.status_code != 200:
            raise SearchFailed(f"tavily_http_{resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise SearchFailed("tavily_bad_json") from None
        if not isinstance(data, dict):
            raise SearchFailed("tavily_bad_json")
        return data
    raise SearchFailed(last)


def _collect(data: dict) -> list[Candidate]:
    """Image URLs from the results (with their page), then top-level ones; deduped, capped per page."""
    seen: set[str] = set()
    found: list[Candidate] = []

    def add(item: object, page: str | None, title: str | None) -> None:
        url = item.get("url") if isinstance(item, dict) else item
        if isinstance(url, str) and url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            found.append(Candidate(url=url, page=page, title=title))

    for result in data.get("results") or []:
        if not isinstance(result, dict):
            continue
        for item in (result.get("images") or [])[:MAX_PER_PAGE]:
            add(item, result.get("url"), result.get("title"))
    for item in data.get("images") or []:
        add(item, None, item.get("description") if isinstance(item, dict) else None)
    # Stable: Tavily's relevance order within each domain tier.
    found.sort(key=lambda c: _tier(c.domain))
    return found[:MAX_DOWNLOADS]


async def _fetch(candidates: list[Candidate], client: httpx.AsyncClient) -> list[Candidate]:
    """Download all candidates concurrently; keep real, large-enough, distinct images."""

    async def fetch(c: Candidate) -> None:
        try:
            data = await _download(c.url, client)
        except (ValueError, httpx.HTTPError):
            c.status = "download_failed"
            return
        mime = _sniff_mime(data)
        if mime is None:
            c.status = "not_image"
            return
        c.image, c.size = ReferenceImage(mime, data), image_size(data)
        c.status = "too_small" if c.size is None or min(c.size) < MIN_SHORT_SIDE else "ok"

    await asyncio.gather(*(fetch(c) for c in candidates))
    kept, hashes = [], set()
    for c in candidates:
        if c.status != "ok":
            continue
        digest = hashlib.sha256(c.image.data).hexdigest()
        if digest in hashes:
            c.status = "duplicate"
            continue
        hashes.add(digest)
        kept.append(c)
    return kept


async def _judge(subject: str, batch: list[Candidate], settings: Settings, client: httpx.AsyncClient) -> str:
    """One vision call over the whole batch; fills each candidate's verdict, returns the consensus."""
    content: list[dict] = [{"type": "text", "text": JUDGE_PROMPT.format(subject=subject, n=len(batch))}]
    for i, c in enumerate(batch):
        content.append({"type": "text", "text": f"[{i}] domain={c.domain} title={c.title or ''}"[:300]})
        content.append({"type": "image_url", "image_url": {"url": c.image.data_uri}})
    payload = {
        "model": f"gemini/{settings.reference_judge_model}",
        "messages": [{"role": "user", "content": content}],
        "response_format": {"type": "json_object"},
        "cache": {"no-cache": True, "no-store": True},
    }
    try:
        resp = await client.post(
            f"{settings.litellm_base_url}/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {settings.litellm_api_key}"},
            timeout=JUDGE_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise SearchFailed(f"judge_{type(e).__name__}") from None
    if resp.status_code != 200:
        raise SearchFailed(f"judge_http_{resp.status_code}")
    try:
        result = json.loads(resp.json()["choices"][0]["message"]["content"])
        verdicts = result["images"]
        for v in verdicts:
            index = v["index"]
            if isinstance(index, int) and 0 <= index < len(batch) and isinstance(v["matches_consensus"], bool):
                batch[index].verdict = v
        consensus = str(result.get("consensus_appearance") or "")
    except (ValueError, KeyError, IndexError, TypeError):
        raise SearchFailed("judge_bad_output") from None
    return consensus


def _select(batch: list[Candidate], limit: int) -> list[Candidate]:
    passed = [c for c in batch if c.verdict.get("matches_consensus") is True]
    passed.sort(key=lambda c: (c.verdict.get("view") != "back", _tier(c.domain), -(c.size[0] * c.size[1])))
    return passed[:limit]


async def _search(subject: str, settings: Settings, client: httpx.AsyncClient) -> SearchOutcome:
    query = f"{subject} official product photo"
    started = time.perf_counter()
    data = await _tavily_search(query, settings, client)
    search_ms = int((time.perf_counter() - started) * 1000)

    candidates = _collect(data)
    kept = await _fetch(candidates, client)
    batch, total = [], 0
    for c in kept:
        if len(batch) >= MAX_JUDGED or total + len(c.image.data) > JUDGE_MAX_BYTES:
            continue
        batch.append(c)
        total += len(c.image.data)

    consensus, judge_ms = "", 0
    if batch:
        started = time.perf_counter()
        consensus = await _judge(subject, batch, settings, client)
        judge_ms = int((time.perf_counter() - started) * 1000)
    chosen = _select(batch, settings.reference_search_images)

    logger.info(
        "reference_search_finished",
        subject=subject,
        query=query,
        consensus=consensus,
        candidates=[
            {"url": c.url, "domain": c.domain, "status": c.status, "size": c.size, **c.verdict} for c in candidates
        ],
    )
    return SearchOutcome(
        status="found" if chosen else "none_accepted",
        images=[c.image for c in chosen],
        urls=[c.url for c in chosen],
        candidates=len(batch),
        accepted=sum(1 for c in batch if c.verdict.get("matches_consensus") is True),
        log={"reference_search_ms": search_ms, "reference_judge_ms": judge_ms, "tavily_searches": 1},
    )


async def find_references(
    subject: str,
    slots: int,
    settings: Settings,
    redis: aioredis.Redis,
    client: httpx.AsyncClient,
    no_cache: bool = False,
) -> SearchOutcome:
    """Reference images for ``subject``, at most ``slots``. Never raises for search problems."""
    if slots <= 0:
        return _finish(SearchOutcome(status="skipped"))
    limit = min(slots, settings.reference_search_images)
    key = _cache_key(subject)

    if not no_cache:
        raw = await redis.get(key)
        if raw is not None:
            hit = json.loads(raw)
            urls = hit["urls"][:limit]
            try:
                images = await load_reference_images(urls, settings, client) if urls else []
            except ReferenceImageError as e:
                # A cached image went away: drop the entry so the next request searches again.
                await redis.delete(key)
                logger.warning("reference_cache_image_unavailable", subject=subject, error=e.message)
                return _finish(SearchOutcome(status="search_failed", cached=True, log={"reason": "cached_image_gone"}))
            outcome = SearchOutcome(
                status=hit["status"],
                images=images,
                urls=urls,
                cached=True,
                candidates=hit["candidates"],
                accepted=hit["accepted"],
            )
            return _finish(outcome)

    try:
        outcome = await _search(subject, settings, client)
    except SearchFailed as e:
        logger.warning("reference_search_failed", subject=subject, reason=str(e))
        return _finish(SearchOutcome(status="search_failed", log={"reason": str(e)}))

    ttl = FOUND_TTL if outcome.status == "found" else NONE_ACCEPTED_TTL
    entry = {"status": outcome.status, "urls": outcome.urls, "candidates": outcome.candidates}
    await redis.set(key, json.dumps({**entry, "accepted": outcome.accepted}), ex=ttl)
    outcome.images, outcome.urls = outcome.images[:limit], outcome.urls[:limit]
    return _finish(outcome)


def _finish(outcome: SearchOutcome) -> SearchOutcome:
    REFERENCE_SEARCHES.labels(status=outcome.status).inc()
    return outcome
