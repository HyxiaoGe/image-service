from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    """Caller-facing error codes. Generic on purpose: no caller-specific semantics."""

    INVALID_PARAMS = "invalid_params"
    MODEL_NOT_FOUND = "model_not_found"
    CONTENT_FILTERED = "content_filtered"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_ERROR = "upstream_error"
    NO_IMAGE_RETURNED = "no_image_returned"
    CONFIG_MISSING = "config_missing"
    GENERATION_IN_PROGRESS = "generation_in_progress"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class ErrorSpec:
    http_status: int
    retryable: bool  # whether an identical retry may succeed
    message: str  # fixed caller-facing text; upstream wording only goes to logs


ERROR_SPECS: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.INVALID_PARAMS: ErrorSpec(422, False, "Request parameters are invalid."),
    ErrorCode.MODEL_NOT_FOUND: ErrorSpec(400, False, "Model not found. Use GET /v1/models for available models."),
    ErrorCode.CONTENT_FILTERED: ErrorSpec(
        422,
        False,
        "The request was blocked by the upstream content safety filter. Rephrasing to evade the filter will not help.",
    ),
    ErrorCode.UPSTREAM_TIMEOUT: ErrorSpec(504, True, "The upstream model timed out. Please retry."),
    ErrorCode.UPSTREAM_UNAVAILABLE: ErrorSpec(
        502, True, "The upstream model is temporarily unavailable. Please retry."
    ),
    ErrorCode.UPSTREAM_ERROR: ErrorSpec(502, False, "The upstream model rejected the request."),
    ErrorCode.NO_IMAGE_RETURNED: ErrorSpec(502, True, "The upstream model returned no image."),
    ErrorCode.CONFIG_MISSING: ErrorSpec(503, False, "The service is missing configuration for this model."),
    ErrorCode.GENERATION_IN_PROGRESS: ErrorSpec(
        503, True, "An identical request is still being generated. Please retry shortly."
    ),
    ErrorCode.INTERNAL_ERROR: ErrorSpec(500, False, "Internal error while generating the image."),
}


def validation_error_fields(errors: list[dict]) -> list[str]:
    """Field names from pydantic/FastAPI validation errors, in order, de-duplicated.

    FastAPI prefixes body locations with ``"body"``; plain pydantic errors don't.
    """
    fields: list[str] = []
    for err in errors:
        loc = [part for part in err.get("loc", ()) if part != "body"]
        name = str(loc[0]) if loc else "body"
        if name not in fields:
            fields.append(name)
    return fields
