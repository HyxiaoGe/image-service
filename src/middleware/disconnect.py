from __future__ import annotations

import asyncio

import anyio
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.models.lifetime import RequestLifetime


class DisconnectMiddleware:
    """Relay the request body once, while observing disconnects for REST and MCP.

    MCP tools may outlive their HTTP handler in the SDK's lifespan task group.
    The shared lifetime stays attached to the original request's state.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"].rstrip("/") not in {"/v1/generate", "/mcp"}
        ):
            await self.app(scope, receive, send)
            return

        lifetime = RequestLifetime()
        scope.setdefault("state", {})["image_generation"] = lifetime
        writer, reader = anyio.create_memory_object_stream[Message](1)

        async def relay() -> None:
            async with writer:
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        lifetime.disconnected.set()
                    await writer.send(message)
                    if message["type"] == "http.disconnect":
                        return

        async def forwarded_receive() -> Message:
            try:
                return await reader.receive()
            except anyio.EndOfStream:
                return {"type": "http.disconnect"}

        async with reader, anyio.create_task_group() as tasks:
            tasks.start_soon(relay)
            try:
                await self.app(scope, forwarded_receive, send)
            except asyncio.CancelledError:
                # A disconnected HTTP client has no response to receive. Keep
                # ordinary connection cancellation out of ASGI error logs.
                if not lifetime.disconnected.is_set():
                    raise
            finally:
                tasks.cancel_scope.cancel()
