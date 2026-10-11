"""DEFAULT_FALLBACK_MODEL: used when the caller passes no fallback_model; explicit values win."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import Settings
from src.services import generator
from tests.conftest import make_gemini_response
from tests.contract.conftest import FILTERED

GEMINI = "gemini-3-pro-image-preview"  # default model in tests
FLASH_31 = "gemini-3.1-flash-image-preview"
SEEDREAM = "doubao-seedream-4-5"
ARK_SENSITIVE = (400, {"error": {"code": "InputTextSensitiveContentDetected", "message": "sensitive"}})


@pytest.fixture
def backup(app, routing):
    """Gemini blocks, Ark draws, and Seedream is configured as the default backup."""
    app.state.settings.default_fallback_model = SEEDREAM
    return routing


async def post(client, **body):
    return await client.post("/v1/generate", json={"prompt": "an avengers poster", **body})


@pytest.mark.asyncio
async def test_default_fallback_applies_when_not_passed(client, backup):
    data = (await post(client)).json()

    assert data["model"] == SEEDREAM
    assert data["requested_model"] == GEMINI
    assert data["fallback_used"] is True


@pytest.mark.asyncio
async def test_explicit_fallback_overrides_default(client, backup, app):
    backup.gemini = (200, FILTERED)
    data = (await post(client, model=SEEDREAM, fallback_model=FLASH_31)).json()
    # Seedream succeeds on its own here, so no fallback; the point is no validation clash.
    assert data["model"] == SEEDREAM
    assert data["fallback_used"] is False


@pytest.mark.asyncio
async def test_none_disables_default_fallback(client, backup):
    data = (await post(client, fallback_model="none")).json()

    assert data["error"] == "content_filtered"
    assert backup.calls["ark"] == 0


@pytest.mark.asyncio
async def test_seedream_primary_falls_back_to_gemini(client, backup, app):
    """Switching roles via config: Seedream main, Gemini backup."""
    app.state.settings.default_model = SEEDREAM
    app.state.settings.default_fallback_model = FLASH_31
    backup.ark = ARK_SENSITIVE
    backup.gemini = (200, make_gemini_response())

    data = (await post(client, aspect_ratio="16:9")).json()

    assert data["model"] == FLASH_31
    assert data["requested_model"] == SEEDREAM
    assert data["fallback_used"] is True
    assert backup.calls == {"gemini": 1, "ark": 1}


@pytest.mark.asyncio
async def test_default_fallback_same_as_model_is_skipped(client, backup):
    """Caller picks the model that is also the default backup: no fallback, no error."""
    backup.ark = ARK_SENSITIVE

    resp = await post(client, model=SEEDREAM)

    assert resp.json()["error"] == "content_filtered"
    assert backup.calls == {"gemini": 0, "ark": 1}


@pytest.mark.asyncio
async def test_default_fallback_skipped_when_ratio_unsupported(client, backup, app):
    """Seedream main at 7:5 with a Gemini default backup: generate without fallback rather than 422."""
    app.state.settings.default_model = SEEDREAM
    app.state.settings.default_fallback_model = FLASH_31

    resp = await post(client, aspect_ratio="7:5")

    assert resp.status_code == 200
    assert resp.json()["model"] == SEEDREAM


@pytest.mark.asyncio
async def test_explicit_fallback_still_validated(client, backup):
    resp = await post(client, fallback_model=GEMINI)

    assert resp.status_code == 422
    assert resp.json()["fields"] == ["fallback_model"]


@pytest.mark.asyncio
async def test_log_records_fallback_source(client, backup, monkeypatch):
    events = []

    class Recorder:
        def __getattr__(self, _level):
            return lambda event, **kw: events.append((event, kw))

    monkeypatch.setattr(generator, "logger", Recorder())
    await post(client)

    [finished] = [kw for event, kw in events if event == "generation_finished"]
    assert finished["fallback_model"] == SEEDREAM
    assert finished["fallback_source"] == "default"


def test_settings_reject_unknown_models():
    with pytest.raises(ValidationError, match="DEFAULT_MODEL"):
        Settings(default_model="nope")
    with pytest.raises(ValidationError, match="DEFAULT_FALLBACK_MODEL"):
        Settings(default_fallback_model="nope")
    with pytest.raises(ValidationError, match="differ"):
        Settings(default_model=SEEDREAM, default_fallback_model=SEEDREAM)


def test_settings_empty_fallback_disables(monkeypatch):
    monkeypatch.setenv("DEFAULT_FALLBACK_MODEL", "")
    assert Settings().default_fallback_model is None


def test_settings_defaults():
    s = Settings(_env_file=None)
    assert s.default_model == "gemini-nano-banana-2.1"
    assert s.default_fallback_model == "doubao-seedream-5-0-pro"
