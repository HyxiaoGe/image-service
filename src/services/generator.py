from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import re
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

import httpx
import structlog

from src.config import SUPPORTED_MODELS, Settings
from src.models.errors import ERROR_SPECS, ErrorCode
from src.models.request import GenerateRequest
from src.models.response import GenerateResponse
from src.services.cache import CacheService, compute_cache_key
from src.services.metrics import (
    CACHE_HITS,
    FALLBACKS,
    GENERATION_DURATION,
    GENERATION_ERRORS,
    REQUEST_COUNT,
)

logger = structlog.get_logger()


class GenerationError(Exception):
    """Raised when image generation fails; carries a caller-facing ErrorCode.

    ``message`` defaults to the code's fixed text. ``upstream_detail`` holds the
    raw upstream wording for logs only and is never returned to callers.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        *,
        model: str | None = None,
        upstream_status: int | None = None,
        upstream_detail: str | None = None,
        fields: list[str] | None = None,
        fallback_from: dict[str, str] | None = None,
    ):
        spec = ERROR_SPECS[code]
        self.code = code
        self.message = message or spec.message
        self.status_code = spec.http_status
        self.retryable = spec.retryable
        self.model = model
        self.upstream_status = upstream_status
        self.upstream_detail = upstream_detail[:500] if upstream_detail else None
        self.fields = fields
        self.fallback_from = fallback_from  # set when the fallback model also failed
        super().__init__(self.message)


MAX_RETRIES = 2
REQUEST_TIMEOUT = 300.0  # 5 minutes — complex prompts can take a while
# The generation lock must outlive the slowest possible generation (every attempt
# timing out plus retry sleeps), or a second identical request could start a
# duplicate upstream call while the first is still running.
LOCK_TTL_SECONDS = int(REQUEST_TIMEOUT * (MAX_RETRIES + 1)) + 60

# Upstream POST attempts (incl. retries) for the current request; read by the
# summary log in handle_generate.
_upstream_attempts: ContextVar[int] = ContextVar("upstream_attempts", default=0)

# What Google Search did for the current request (queries, source count), when
# grounding was on; read by the summary log.
_search_stats: ContextVar[dict[str, object] | None] = ContextVar("search_stats", default=None)

GOOGLE_SEARCH_TOOL = {"googleSearch": {}}

# Markers Gemini uses for safety blocks (blockReason / finishReason values) when
# they surface as an HTTP error instead of a 200 with finish_reason=content_filter.
_GEMINI_SAFETY_MARKERS = ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST")

# Seedream image-size constraints. 4.5 rejects any size below 3,686,400 px
# ("image size must be at least 3686400 pixels"); 4.0 also accepts these
# dimensions, so a single floor keeps the size logic model-agnostic. Sides are
# aligned to 32px and clamped to Ark's [1024, 4096] per-side range.
ARK_MIN_PIXELS = 3_686_400  # e.g. 2560x1440 (16:9), 1920x1920 (1:1)
ARK_MIN_SIDE = 1024
ARK_MAX_SIDE = 4096
ARK_SIZE_ALIGN = 32


def _parse_ratio(value: str | None) -> tuple[int, int] | None:
    """Parse ``"W:H"`` or ``"WxH"`` into a positive ``(w, h)`` tuple, else None."""
    if not value:
        return None
    # Bounded digit runs: real ratios/sizes are short, and unbounded runs can
    # exceed int()'s 4300-digit limit (ValueError) on user-supplied input.
    m = re.fullmatch(r"(\d{1,5})[:x](\d{1,5})", value.strip())
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0:
        return None
    return w, h


def _ceil_align(value: float) -> int:
    return int(math.ceil(value / ARK_SIZE_ALIGN) * ARK_SIZE_ALIGN)


def _resolve_ark_size(aspect_ratio: str | None, size: str | None) -> str:
    """Compute an explicit ``WxH`` Ark size that preserves the requested aspect
    ratio and meets Seedream's minimum pixel budget.

    Hardcoding a tier like ``"2K"`` lets Seedream pick the aspect ratio per
    image, producing inconsistent landscape/portrait output. Passing explicit
    dimensions locks the orientation the caller asked for. The aspect ratio is
    preferred over ``size`` because callers may cap ``size`` (e.g. 2048/side).
    """
    aw, ah = _parse_ratio(aspect_ratio) or _parse_ratio(size) or (1, 1)
    ratio = aw / ah
    # Exact dims hitting the pixel floor, then round each side up to the 32px
    # grid (rounding up only increases the count, so the floor still holds).
    height = math.sqrt(ARK_MIN_PIXELS / ratio)
    width = ratio * height
    w = min(max(_ceil_align(width), ARK_MIN_SIDE), ARK_MAX_SIDE)
    h = min(max(_ceil_align(height), ARK_MIN_SIDE), ARK_MAX_SIDE)
    # Extreme ratios can clamp a side and dip back under the floor; bump the
    # smaller side until the budget is met or both sides hit the ceiling.
    while w * h < ARK_MIN_PIXELS:
        if w <= h and w < ARK_MAX_SIDE:
            w += ARK_SIZE_ALIGN
        elif h < ARK_MAX_SIDE:
            h += ARK_SIZE_ALIGN
        else:
            break
    return f"{w}x{h}"


async def _post_with_retries(
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    model: str,
    provider: str,
    http_client: httpx.AsyncClient | None = None,
) -> httpx.Response:
    """POST with bounded retries on timeout/connect errors.

    Returns the upstream response (caller checks status_code). Raises
    GenerationError after exhausting retries.
    """
    resp = None
    for attempt in range(1, MAX_RETRIES + 2):  # 1 initial + MAX_RETRIES retries
        _upstream_attempts.set(_upstream_attempts.get() + 1)
        logger.info("upstream_request", provider=provider, model=model, attempt=attempt)
        try:
            if http_client is not None:
                resp = await http_client.post(url, headers=headers, json=payload)
            else:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    resp = await client.post(url, headers=headers, json=payload)
            break  # Success, exit retry loop
        except httpx.TimeoutException:
            logger.warning("upstream_timeout", provider=provider, model=model, attempt=attempt)
            if attempt < MAX_RETRIES + 1:
                await asyncio.sleep(2)
                continue
            raise GenerationError(
                ErrorCode.UPSTREAM_TIMEOUT,
                upstream_detail=f"{provider} timed out after {MAX_RETRIES + 1} attempts",
            ) from None
        except httpx.ConnectError:
            logger.warning("upstream_connect_error", provider=provider, model=model, attempt=attempt)
            if attempt < MAX_RETRIES + 1:
                await asyncio.sleep(2)
                continue
            raise GenerationError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                upstream_detail=f"cannot connect to {provider} after {MAX_RETRIES + 1} attempts",
            ) from None
        except httpx.TransportError as e:
            # e.g. connection dropped mid-response: not retried, the upstream may
            # already be generating (and billing) this request.
            raise GenerationError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                upstream_detail=f"{provider} transport error: {type(e).__name__}",
            ) from None
    assert resp is not None  # loop always breaks with resp set or raises
    return resp


def _upstream_error_fields(resp: httpx.Response) -> tuple[str, str]:
    """Best-effort ``(error.code, error.message)`` from an OpenAI-style error body."""
    try:
        err = resp.json().get("error")
    except Exception:
        err = None
    if not isinstance(err, dict):
        return "", resp.text[:500]
    return str(err.get("code") or ""), str(err.get("message") or "")


def _json_body(resp: httpx.Response, provider: str) -> dict:
    """Parse a 200 body; a non-JSON or non-object body counts as "no image"."""
    try:
        data = resp.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise GenerationError(
            ErrorCode.NO_IMAGE_RETURNED, upstream_detail=f"non-JSON {provider} body: {resp.text[:200]!r}"
        )
    return data


def _status_error_code(status: int) -> ErrorCode:
    if status == 429 or status >= 500:
        return ErrorCode.UPSTREAM_UNAVAILABLE
    return ErrorCode.UPSTREAM_ERROR


def _gemini_http_error(resp: httpx.Response) -> GenerationError:
    _, message = _upstream_error_fields(resp)
    status = resp.status_code
    # 3.1 Flash says "aspect_ratio must be one of ..."; Pro / 2.5 say
    # "Aspect ratio X is not supported for this model".
    ratio_rejected = "aspect_ratio must be one of" in message or (
        "Aspect ratio" in message and "is not supported for this model" in message
    )
    if status == 400 and ratio_rejected:
        return GenerationError(
            ErrorCode.INVALID_PARAMS,
            "aspect_ratio is not supported by this model.",
            upstream_status=status,
            upstream_detail=message,
            fields=["aspect_ratio"],
        )
    if status in (400, 403) and any(marker in message for marker in _GEMINI_SAFETY_MARKERS):
        code = ErrorCode.CONTENT_FILTERED
    else:
        code = _status_error_code(status)
    return GenerationError(code, upstream_status=status, upstream_detail=message)


def _ark_http_error(resp: httpx.Response) -> GenerationError:
    # Ark reports moderation as error codes like InputTextSensitiveContentDetected /
    # OutputImageSensitiveContentDetected (per Volcengine docs; not yet seen live).
    ark_code, message = _upstream_error_fields(resp)
    status = resp.status_code
    code = ErrorCode.CONTENT_FILTERED if "SensitiveContent" in ark_code else _status_error_code(status)
    return GenerationError(code, upstream_status=status, upstream_detail=f"{ark_code}: {message}".strip(": "))


def _search_summary(data: dict) -> dict[str, object]:
    """Search queries / source count from LiteLLM's Gemini grounding metadata, for logs."""
    queries: list[str] = []
    sources = 0
    for meta in data.get("vertex_ai_grounding_metadata") or []:
        if isinstance(meta, dict):
            queries += meta.get("webSearchQueries") or []
            queries += meta.get("imageSearchQueries") or []
            sources += len(meta.get("groundingChunks") or [])
    usage = data.get("usage") or {}
    requests = (usage.get("prompt_tokens_details") or {}).get("web_search_requests") or 0
    return {"search_queries": queries, "search_sources": sources, "search_requests": requests}


