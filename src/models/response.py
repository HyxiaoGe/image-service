from __future__ import annotations

from pydantic import BaseModel


class GenerateResponse(BaseModel):
    id: str
    image_url: str
    model: str  # model that actually produced the image
    requested_model: str  # model the caller asked for (or the default)
    fallback_used: bool = False
    cached: bool
    created_at: str


class ModelInfoResponse(BaseModel):
    id: str
    name: str
    provider: str
    aspect_ratios: list[str] | None = None  # None = any W:H ratio


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
    retry_after: int | None = None
