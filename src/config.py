from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

EndpointType = Literal["chat", "images"]
SearchType = Literal["web", "image"]


# Aspect ratios Gemini image models accept via imageConfig.aspectRatio, verified
# against the live API on 2026-10-10 (unsupported ratios return HTTP 400).
GEMINI_ASPECT_RATIOS: tuple[str, ...] = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
# 3.1 Flash additionally accepts the extreme ratios.
GEMINI_FLASH_31_ASPECT_RATIOS: tuple[str, ...] = (*GEMINI_ASPECT_RATIOS, "1:4", "4:1", "1:8", "8:1")


class ModelInfo:
    def __init__(
        self,
        id: str,
        name: str,
        provider: str,
        endpoint_type: EndpointType = "chat",
        aspect_ratios: tuple[str, ...] | None = None,
        search_types: tuple[SearchType, ...] = (),
    ) -> None:
        self.id = id
        self.name = name
        self.provider = provider
        self.endpoint_type = endpoint_type
        # None = any W:H ratio (Seedream maps it to explicit pixel dimensions).
        self.aspect_ratios = aspect_ratios
        # Google Search types the model accepts for grounding (verified via LiteLLM
        # on 2026-10-11). gemini-2.5-flash-image rejects search entirely ("Search as
        # tool is not enabled"); 3 Pro / Nano Banana Pro reject image search
        # ("Image search as tool is not enabled").
        self.search_types = search_types

    @property
    def grounding(self) -> bool:
        return bool(self.search_types)


SUPPORTED_MODELS: dict[str, ModelInfo] = {
    "nano-banana-pro-preview": ModelInfo(
        id="nano-banana-pro-preview",
        name="Nano Banana Pro",
        provider="Google",
        aspect_ratios=GEMINI_ASPECT_RATIOS,
        search_types=("web",),
    ),
    "gemini-3-pro-image-preview": ModelInfo(
        id="gemini-3-pro-image-preview",
        name="Gemini 3 Pro Image",
        provider="Google",
        aspect_ratios=GEMINI_ASPECT_RATIOS,
        search_types=("web",),
    ),
    "gemini-3.1-flash-image-preview": ModelInfo(
        id="gemini-3.1-flash-image-preview",
        name="Gemini 3.1 Flash Image",
        provider="Google",
        aspect_ratios=GEMINI_FLASH_31_ASPECT_RATIOS,
        search_types=("web", "image"),
    ),
    "gemini-2.5-flash-image": ModelInfo(
        id="gemini-2.5-flash-image",
        name="Gemini 2.5 Flash Image",
        provider="Google",
        aspect_ratios=GEMINI_ASPECT_RATIOS,
    ),
    "doubao-seedream-4-5": ModelInfo(
        id="doubao-seedream-4-5",
        name="Doubao Seedream 4.5",
        provider="Volcano",
        endpoint_type="images",
    ),
    "doubao-seedream-4-0": ModelInfo(
        id="doubao-seedream-4-0",
        name="Doubao Seedream 4.0",
        provider="Volcano",
        endpoint_type="images",
    ),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    litellm_base_url: str = Field("http://localhost:4000", description="LiteLLM Proxy URL")
    litellm_api_key: str = Field("", description="LiteLLM Virtual Key")
    default_model: str = "gemini-3.1-flash-image-preview"
    redis_url: str = "redis://redis:6379/0"
    storage_dir: str = "storage/images"
    static_url_prefix: str = "/static/images"
    host: str = "0.0.0.0"
    port: int = 8090
    service_base_url: str = "http://localhost:8090"
    api_key: str | None = None
    rate_limit_global: int = 20
    rate_limit_per_ip: int = 5
    rate_limit_window: int = 60
    cache_ttl_seconds: int = 604800  # 7 days
    ark_api_key: str = Field("", description="Volcengine Ark API key (Seedream)")
    ark_base_url: str = Field(
        "https://ark.cn-beijing.volces.com/api/v3",
        description="Ark OpenAI-compatible base URL",
    )
    # Version selector for the Seedream path: every Volcano alias in
    # SUPPORTED_MODELS runs whichever wire id this pins. Bump this env to change
    # the deployed Seedream version without touching code.
    ark_model_id: str = Field(
        "doubao-seedream-4-5-251128",
        description="Ark Seedream wire model id (version-pinned)",
    )
