"""Integration test for cache deduplication."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_cache_dedup_same_prompt(
    client: httpx.AsyncClient,
    mock_transport,
):
    """T017: Same prompt+model generates once, second call returns cached."""
    payload = {"prompt": "a sunset over mountains", "model": "gemini-3-pro-image-preview"}

    # First request — cache miss
    resp1 = await client.post("/v1/generate", json=payload)
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["cached"] is False

    # Second request — cache hit
    resp2 = await client.post("/v1/generate", json=payload)
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["cached"] is True
    assert data2["image_url"] == data1["image_url"]

    # The upstream image API should have been called only once
    assert mock_transport.call_count == 1
