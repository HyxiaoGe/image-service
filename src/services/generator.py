from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import os
import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import anyio
import httpx
import structlog

from src.config import SUPPORTED_MODELS, Settings
from src.models.errors import ERROR_SPECS, ErrorCode
from src.models.identity import CallerIdentity
from src.models.lifetime import RequestLifetime
from src.models.request import MAX_REFERENCE_IMAGES, GenerateRequest
from src.models.response import GenerateResponse, ReferenceSearchInfo
from src.services.budget import CLEANUP_TIMEOUT_SECONDS, current_budget, generation_scope
from src.services.cache import CacheService, compute_cache_key
from src.services.metrics import (
    CACHE_HITS,
    FALLBACKS,
    GENERATION_DURATION,
    GENERATION_ERRORS,
    REQUEST_COUNT,
)
from src.services.reference_search import GENERATION_TTL, SearchOutcome, find_references
from src.services.references import ReferenceImage, ReferenceImageError, _sniff_mime, image_size, load_reference_images
from src.services.usage import UsageStore, ark_estimate, usage_store


@dataclass
class UsageRun:
    request_id: str
    store: UsageStore | None


_caller_identity: ContextVar[CallerIdentity | None] = ContextVar("caller_identity", default=None)
_usage_run: ContextVar[UsageRun | None] = ContextVar("usage_run", default=None)

logger = structlog.get_logger()

# Appended to the prompt when reference_subject found images (approved wording).
REFERENCE_HINT = "参考图是 {subject} 的真实照片，外观以它为准。"
QUALITY_MODELS = {
    "standard": "doubao-seedream-5-0-flash",
    "high": "doubao-seedream-5-0-pro",
}


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
        maybe_billed: bool = False,
        timeout_fallback_allowed: bool = False,
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
        self.fallback_from = fallback_from  # set when every fallback model also failed
        # The upstream may already have produced (and billed) an image, e.g. the
        # connection dropped mid-response. Only generation timeouts explicitly
        # opt into a backup despite that uncertainty.
        self.maybe_billed = maybe_billed
        self.timeout_fallback_allowed = timeout_fallback_allowed
        super().__init__(self.message)


MAX_RETRIES = 2


@dataclass
class ModelAttempt:
    deadline: float
    submitted: bool = False
    response_received: bool = False


_model_attempt: ContextVar[ModelAttempt | None] = ContextVar("model_attempt", default=None)

# Upstream POST attempts (incl. retries) for the current request; read by the
# summary log in handle_generate.
_upstream_attempts: ContextVar[int] = ContextVar("upstream_attempts", default=0)

# What Google Search did for the current request (queries, source count), when
# grounding was on; read by the summary log.
_search_stats: ContextVar[dict[str, object] | None] = ContextVar("search_stats", default=None)

# Summary-log fields of the current request, so deeper steps can add what they
# resolve (e.g. the effective fallback model).
_log_fields: ContextVar[dict[str, object]] = ContextVar("log_fields")


def _google_search_tool(search_types: tuple[str, ...]) -> dict:
    """Gemini googleSearch tool. Without searchTypes Gemini only does web search;
    image search must be requested explicitly (LiteLLM forwards searchTypes as-is)."""
    if search_types == ("web",):
        return {"googleSearch": {}}
    return {"googleSearch": {"searchTypes": {f"{t}Search": {} for t in search_types}}}


# Markers Gemini uses for safety blocks (blockReason / finishReason values) when
# they surface as an HTTP error instead of a 200 with finish_reason=content_filter.
_GEMINI_SAFETY_MARKERS = ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST")

# Seedream limits apply to total pixels and ratio, not each side. Keep the
# 32px grid used by our existing integration, without clamping long images.
ARK_SIZE_ALIGN = 32
ARK_MAX_RATIO = 16
ARK_PRO_LOW_PRICE_PIXELS = 2_610_000


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


def _resolve_ark_size(aspect_ratio: str | None, size: str | None, model: str = "doubao-seedream-4-5") -> str:
    """Use size's pixel budget and aspect_ratio's shape within this model's limits.

    Align the short side first, then derive the long side from it so extreme
    ratios survive rounding. Pro requests in the cheaper pixel tier stay there.
    """
    info = SUPPORTED_MODELS[model]
    aw, ah = _parse_ratio(aspect_ratio) or _parse_ratio(size) or (1, 1)
    ratio = aw / ah
    if not 1 / ARK_MAX_RATIO <= ratio <= ARK_MAX_RATIO:
        raise GenerationError(
            ErrorCode.INVALID_PARAMS,
            "Seedream aspect_ratio must be between 1:16 and 16:1.",
            fields=["aspect_ratio"],
        )
    sw, sh = _parse_ratio(size) or (1024, 1024)
    target = min(max(sw * sh, info.ark_min_pixels), info.ark_max_pixels)
    ceiling = info.ark_max_pixels
    if model == "doubao-seedream-5-0-pro" and target <= ARK_PRO_LOW_PRICE_PIXELS:
        ceiling = min(ceiling, ARK_PRO_LOW_PRICE_PIXELS)
    long_ratio = max(ratio, 1 / ratio)
    short = _ceil_align(math.sqrt(target / long_ratio))
    long = _ceil_align(short * long_ratio)
    while short * long > ceiling:
        short -= ARK_SIZE_ALIGN
        long = _ceil_align(short * long_ratio)
    w, h = (long, short) if ratio >= 1 else (short, long)
    return f"{w}x{h}"


