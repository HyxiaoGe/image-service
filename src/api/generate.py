from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.middleware.auth import check_api_key
from src.middleware.rate_limit import check_rate_limit
from src.models.errors import ERROR_SPECS, ErrorCode, validation_error_fields
from src.models.request import GenerateRequest
from src.models.response import ErrorResponse, GenerateResponse
from src.services.generator import GenerationError, handle_generate

logger = structlog.get_logger()

router = APIRouter()


def error_response(
    code: ErrorCode,
    message: str | None = None,
    *,
    model: str | None = None,
    fields: list[str] | None = None,
) -> JSONResponse:
    spec = ERROR_SPECS[code]
    return JSONResponse(
        status_code=spec.http_status,
        content=ErrorResponse(
            error=code.value,
            message=message or spec.message,
            retryable=spec.retryable,
            model=model,
            fields=fields,
        ).model_dump(exclude_none=True),
    )


async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """Replace FastAPI's default 422 ``detail`` body with the invalid_params error shape."""
    errors = list(exc.errors())
    first = errors[0].get("msg") if errors else None
    return error_response(ErrorCode.INVALID_PARAMS, first, fields=validation_error_fields(errors))


@router.post(
    "/generate",
    response_model=GenerateResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
        504: {"model": ErrorResponse},
    },
)
async def generate(body: GenerateRequest, request: Request) -> GenerateResponse | JSONResponse:
    # API key check
    auth_response = check_api_key(request)
    if auth_response is not None:
        return auth_response

    # Rate limiting
    rate_limit_response = await check_rate_limit(request)
    if rate_limit_response is not None:
        return rate_limit_response

    settings = request.app.state.settings
    cache = request.app.state.cache

    try:
        http_client = getattr(request.app.state, "http_client", None)
        return await handle_generate(body, settings, cache, http_client=http_client, entry="rest")
    except GenerationError as e:
        return error_response(e.code, e.message, model=e.model, fields=e.fields)
    except Exception:
        # handle_generate already logged the traceback; never leak it to the caller.
        return error_response(ErrorCode.INTERNAL_ERROR)
