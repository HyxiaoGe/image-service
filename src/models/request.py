from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator


class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=10000, description="Text prompt")
    model: str | None = Field(None, description="Image model ID; see GET /v1/models. Uses default if omitted.")
    aspect_ratio: str = Field("1:1", description="Aspect ratio e.g. 16:9")
    size: str = Field("1024x1024", description="Dimensions as WxH")
    style: str | None = Field(None, description="Style hint e.g. watercolor")
    fallback_model: str | None = Field(
        None,
        description="Model to retry with once if the primary model's content filter blocks the request. "
        "Off by default; other failures never fall back.",
    )
    no_cache: bool = Field(
        False,
        description="Skip the cache and generate a new image (e.g. regenerate). The result still replaces the cache entry.",
    )

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
