from __future__ import annotations

from fastmcp import FastMCP

from src.config import SUPPORTED_MODELS, Settings
from src.models.request import GenerateRequest
from src.services.cache import CacheService
from src.services.generator import GenerationError, handle_generate

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


@mcp.tool()
async def generate_image(
    prompt: str,
    model: str | None = None,
    aspect_ratio: str = "1:1",
    size: str = "1024x1024",
    style: str | None = None,
) -> str:
    """Generate an image from a text prompt via the image-service backend.

    Args:
        prompt: Text description of the image to generate.
        model: Image model ID (optional, uses default if omitted). See list_models.
        aspect_ratio: Aspect ratio e.g. '16:9', '1:1'. Default '1:1'.
        size: Output dimensions as 'WxH'. Default '1024x1024'.
        style: Style hint e.g. 'watercolor', 'photorealistic' (optional).
    """
    if _settings is None or _cache is None:
        return "Error: MCP server not initialized"

    try:
        request = GenerateRequest(
            prompt=prompt,
            model=model,
            aspect_ratio=aspect_ratio,
            size=size,
            style=style,
        )
        result = await handle_generate(request, _settings, _cache, http_client=_http_client)

        full_url = f"{_settings.service_base_url}{result.image_url}"

        lines = [
            f"Generated image: {full_url}",
            f"Model: {result.model}",
            f"Cached: {result.cached}",
            f"ID: {result.id}",
        ]
        return "\n".join(lines)
    except GenerationError as e:
        return f"Error generating image: {e.message}"


@mcp.tool()
async def list_models() -> str:
    """List available image generation models."""
    lines = ["Available models:", ""]
    for i, model_info in enumerate(SUPPORTED_MODELS.values(), 1):
        lines.append(f"{i}. {model_info.name} ({model_info.id}) — {model_info.provider}")
    return "\n".join(lines)
