"""The release version must identify the same service across packaging and APIs."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from fastmcp import Client

from src import __version__


@pytest.mark.asyncio
async def test_release_version_matches_package_health_openapi_and_mcp(client, app):
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    package = next(p for p in lock["package"] if p["name"] == "image-service")
    assert project["project"]["version"] == package["version"] == __version__
    health = await client.get("/v1/health")
    openapi = await client.get("/openapi.json")
    assert health.json()["version"] == openapi.json()["info"]["version"] == __version__
    async with Client(app.state.mcp) as mcp_client:
        assert mcp_client.initialize_result.serverInfo.version == __version__
        assert "edit existing images" in mcp_client.initialize_result.instructions
