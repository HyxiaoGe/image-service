from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EndpointType = Literal["chat", "images", "dashscope"]
SearchType = Literal["web", "image"]


# Aspect ratios Gemini image models accept via imageConfig.aspectRatio, verified
# against the live API on 2026-10-10 (unsupported ratios return HTTP 400).
GEMINI_ASPECT_RATIOS: tuple[str, ...] = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
# 3.1 Flash additionally accepts the extreme ratios, and so do the GA models
# (nano-banana-2.1, 3.1-flash-image, 3-pro-image; verified 2026-10-11).
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
        ark_model_id: str | None = None,
        sequential_param: bool = False,
        ark_min_pixels: int = 3_686_400,
        ark_max_pixels: int = 16_777_216,
    ) -> None:
        self.id = id
        self.name = name
        self.provider = provider
        self.endpoint_type = endpoint_type
        # None = continuous ratios, subject to the provider's limits.
        self.aspect_ratios = aspect_ratios
        # Google Search types the model accepts for grounding (verified via LiteLLM
        # on 2026-10-11). gemini-2.5-flash-image rejects search entirely ("Search as
        # tool is not enabled"); 3 Pro / Nano Banana Pro reject image search
        # ("Image search as tool is not enabled").
        self.search_types = search_types
        # Seedream only: pinned Ark wire id; None runs Settings.ark_model_id.
        self.ark_model_id = ark_model_id
        # Pro / Flash reject sequential_image_generation; 4.x and Lite support it.
        self.sequential_param = sequential_param
        self.ark_min_pixels = ark_min_pixels
        self.ark_max_pixels = ark_max_pixels

    @property
    def grounding(self) -> bool:
        return bool(self.search_types)


