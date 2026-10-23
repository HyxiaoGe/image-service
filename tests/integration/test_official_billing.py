from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import anyio
import httpx
import pytest
from pydantic import SecretStr

from src.config import ProductConfig
from src.services import billing

PERIOD = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")
RESOURCE = "apikey-test-product"
AK = "billing-test-access-key"
SK = "billing-test-secret-key"


def row(amount="0.12", **fields):
    return {
        "Product": "Doubao-Seedream",
        "SplitItemID": RESOURCE,
        "BillPeriod": PERIOD,
        "Currency": "CNY",
        "PayableAmount": amount,
        "PaidAmount": amount,
        **fields,
    }


def response(rows=None, total=None, **fields):
    rows = [row()] if rows is None else rows
    return {
        "ResponseMetadata": {"RequestId": "official-test-request"},
        "Result": {"List": rows, "Total": len(rows) if total is None else total, "Warning": "", **fields},
    }


@pytest.fixture
def official(app):
    settings = app.state.settings
    settings.billing_enabled = True
    settings.billing_access_key = SecretStr(AK)
    settings.billing_secret_key = SecretStr(SK)
    settings.products["test"].ark_resource_id = RESOURCE
    calls, replies = [], [response()]

    async def upstream(request):
        calls.append(request)
        value = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, httpx.Response) else httpx.Response(200, json=value)

    app.state.billing_http_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    return calls, replies


async def get(client, **params):
    return await client.get("/v1/billing", params={"bill_period": PERIOD, **params})


async def test_product_scoped_signed_summary_and_cache(client, app, official, fake_redis, caplog):
    calls, replies = official
    replies[0] = response([row("0.123456789012"), row("-0.02")])
    result = await get(client, app_id="other", ark_resource_id="other")
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["app_id"] == "test" and data["ark_resource_id"] == RESOURCE
    assert data["cost_kind"] == "official_split_bill"
    assert data["payable_amount_cny"] == data["paid_amount_cny"] == "0.103456789012"
    assert data["settlement_finality"] == "not_asserted" and data["expected_delay_days"] == 2
    assert result.headers["Cache-Control"] == "private, no-store"
    wire = json.loads(calls[0].content)
    assert wire["SplitItemID"] == RESOURCE and wire["SplitDimension"] == '{"Doubao-Seedream":"apikey_id"}'
    assert wire["Product"] == ["Doubao-Seedream"] and data["product_code"] == "Doubao-Seedream"
    assert wire["IgnoreZero"] == 0 and wire["NeedRecordNum"] == 1 and wire["GroupPeriod"] == 0
    assert calls[0].url.host == "open.volcengineapi.com"
    assert calls[0].url.params["Action"] == "ListSplitBillDetail"
    assert calls[0].headers["Authorization"].startswith("HMAC-SHA256 Credential=" + AK + "/")
    assert "/cn-beijing/billing/request" in calls[0].headers["Authorization"]
    assert calls[0].headers["X-Content-Sha256"] == hashlib.sha256(calls[0].content).hexdigest()
    assert SK not in str(calls[0].headers)
    second = await get(client)
    assert second.json()["cached"] is True and len(calls) == 1
    cached = "".join([await fake_redis.get(k) for k in await fake_redis.keys(billing.PREFIX + "report:*")])
    for secret in [AK, SK, "Authorization", "official-test-request"]:
        assert secret not in cached and secret not in result.text and secret not in caplog.text


async def test_empty_official_bill_is_not_zero_or_final(client, official):
    official[1][0] = response([])
    data = (await get(client)).json()
    assert data["status"] == "no_data" and data["record_count"] == 0
    assert data["payable_amount_cny"] is None and data["paid_amount_cny"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("SplitItemID", "another-product"),
        ("Product", "ark_bd"),
        ("BillPeriod", "2000-01"),
        ("Currency", "USD"),
        ("PayableAmount", "NaN"),
        ("PaidAmount", "Infinity"),
        ("PayableAmount", None),
        ("PayableAmount", 0.12),
    ],
)
async def test_invalid_or_other_product_rows_never_return_money(client, official, fake_redis, field, value):
    official[1][0] = response([row(**{field: value})])
    result = await get(client)
    assert result.status_code == 502 and result.json()["error"] == "billing_incomplete"
    assert "another-product" not in result.text
    assert not await fake_redis.keys(billing.PREFIX + "report:*")


@pytest.mark.parametrize(
    "reply",
    [
        response(Warning="secret ignored filter"),
        response([], total=2),
        response([row()], total=0),
        response(Total=-1),
        {"Result": {"List": [], "Total": 0}},
        {"ResponseMetadata": {"Error": {"Code": "secret-error"}}, "Result": {"List": [], "Total": 0}},
    ],
)
async def test_warning_malformed_and_incomplete_bills_fail_closed(client, official, reply):
    official[1][0] = reply
    result = await get(client)
    assert result.status_code in {502, 503}
    assert "secret" not in result.text


async def test_all_pages_collected_without_rounding_or_truncation(client, official, monkeypatch):
    monkeypatch.setattr(billing, "PAGE_SIZE", 1)
    official[1][:] = [response([row()], total=2), response([row("0.22")], total=2)]
    result = await get(client)
    assert result.status_code == 200
    assert result.json()["record_count"] == 2 and result.json()["payable_amount_cny"] == "0.34"
    assert [json.loads(r.content)["Offset"] for r in official[0]] == [0, 1]