async def call_gemini(
    prompt: str,
    model: str,
    settings: Settings,
    style: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    aspect_ratio: str | None = None,
    grounding: bool = False,
) -> tuple[bytes, str]:
    """Call LiteLLM Proxy (OpenAI-compatible chat+modalities) and return (image_bytes, format_ext)."""
    content = prompt
    if style:
        content = f"{prompt}, style: {style}"

    payload: dict[str, object] = {
        "model": f"gemini/{model}",
        "messages": [{"role": "user", "content": content}],
        "modalities": ["text", "image"],
        # LiteLLM's response cache does not key on imageConfig and would hand back
        # an image with the wrong ratio (or a stale one on regenerate). This
        # service caches on the full parameter set itself, so skip LiteLLM's.
        "cache": {"no-cache": True, "no-store": True},
    }
    if aspect_ratio:
        # LiteLLM forwards a top-level imageConfig to Gemini's generationConfig.
        payload["imageConfig"] = {"aspectRatio": aspect_ratio}
    if grounding:
        # LiteLLM maps this to Gemini's google_search tool. The model still
        # decides whether to search; _search_stats records what it did.
        payload["tools"] = [GOOGLE_SEARCH_TOOL]

    url = f"{settings.litellm_base_url}/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.litellm_api_key}",
    }

    resp = await _post_with_retries(url, headers, payload, model=model, provider="litellm", http_client=http_client)

    if resp.status_code != 200:
        raise _gemini_http_error(resp)

    data = _json_body(resp, "litellm")
    if grounding:
        _search_stats.set(_search_summary(data))
    choices = data.get("choices") or []
    if not choices:
        raise GenerationError(ErrorCode.NO_IMAGE_RETURNED, upstream_detail="no choices in LiteLLM response")

    finish_reason = choices[0].get("finish_reason")
    message = choices[0].get("message") or {}
    images = message.get("images") or []

    for img in images:
        data_url = img.get("image_url", {}).get("url", "")
        if data_url.startswith("data:"):
            header, _, b64_data = data_url.partition(",")
            mime_type = header.split(";")[0].replace("data:", "")
            image_bytes = base64.b64decode(b64_data)
            fmt = mime_type.split("/")[-1] if "/" in mime_type else "png"
            return image_bytes, fmt

    detail = f"finish_reason={finish_reason} content={str(message.get('content') or '')[:300]!r}"
    if finish_reason == "content_filter":
        raise GenerationError(ErrorCode.CONTENT_FILTERED, upstream_detail=detail)
    raise GenerationError(ErrorCode.NO_IMAGE_RETURNED, upstream_detail=detail)


