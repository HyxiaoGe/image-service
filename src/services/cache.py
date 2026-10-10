from __future__ import annotations

import hashlib
import json

import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger()

LOCK_TTL_SECONDS = 30
LOCK_POLL_INTERVAL = 0.5
LOCK_MAX_WAIT = 60


# Bump when cached images stop matching what the same parameters produce now.
# v2 (2026-10-10): Gemini started honouring aspect_ratio; older entries are
# landscape images stored under every ratio's key.
CACHE_KEY_VERSION = "v2"


def compute_cache_key(prompt: str, model: str, aspect_ratio: str, size: str, style: str | None = None) -> str:
    """Compute SHA-256 cache key from normalized generation parameters."""
    style_part = (style or "").strip().lower()
    raw = f"{CACHE_KEY_VERSION}:{prompt.strip().lower()}:{model.lower()}:{aspect_ratio}:{size}:{style_part}"
    return hashlib.sha256(raw.encode()).hexdigest()


class CacheService:
    def __init__(self, redis_client: aioredis.Redis, ttl: int) -> None:
        self.redis = redis_client
        self.ttl = ttl

    async def get_cached(self, cache_key: str) -> dict | None:
        """Return cached metadata dict or None on miss."""
        data = await self.redis.get(f"image:{cache_key}")
        if data is None:
            return None
        logger.info("cache_hit", cache_key=cache_key)
        return json.loads(data)

    async def set_cached(self, cache_key: str, metadata: dict) -> None:
        """Store generation metadata in cache with TTL."""
        await self.redis.set(
            f"image:{cache_key}",
            json.dumps(metadata),
            ex=self.ttl,
        )
        logger.info("cache_set", cache_key=cache_key)

    async def acquire_lock(self, cache_key: str) -> bool:
        """Try to acquire a generation lock. Returns True if acquired."""
        acquired = await self.redis.set(
            f"lock:image:{cache_key}",
            "1",
            nx=True,
            ex=LOCK_TTL_SECONDS,
        )
        return acquired is not None and acquired is not False

    async def release_lock(self, cache_key: str) -> None:
        """Release a generation lock."""
        await self.redis.delete(f"lock:image:{cache_key}")

    async def ping(self) -> bool:
        """Check if Redis is reachable."""
        try:
            return await self.redis.ping()
        except Exception:
            return False
