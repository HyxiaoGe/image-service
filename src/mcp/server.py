from __future__ import annotations

import structlog
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from fastmcp.tools import ToolResult
from pydantic import ValidationError

from src import __version__
from src.config import SUPPORTED_MODELS, Settings
from src.models.errors import ERROR_SPECS, ErrorCode, validation_error_fields
from src.models.request import GenerateRequest, ImageQuality
from src.services.cache import CacheService
from src.services.generator import GenerationError, handle_generate

logger = structlog.get_logger()

mcp = FastMCP(
    "image-service",
    version=__version__,
    instructions="Use generate_image to create images or edit existing images with reference_images and edit "
    "instructions. For another edit, pass the previous image_url as a reference. quality=standard selects "
    "Seedream Flash; quality=high selects Pro; an explicit model takes priority. See the tool description "
    "for Seedream coordinate editing and supported input limits.",
)

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
    fallback_from: dict[str, str] | None = None,
    retry_after: int | None = None,
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
    if fallback_from:
        error["fallback_from"] = fallback_from
    if retry_after is not None:
        error["retry_after"] = retry_after
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
    fallback_model: str | None = None,
    grounding: bool = False,
    reference_images: list[str] | None = None,
    reference_subject: str | None = None,
    no_cache: bool = False,
    quality: ImageQuality | None = None,
) -> ToolResult:
    """Generate a new image or edit an existing image using reference_images.

    To edit, pass the original image URL in reference_images and describe the
    requested changes and what to preserve in prompt. For another editing round,
    pass the previous result's image_url as the new reference; each call creates
    a new image URL and does not overwrite the original. Preserve the input's
    shape by explicitly setting aspect_ratio; it is not inferred from the image.

    Seedream Flash and Pro also accept coordinate edits in prompt:
    'In image 1 <bbox>400 420 610 620</bbox>, add a gold star; preserve the rest.'
    Use <point>x y</point> for a point or <bbox>x1 y1 x2 y2</bbox> for a box,
    with coordinates normalized to 0-999 relative to the whole reference image.
    Select Seedream with quality or model; set fallback_model='none' when relying
    on this provider-specific coordinate syntax. Edits are generative and do not
    guarantee pixel-identical content outside the region. Local file uploads,
    mask/strength controls, layer arrays and transparent-output controls are not
    exposed by this tool.

    Args:
        prompt: Description for a new image, or editing instructions explaining
            what to change and preserve in the supplied reference image(s).
        model: Image model ID (optional). Overrides quality when set. See list_models.
        aspect_ratio: Output aspect ratio e.g. '16:9', '1:1'. Default '1:1'.
            For edits, set the original image's ratio to preserve its shape.
        size: Pixel budget as 'WxH'; aspect_ratio controls shape. Seedream fits
            this to its model's pixel limits. Gemini ignores size. Default
            '1024x1024'; use '1536x1536' for about 1.5K or '2048x2048' for about 2K.
        style: Style hint e.g. 'watercolor', 'photorealistic' (optional).
        fallback_model: Model to retry with if `model`'s content filter blocks the
            request or its provider is unavailable. Omit to use the service's
            default backup models (tried in order); pass 'none' to disable.
            Generation timeouts may use the next backup within the remaining
            total budget; the timed-out model is not resent the request. An
            already-submitted timed-out request may still be billed upstream.
            The result's "Model" line shows which model produced the image.
        grounding: Set true to let the model look things up with Google Search
            first, so recent products, landmarks and events are drawn accurately.
            Models without search support ignore it ("Grounding used" shows
            whether it applied).
        reference_images: Up to 3 public http(s) image URLs (PNG/JPEG/WebP, max
            4 MB each) for the model to follow, e.g. the real product's look or an
            image to edit. Image URLs returned by this tool also work. For an
            edit, put the base image first and identify any additional references
            in prompt. To continue editing, pass the previous image_url here.
            Accepts URLs only, not local paths or Base64 strings.
        reference_subject: The specific real-world object whose real look must be
            followed, e.g. a product model like 'iPhone 18 Pro'. The service then
            searches real photos of it and passes the best match to the model.
            Set it only when the user names such an object; it is never guessed
            from the prompt. "Reference search" shows the outcome: found,
            none_accepted (nothing usable found, so the look may differ from the
            real product), search_failed, or skipped (no free reference slot).
        no_cache: Set true to regenerate: skip the cached image for identical
            parameters and produce a new one (it then replaces the cache entry).
        quality: Model preference: 'standard' selects Seedream 5.0 Flash for
            everyday images, drafts and routine edits; 'high' selects Seedream
            5.0 Pro when the user prioritizes polished final images and accepts
            higher cost and latency. Omit both model and quality to use the
            service default. Explicit model takes priority. Resolution is set
            separately with size. Text or reference images alone do not require
            high. Existing fallback rules apply; the result reports the actual
            model. Lite remains available through an explicit model ID.

    On failure the text starts with "Error generating image [<code>]:" and the
    structured content is {"ok": false, "error": {"code", "message", "retryable", ...}}.
    The service applies one configurable total deadline (default 135 seconds),
    including reference lookup, cache waits and all model attempts. Cancellation
    or HTTP disconnect stops local work and further attempts; it cannot guarantee
    that a provider stops or refunds an already-submitted generation.
    REST and MCP share per-product/user request and concurrency limits, including
    cache hits. On rate_limited, wait error.retry_after seconds before retrying;
    do not repeatedly switch entry points or retry without a bound.
    """
    if _settings is None or _cache is None:
        return _error_result(ErrorCode.CONFIG_MISSING, "MCP server not initialized")

    try:
        request = GenerateRequest(
            prompt=prompt,
            model=model,
            quality=quality,
            aspect_ratio=aspect_ratio,
            size=size,
            style=style,
            fallback_model=fallback_model,
            grounding=grounding,
            reference_images=reference_images,
            reference_subject=reference_subject,
            no_cache=no_cache,
        )
    except ValidationError as e:
        errors = e.errors()
        first = errors[0].get("msg") if errors else None
        return _error_result(ErrorCode.INVALID_PARAMS, first, fields=validation_error_fields(errors))

    try:
        try:
            state = get_http_request().state
            lifetime = getattr(state, "image_generation", None)
            caller = getattr(state, "image_caller", None)
        except RuntimeError:  # In-memory / stdio MCP has no HTTP request.
            lifetime = None
            caller = None
        result = await handle_generate(
            request,
            _settings,
            _cache,
            http_client=_http_client,
            entry="mcp",
            lifetime=lifetime,
            caller=caller,
        )
    except GenerationError as e:
        return _error_result(
            e.code, e.message, model=e.model, fields=e.fields, fallback_from=e.fallback_from, retry_after=e.retry_after
        )
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
        f"Grounding used: {result.grounding_used}",
    ]
    if result.reference_images_used:
        lines.append(f"Reference images used: {', '.join(result.reference_images_used)}")
    if result.reference_search:
        lines.append(f"Reference search: {result.reference_search.status}")
    return ToolResult(
        content="\n".join(lines),
        structured_content={
            "ok": True,
            "image_url": full_url,
            "model": result.model,
            "requested_model": result.requested_model,
            "fallback_used": result.fallback_used,
            "grounding_used": result.grounding_used,
            "cached": result.cached,
            "id": result.id,
            "reference_images_used": result.reference_images_used,
            "reference_search": result.reference_search.model_dump() if result.reference_search else None,
        },
    )


@mcp.tool()
async def list_models() -> str | ToolResult:
    """List available image generation models."""
    try:
        caller = getattr(get_http_request().state, "image_caller", None)
    except RuntimeError:
        caller = None
    if caller is None:
        return _error_result(ErrorCode.UNAUTHORIZED)
    lines = ["Available models:", ""]
    for i, model_info in enumerate(SUPPORTED_MODELS.values(), 1):
        ratios = ", ".join(model_info.aspect_ratios) if model_info.aspect_ratios else "continuous W:H"
        if model_info.endpoint_type == "images":
            ratios = "1:16 to 16:1"
        lines.append(
            f"{i}. {model_info.name} ({model_info.id}) — {model_info.provider} — aspect ratios: {ratios}"
            + (f" — grounding: {' + '.join(model_info.search_types)} search" if model_info.grounding else "")
        )
    return "\n".join(lines)
