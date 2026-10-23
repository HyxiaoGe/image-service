from __future__ import annotations

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from src.config import ProductConfig
from src.middleware.auth import IdentityVerifier
from tests.conftest import MockTransport, make_seedream_response


@pytest.fixture
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key, **changes):
    now = int(time.time())
    claims = {
        "iss": "https://auth.test",
        "aud": "image-service",
        "type": "access",
        "principal_type": "user",
        "sub": "user-1",
        "app_id": "fusion",
        "azp": "fusion-api",
        "scopes": ["image:read", "image:generate", "usage:read"],
        "iat": now,
        "exp": now + 300,
        "jti": "test-token",
        "sid": "session-1",
        "auth_generation": 0,
    }
    claims.update(changes)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})


@pytest.fixture
def secured(app, signing_key, monkeypatch):
    s = app.state.settings
    s.auth_mode = "required"
    s.auth_issuer = "https://auth.test"
    s.auth_jwks_url = "https://auth.test/.well-known/jwks.json"
    s.products = {
        "fusion": ProductConfig(
            authorized_parties=["fusion-api"], ark_key_env="TEST_FUSION_ARK_KEY", ark_key_alias="fusion"
        ),
        "validation": ProductConfig(
            authorized_parties=["validation-cli"], ark_key_env="TEST_VALIDATION_ARK_KEY", ark_key_alias="validation"
        ),
    }
    monkeypatch.setenv("TEST_FUSION_ARK_KEY", "fusion-upstream-secret")
    monkeypatch.setenv("TEST_VALIDATION_ARK_KEY", "validation-upstream-secret")
    verifier = IdentityVerifier(s)
    verifier.validator._jwks_cache = {
        "keys": [dict(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True), kid="test")]
    }
    verifier.validator._cache_time = time.time()
    app.state.identity_verifier = verifier
    transport = MockTransport(make_seedream_response())
    app.state.http_client = httpx.AsyncClient(transport=transport)
    return transport


async def generate(client, bearer, **body):
    return await client.post(
        "/v1/generate",
        headers={"Authorization": "Bearer " + bearer},
        json={"prompt": "a cup", "model": "doubao-seedream-5-0-flash", **body},
    )


@pytest.mark.parametrize(
    "change",
    [
        {"aud": "fusion-web"},
        {"iss": "https://wrong.test"},
        {"type": "refresh"},
        {"exp": 1},
        {"app_id": "unknown"},
        {"azp": "other-product"},
        {"sid": ""},
        {"iat": None},
        {"jti": None},
        {"principal_type": "unknown"},
        {"exp": int(time.time()) + 3600},
    ],
)
async def test_invalid_identity_cannot_submit(client, secured, signing_key, change):
    response = await generate(client, token(signing_key, **change))
    assert response.status_code in (401, 403)
    assert secured.call_count == 0


async def test_forged_signature_rejected(client, secured):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert (await generate(client, token(other))).status_code == 401
    assert secured.call_count == 0


async def test_missing_identity_and_read_only_rejected_before_upstream(client, secured, signing_key):
    assert (await client.post("/v1/generate", json={"prompt": "x"})).status_code == 401
    assert (await generate(client, token(signing_key, scopes=["image:read"]))).status_code == 403
    assert secured.call_count == 0


async def test_products_use_own_key_and_cache(client, app, secured, signing_key):
    first = await generate(client, token(signing_key))
    assert first.status_code == 200, first.text
    assert secured.last_request.headers["authorization"] == "Bearer fusion-upstream-secret"
    validation = token(
        signing_key, app_id="validation", azp="validation-cli", principal_type="service", sub="validation-cli", sid=None
    )
    second = await generate(client, validation)
    assert second.status_code == 200, second.text
    assert secured.last_request.headers["authorization"] == "Bearer validation-upstream-secret"
    assert second.json()["id"] != first.json()["id"]
    third = await generate(client, token(signing_key, sub="user-2", sid="session-2"))
    assert third.json()["cached"] is True
    assert third.json()["id"] == first.json()["id"]
    assert secured.call_count == 2


async def test_missing_product_key_never_borrows_default(client, app, secured, signing_key, monkeypatch):
    app.state.settings.ark_api_key = "legacy-secret"
    monkeypatch.delenv("TEST_FUSION_ARK_KEY")
    response = await generate(client, token(signing_key))
    assert response.status_code == 503
    assert response.json()["error"] == "config_missing"
    assert secured.call_count == 0


async def test_revoked_session_rejected(client, app, secured, signing_key):
    await app.state.redis.set("revoked_sid:session-1", "1")
    assert (await generate(client, token(signing_key))).status_code == 401
    assert secured.call_count == 0


async def test_mcp_read_only_scope_checked_at_http_boundary(client, secured, signing_key):
    response = await client.post(
        "/mcp",
        headers={"Authorization": "Bearer " + token(signing_key, scopes=["image:read"])},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "generate_image", "arguments": {"prompt": "x"}},
        },
    )
    assert response.status_code == 403
    assert secured.call_count == 0


async def test_usage_endpoint_never_public(client):
    response = await client.get("/v1/usage")
    assert response.status_code in (401, 403)


async def test_mixed_mode_keeps_controlled_legacy_and_rejects_bad_tokens(client, app, secured, signing_key):
    s = app.state.settings
    s.auth_mode = "mixed"
    s.api_key = "transition-secret"
    s.ark_api_key = "legacy-ark-secret"
    s.allow_legacy_anonymous = True
    assert (await generate(client, "bad-token")).status_code == 401
    assert secured.call_count == 0
    response = await generate(client, "transition-secret")
    assert response.status_code == 200
    assert secured.last_request.headers["authorization"] == "Bearer legacy-ark-secret"
    response = await generate(client, token(signing_key), no_cache=True)
    assert response.status_code == 200
    assert secured.last_request.headers["authorization"] == "Bearer fusion-upstream-secret"
    assert (await client.get("/v1/usage", headers={"Authorization": "Bearer transition-secret"})).status_code == 403


async def test_required_mode_never_accepts_legacy_secret(client, app, secured):
    app.state.settings.api_key = "old-secret"
    assert (await generate(client, "old-secret")).status_code == 401
    assert secured.call_count == 0


async def test_user_logout_marker_is_checked(client, app, secured, signing_key):
    await app.state.redis.set("revoked_user:user-1", str(time.time() + 0.5))
    assert (await generate(client, token(signing_key))).status_code == 401
    assert secured.call_count == 0


async def test_revocation_redis_failure_matches_existing_fail_open_contract(
    client, app, secured, signing_key, monkeypatch
):
    async def unavailable(*args):
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(app.state.redis, "mget", unavailable)
    assert (await generate(client, token(signing_key))).status_code == 200


async def test_usage_scope_cannot_select_another_product(client, app, secured, signing_key):
    response = await client.get(
        "/v1/usage?app_id=validation", headers={"Authorization": "Bearer " + token(signing_key)}
    )
    # 关闭的记录功能返回 503；启用后只能使用 Token 的产品身份，参数不得覆盖。
    assert response.status_code == 503


async def test_jwks_unavailable_is_not_an_anonymous_fallback(client, app, secured, signing_key, monkeypatch):
    async def unavailable(*args):
        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(app.state.identity_verifier.validator, "verify_async", unavailable)
    response = await generate(client, token(signing_key))
    assert response.status_code == 503
    assert secured.call_count == 0
