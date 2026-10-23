from __future__ import annotations

import hashlib
import json
import math
import secrets
from contextlib import asynccontextmanager

import anyio
import structlog
from redis.exceptions import WatchError

from src.config import Settings
from src.models.errors import ErrorCode
from src.models.identity import CallerIdentity
from src.services.budget import current_budget

logger = structlog.get_logger()
PREFIX = "admission:image:v1:"


class AdmissionError(Exception):
    def __init__(self, code: ErrorCode, retry_after: int | None = None):
        self.code, self.retry_after = code, retry_after
        super().__init__(code.value)


def identity_keys(caller: CallerIdentity) -> list[str]:
    """Redis 中不保存用户 ID；主体类型避免用户名与产品服务主体碰撞。"""

    def digest(parts):
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()

    return [
        "global",
        "product:" + digest([caller.app_id]),
        "user:" + digest([caller.app_id, caller.principal_type, caller.subject]),
    ]


async def _acquire(redis, settings: Settings, identities: list[str], token: str, lease_keys: list[str]) -> None:
    rates = [settings.rate_limit_global, settings.rate_limit_product, settings.rate_limit_user]
    concurrent = [settings.max_concurrent_global, settings.max_concurrent_product, settings.max_concurrent_user]
    lease_seconds = math.ceil(settings.generation_timeout_seconds) + 5
    # WATCH/MULTI 使多进程、REST/MCP 的六项检查和占用同时生效。
    # 被拒绝的调用不消耗其他用户/产品的请求额度。
    for _ in range(8):
        seconds, micros = await redis.time()
        now = int(seconds) + int(micros) / 1_000_000
        window = int(now) // settings.rate_limit_window
        rate_keys = [f"{PREFIX}rate:{identity}:{window}" for identity in identities]
        retry_after = max(1, math.ceil((window + 1) * settings.rate_limit_window - now))
        async with redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(*rate_keys, *lease_keys)
                counts = [int(await pipe.get(key) or 0) for key in rate_keys]
                active = [await pipe.zcount(key, f"({now}", "+inf") for key in lease_keys]
                if any(count >= limit for count, limit in zip(counts, rates, strict=True)):
                    raise AdmissionError(ErrorCode.RATE_LIMITED, retry_after)
                if any(count >= limit for count, limit in zip(active, concurrent, strict=True)):
                    raise AdmissionError(ErrorCode.RATE_LIMITED, 1)
                pipe.multi()
                for key in rate_keys:
                    pipe.incr(key)
                    pipe.expire(key, settings.rate_limit_window * 2)
                for key in lease_keys:
                    pipe.zremrangebyscore(key, "-inf", now)
                    pipe.zadd(key, {token: now + lease_seconds})
                    pipe.expire(key, lease_seconds)
                await pipe.execute()
                return
            except WatchError:
                continue
    raise AdmissionError(ErrorCode.ADMISSION_UNAVAILABLE)


async def _release(redis, lease_keys: list[str], token: str) -> None:
    async with redis.pipeline(transaction=True) as pipe:
        for key in lease_keys:
            pipe.zrem(key, token)
        await pipe.execute()


@asynccontextmanager
async def generation_admission(redis, settings: Settings, caller: CallerIdentity):
    identities = identity_keys(caller)
    lease_keys = [f"{PREFIX}active:{identity}" for identity in identities]
    token = secrets.token_hex(16)
    try:
        try:
            with anyio.fail_after(0.75):
                await _acquire(redis, settings, identities, token, lease_keys)
        except AdmissionError:
            raise
        except Exception:
            # 不因 Redis 故障放行付费生成，也不向调用方泄露连接信息。
            raise AdmissionError(ErrorCode.ADMISSION_UNAVAILABLE) from None
        yield
    finally:
        # 即使 EXEC 已提交但回包丢失/任务取消，也尝试清掉自己的占位。
        # 进程退出或清理失败时，租约会在总生成预算之后自动过期。
        try:
            budget = current_budget()
            cleanup = min(0.2, budget.cleanup_remaining()) if budget is not None else 0.2
            with anyio.move_on_after(cleanup, shield=True) as scope:
                await _release(redis, lease_keys, token)
            if scope.cancelled_caught:
                logger.warning("admission_release_timed_out", app_id=caller.app_id)
        except Exception:
            logger.warning("admission_release_failed", app_id=caller.app_id)