async def _post_with_retries(
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    model: str,
    provider: str,
    http_client: httpx.AsyncClient | None = None,
    *,
    timeout_seconds: float = 60,
) -> httpx.Response:
    """Retry only connection/pool failures, inside the model and request deadlines.

    Returns the upstream response (caller checks status_code). Raises
    GenerationError after exhausting retries.
    """
    resp = None
    for attempt in range(1, MAX_RETRIES + 2):  # 1 initial + MAX_RETRIES retries
        budget = current_budget()
        if budget is not None:
            await budget.checkpoint()
        else:
            await anyio.lowlevel.checkpoint()
        state = _model_attempt.get()
        if state is not None and state.deadline <= time.monotonic():
            raise GenerationError(ErrorCode.UPSTREAM_TIMEOUT, timeout_fallback_allowed=True)
        remaining = min(timeout_seconds, max(0.001, state.deadline - time.monotonic())) if state else timeout_seconds
        timeout = httpx.Timeout(remaining, connect=min(5, remaining), pool=min(5, remaining))
        run = _usage_run.get()
        attempt_id = None
        if run and run.store:
            alias = (
                _log_fields.get({}).get("credential_alias", "legacy-ark") if provider == "ark" else f"shared-{provider}"
            )
            try:
                attempt_id = await run.store.start_attempt(
                    run.request_id,
                    {
                        "model": model,
                        "provider": provider,
                        "credential_alias": alias,
                        "credential_resource_id": _log_fields.get({}).get("credential_resource_id")
                        if provider == "ark"
                        else None,
                    },
                )
            except Exception:
                raise GenerationError(ErrorCode.USAGE_UNAVAILABLE) from None
        attempt_started = time.monotonic()

        async def record_attempt(
            outcome: str, *, run=run, attempt_id=attempt_id, attempt_started=attempt_started, **extra
        ) -> None:
            if run and run.store and attempt_id:
                try:
                    with anyio.move_on_after(0.5, shield=True):
                        await run.store.finish_attempt(
                            attempt_id,
                            {
                                "outcome": outcome,
                                "duration_ms": int((time.monotonic() - attempt_started) * 1000),
                                **extra,
                            },
                        )
                except Exception:
                    # 已有持久化的 submitted 行，保留为待对账，不因记账更新失败重发生图。
                    logger.error("usage_attempt_update_failed", request_id=run.request_id, attempt_id=attempt_id)

        _upstream_attempts.set(_upstream_attempts.get() + 1)
        logger.info("upstream_request", provider=provider, model=model, attempt=attempt)
        try:
            if budget is not None:
                await budget.checkpoint()
            if state is not None:
                state.submitted = True
            if http_client is not None:
                resp = await http_client.post(url, headers=headers, json=payload, timeout=timeout)
            else:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, headers=headers, json=payload)
            if state is not None:
                state.response_received = resp.status_code == 200
            details = {
                "http_status": resp.status_code,
                "upstream_request_id": resp.headers.get("x-tt-logid") or resp.headers.get("x-request-id"),
            }
            if provider == "ark" and resp.status_code == 200:
                try:
                    data = resp.json()
                    if isinstance(data, dict):
                        details.update(ark_estimate(payload, data))
                        usage = data.get("usage")
                        if isinstance(usage, dict):
                            details.update(
                                {
                                    k: v
                                    for k, v in usage.items()
                                    if k in {"input_images", "generated_images", "output_tokens", "total_tokens"}
                                    and type(v) is int
                                }
                            )
                except ValueError:
                    pass
            await record_attempt("response", **details)
            break  # Success, exit retry loop
        except httpx.TimeoutException as e:
            logger.warning("upstream_timeout", provider=provider, model=model, attempt=attempt)
            safe_to_retry = isinstance(e, (httpx.ConnectTimeout, httpx.PoolTimeout))
            if safe_to_retry and state is not None:
                state.submitted = False
            await record_attempt("not_submitted" if safe_to_retry else "timeout")
            if safe_to_retry and attempt < MAX_RETRIES + 1:
                await asyncio.sleep(2)
                continue
            raise GenerationError(
                ErrorCode.UPSTREAM_TIMEOUT,
                upstream_detail=f"{provider} timed out after {attempt} attempts",
                maybe_billed=not safe_to_retry,
                timeout_fallback_allowed=True,
            ) from None
        except httpx.ConnectError:
            if state is not None:
                state.submitted = False
            await record_attempt("not_submitted")
            logger.warning("upstream_connect_error", provider=provider, model=model, attempt=attempt)
            if attempt < MAX_RETRIES + 1:
                await asyncio.sleep(2)
                continue
            raise GenerationError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                upstream_detail=f"cannot connect to {provider} after {MAX_RETRIES + 1} attempts",
            ) from None
        except httpx.TransportError as e:
            await record_attempt("transport_error")
            # e.g. connection dropped mid-response: not retried, the upstream may
            # already be generating (and billing) this request.
            raise GenerationError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                upstream_detail=f"{provider} transport error: {type(e).__name__}",
                maybe_billed=True,
            ) from None
        except asyncio.CancelledError:
            await record_attempt("cancelled" if state is None or state.submitted else "not_submitted")
            raise
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
    # 402: our provider balance is exhausted -- the provider is down for us until topped up.
    if status in (402, 429) or status >= 500:
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
    if "SensitiveContent" in ark_code:
        code = ErrorCode.CONTENT_FILTERED
    elif status == 403 and ark_code in {"AccountOverdueError", "OperationDenied.ServiceOverdue"}:
        code = ErrorCode.UPSTREAM_UNAVAILABLE
    else:
        code = _status_error_code(status)
    return GenerationError(code, upstream_status=status, upstream_detail=f"{ark_code}: {message}".strip(": "))


