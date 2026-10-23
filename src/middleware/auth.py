from __future__ import annotations

import os
import re
import secrets

from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from src.config import Settings
from src.models.identity import CallerIdentity

_USER_PATTERN = re.compile(r"[a-zA-Z0-9_.:@-]{1,128}")
_CORRELATION_PATTERN = re.compile(r"[a-zA-Z0-9_.:-]{1,128}")
_SCENES = frozenset({"chat_image", "image_edit", "validation"})


def _same_bearer(header: str, key: str) -> bool:
    return secrets.compare_digest(header.encode("utf-8"), ("Bearer " + key).encode("utf-8"))


def match_product(settings: Settings, bearer: str) -> str | None:
    """按服务密钥认出调用产品；逐个常量时间比较，未配置密钥的产品不可被匹配。"""
    matches = []
    for app_id, product in settings.products.items():
        key = os.environ.get(product.service_key_env, "").strip()
        if key and _same_bearer(bearer, key):
            matches.append(app_id)
    # 相同密钥误配到多个产品时拒绝，不能依据配置顺序选择产品。
    return matches[0] if len(matches) == 1 else None


class CallerAuthMiddleware:
    """在 HTTP 边界认证 REST/MCP；产品身份只来自服务密钥，请求头里的用户只用于统计。"""

    def __init__(self, app: ASGIApp, settings: Settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "").rstrip("/")
        if scope["type"] != "http" or path not in {"/mcp", "/v1/generate", "/v1/models", "/v1/usage", "/v1/billing"}:
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
