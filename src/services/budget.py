from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic

import anyio

from src.config import Settings
from src.models.lifetime import RequestLifetime

CLEANUP_TIMEOUT_SECONDS = 1.0


@dataclass
class GenerationBudget:
    deadline: float
    cleanup_timeout: float
    lifetime: RequestLifetime | None

    def remaining(self) -> float:
        return max(0, self.deadline - monotonic())

    async def checkpoint(self) -> None:
        await anyio.lowlevel.checkpoint()
        if self.lifetime is not None and self.lifetime.disconnected.is_set():
            raise asyncio.CancelledError("client disconnected")


_budget: ContextVar[GenerationBudget | None] = ContextVar("generation_budget", default=None)


def current_budget() -> GenerationBudget | None:
    return _budget.get()


@asynccontextmanager
async def generation_scope(settings: Settings, lifetime: RequestLifetime | None) -> AsyncIterator[GenerationBudget]:
    started = lifetime.started_at if lifetime is not None else monotonic()
    # Reserve bounded time inside the total budget for releasing the dedup lock.
    cleanup = min(CLEANUP_TIMEOUT_SECONDS, settings.generation_timeout_seconds / 10)
    budget = GenerationBudget(started + settings.generation_timeout_seconds - cleanup, cleanup, lifetime)
    token = _budget.set(budget)
    owner = asyncio.current_task()
    watcher = None

    async def watch_disconnect() -> None:
        await lifetime.disconnected.wait()
        owner.cancel()

    try:
        await budget.checkpoint()
        if lifetime is not None:
            watcher = asyncio.create_task(watch_disconnect())
        with anyio.fail_after(budget.remaining()):
            yield budget
    finally:
        if watcher is not None:
            watcher.cancel()
            with anyio.CancelScope(shield=True), suppress(asyncio.CancelledError):
                await watcher
        _budget.reset(token)
