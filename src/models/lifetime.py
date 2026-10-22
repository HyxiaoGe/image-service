from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import monotonic


@dataclass
class RequestLifetime:
    started_at: float = field(default_factory=monotonic)
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)
