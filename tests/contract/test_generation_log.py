"""The per-request generation_finished summary log."""

from __future__ import annotations

import httpx
import pytest

from src.services import generator
from tests.conftest import MockTransport

PROMPT = "a very secret prompt about lighthouses"


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def _record(self, level: str):
        def log(event: str, **kw) -> None:
            self.events.append((level, event, kw))

        return log

    def __getattr__(self, level: str):
        return self._record(level)

    def finished(self) -> list[tuple[str, dict]]:
        return [(level, kw) for level, event, kw in self.events if event == "generation_finished"]


@pytest.fixture
def log(monkeypatch) -> RecordingLogger:
    recorder = RecordingLogger()
    monkeypatch.setattr(generator, "logger", recorder)
    return recorder


@pytest.mark.asyncio
async def test_success_then_cached(client, log):
    for _ in range(2):
        assert (await client.post("/v1/generate", json={"prompt": PROMPT})).status_code == 200

    (lvl1, first), (lvl2, second) = log.finished()
    assert (lvl1, first["outcome"], first["attempts"]) == ("info", "success", 1)
    assert (lvl2, second["outcome"], second["attempts"]) == ("info", "cached", 0)
    for entry in (first, second):
        assert entry["entry"] == "rest"
        assert entry["model"] == entry["requested_model"] == "gemini-3-pro-image-preview"
        assert entry["prompt_len"] == len(PROMPT)
        assert len(entry["prompt_sha8"]) == 8
        assert isinstance(entry["duration_ms"], int)
        assert PROMPT not in str(entry)


@pytest.mark.asyncio
async def test_failure_records_code_and_upstream_detail(client, app, log):
    body = {"choices": [{"finish_reason": "content_filter", "message": {"content": "nope"}}]}
    app.state.http_client = httpx.AsyncClient(transport=MockTransport(response_data=body))

    assert (await client.post("/v1/generate", json={"prompt": PROMPT})).status_code == 422

    [(level, entry)] = log.finished()
    assert level == "warning"
    assert entry["outcome"] == "error"
    assert entry["error_code"] == "content_filtered"
    assert "finish_reason=content_filter" in entry["upstream_detail"]
    assert PROMPT not in str(entry)


@pytest.mark.asyncio
async def test_unexpected_exception_logged_with_traceback(client, app, log, monkeypatch):
    async def boom(*_a, **_kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(generator, "call_gemini", boom)

    assert (await client.post("/v1/generate", json={"prompt": PROMPT})).status_code == 500

    [(level, entry)] = log.finished()
    assert level == "exception"
    assert entry["error_code"] == "internal_error"