async def call_seedream_images(
    prompt: str,
    model: str,
    settings: Settings,
    style: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    aspect_ratio: str | None = None,
    size: str | None = None,
) -> tuple[bytes, str]:
    """Call Volcengine Ark (OpenAI Images API) for Seedream; return (image_bytes, 'jpeg')."""
    if not settings.ark_api_key:
        raise GenerationError(ErrorCode.CONFIG_MISSING, upstream_detail="ARK_API_KEY is not configured")

    content = prompt
    if style:
        content = f"{prompt}, style: {style}"

    payload = {
        "model": settings.ark_model_id,
        "prompt": content,
        "size": _resolve_ark_size(aspect_ratio, size),
        "response_format": "b64_json",
        "sequential_image_generation": "disabled",
        "watermark": False,
    }

    url = f"{settings.ark_base_url}/images/generations"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.ark_api_key}",
    }

    resp = await _post_with_retries(url, headers, payload, model=model, provider="ark", http_client=http_client)

    if resp.status_code != 200:
        raise _ark_http_error(resp)

    data = _json_body(resp, "ark")
    items = data.get("data", [])
    if items:
        b64_data = items[0].get("b64_json")
        if b64_data:
            return base64.b64decode(b64_data), "jpeg"

    raise GenerationError(ErrorCode.NO_IMAGE_RETURNED, upstream_detail="no b64_json in Ark response")


