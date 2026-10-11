from __future__ import annotations

from pydantic import BaseModel


class ReferenceSearchInfo(BaseModel):
    # found: at least one image passed; none_accepted: searched, nothing passed;
    # search_failed: search or judging failed (not cached); skipped: no free slot
    # left after the caller's reference_images.
    status: str
    cached: bool = False  # result came from the per-subject search cache
    candidates: int = 0  # images judged
    accepted: int = 0  # images that passed


class GenerateResponse(BaseModel):
    id: str
    image_url: str
    model: str  # model that actually produced the image
    requested_model: str  # model the caller asked for (or the default)
    fallback_used: bool = False
    grounding_used: bool = False  # search tool was enabled for the model that produced the image
    cached: bool
    created_at: str
    reference_images_used: list[str] = []  # reference images sent to the model (caller's first, then found ones)
    reference_search: ReferenceSearchInfo | None = None  # only when reference_subject was set


class ModelInfoResponse(BaseModel):
    id: str
    name: str
    provider: str
    aspect_ratios: list[str] | None = None
    grounding: bool = False  # supports the grounding (Google Search) option
    search_types: list[str] = []  # what grounding searches with this model: "web", "image"


class ModelsListResponse(BaseModel):
    models: list[ModelInfoResponse]


class HealthCheck(BaseModel):
    name: str
    status: str


class HealthResponse(BaseModel):
    status: str  # "healthy", "degraded", "unhealthy"
    version: str
    checks: dict[str, str]


class ErrorResponse(BaseModel):
    error: str  # an ErrorCode value for generation errors
    message: str
    retryable: bool | None = None
    model: str | None = None
    fields: list[str] | None = None  # invalid_params: offending request fields
    fallback_from: dict[str, str] | None = None  # fallback also failed: primary model and its error code
    retry_after: int | None = None
