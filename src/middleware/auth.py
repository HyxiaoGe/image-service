from __future__ import annotations

import os
import re
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from src.config import Settings
from src.models.identity import CallerIdentity
from src.models.response import ErrorResponse

_USER_PATTERN = re.compile(r"[a-zA-Z0-9_.:@-]{1,128}")
_CORRELATION_PATTERN = re.compile(r"[a-zA-Z0-9_.:-]{1,128}")
_SCENES = frozenset({"chat_image", "image_edit", "validation"})


def _same_bearer(header: str, key: str) -> bool:
    return secrets.compare_digest(header.encode("utf-8"), ("Bearer " + key).encode("utf-8"))


def match_product(settings: Settings, bearer: str) -> str | None:
    """按服务密钥认出调用产品；逐个常量时间比较，未配置密钥的产品不可被匹配。"""
    matched = None
    for app_id, product in settings.products.items():
        key = os.environ.get(product.service_key_env, "").strip()
        if key and _same_bearer(bearer, key):
            matched = app_id
    return matched


class CallerAuthMiddleware:
    """在 HTTP 边界认证 REST/MCP；产品身份只来自服务密钥，请求头里的用户只用于统计。"""

    def __init__(self, app: ASGIApp, settings: Settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "").rstrip("/")
        if scope["type"] != "http" or path not in {"/mcp", "/v1/generate", "/v1/models", "/v1/usage"}:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        s = self.settings
        scene = headers.get("x-image-scene", "unspecified")
        if scene not in _SCENES:
            scene = "unspecified"
        correlation_id = headers.get("x-request-id")
        if correlation_id and not _CORRELATION_PATTERN.fullmatch(correlation_id):
            correlation_id = None
        metadata = {"scene": scene, "correlation_id": correlation_id}
        bearer = headers.get("authorization", "")
        caller: CallerIdentity | None = None
        app_id = match_product(s, bearer) if bearer.startswith("Bearer ") else None
        if app_id is not None:
            user = headers.get("x-image-user")
            if user is not None and not _USER_PATTERN.fullmatch(user):
                user = None
            caller = CallerIdentity.product(app_id, user, **metadata)
        elif path == "/v1/usage":
            pass
        elif s.auth_mode == "legacy":
            # 保持默认版本的 REST 静态 Key / MCP 无鉴权行为。
            if not (path == "/v1/generate" and s.api_key and not _same_bearer(bearer, s.api_key)):
                caller = CallerIdentity.legacy(s.legacy_app_id, **metadata)
        elif s.auth_mode == "mixed" and (
            (s.api_key and _same_bearer(bearer, s.api_key)) or (not bearer and s.allow_legacy_anonymous)
        ):
            caller = CallerIdentity.legacy(s.legacy_app_id, **metadata)
        if caller is None:
            response = JSONResponse(
                {"error": "unauthorized", "message": "Request identity could not be accepted."},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["image_caller"] = caller
        await self.app(scope, receive, send)


def check_api_key(request: Request) -> JSONResponse | None:
    """Check Bearer token if API_KEY is configured. Returns None if OK."""
    if getattr(request.state, "image_caller", None) is not None:
        return None  # HTTP 边界已经完成认证。
    settings = request.app.state.settings
    if not settings.api_key:
        return None  # No auth configured, allow all

    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                error="unauthorized",
                message="Missing or invalid Authorization header. Use: Bearer <API_KEY>",
            ).model_dump(),
        )

    token = auth_header[7:]  # Strip "Bearer "
    if token != settings.api_key:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                error="unauthorized",
                message="Invalid API key.",
            ).model_dump(),
        )

    return None
