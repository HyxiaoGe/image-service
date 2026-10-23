from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from src.config import ProductConfig, Settings
from tests.conftest import MockTransport, make_seedream_response

FUSION_KEY = "fusion-service-secret"
VALIDATION_KEY = "validation-service-secret"


@pytest.fixture
def secured(app, monkeypatch):
    s = app.state.settings
    s.products = {
        "fusion": ProductConfig(
            service_key_env="TEST_FUSION_SERVICE_KEY", ark_key_env="TEST_FUSION_ARK_KEY", ark_key_alias="fusion"
        ),
        "validation": ProductConfig(
            service_key_env="TEST_VALIDATION_SERVICE_KEY",
            ark_key_env="TEST_VALIDATION_ARK_KEY",
            ark_key_alias="validation",
        ),
    }
    monkeypatch.setenv("TEST_FUSION_SERVICE_KEY", FUSION_KEY)
    monkeypatch.setenv("TEST_VALIDATION_SERVICE_KEY", VALIDATION_KEY)
    monkeypatch.setenv("TEST_FUSION_ARK_KEY", "fusion-upstream-secret")
    monkeypatch.setenv("TEST_VALIDATION_ARK_KEY", "validation-upstream-secret")
    transport = MockTransport(make_seedream_response())
    app.state.http_client = httpx.AsyncClient(transport=transport)
    return transport


async def generate(client, bearer, user=None, **body):
    headers = {"Authorization": "Bearer " + bearer}
    if user is not None:
        headers["X-Image-User"] = user
    return await client.post(
        "/v1/generate",
        headers=headers,
        json={"prompt": "a cup", "model": "doubao-seedream-5-0-flash", **body},
    )


@pytest.mark.parametrize("bearer", ["wrong-key", "", FUSION_KEY + "x", FUSION_KEY[:-1]])
async def test_unknown_service_key_cannot_submit(client, secured, bearer):
    assert (await generate(client, bearer)).status_code == 401
    assert secured.call_count == 0


async def test_missing_identity_rejected_before_upstream(client, secured):
    assert (await client.post("/v1/generate", headers={"Authorization": ""}, json={"prompt": "x"})).status_code == 401
    assert secured.call_count == 0


async def test_product_without_configured_service_key_cannot_be_matched(client, secured, monkeypatch):
    monkeypatch.setenv("TEST_VALIDATION_SERVICE_KEY", "")
    assert (await generate(client, "")).status_code == 401
    assert secured.call_count == 0


async def test_products_use_own_key_and_cache(client, app, secured):
    first = await generate(client, FUSION_KEY, user="user-1")
    assert first.status_code == 200, first.text
    assert secured.last_request.headers["authorization"] == "Bearer fusion-upstream-secret"
    second = await generate(client, VALIDATION_KEY)
    assert second.status_code == 200, second.text
    assert secured.last_request.headers["authorization"] == "Bearer validation-upstream-secret"
    assert second.json()["id"] != first.json()["id"]
    third = await generate(client, FUSION_KEY, user="user-2")
    assert third.json()["cached"] is True
    assert third.json()["id"] == first.json()["id"]
    assert secured.call_count == 2


async def test_missing_product_key_never_borrows_default(client, app, secured, monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "legacy-secret")
    monkeypatch.delenv("TEST_FUSION_ARK_KEY")
    response = await generate(client, FUSION_KEY)
    assert response.status_code == 503
    assert response.json()["error"] == "config_missing"
    assert secured.call_count == 0


async def test_usage_endpoint_never_public(client):
    response = await client.get("/v1/usage", headers={"Authorization": ""})
    assert response.status_code == 401


async def test_usage_cannot_select_another_product(client, app, secured):
    response = await client.get("/v1/usage?app_id=validation", headers={"Authorization": "Bearer " + FUSION_KEY})
    # 关闭的记录功能返回 503；启用后只能使用服务密钥所属产品，参数不得覆盖。
    assert response.status_code == 503


def test_products_cannot_share_a_service_key(tmp_storage):
    product = {"service_key_env": "SHARED_SERVICE_KEY", "ark_key_env": "ARK_A", "ark_key_alias": "a"}
    with pytest.raises(ValidationError):
        Settings(storage_dir=str(tmp_storage), products={"a": product, "b": {**product, "ark_key_alias": "b"}})


@pytest.mark.parametrize("mode", ["legacy", "mixed"])
def test_removed_auth_modes_fail_configuration(mode):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, auth_mode=mode)


async def test_old_shared_key_does_not_authenticate(client, secured, monkeypatch):
    monkeypatch.setenv("API_KEY", "old-secret")
    assert (await generate(client, "old-secret")).status_code == 401
    assert secured.call_count == 0


async def test_duplicate_service_key_values_fail_closed(client, secured, monkeypatch):
    monkeypatch.setenv("TEST_VALIDATION_SERVICE_KEY", FUSION_KEY)
    assert (await generate(client, FUSION_KEY)).status_code == 401
    assert secured.call_count == 0


async def test_in_memory_mcp_without_http_identity_cannot_generate(app, mock_transport, monkeypatch):
    from fastmcp import Client

    def no_request():
        raise RuntimeError("No HTTP context")

    monkeypatch.setattr("src.mcp.server.get_http_request", no_request)
    async with Client(app.state.mcp) as mcp_client:
        for name, args in [("generate_image", {"prompt": "cup"}), ("list_models", {})]:
            result = await mcp_client.call_tool(name, args)
            assert result.structured_content["error"]["code"] == "unauthorized"
    assert mock_transport.call_count == 0