def _search_summary(data: dict) -> dict[str, object]:
    """Search queries / source counts from LiteLLM's Gemini grounding metadata, for logs.

    Empty queries mean the model chose not to search even though the tool was on.
    """
    web: list[str] = []
    image: list[str] = []
    web_sources = image_sources = 0
    for meta in data.get("vertex_ai_grounding_metadata") or []:
        if isinstance(meta, dict):
            web += meta.get("webSearchQueries") or []
            image += meta.get("imageSearchQueries") or []
            for chunk in meta.get("groundingChunks") or []:
                if "image" in chunk:
                    image_sources += 1
                else:
                    web_sources += 1
    return {
        "search_queries": web,
        "image_search_queries": image,
        "search_sources": web_sources,
        "image_search_sources": image_sources,
    }


def _gemini_content(text: str, reference_images: list[ReferenceImage] | None) -> str | list[dict]:
    """Plain text, or OpenAI-style multi-part content (text first, then images) that
    LiteLLM turns into Gemini inline image parts."""
    if not reference_images:
        return text
    return [
        {"type": "text", "text": text},
        *({"type": "image_url", "image_url": {"url": ref.data_uri}} for ref in reference_images),
    ]


async def call_gemini(
    prompt: str,
    model: str,
    settings: Settings,
    style: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    aspect_ratio: str | None = None,
    search_types: tuple[str, ...] = (),
    reference_images: list[ReferenceImage] | None = None,
) -> tuple[bytes, str]:
    """Call LiteLLM Proxy (OpenAI-compatible chat+modalities) and return (image_bytes, format_ext)."""
    content = prompt
    if style:
        content = f"{prompt}, style: {style}"

    payload: dict[str, object] = {
        "model": f"gemini/{model}",
        "messages": [{"role": "user", "content": _gemini_content(content, reference_images)}],
        "modalities": ["text", "image"],
        # LiteLLM's response cache does not key on imageConfig and would hand back
        # an image with the wrong ratio (or a stale one on regenerate). This
        # service caches on the full parameter set itself, so skip LiteLLM's.
        "cache": {"no-cache": True, "no-store": True},
    }
    if aspect_ratio:
        # LiteLLM forwards a top-level imageConfig to Gemini's generationConfig.
        payload["imageConfig"] = {"aspectRatio": aspect_ratio}
    if search_types:
        # LiteLLM maps this to Gemini's google_search tool. The model still
        # decides whether to search; _search_stats records what it did.
        payload["tools"] = [_google_search_tool(search_types)]

    url = f"{settings.litellm_base_url}/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.litellm_api_key}",
    }

    resp = await _post_with_retries(
        url,
        headers,
        payload,
        model=model,
        provider="litellm",
        http_client=http_client,
        timeout_seconds=settings.model_timeout_seconds,
    )

    if resp.status_code != 200:
        raise _gemini_http_error(resp)

    data = _json_body(resp, "litellm")
    if search_types:
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
    reference_images: list[ReferenceImage] | None = None,
) -> tuple[bytes, str]:
    """Call Volcengine Ark for Seedream; return image bytes and detected format."""
    caller = _caller_identity.get()
    ark_key = settings.ark_api_key
    alias, resource_id = "legacy-ark", None
    if caller is not None and caller.auth_method != "legacy":
        product = settings.products.get(caller.app_id)
        if product is None:
            raise GenerationError(ErrorCode.CONFIG_MISSING)
        ark_key = os.environ.get(product.ark_key_env, "").strip()
        alias, resource_id = product.ark_key_alias, product.ark_resource_id
    if not ark_key:
        raise GenerationError(ErrorCode.CONFIG_MISSING, upstream_detail="ARK_API_KEY is not configured")
    _log_fields.get({}).update(credential_alias=alias, credential_resource_id=resource_id)

    content = prompt
    if style:
        content = f"{prompt}, style: {style}"

    info = SUPPORTED_MODELS[model]
    payload = {
        "model": info.ark_model_id or settings.ark_model_id,
        "prompt": content,
        "size": _resolve_ark_size(aspect_ratio, size, model),
        "response_format": "b64_json",
        "watermark": False,
    }
    if info.sequential_param:
        payload["sequential_image_generation"] = "disabled"
    if reference_images:
        uris = [ref.data_uri for ref in reference_images]
        payload["image"] = uris[0] if len(uris) == 1 else uris

    url = f"{settings.ark_base_url}/images/generations"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ark_key}",
    }

    _log_fields.get({}).update(ark_model=payload["model"], ark_size=payload["size"])
    resp = await _post_with_retries(
        url,
        headers,
        payload,
        model=model,
        provider="ark",
        http_client=http_client,
        timeout_seconds=_model_timeout(model, settings),
    )
    _log_fields.get({})["ark_request_id"] = resp.headers.get("x-tt-logid") or resp.headers.get("x-request-id")

    if resp.status_code != 200:
        raise _ark_http_error(resp)

    data = _json_body(resp, "ark")
    _log_fields.get({})["ark_usage"] = data.get("usage")
    items = data.get("data", [])
    if items:
        b64_data = items[0].get("b64_json")
        if b64_data:
            image_bytes = base64.b64decode(b64_data)
            mime = _sniff_mime(image_bytes)
            _log_fields.get({})["ark_output_size"] = items[0].get("size")
            return image_bytes, mime.split("/")[-1] if mime else "jpeg"

    raise GenerationError(ErrorCode.NO_IMAGE_RETURNED, upstream_detail="no b64_json in Ark response")


