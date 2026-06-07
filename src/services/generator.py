from __future__ import annotations

import asyncio
import base64
import math
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import structlog

from src.config import SUPPORTED_MODELS, Settings
from src.models.request import GenerateRequest
from src.models.response import GenerateResponse
from src.services.cache import CacheService, compute_cache_key
from src.services.metrics import (
    CACHE_HITS,
    GENERATION_DURATION,
    GENERATION_ERRORS,
    REQUEST_COUNT,
)

logger = structlog.get_logger()


class GenerationError(Exception):
    """Raised when upstream image generation fails."""

    def __init__(self, message: str, status_code: int = 502):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


MAX_RETRIES = 2
REQUEST_TIMEOUT = 300.0  # 5 minutes — complex prompts can take a while

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
    m = re.fullmatch(r"(\d+)[:x](\d+)", value.strip())
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
                f"Upstream request timed out after {MAX_RETRIES + 1} attempts, please retry"
            ) from None
        except httpx.ConnectError:
            logger.warning("upstream_connect_error", provider=provider, model=model, attempt=attempt)
            if attempt < MAX_RETRIES + 1:
                await asyncio.sleep(2)
                continue
            raise GenerationError(f"Cannot connect to {provider} after retries") from None
    assert resp is not None  # loop always breaks with resp set or raises
    return resp


async def call_gemini(
    prompt: str,
    model: str,
    settings: Settings,
    style: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[bytes, str]:
    """Call LiteLLM Proxy (OpenAI-compatible chat+modalities) and return (image_bytes, format_ext)."""
    content = prompt
    if style:
        content = f"{prompt}, style: {style}"

    payload = {
        "model": f"gemini/{model}",
        "messages": [{"role": "user", "content": content}],
        "modalities": ["text", "image"],
    }

    url = f"{settings.litellm_base_url}/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.litellm_api_key}",
    }

    resp = await _post_with_retries(url, headers, payload, model=model, provider="litellm", http_client=http_client)

    if resp.status_code != 200:
        error_msg = "upstream error"
        try:
            error_data = resp.json()
            error_msg = error_data.get("error", {}).get("message", error_msg)
        except Exception:
            pass
        raise GenerationError(f"LiteLLM Proxy error: {error_msg}")

    data = resp.json()
    choices = data.get("choices", [])
    if not choices:
        raise GenerationError("No choices returned from LiteLLM Proxy")

    message = choices[0].get("message", {})
    images = message.get("images", [])

    for img in images:
        data_url = img.get("image_url", {}).get("url", "")
        if data_url.startswith("data:"):
            header, _, b64_data = data_url.partition(",")
            mime_type = header.split(";")[0].replace("data:", "")
            image_bytes = base64.b64decode(b64_data)
            fmt = mime_type.split("/")[-1] if "/" in mime_type else "png"
            return image_bytes, fmt

    raise GenerationError("No image content in LiteLLM Proxy response")


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
        raise GenerationError("ARK_API_KEY is not configured", status_code=502)

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
        error_msg = "upstream error"
        try:
            error_data = resp.json()
            error_msg = error_data.get("error", {}).get("message", error_msg)
        except Exception:
            pass
        raise GenerationError(f"Ark error: {error_msg}")

    data = resp.json()
    items = data.get("data", [])
    if items:
        b64_data = items[0].get("b64_json")
        if b64_data:
            return base64.b64decode(b64_data), "jpeg"

    raise GenerationError("No image content in Ark response")


def save_image(image_bytes: bytes, cache_key: str, fmt: str, storage_dir: str) -> str:
    """Save image to disk and return the relative URL path."""
    filename = f"{cache_key}.{fmt}"
    path = Path(storage_dir) / filename
    path.write_bytes(image_bytes)
    logger.info("image_saved", path=str(path), size=len(image_bytes))
    return filename


async def handle_generate(
    request: GenerateRequest,
    settings: Settings,
    cache: CacheService,
    http_client: httpx.AsyncClient | None = None,
) -> GenerateResponse:
    """Orchestrate generation: cache check → generate → save → cache set."""
    model = request.model or settings.default_model

    # Validate model
    if model not in SUPPORTED_MODELS:
        raise GenerationError(
            f"Model '{model}' not found. Use GET /v1/models for available models.",
            status_code=400,
        )

    cache_key = compute_cache_key(request.prompt, model, request.aspect_ratio, request.size, request.style)

    # Check cache
    cached = await cache.get_cached(cache_key)
    if cached is not None:
        CACHE_HITS.inc()
        REQUEST_COUNT.labels(model=model, status="cached").inc()
        return GenerateResponse(
            id=cached["id"],
            image_url=cached["image_url"],
            model=cached["model"],
            cached=True,
            created_at=cached["created_at"],
        )

    # Acquire lock (thundering herd protection)
    lock_acquired = await cache.acquire_lock(cache_key)
    if not lock_acquired:
        for _ in range(20):
            await asyncio.sleep(0.5)
            cached = await cache.get_cached(cache_key)
            if cached is not None:
                return GenerateResponse(
                    id=cached["id"],
                    image_url=cached["image_url"],
                    model=cached["model"],
                    cached=True,
                    created_at=cached["created_at"],
                )
        raise GenerationError("Generation timed out waiting for lock")

    try:
        with GENERATION_DURATION.labels(model=model).time():
            if SUPPORTED_MODELS[model].endpoint_type == "images":
                image_bytes, fmt = await call_seedream_images(
                    prompt=request.prompt,
                    model=model,
                    settings=settings,
                    style=request.style,
                    http_client=http_client,
                    aspect_ratio=request.aspect_ratio,
                    size=request.size,
                )
            else:
                image_bytes, fmt = await call_gemini(
                    prompt=request.prompt,
                    model=model,
                    settings=settings,
                    style=request.style,
                    http_client=http_client,
                )

        filename = save_image(image_bytes, cache_key, fmt, settings.storage_dir)

        gen_id = str(uuid.uuid4())
        image_url = f"{settings.static_url_prefix}/{filename}"
        created_at = datetime.now(UTC).isoformat()

        metadata = {
            "id": gen_id,
            "image_url": image_url,
            "model": model,
            "created_at": created_at,
        }
        await cache.set_cached(cache_key, metadata)

        REQUEST_COUNT.labels(model=model, status="success").inc()
        return GenerateResponse(
            id=gen_id,
            image_url=image_url,
            model=model,
            cached=False,
            created_at=created_at,
        )
    except GenerationError:
        GENERATION_ERRORS.labels(model=model, error_type="generation").inc()
        REQUEST_COUNT.labels(model=model, status="error").inc()
        raise
    finally:
        await cache.release_lock(cache_key)
