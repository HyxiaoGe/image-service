from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# Enough for a few views of one subject (front / back / side); every image is
# downloaded per request and sent upstream inline, so more costs latency and size.
MAX_REFERENCE_IMAGES = 3


class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=10000, description="Text prompt")
    model: str | None = Field(None, description="Image model ID; see GET /v1/models. Uses default if omitted.")
    aspect_ratio: str = Field("1:1", description="Aspect ratio e.g. 16:9")
    size: str = Field("1024x1024", description="Dimensions as WxH")
    style: str | None = Field(None, description="Style hint e.g. watercolor")
    fallback_model: str | None = Field(
        None,
        description="Model to retry with if the primary model's content filter blocks the request or its "
        "provider is unavailable. Omitted: the service's DEFAULT_FALLBACK_MODEL chain, tried in order. "
        "'none': no fallback. Timeouts and other failures never fall back.",
    )
    grounding: bool = Field(
        False,
        description="Let the model use Google Search (web, plus image search where the model supports it; "
        "see search_types in /v1/models) to draw recent products, "
        "places and events accurately. Ignored by models without search support; "
        "see grounding_used in the response.",
    )
    reference_images: list[str] | None = Field(
        None,
        max_length=MAX_REFERENCE_IMAGES,
        description=f"Up to {MAX_REFERENCE_IMAGES} public http(s) image URLs (PNG/JPEG/WebP, <= 4 MB each) "
        "the model should follow, e.g. the real product's look or an image to edit.",
    )
    reference_subject: str | None = Field(
        None,
        max_length=200,
        description="The specific real-world object to draw accurately, e.g. a product model ('iPhone 18 Pro'). "
        "When set, the service searches for real photos of it and passes the best match as a reference image "
        "(after any reference_images, within the 3-image limit). Never inferred from the prompt.",
    )
    no_cache: bool = Field(
        False,
        description="Skip the cache and generate a new image (e.g. regenerate). The result still replaces the cache entry.",
    )

    @field_validator("reference_images")
    @classmethod
    def validate_reference_images(cls, v: list[str] | None) -> list[str] | None:
        if not v:
            return None
        for url in v:
            if not re.fullmatch(r"https?://\S+", url):
                raise ValueError("reference_images must be http(s) URLs")
        return v

    @field_validator("reference_subject")
    @classmethod
    def strip_reference_subject(cls, v: str | None) -> str | None:
        v = " ".join(v.split()) if v else None
        return v or None

    @field_validator("prompt")
    @classmethod
    def strip_prompt(cls, v: str) -> str:
        return v.strip()

    @field_validator("aspect_ratio")
    @classmethod
    def validate_aspect_ratio(cls, v: str) -> str:
        if not re.fullmatch(r"\d{1,5}:\d{1,5}", v):
            raise ValueError("aspect_ratio must match pattern N:N (e.g. 16:9)")
        return v

    @field_validator("size")
    @classmethod
    def validate_size(cls, v: str) -> str:
        m = re.fullmatch(r"(\d{1,5})x(\d{1,5})", v)
        if not m:
            raise ValueError("size must match pattern WxH (e.g. 1024x1024)")
        w, h = int(m.group(1)), int(m.group(2))
        if w > 2048 or h > 2048:
            raise ValueError("maximum dimension is 2048 per side")
        if w < 1 or h < 1:
            raise ValueError("dimensions must be positive")
        return v
