from __future__ import annotations

import structlog
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from pydantic import ValidationError

from src.config import SUPPORTED_MODELS, Settings
from src.models.errors import ERROR_SPECS, ErrorCode, validation_error_fields
from src.models.request import GenerateRequest
from src.services.cache import CacheService
from src.services.generator import GenerationError, handle_generate

logger = structlog.get_logger()

mcp = FastMCP("image-service")

# These will be set during app startup
_settings: Settings | None = None
_cache: CacheService | None = None
_http_client = None


def init_mcp(settings: Settings, cache: CacheService, http_client=None) -> None:
    global _settings, _cache, _http_client
    _settings = settings
    _cache = cache
    _http_client = http_client


def _error_result(
    code: ErrorCode,
    message: str | None = None,
    *,
    model: str | None = None,
    fields: list[str] | None = None,
) -> ToolResult:
    """Failure result: text keeps the ``Error`` prefix callers already match on,
    structured content carries the code. ``isError`` stays false so existing MCP
    clients read the payload instead of treating it as a transport failure."""
    spec = ERROR_SPECS[code]
    message = message or spec.message
    error: dict[str, object] = {"code": code.value, "message": message, "retryable": spec.retryable}
    if model:
        error["model"] = model
    if fields:
        error["fields"] = fields
    return ToolResult(
        content=f"Error generating image [{code.value}]: {message}",
        structured_content={"ok": False, "error": error},
    )


@mcp.tool()
async def generate_image(
    prompt: str,
    model: str | None = None,
    aspect_ratio: str = "1:1",
    size: str = "1024x1024",
    style: str | None = None,
    no_cache: bool = False,
) -> ToolResult:
    """Generate an image from a text prompt via the image-service backend.

    Args:
        prompt: Text description of the image to generate.
        model: Image model ID (optional, uses default if omitted). See list_models.
        aspect_ratio: Aspect ratio e.g. '16:9', '1:1'. Default '1:1'.
        size: Output dimensions as 'WxH'. Default '1024x1024'.
        style: Style hint e.g. 'watercolor', 'photorealistic' (optional).
        no_cache: Set true to regenerate: skip the cached image for identical
            parameters and produce a new one (it then replaces the cache entry).

    On failure the text starts with "Error generating image [<code>]:" and the
    structured content is {"ok": false, "error": {"code", "message", "retryable", ...}}.
    """
    if _settings is None or _cache is None:
        return _error_result(ErrorCode.CONFIG_MISSING, "MCP server not initialized")

    try:
        request = GenerateRequest(
            prompt=prompt,
            model=model,
            aspect_ratio=aspect_ratio,
            size=size,
            style=style,
            no_cache=no_cache,
        )
    except ValidationError as e:
        errors = e.errors()
        first = errors[0].get("msg") if errors else None
        return _error_result(ErrorCode.INVALID_PARAMS, first, fields=validation_error_fields(errors))

    try:
        result = await handle_generate(request, _settings, _cache, http_client=_http_client, entry="mcp")
    except GenerationError as e:
        return _error_result(e.code, e.message, model=e.model, fields=e.fields)
    except Exception:
        # handle_generate already logged the traceback; never leak it to the caller.
        return _error_result(ErrorCode.INTERNAL_ERROR)

    full_url = f"{_settings.service_base_url}{result.image_url}"
    # The first four lines are parsed by existing callers; only append new lines.
    lines = [
        f"Generated image: {full_url}",
        f"Model: {result.model}",
        f"Cached: {result.cached}",
        f"ID: {result.id}",
        f"Requested model: {result.requested_model}",
        f"Fallback used: {result.fallback_used}",
    ]
    return ToolResult(
        content="\n".join(lines),
        structured_content={
            "ok": True,
            "image_url": full_url,
            "model": result.model,
            "requested_model": result.requested_model,
            "fallback_used": result.fallback_used,
            "cached": result.cached,
            "id": result.id,
        },
    )


@mcp.tool()
async def list_models() -> str:
    """List available image generation models."""
    lines = ["Available models:", ""]
    for i, model_info in enumerate(SUPPORTED_MODELS.values(), 1):
        ratios = ", ".join(model_info.aspect_ratios) if model_info.aspect_ratios else "any W:H"
        lines.append(f"{i}. {model_info.name} ({model_info.id}) — {model_info.provider} — aspect ratios: {ratios}")
    return "\n".join(lines)
