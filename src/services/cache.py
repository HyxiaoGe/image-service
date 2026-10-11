from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time

import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger()

LOCK_POLL_INTERVAL = 0.5
LOCK_MAX_WAIT = 120  # how long an identical request waits for the lock holder


# Bump when cached images stop matching what the same parameters produce now.
# v2 (2026-10-10): Gemini started honouring aspect_ratio; older entries are
# landscape images stored under every ratio's key.
CACHE_KEY_VERSION = "v2"


def compute_cache_key(
    prompt: str,
    model: str,
    aspect_ratio: str,
    size: str,
    style: str | None = None,
    fallback_model: str | None = None,
    grounding: bool = False,
    reference_images: list[str] | None = None,
    reference_subject: str | None = None,
) -> str:
    """Compute SHA-256 cache key from normalized generation parameters.

    fallback_model is part of the key so a fallback result is never served to a
    caller that did not opt in. Same for grounding. Both are appended only when
    set, keeping keys for plain requests unchanged.
    """
    style_part = (style or "").strip().lower()
    raw = f"{CACHE_KEY_VERSION}:{prompt.strip().lower()}:{model.lower()}:{aspect_ratio}:{size}:{style_part}"
    if fallback_model:
        raw += f":fallback={fallback_model.lower()}"
    if grounding:
        raw += ":grounding"
    if reference_images:
        # Keyed by URL, not content: a URL whose image changes keeps serving the
        # cached result until TTL or no_cache.
        raw += ":refs=" + "\n".join(reference_images)
    if reference_subject:
        # Versioned with the reference search: images made with references picked
        # (or prompted) under older rules must not be served again (v2: whole back
        # views first + reference hint in the prompt).
        raw += f":subject@v2={reference_subject.strip().lower()}"
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

    async def set_cached(self, cache_key: str, metadata: dict, ttl: int | None = None) -> None:
        """Store generation metadata in cache with TTL (default: the configured cache TTL)."""
        await self.redis.set(
            f"image:{cache_key}",
            json.dumps(metadata),
            ex=min(ttl, self.ttl) if ttl else self.ttl,
        )
        logger.info("cache_set", cache_key=cache_key)

    async def acquire_lock(self, cache_key: str, ttl: int = 60) -> str | None:
        """Try to acquire the generation lock. Returns an owner token, or None if held."""
        token = secrets.token_hex(8)
        acquired = await self.redis.set(f"lock:image:{cache_key}", token, nx=True, ex=ttl)
        return token if acquired else None

    async def release_lock(self, cache_key: str, token: str) -> None:
        """Release the lock only if we still own it (it may have expired and been re-taken)."""
        key = f"lock:image:{cache_key}"
        if await self.redis.get(key) == token:
            await self.redis.delete(key)

    async def wait_for_cached(self, cache_key: str) -> dict | None:
        """Wait for the lock holder's result.

        Returns the cached metadata once it appears, or None if the lock is gone
        without a result (the holder failed) or LOCK_MAX_WAIT elapses.
        """
        deadline = time.monotonic() + LOCK_MAX_WAIT
        while time.monotonic() < deadline:
            await asyncio.sleep(LOCK_POLL_INTERVAL)
            cached = await self.get_cached(cache_key)
            if cached is not None:
                return cached
            if not await self.redis.exists(f"lock:image:{cache_key}"):
                # Holder finished; re-check once in case it wrote between our reads.
                return await self.get_cached(cache_key)
        return None

    async def ping(self) -> bool:
        """Check if Redis is reachable."""
        try:
            return await self.redis.ping()
        except Exception:
            return False