# Qwen-Image bills 1k and 2k output separately (3.0-pro: 0.25 vs 0.5 CNY); stay
# within the 1k budget. Bailian accepts 512*512..2048*2048 total, ratio 1:8..8:1.
QWEN_MAX_PIXELS = 1024 * 1024
QWEN_SIZE_ALIGN = 16
QWEN_MAX_RATIO = 8
QWEN_DOWNLOAD_TIMEOUT = 60.0
# Bailian moderation codes (DataInspectionFailed / data_inspection_failed for
# input or output review, IPInfringementSuspect for suspected IP infringement).
_QWEN_FILTER_MARKERS = ("DataInspection", "data_inspection", "IPInfringement")


def _resolve_qwen_size(aspect_ratio: str | None, size: str | None) -> str:
    """Largest ``WxH`` on the 16px grid that keeps the ratio within the 1k budget."""
    aw, ah = _parse_ratio(aspect_ratio) or _parse_ratio(size) or (1, 1)
    ratio = min(max(aw / ah, 1 / QWEN_MAX_RATIO), QWEN_MAX_RATIO)
    height = math.sqrt(QWEN_MAX_PIXELS / ratio)
    w = int(ratio * height) // QWEN_SIZE_ALIGN * QWEN_SIZE_ALIGN
    h = int(height) // QWEN_SIZE_ALIGN * QWEN_SIZE_ALIGN
    # Grid rounding can push an extreme ratio just past 8:1; clamp the long side.
    w, h = min(w, QWEN_MAX_RATIO * h), min(h, QWEN_MAX_RATIO * w)
    return f"{w}x{h}"


def _qwen_http_error(resp: httpx.Response) -> GenerationError:
    qwen_code, message = _upstream_error_fields(resp)
    if not qwen_code:  # native DashScope shape: {"code": ..., "message": ...}
        try:
            body = resp.json()
            qwen_code, message = str(body.get("code") or ""), str(body.get("message") or message)
        except Exception:
            pass
    status = resp.status_code
    filtered = any(marker in qwen_code or marker in message for marker in _QWEN_FILTER_MARKERS)
    code = ErrorCode.CONTENT_FILTERED if filtered else _status_error_code(status)
    return GenerationError(code, upstream_status=status, upstream_detail=f"{qwen_code}: {message}".strip(": "))


def _qwen_image_url(data: dict) -> str | None:
    """``output.choices[0].message.content[*].image`` from a DashScope sync response."""
    try:
        content = data["output"]["choices"][0]["message"]["content"]
        return next((part["image"] for part in content if isinstance(part, dict) and part.get("image")), None)
    except (KeyError, IndexError, TypeError):
        return None


async def call_qwen_images(
    prompt: str,
    model: str,
    settings: Settings,
    style: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    aspect_ratio: str | None = None,
    size: str | None = None,
    reference_images: list[ReferenceImage] | None = None,
) -> tuple[bytes, str]:
    """Call Bailian's native DashScope sync API for Qwen-Image; return (image_bytes, format).

    The OpenAI-compatible images route only exists on per-workspace hosts (the
    legacy dashscope.aliyuncs.com host answers 404), while this native route works
    on both. Bailian only returns a 24-hour result URL, so the image is downloaded
    here and stored like any other generation.
    """
    if not settings.dashscope_api_key:
        raise GenerationError(ErrorCode.CONFIG_MISSING, upstream_detail="DASHSCOPE_API_KEY is not configured")

    content = prompt
    if style:
        content = f"{prompt}, style: {style}"

    parts: list[dict[str, str]] = [{"image": ref.data_uri} for ref in reference_images or []]
    parts.append({"text": content})
    payload: dict[str, object] = {
        "model": model,
        "input": {"messages": [{"role": "user", "content": parts}]},
        "parameters": {
            "size": _resolve_qwen_size(aspect_ratio, size).replace("x", "*"),
            "n": 1,
            "watermark": False,
            # Keep the caller's prompt as written (and the cache meaningful): no rewriting.
            "prompt_extend": False,
        },
    }

    url = f"{settings.dashscope_base_url}/services/aigc/multimodal-generation/generation"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {settings.dashscope_api_key}"}
    resp = await _post_with_retries(
        url,
        headers,
        payload,
        model=model,
        provider="dashscope",
        http_client=http_client,
        timeout_seconds=settings.model_timeout_seconds,
    )
    if resp.status_code != 200:
        raise _qwen_http_error(resp)

    data = _json_body(resp, "dashscope")
    _log_fields.get()["dashscope_usage"] = data.get("usage")
    image_url = _qwen_image_url(data)
    if not image_url:
        raise GenerationError(ErrorCode.NO_IMAGE_RETURNED, upstream_detail="no image url in DashScope response")

    # The image already exists (and is billed); a failed download is not retried
    # as a new generation.
    try:
        if http_client is not None:
            result = await http_client.get(image_url, timeout=QWEN_DOWNLOAD_TIMEOUT)
        else:
            async with httpx.AsyncClient(timeout=QWEN_DOWNLOAD_TIMEOUT) as client:
                result = await client.get(image_url)
    except httpx.HTTPError as e:
        raise GenerationError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            upstream_detail=f"DashScope result download failed: {type(e).__name__}",
            maybe_billed=True,
        ) from None
    mime = _sniff_mime(result.content) if result.status_code == 200 else None
    if mime is None:
        raise GenerationError(
            ErrorCode.NO_IMAGE_RETURNED, upstream_detail=f"DashScope result download: HTTP {result.status_code}"
        )
    return result.content, mime.split("/")[1]


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
    _log_fields.get({}).update(artifact_app_id=cached.get("app_id"), artifact_subject=cached.get("subject"))
    return GenerateResponse(
        id=cached["id"],
        image_url=cached["image_url"],
        model=cached["model"],
        requested_model=requested_model,
        fallback_used=cached["model"] != requested_model,
        grounding_used=cached.get("grounding_used", False),
        cached=True,
        created_at=cached["created_at"],
        reference_images_used=cached.get("reference_images_used") or [],
        reference_search=cached.get("reference_search"),
    )