SUPPORTED_MODELS: dict[str, ModelInfo] = {
    "gemini-nano-banana-2.1": ModelInfo(
        id="gemini-nano-banana-2.1",
        name="Nano Banana 2.1",
        provider="Google",
        aspect_ratios=GEMINI_FLASH_31_ASPECT_RATIOS,
        search_types=("web", "image"),
    ),
    "gemini-3.1-flash-image": ModelInfo(
        id="gemini-3.1-flash-image",
        name="Gemini 3.1 Flash Image (Nano Banana 2)",
        provider="Google",
        aspect_ratios=GEMINI_FLASH_31_ASPECT_RATIOS,
        search_types=("web", "image"),
    ),
    "gemini-3-pro-image": ModelInfo(
        id="gemini-3-pro-image",
        name="Gemini 3 Pro Image (Nano Banana Pro)",
        provider="Google",
        aspect_ratios=GEMINI_FLASH_31_ASPECT_RATIOS,
        search_types=("web",),
    ),
    # Google preview ids: past their earliest shutdown date (2026-06-25) and may
    # stop working at any time; kept for callers that still pass them.
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
    "doubao-seedream-5-0-pro": ModelInfo(
        id="doubao-seedream-5-0-pro",
        name="Doubao Seedream 5.0 Pro",
        provider="Volcano",
        endpoint_type="images",
        ark_model_id="doubao-seedream-5-0-pro-260628",
        ark_min_pixels=921_600,
        ark_max_pixels=4_624_220,
    ),
    "doubao-seedream-5-0-flash": ModelInfo(
        id="doubao-seedream-5-0-flash",
        name="Doubao Seedream 5.0 Flash",
        provider="Volcano",
        endpoint_type="images",
        ark_model_id="doubao-seedream-5-0-flash-260915",
        ark_min_pixels=921_600,
        ark_max_pixels=4_624_220,
    ),
    "doubao-seedream-5-0-lite": ModelInfo(
        id="doubao-seedream-5-0-lite",
        name="Doubao Seedream 5.0 Lite",
        provider="Volcano",
        endpoint_type="images",
        ark_model_id="doubao-seedream-5-0-260128",
    ),
    # Alibaba Bailian (DashScope OpenAI-compatible images API). API-only models;
    # 1-3 reference images, any ratio from 1:8 to 8:1.
    "qwen-image-3.0-pro": ModelInfo(
        id="qwen-image-3.0-pro",
        name="Qwen-Image 3.0 Pro",
        provider="Alibaba",
        endpoint_type="dashscope",
    ),
    "qwen-image-3.0": ModelInfo(
        id="qwen-image-3.0",
        name="Qwen-Image 3.0",
        provider="Alibaba",
        endpoint_type="dashscope",
    ),
    # Both 4.x aliases run Settings.ark_model_id (4.5 by default).
    "doubao-seedream-4-5": ModelInfo(
        id="doubao-seedream-4-5",
        name="Doubao Seedream 4.5",
        provider="Volcano",
        endpoint_type="images",
        sequential_param=True,
    ),
    "doubao-seedream-4-0": ModelInfo(
        id="doubao-seedream-4-0",
        name="Doubao Seedream 4.0",
        provider="Volcano",
        endpoint_type="images",
        sequential_param=True,
    ),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    litellm_base_url: str = Field("http://localhost:4000", description="LiteLLM Proxy URL")
    litellm_api_key: str = Field("", description="LiteLLM Virtual Key")
    # Main / backup models when the caller omits model / fallback_model. Switch
    # them by editing these env vars and restarting (see README).
    default_model: str = "doubao-seedream-5-0-flash"
    # Comma-separated backup chain, tried in order when the caller passes no
    # fallback_model (see FALLBACK_CODES in generator for what triggers it).
    default_fallback_model: str | None = Field(
        "gemini-nano-banana-2.1,qwen-image-3.0",
        description="Comma-separated backup models, tried in order when the caller passes no fallback_model; "
        "empty disables",
    )
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
    # Version selector for the Seedream 4.x aliases: both run whichever wire id
    # this pins (5.0 models pin their own in SUPPORTED_MODELS). Bump this env to
    # change the deployed 4.x version without touching code.
    ark_model_id: str = Field(
        "doubao-seedream-4-5-251128",
        description="Ark Seedream wire model id (version-pinned)",
    )
    dashscope_api_key: str = Field("", description="Alibaba Bailian (DashScope) API key (Qwen-Image)")
    # Native DashScope API root. Alibaba recommends the workspace host
    # (https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1); the legacy host still works.
    dashscope_base_url: str = Field(
        "https://dashscope.aliyuncs.com/api/v1",
        description="DashScope native API base URL",
    )

    # Automatic reference search (reference_subject): Tavily key pool shared with
    # Fusion's reader-service (comma separated), the vision model that judges the
    # candidates, and how many found images go to the image model.
    tavily_api_keys: str = Field("", description="Comma-separated Tavily API keys; empty disables reference search")
    reference_judge_model: str = Field("gemini-3.8-flash", description="LiteLLM model judging reference candidates")
    reference_search_images: int = Field(1, ge=1, le=2, description="Found reference images passed to the model")

    @field_validator("default_fallback_model", mode="before")
    @classmethod
    def _normalize_fallback(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        models = [m.strip() for m in v.split(",") if m.strip()]
        return ",".join(models) or None

    @property
    def default_fallback_models(self) -> tuple[str, ...]:
        return tuple(self.default_fallback_model.split(",")) if self.default_fallback_model else ()

    @model_validator(mode="after")
    def _check_default_models(self) -> Settings:
        # Fail at startup (the deploy health check catches it) rather than on
        # every request after a typo in .env.
        if self.default_model not in SUPPORTED_MODELS:
            raise ValueError(f"DEFAULT_MODEL '{self.default_model}' is not a supported model")
        chain = self.default_fallback_models
        # Existing installations may pin Gemini as primary while inheriting the
        # new default chain. Only implicit defaults may drop the primary; an
        # explicitly configured self-fallback still fails startup as before.
        if self.default_model in chain and "default_fallback_model" not in self.model_fields_set:
            chain = tuple(m for m in chain if m != self.default_model)
            self.default_fallback_model = ",".join(chain) or None
        for fallback in chain:
            if fallback not in SUPPORTED_MODELS:
                raise ValueError(f"DEFAULT_FALLBACK_MODEL '{fallback}' is not a supported model")
            if fallback == self.default_model:
                raise ValueError("DEFAULT_FALLBACK_MODEL must differ from DEFAULT_MODEL")
        if len(set(chain)) != len(chain):
            raise ValueError("DEFAULT_FALLBACK_MODEL lists a model twice")
        return self