async def test_pagination_limit_never_returns_partial_sum(client, official, monkeypatch):
    monkeypatch.setattr(billing, "PAGE_SIZE", 1)
    monkeypatch.setattr(billing, "MAX_PAGES", 1)
    official[1][0] = response([row()], total=2)
    result = await get(client)
    assert result.status_code == 502 and result.json()["error"] == "billing_incomplete"


async def test_repeated_page_never_double_counts(client, official, monkeypatch):
    monkeypatch.setattr(billing, "PAGE_SIZE", 1)
    official[1][0] = response([row()], total=2)
    result = await get(client)
    assert result.status_code == 502 and result.json()["error"] == "billing_incomplete"


async def test_large_precise_amounts_and_refunds_preserve_decimal_precision(client, official):
    official[1][0] = response([row("99999999999999999999.123456789012"), row("-0.000000000001")])
    result = await get(client)
    assert result.status_code == 200
    assert result.json()["payable_amount_cny"] == "99999999999999999999.123456789011"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
async def test_upstream_errors_sanitized_and_redirects_not_followed(client, official, status):
    official[1][0] = httpx.Response(status, text="secret body", headers={"Location": "https://example.com/steal"})
    result = await get(client)
    assert result.status_code in {502, 503}
    assert "secret body" not in result.text and len(official[0]) == 1


async def test_product_and_credential_cache_isolation(client, app, official, monkeypatch):
    assert (await get(client)).status_code == 200
    app.state.settings.products["other"] = ProductConfig(
        service_key_env="OTHER_SERVICE", ark_key_env="OTHER_ARK", ark_key_alias="other", ark_resource_id="apikey-other"
    )
    monkeypatch.setenv("OTHER_SERVICE", "other-caller-key")
    official[1][0] = response([row(SplitItemID="apikey-other")])
    result = await client.get(
        "/v1/billing",
        headers={"Authorization": "Bearer other-caller-key"},
        params={"bill_period": PERIOD, "app_id": "test"},
    )
    assert result.json()["app_id"] == "other" and result.json()["cached"] is False
    assert json.loads(official[0][-1].content)["SplitItemID"] == "apikey-other"
    app.state.settings.billing_access_key = SecretStr("new-account-access-key")
    official[1][0] = response()
    assert (await get(client)).json()["cached"] is False
    assert len(official[0]) == 3


@pytest.mark.parametrize("parameter", ["disabled", "ak", "sk", "resource"])
async def test_unconfigured_returns_503_even_with_cached_data(client, app, official, parameter):
    assert (await get(client)).status_code == 200
    s = app.state.settings
    if parameter == "disabled":
        s.billing_enabled = False
    elif parameter == "ak":
        s.billing_access_key = SecretStr("")
    elif parameter == "sk":
        s.billing_secret_key = SecretStr("")
    else:
        s.products["test"].ark_resource_id = None
    result = await get(client)
    assert result.status_code == 503 and result.json()["error"] == "billing_not_configured"
    assert len(official[0]) == 1


@pytest.mark.parametrize("period", ["2026-13", "2026-1", "2026-00", "2000-01", "2999-01", "oops"])
async def test_bad_period_rejected_before_upstream(client, official, period):
    assert (await get(client, bill_period=period)).status_code == 422
    assert not official[0]


async def test_missing_or_wrong_key_is_rejected_before_billing(client, official):
    for key in ["", "Bearer wrong"]:
        result = await client.get("/v1/billing", headers={"Authorization": key}, params={"bill_period": PERIOD})
        assert result.status_code == 401
    assert not official[0]


async def test_global_query_lock_rejects_other_queries_and_cancel_releases(client, app, official, fake_redis):
    started = asyncio.Event()

    async def stuck(request):
        started.set()
        await anyio.sleep_forever()

    app.state.billing_http_client = httpx.AsyncClient(transport=httpx.MockTransport(stuck))
    task = asyncio.create_task(get(client))
    try:
        await asyncio.wait_for(started.wait(), 1)
        rejected = await get(client)
        assert rejected.status_code == 503 and rejected.json()["error"] == "billing_busy"
        assert rejected.headers["Retry-After"] == "1"
        assert await fake_redis.exists(billing.PREFIX + "lock")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not await fake_redis.exists(billing.PREFIX + "lock")


async def test_redis_failure_and_total_timeout_do_not_publish_partial_money(
    client, app, official, fake_redis, monkeypatch
):
    async def stuck(request):
        await anyio.sleep_forever()

    app.state.billing_http_client = httpx.AsyncClient(transport=httpx.MockTransport(stuck))
    monkeypatch.setattr(billing, "TIMEOUT_SECONDS", 0.05)
    result = await get(client)
    assert result.status_code == 503
    assert not await fake_redis.keys(billing.PREFIX + "report:*")

    async def broken(*args, **kwargs):
        raise OSError("secret redis password")

    monkeypatch.setattr(fake_redis, "get", broken)
    result = await get(client)
    assert result.status_code == 503 and "secret" not in result.text


def test_billing_secrets_are_redacted_in_configuration(settings):
    settings.billing_access_key = SecretStr(AK)
    settings.billing_secret_key = SecretStr(SK)
    assert AK not in repr(settings) and SK not in repr(settings)