async def handle_generate(
    request: GenerateRequest,
    settings: Settings,
    cache: CacheService,
    http_client: httpx.AsyncClient | None = None,
    entry: str = "rest",
    lifetime: RequestLifetime | None = None,
    caller: CallerIdentity | None = None,
) -> GenerateResponse:
    """设置独立身份上下文；落盘记账失败时不提交新的上游请求。"""
    if caller is None:
        if settings.auth_mode != "legacy":
            raise GenerationError(ErrorCode.CONFIG_MISSING, "Authenticated HTTP caller is required.")
        caller = CallerIdentity.legacy(settings.legacy_app_id)
    started = lifetime.started_at if lifetime else time.monotonic()
    path = settings.usage_db_path or str(Path(settings.storage_dir).parent / "usage.sqlite3")
    store = usage_store(path, settings.usage_retention_days) if settings.usage_enabled else None
    if store and lifetime is None:
        lifetime = RequestLifetime(started_at=started)
    run = UsageRun(str(uuid.uuid4()), store)
    identity_token = _caller_identity.set(caller)
    usage_token = _usage_run.set(run)
    fields_token = _log_fields.set({})
    outcome, result, error_code = "error", None, None
    try:
        if store:
            try:
                await store.begin(
                    {
                        "request_id": run.request_id,
                        "app_id": caller.app_id,
                        "subject": caller.subject,
                        "principal_type": caller.principal_type,
                        "authorized_party": caller.authorized_party,
                        "auth_method": caller.auth_method,
                        "scene": caller.scene,
                        "correlation_id": caller.correlation_id,
                        "entry": entry,
                        "requested_model": request.model or QUALITY_MODELS.get(request.quality, settings.default_model),
                    }
                )
            except Exception:
                raise GenerationError(ErrorCode.USAGE_UNAVAILABLE) from None
        result = await _handle_generate(request, settings, cache, http_client, entry, lifetime)
        outcome = "cached" if result.cached else "success"
        return result
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except GenerationError as exc:
        error_code = exc.code.value
        raise
    except Exception:
        error_code = ErrorCode.INTERNAL_ERROR.value
        raise
    finally:
        if store:
            record = {
                **_log_fields.get({}),
                "outcome": outcome,
                "error_code": error_code,
                "generation_id": result.id if result else _log_fields.get({}).get("generation_id"),
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
            if result:
                record["model"] = result.model
            try:
                remaining = max(0, settings.generation_timeout_seconds - (time.monotonic() - started))
                with anyio.move_on_after(min(0.5, remaining), shield=True):
                    await store.finish(run.request_id, record)
            except Exception:
                logger.error("usage_request_update_failed", request_id=run.request_id)
        _log_fields.reset(fields_token)
        _usage_run.reset(usage_token)
        _caller_identity.reset(identity_token)


async def _handle_generate(
    request: GenerateRequest,
    settings: Settings,
    cache: CacheService,
    http_client: httpx.AsyncClient | None,
    entry: str,
    lifetime: RequestLifetime | None,
) -> GenerateResponse:
    """Generate (or fetch from cache) and emit one ``generation_finished`` summary log."""
    started = lifetime.started_at if lifetime is not None else time.monotonic()
    _upstream_attempts.set(0)
    _search_stats.set(None)
    if request.model:
        requested_model, model_source = request.model, "model"
    elif request.quality is not None:
        requested_model, model_source = QUALITY_MODELS[request.quality], "quality"
    else:
        requested_model, model_source = settings.default_model, "default"
    fields: dict[str, object] = {
        "request_id": _usage_run.get().request_id,
        "entry": entry,
        "requested_model": requested_model,
        "quality": request.quality,
        "model_source": model_source,
        "total_budget_seconds": settings.generation_timeout_seconds,
        "stage": "validation",
        "aspect_ratio": request.aspect_ratio,
        "size": request.size,
        "grounding": request.grounding,
        "no_cache": request.no_cache,
        "reference_subject": request.reference_subject,
        "prompt_len": len(request.prompt),
        "prompt_sha8": hashlib.sha256(request.prompt.encode()).hexdigest()[:8],
        # Overwritten with the effective value once validation passes.
        "fallback_model": request.fallback_model,
        "fallback_source": None,
    }
    caller = _caller_identity.get()
    fields.update(
        app_id=caller.app_id,
        subject=caller.subject,
        principal_type=caller.principal_type,
        authorized_party=caller.authorized_party,
        auth_method=caller.auth_method,
        scene=caller.scene,
        correlation_id=caller.correlation_id,
    )
    _log_fields.set(fields)

    def finish(level: str, **extra: object) -> None:
        getattr(logger, level)(
            "generation_finished",
            **fields,
            **extra,
            attempts=_upstream_attempts.get(),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        try:
            async with generation_scope(settings, lifetime):
                result = await _generate(request, requested_model, settings, cache, http_client)
        except TimeoutError:
            REQUEST_COUNT.labels(model=fields.get("active_model") or requested_model, status="error").inc()
            raise GenerationError(
                ErrorCode.UPSTREAM_TIMEOUT,
                "Generation exceeded the total time budget.",
                model=fields.get("active_model") or requested_model,
                upstream_detail="total generation deadline exceeded",
                fallback_from=(fields["fallback_tried"][0] if fields.get("fallback_tried") else None),
            ) from None
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
    except asyncio.CancelledError:
        REQUEST_COUNT.labels(model=fields.get("active_model") or requested_model, status="cancelled").inc()
        finish(
            "warning",
            outcome="cancelled",
            model=fields.get("active_model") or requested_model,
            cancel_reason="client_disconnected" if lifetime and lifetime.disconnected.is_set() else "caller_cancelled",
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
    if SUPPORTED_MODELS[model].endpoint_type == "images" and not _supports_aspect_ratio(model, aspect_ratio):
        raise GenerationError(
            ErrorCode.INVALID_PARAMS,
            "Seedream aspect_ratio must be between 1:16 and 16:1.",
            fields=[field],
        )


def _supports_aspect_ratio(model: str, aspect_ratio: str) -> bool:
    info = SUPPORTED_MODELS[model]
    if info.aspect_ratios is not None:
        return aspect_ratio in info.aspect_ratios
    if info.endpoint_type == "images":
        ratio = _parse_ratio(aspect_ratio)
        return ratio is not None and 1 / ARK_MAX_RATIO <= ratio[0] / ratio[1] <= ARK_MAX_RATIO
    return True


def _validate(request: GenerateRequest, model: str) -> None:
    """Reject bad model / ratio / fallback before touching the cache or upstream."""
    if model not in SUPPORTED_MODELS:
        raise GenerationError(
            ErrorCode.MODEL_NOT_FOUND,
            f"Model '{model}' not found. Use GET /v1/models for available models.",
            fields=["model"],
        )
    _check_aspect_ratio(model, request.aspect_ratio, "aspect_ratio")


NO_FALLBACK = "none"


def _resolve_fallback(request: GenerateRequest, model: str, settings: Settings) -> tuple[list[str], str]:
    """Effective fallback chain and where it came from ("request", "default" or "none").

    An explicit fallback_model is validated strictly (invalid_params on mistakes)
    and is the whole chain. Each configured default applies only where it can
    work: it is skipped, not rejected, when it equals the chosen model or can't
    do the requested ratio.
    """
    fallback = request.fallback_model
    if fallback == NO_FALLBACK:
        return [], "none"
    if fallback is None:
        chain = [
            m
            for m in settings.default_fallback_models
            if m != model and _supports_aspect_ratio(m, request.aspect_ratio)
        ]
        return chain, "default" if chain else "none"
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
    return [fallback], "request"


def _grounding_applies(request: GenerateRequest, model: str) -> bool:
    """grounding is a request; models without search support silently ignore it."""
    return request.grounding and SUPPORTED_MODELS[model].grounding


def _model_timeout(model: str, settings: Settings) -> float:
    return (
        settings.seedream_pro_timeout_seconds if model == "doubao-seedream-5-0-pro" else settings.model_timeout_seconds
    )


def _fallback_reserve(fallbacks: list[str], settings: Settings) -> float:
    """Leave the first backup its own full model budget, so a slow primary never starves it."""
    if not fallbacks:
        return 0
    return max(settings.fallback_reserve_seconds, _model_timeout(fallbacks[0], settings))


async def _call_model(
    request: GenerateRequest,
    model: str,
    settings: Settings,
    http_client: httpx.AsyncClient | None,
    references: list[ReferenceImage],
    *,
    reserve_seconds: float = 0,
) -> tuple[bytes, str]:
    """One upstream generation with ``model``; counts per-model duration and errors."""
    budget = current_budget()
    if budget is not None:
        await budget.checkpoint()
    timeout = _model_timeout(model, settings)
    if budget is not None:
        remaining = budget.remaining()
        reserved = remaining - reserve_seconds
        # Keep the reserve only if this model can still start; otherwise it gets what is left.
        timeout = min(timeout, max(0, reserved if reserved >= settings.min_model_budget_seconds else remaining))
    _log_fields.get().update(active_model=model, model_budget_seconds=timeout, stage="generation")
    state = ModelAttempt(time.monotonic() + timeout)
    token = _model_attempt.set(state)
    try:
        if timeout < settings.min_model_budget_seconds:
            raise GenerationError(
                ErrorCode.UPSTREAM_TIMEOUT,
                "Not enough time remains to start this model.",
                timeout_fallback_allowed=True,
            )
        try:
            with GENERATION_DURATION.labels(model=model).time(), anyio.fail_after(timeout):
                return await _invoke_model(request, model, settings, http_client, references)
        except TimeoutError:
            raise GenerationError(
                ErrorCode.UPSTREAM_TIMEOUT,
                upstream_detail=f"model deadline exceeded ({timeout:.3f}s)",
                maybe_billed=state.submitted,
                timeout_fallback_allowed=not state.response_received,
            ) from None
    except GenerationError as e:
        e.model = e.model or model
        if e.code == ErrorCode.UPSTREAM_TIMEOUT:
            logger.warning(
                "model_timed_out",
                model=model,
                maybe_billed=e.maybe_billed,
                timeout_fallback_allowed=e.timeout_fallback_allowed,
                remaining_seconds=budget.remaining() if budget is not None else None,
            )
        GENERATION_ERRORS.labels(model=model, error_type=e.code.value).inc()
        raise
    finally:
        _model_attempt.reset(token)


async def _invoke_model(
    request: GenerateRequest,
    model: str,
    settings: Settings,
    http_client: httpx.AsyncClient | None,
    references: list[ReferenceImage],
) -> tuple[bytes, str]:
    kwargs = dict(
        prompt=request.prompt,
        model=model,
        settings=settings,
        style=request.style,
        http_client=http_client,
        aspect_ratio=request.aspect_ratio,
        reference_images=references,
    )
    endpoint = SUPPORTED_MODELS[model].endpoint_type
    if endpoint == "dashscope":
        return await call_qwen_images(**kwargs, size=request.size)
    if endpoint == "images":
        return await call_seedream_images(**kwargs, size=request.size)
    return await call_gemini(**kwargs, search_types=SUPPORTED_MODELS[model].search_types if request.grounding else ())


async def _load_references(
    request: GenerateRequest, settings: Settings, http_client: httpx.AsyncClient | None
) -> list[ReferenceImage]:
    """Download reference images once (after the cache check, so hits stay fast)."""
    if not request.reference_images:
        return []
    started = time.perf_counter()
    try:
        if http_client is None:
            async with httpx.AsyncClient() as client:
                references = await load_reference_images(request.reference_images, settings, client)
        else:
            references = await load_reference_images(request.reference_images, settings, http_client)
    except ReferenceImageError as e:
        raise GenerationError(ErrorCode.INVALID_PARAMS, e.message, fields=["reference_images"]) from None
    _log_fields.get().update(
        reference_count=len(references),
        reference_bytes=sum(len(r.data) for r in references),
        reference_ms=int((time.perf_counter() - started) * 1000),
    )
    return references


async def _find_references(
    request: GenerateRequest,
    caller_count: int,
    settings: Settings,
    cache: CacheService,
    http_client: httpx.AsyncClient | None,
) -> SearchOutcome | None:
    """Search reference images for ``reference_subject`` into the slots the caller left free."""
    if not request.reference_subject:
        return None
    started = time.perf_counter()
    slots = MAX_REFERENCE_IMAGES - caller_count
    if http_client is None:
        async with httpx.AsyncClient() as client:
            outcome = await find_references(
                request.reference_subject, slots, settings, cache.redis, client, request.no_cache
            )
    else:
        outcome = await find_references(
            request.reference_subject, slots, settings, cache.redis, http_client, request.no_cache
        )
    _log_fields.get().update(
        reference_search_status=outcome.status,
        reference_search_cached=outcome.cached,
        reference_candidates=outcome.candidates,
        reference_accepted=outcome.accepted,
        reference_found_count=len(outcome.images),
        reference_search_total_ms=int((time.perf_counter() - started) * 1000),
        **outcome.log,
    )
    return outcome


# Failures that move on to the next model: a moderation block, or the provider
# being down (402 balance exhausted / 429 / 5xx / unreachable). Generation
# timeouts can use a backup inside the remaining budget, but are never resent
# to the same model. A submitted timed-out request may still be billed upstream.
FALLBACK_CODES = frozenset({ErrorCode.CONTENT_FILTERED, ErrorCode.UPSTREAM_UNAVAILABLE})


def _falls_back(e: GenerationError) -> bool:
    if e.code == ErrorCode.UPSTREAM_TIMEOUT:
        return e.timeout_fallback_allowed
    return e.code in FALLBACK_CODES and not e.maybe_billed


async def _generate_with_fallback(
    request: GenerateRequest,
    model: str,
    fallbacks: list[str],
    settings: Settings,
    http_client: httpx.AsyncClient | None,
    references: list[ReferenceImage],
) -> tuple[bytes, str, str]:
    """Generate with ``model``, then each of ``fallbacks`` in order while failures qualify.

    Returns (image bytes, format, model that produced the image). If every model
    fails, the last error is raised with ``fallback_from`` naming the primary
    model's failure.
    """
    try:
        image_bytes, fmt = await _call_model(
            request,
            model,
            settings,
            http_client,
            references,
            reserve_seconds=_fallback_reserve(fallbacks, settings),
        )
        return image_bytes, fmt, model
    except GenerationError as e:
        if not fallbacks or not _falls_back(e):
            raise
        first_failure = {"model": model, "code": e.code.value}

    tried: list[dict[str, str]] = [first_failure]
    _log_fields.get()["fallback_tried"] = tried
    previous = model
    for i, fallback in enumerate(fallbacks):
        budget = current_budget()
        if budget is not None:
            await budget.checkpoint()
            if budget.remaining() < settings.min_model_budget_seconds:
                raise GenerationError(
                    ErrorCode.UPSTREAM_TIMEOUT,
                    "Not enough time remains to start a backup model.",
                    model=previous,
                    fallback_from=first_failure,
                )
        logger.info("fallback_started", from_model=previous, to_model=fallback, reason=tried[-1]["code"])
        try:
            image_bytes, fmt = await _call_model(request, fallback, settings, http_client, references)
        except GenerationError as e:
            FALLBACKS.labels(from_model=model, to_model=fallback, outcome="error").inc()
            tried.append({"model": fallback, "code": e.code.value})
            if i == len(fallbacks) - 1 or not _falls_back(e):
                e.fallback_from = first_failure
                raise
            previous = fallback
            continue
        FALLBACKS.labels(from_model=model, to_model=fallback, outcome="success").inc()
        return image_bytes, fmt, fallback
    raise AssertionError("unreachable: the loop returns or raises")


async def _generate(
    request: GenerateRequest,
    model: str,
    settings: Settings,
    cache: CacheService,
    http_client: httpx.AsyncClient | None,
) -> GenerateResponse:
    """Orchestrate generation: validate → cache check → generate (with fallback) → save → cache set."""
    _validate(request, model)
    _log_fields.get()["stage"] = "cache"
    fallbacks, fallback_source = _resolve_fallback(request, model, settings)
    # One model logs and keys exactly as before the chain existed.
    fallback_key = ",".join(fallbacks) or None
    _log_fields.get().update(fallback_model=fallback_key, fallback_source=fallback_source)

    cache_key = compute_cache_key(
        request.prompt,
        model,
        request.aspect_ratio,
        request.size,
        request.style,
        fallback_key,
        request.grounding,
        request.reference_images,
        request.reference_subject,
        namespace=(
            _caller_identity.get().app_id
            if _caller_identity.get() and _caller_identity.get().auth_method != "legacy"
            else None
        ),
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
        lock_token = await cache.acquire_lock(cache_key, ttl=math.ceil(settings.generation_timeout_seconds) + 5)
        if lock_token is None:
            cached = await cache.wait_for_cached(cache_key)
            if cached is not None:
                return _cached_response(cached, model)
            raise GenerationError(ErrorCode.GENERATION_IN_PROGRESS)

    try:
        _log_fields.get()["stage"] = "references"
        references = await _load_references(request, settings, http_client)
        outcome = await _find_references(request, len(references), settings, cache, http_client)
        reference_urls = list(request.reference_images or [])
        reference_search = None
        upstream_request = request
        if outcome is not None:
            if outcome.images:
                # Without being told what the found image is, the model ignored it and drew
                # the product from memory (0/4 vs 2/4 in testing). The sentence says what the
                # image is and how to use it; it never describes the look. Not part of the
                # cache key, and caller-supplied reference_images get no such sentence.
                hint = REFERENCE_HINT.format(subject=request.reference_subject)
                upstream_request = request.model_copy(update={"prompt": f"{request.prompt.rstrip()}\n{hint}"})
                _log_fields.get()["reference_hint_added"] = True
            references += outcome.images
            reference_urls += outcome.urls
            reference_search = ReferenceSearchInfo(
                status=outcome.status,
                cached=outcome.cached,
                candidates=outcome.candidates,
                accepted=outcome.accepted,
            )
        image_bytes, fmt, actual_model = await _generate_with_fallback(
            upstream_request, model, fallbacks, settings, http_client, references
        )

        budget = current_budget()
        if budget is not None:
            await budget.checkpoint()
        _log_fields.get()["stage"] = "persist"

        grounding_used = _grounding_applies(request, actual_model)
        gen_id = str(uuid.uuid4())
        filename = save_image(image_bytes, gen_id, fmt, settings.storage_dir)
        caller = _caller_identity.get()
        _log_fields.get().update(generation_id=gen_id, artifact_app_id=caller.app_id, artifact_subject=caller.subject)
        dimensions = image_size(image_bytes)
        if dimensions:
            _log_fields.get()["output_size"] = f"{dimensions[0]}x{dimensions[1]}"

        image_url = f"{settings.static_url_prefix}/{filename}"
        created_at = datetime.now(UTC).isoformat()

        metadata = {
            "id": gen_id,
            "image_url": image_url,
            "model": actual_model,
            "grounding_used": grounding_used,
            "created_at": created_at,
            "reference_images_used": reference_urls,
            "reference_search": reference_search.model_dump() if reference_search else None,
            "app_id": caller.app_id,
            "subject": caller.subject,
        }
        await cache.set_cached(cache_key, metadata, ttl=GENERATION_TTL.get(outcome.status) if outcome else None)

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
            reference_images_used=reference_urls,
            reference_search=reference_search,
        )
    except GenerationError as e:
        REQUEST_COUNT.labels(model=e.model or model, status="error").inc()
        raise
    finally:
        if lock_token is not None:
            budget = current_budget()
            cleanup = budget.cleanup_timeout if budget is not None else CLEANUP_TIMEOUT_SECONDS
            try:
                with anyio.move_on_after(cleanup, shield=True) as cleanup_scope:
                    await cache.release_lock(cache_key, lock_token)
                if cleanup_scope.cancelled_caught:
                    logger.warning("generation_lock_release_timed_out", cache_key=cache_key)
            except Exception:
                logger.warning("generation_lock_release_failed", cache_key=cache_key)