def save_image(image_bytes: bytes, gen_id: str, fmt: str, storage_dir: str) -> str:
    """Save image to disk under its generation id and return the filename.

    Named by generation id, not cache key, so a regenerated image never
    overwrites a file whose URL was already handed out.
    """
    filename = f"{gen_id}.{fmt}"
    path = Path(storage_dir) / filename
    path.write_bytes(image_bytes)
    logger.info("image_saved", path=str(path), size=len(image_bytes))
    return filename


def _cached_response(cached: dict, requested_model: str) -> GenerateResponse:
    return GenerateResponse(
        id=cached["id"],
        image_url=cached["image_url"],
        model=cached["model"],
        requested_model=requested_model,
        fallback_used=cached["model"] != requested_model,
        grounding_used=cached.get("grounding_used", False),
        cached=True,
        created_at=cached["created_at"],
    )


async def handle_generate(
    request: GenerateRequest,
    settings: Settings,
    cache: CacheService,
    http_client: httpx.AsyncClient | None = None,
    entry: str = "rest",
) -> GenerateResponse:
    """Generate (or fetch from cache) and emit one ``generation_finished`` summary log."""
    started = time.perf_counter()
    _upstream_attempts.set(0)
    _search_stats.set(None)
    requested_model = request.model or settings.default_model
    fields: dict[str, object] = {
        "request_id": str(uuid.uuid4()),
        "entry": entry,
        "requested_model": requested_model,
        "aspect_ratio": request.aspect_ratio,
        "size": request.size,
        "fallback_model": request.fallback_model,
        "grounding": request.grounding,
        "no_cache": request.no_cache,
        "prompt_len": len(request.prompt),
        "prompt_sha8": hashlib.sha256(request.prompt.encode()).hexdigest()[:8],
    }

    def finish(level: str, **extra: object) -> None:
        getattr(logger, level)(
            "generation_finished",
            **fields,
            **extra,
            attempts=_upstream_attempts.get(),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    try:
        result = await _generate(request, requested_model, settings, cache, http_client)
    except GenerationError as e:
        e.model = e.model or requested_model
        finish(
            "warning",
            outcome="error",
            model=e.model,
            error_code=e.code.value,
            upstream_status=e.upstream_status,
            upstream_detail=e.upstream_detail,
            fallback_from=e.fallback_from,
            **(_search_stats.get() or {}),
        )
        raise
    except Exception:
        finish("exception", outcome="error", model=requested_model, error_code=ErrorCode.INTERNAL_ERROR.value)
        raise
    finish(
        "info",
        outcome="cached" if result.cached else "success",
        model=result.model,
        fallback_used=result.fallback_used,
        grounding_used=result.grounding_used,
        id=result.id,
        **(_search_stats.get() or {}),
    )
    return result


def _check_aspect_ratio(model: str, aspect_ratio: str, field: str) -> None:
    allowed = SUPPORTED_MODELS[model].aspect_ratios
    if allowed is not None and aspect_ratio not in allowed:
        raise GenerationError(
            ErrorCode.INVALID_PARAMS,
            f"aspect_ratio '{aspect_ratio}' is not supported by {model}. Supported: {', '.join(allowed)}.",
            fields=[field],
        )


def _validate(request: GenerateRequest, model: str) -> None:
    """Reject bad model / ratio / fallback before touching the cache or upstream."""
    if model not in SUPPORTED_MODELS:
        raise GenerationError(
            ErrorCode.MODEL_NOT_FOUND,
            f"Model '{model}' not found. Use GET /v1/models for available models.",
            fields=["model"],
        )
    _check_aspect_ratio(model, request.aspect_ratio, "aspect_ratio")

    fallback = request.fallback_model
    if fallback is None:
        return
    if fallback not in SUPPORTED_MODELS:
        raise GenerationError(
            ErrorCode.INVALID_PARAMS,
            f"fallback_model '{fallback}' not found. Use GET /v1/models for available models.",
            fields=["fallback_model"],
        )
    if fallback == model:
        raise GenerationError(
            ErrorCode.INVALID_PARAMS,
            "fallback_model must differ from model.",
            fields=["fallback_model"],
        )
    # Checked up front so a fallback can't fail on a ratio we already know it rejects.
    _check_aspect_ratio(fallback, request.aspect_ratio, "fallback_model")


def _grounding_applies(request: GenerateRequest, model: str) -> bool:
    """grounding is a request; models without search support silently ignore it."""
    return request.grounding and SUPPORTED_MODELS[model].grounding


async def _call_model(
    request: GenerateRequest,
    model: str,
    settings: Settings,
    http_client: httpx.AsyncClient | None,
) -> tuple[bytes, str]:
    """One upstream generation with ``model``; counts per-model duration and errors."""
    try:
        with GENERATION_DURATION.labels(model=model).time():
            if SUPPORTED_MODELS[model].endpoint_type == "images":
                return await call_seedream_images(
                    prompt=request.prompt,
                    model=model,
                    settings=settings,
                    style=request.style,
                    http_client=http_client,
                    aspect_ratio=request.aspect_ratio,
                    size=request.size,
                )
            return await call_gemini(
                prompt=request.prompt,
                model=model,
                settings=settings,
                style=request.style,
                http_client=http_client,
                aspect_ratio=request.aspect_ratio,
                grounding=_grounding_applies(request, model),
            )
    except GenerationError as e:
        e.model = e.model or model
        GENERATION_ERRORS.labels(model=model, error_type=e.code.value).inc()
        raise


async def _generate_with_fallback(
    request: GenerateRequest,
    model: str,
    settings: Settings,
    http_client: httpx.AsyncClient | None,
) -> tuple[bytes, str, str]:
    """Generate with ``model``; on content_filtered only, try ``fallback_model`` once.

    Returns (image bytes, format, model that produced the image).
    """
    fallback = request.fallback_model
    try:
        image_bytes, fmt = await _call_model(request, model, settings, http_client)
        return image_bytes, fmt, model
    except GenerationError as e:
        if fallback is None or e.code != ErrorCode.CONTENT_FILTERED:
            raise

    logger.info("fallback_started", from_model=model, to_model=fallback)
    try:
        image_bytes, fmt = await _call_model(request, fallback, settings, http_client)
    except GenerationError as e:
        FALLBACKS.labels(from_model=model, to_model=fallback, outcome="error").inc()
        e.fallback_from = {"model": model, "code": ErrorCode.CONTENT_FILTERED.value}
        raise
    FALLBACKS.labels(from_model=model, to_model=fallback, outcome="success").inc()
    return image_bytes, fmt, fallback


async def _generate(
    request: GenerateRequest,
    model: str,
    settings: Settings,
    cache: CacheService,
    http_client: httpx.AsyncClient | None,
) -> GenerateResponse:
    """Orchestrate generation: validate → cache check → generate (with fallback) → save → cache set."""
    _validate(request, model)

    cache_key = compute_cache_key(
        request.prompt,
        model,
        request.aspect_ratio,
        request.size,
        request.style,
        request.fallback_model,
        request.grounding,
    )

    lock_token: str | None = None
    if not request.no_cache:
        cached = await cache.get_cached(cache_key)
        if cached is not None:
            CACHE_HITS.inc()
            REQUEST_COUNT.labels(model=model, status="cached").inc()
            return _cached_response(cached, model)

        # Thundering-herd protection: one generation per parameter set; identical
        # requests wait for its result. no_cache skips this — the caller asked
        # for a fresh image, not the one an in-flight request will produce.
        lock_token = await cache.acquire_lock(cache_key, ttl=LOCK_TTL_SECONDS)
        if lock_token is None:
            cached = await cache.wait_for_cached(cache_key)
            if cached is not None:
                return _cached_response(cached, model)
            raise GenerationError(ErrorCode.GENERATION_IN_PROGRESS)

    try:
        image_bytes, fmt, actual_model = await _generate_with_fallback(request, model, settings, http_client)

        grounding_used = _grounding_applies(request, actual_model)
        gen_id = str(uuid.uuid4())
        filename = save_image(image_bytes, gen_id, fmt, settings.storage_dir)

        image_url = f"{settings.static_url_prefix}/{filename}"
        created_at = datetime.now(UTC).isoformat()

        metadata = {
            "id": gen_id,
            "image_url": image_url,
            "model": actual_model,
            "grounding_used": grounding_used,
            "created_at": created_at,
        }
        await cache.set_cached(cache_key, metadata)

        REQUEST_COUNT.labels(model=actual_model, status="success").inc()
        return GenerateResponse(
            id=gen_id,
            image_url=image_url,
            model=actual_model,
            requested_model=model,
            fallback_used=actual_model != model,
            grounding_used=grounding_used,
            cached=False,
            created_at=created_at,
        )
    except GenerationError as e:
        REQUEST_COUNT.labels(model=e.model or model, status="error").inc()
        raise
    finally:
        if lock_token is not None:
            await cache.release_lock(cache_key, lock_token)
