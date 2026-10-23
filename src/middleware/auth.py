from __future__ import annotations

import json
import math
import re
import secrets

import httpx
import jwt
import structlog
from auth_service_client import JWTValidator
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from src.config import Settings
from src.models.identity import CallerIdentity
from src.models.response import ErrorResponse

logger = structlog.get_logger()


def _same_bearer(header: str, key: str) -> bool:
    return secrets.compare_digest(header.encode("utf-8"), ("Bearer " + key).encode("utf-8"))


class IdentityError(Exception):
    def __init__(self, status: int = 401):
        self.status = status


class IdentityVerifier:
    """复用现有验签 SDK，额外收紧目标 Token 的主体、权限和有效期契约。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.validator = JWTValidator(
            jwks_url=settings.auth_jwks_url,
            issuer=settings.auth_issuer,
            audience=settings.auth_audience,
            require_token_type="access",
            leeway_seconds=settings.auth_leeway_seconds,
        )

    async def verify(self, token: str, redis, **metadata) -> CallerIdentity:
        try:
            if len(token) > 16384:
                raise IdentityError()
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not header.get("kid"):
                raise IdentityError()
            user = await self.validator.verify_async(token)
            p = user.raw_payload
            for field in ("sub", "jti", "azp", "app_id"):
                if not isinstance(p.get(field), str) or not re.fullmatch(r"[a-zA-Z0-9_.:@-]{1,128}", p[field]):
                    raise IdentityError()
            for field in ("iat", "exp"):
                if type(p.get(field)) not in (int, float) or not math.isfinite(p[field]):
                    raise IdentityError()
            if p["exp"] <= p["iat"] or p["exp"] - p["iat"] > 300:
                raise IdentityError()
            if not isinstance(p.get("scopes"), list) or any(not isinstance(s, str) for s in p["scopes"]):
                raise IdentityError()
            product = self.settings.products.get(p["app_id"])
            if product is None or p["azp"] not in product.authorized_parties:
                raise IdentityError(403)
            if p.get("principal_type") not in ("user", "service"):
                raise IdentityError()
            if p["principal_type"] == "user":
                if not isinstance(p.get("sid"), str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,256}", p["sid"]):
                    raise IdentityError()
                if type(p.get("auth_generation")) is not int or p["auth_generation"] < 0:
                    raise IdentityError()
                try:
                    revoked_sid, revoked_user = await redis.mget(f"revoked_sid:{p['sid']}", f"revoked_user:{p['sub']}")
                except Exception:
                    # 与现有 auth-service/Fusion 一致：Redis 不可用时退化到 JWT 自身有效期。
                    logger.warning("identity_revocation_unavailable")
                else:
                    if revoked_sid is not None or (revoked_user is not None and p["iat"] < float(revoked_user)):
                        raise IdentityError()
            return CallerIdentity(
                p["app_id"], p["sub"], p["principal_type"], p["azp"], "jwt", tuple(p["scopes"]), **metadata
            )
        except IdentityError:
            raise
        except httpx.HTTPError:
            raise IdentityError(503) from None
        except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
            raise IdentityError() from None


class CallerAuthMiddleware:
    """在 HTTP 边界认证 REST/MCP；只在需要判断 MCP 权限时有界读取并原样回放请求体。"""

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
        if scene not in {"chat_image", "image_edit", "validation"}:
            scene = "unspecified"
        correlation_id = headers.get("x-request-id")
        if correlation_id and not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", correlation_id):
            correlation_id = None
        metadata = {"scene": scene, "correlation_id": correlation_id}
        try:
            bearer = headers.get("authorization", "")
            if s.auth_mode == "legacy":
                if path == "/v1/usage":
                    raise IdentityError()
                # 保持默认版本的 REST 静态 Key / MCP 无鉴权行为；新模式均在 HTTP 边界统一校验。
                if path == "/v1/generate" and s.api_key and not _same_bearer(bearer, s.api_key):
                    raise IdentityError()
                caller = CallerIdentity.legacy(s.legacy_app_id, **metadata)
            elif s.auth_mode == "mixed" and (
                (s.api_key and _same_bearer(bearer, s.api_key)) or (not bearer and s.allow_legacy_anonymous)
            ):
                caller = CallerIdentity.legacy(s.legacy_app_id, **metadata)
            else:
                if not bearer.startswith("Bearer "):
                    raise IdentityError()
                state = scope["app"].state
                verifier = getattr(state, "identity_verifier", None)
                if verifier is None:
                    verifier = IdentityVerifier(s)
                    state.identity_verifier = verifier
                caller = await verifier.verify(bearer[7:], getattr(state, "auth_redis", state.redis), **metadata)
            permission = (
                "usage:read" if path == "/v1/usage" else "image:generate" if path == "/v1/generate" else "image:read"
            )
            if path == "/mcp" and scope["method"] == "POST" and caller.auth_method == "jwt":
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > 1024 * 1024:
                        raise IdentityError(413)
                    if not message.get("more_body"):
                        break
                try:
                    envelope = json.loads(body)
                except (ValueError, UnicodeError):
                    envelope = {}
                if isinstance(envelope, list):
                    raise IdentityError(400)
                if isinstance(envelope, dict) and envelope.get("method") == "tools/call":
                    params = envelope.get("params")
                    if isinstance(params, dict) and params.get("name") == "generate_image":
                        permission = "image:generate"
                original_receive = receive
                replayed = False

                async def replay():
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await original_receive()

                receive = replay
            if path == "/v1/usage" and caller.auth_method != "jwt":
                raise IdentityError(403)
            if caller.auth_method == "jwt" and permission not in caller.scopes:
                raise IdentityError(403)
            scope.setdefault("state", {})["image_caller"] = caller
        except IdentityError as exc:
            code = (
                "unauthorized"
                if exc.status == 401
                else "forbidden"
                if exc.status == 403
                else "identity_unavailable"
                if exc.status == 503
                else "invalid_request"
            )
            response = JSONResponse(
                {"error": code, "message": "Request identity or permission could not be accepted."},
                status_code=exc.status,
                headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else {},
            )
            await response(scope, receive, send)
            return
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
