"""Contract tests for GET /v1/models."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_list_models(client: httpx.AsyncClient):
    """T027: Returns models list with id/name/provider fields."""
    resp = await client.get("/v1/models")
    assert resp.status_code == 200
    data = resp.json()
    assert "models" in data
    models = data["models"]
    assert len(models) >= 3

    for model in models:
        assert "id" in model
        assert "name" in model
        assert "provider" in model

    ids = [m["id"] for m in models]
    assert "gemini-3-pro-image-preview" in ids
    assert "gemini-3.1-flash-image-preview" in ids
    assert "gemini-2.5-flash-image" in ids
    assert "doubao-seedream-4-0" in ids
