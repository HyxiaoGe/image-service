"""只读官方分账；不把延迟、空账单或不完整分页冒充最终实扣。"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from zoneinfo import ZoneInfo

import anyio
import httpx
import structlog
from redis.exceptions import WatchError
from volcenginesdkcore.signv4 import SignerV4

from src.config import Settings
from src.models.identity import CallerIdentity

logger = structlog.get_logger()
ENDPOINT = "https://open.volcengineapi.com/"
QUERY = {"Action": "ListSplitBillDetail", "Version": "2022-01-01"}
PREFIX = "billing:image:v1:"
CACHE_SECONDS = 900
TIMEOUT_SECONDS = 10
PAGE_SIZE = 100
MAX_PAGES = 10
MESSAGES = {
    "billing_not_configured": (503, False, "Official billing credentials or product resource ID are not configured."),
    "billing_busy": (503, True, "An official billing query is in progress. Please retry shortly."),
    "billing_unavailable": (503, True, "Official billing is temporarily unavailable."),
    "billing_permission_denied": (502, False, "The billing credential is not authorized to query official bills."),
    "billing_incomplete": (502, False, "The official bill could not be verified as a complete product-scoped result."),
    "invalid_params": (422, False, "Use an existing billing month within the last 24 months (YYYY-MM)."),
}


class BillingError(Exception):
    def __init__(self, code: str, retry_after: int | None = None):
        self.code = code
        self.status, self.retryable, self.message = MESSAGES[code]
        self.retry_after = retry_after
        super().__init__(self.message)


def validate_period(period: str) -> None:
    try:
        date = datetime.strptime(period, "%Y-%m")
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        age = (now.year - date.year) * 12 + now.month - date.month
        if date.strftime("%Y-%m") != period or not 0 <= age <= 24:
            raise ValueError
    except ValueError:
        raise BillingError("invalid_params") from None


def _money(value) -> Decimal:
    # 拒绝非有限值、缺字段和非十进制金额；保留退款的负数及官方精度。
    if not isinstance(value, str) or len(value) > 64:
        raise BillingError("billing_incomplete")
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount.copy_abs() >= Decimal("1e20") or amount.as_tuple().exponent < -12:
            raise InvalidOperation
        return amount
    except InvalidOperation:
        raise BillingError("billing_incomplete") from None


async def _pace(redis) -> None:
    # 所有产品、分页、服务实例共用一把查询锁；各次请求至少相隔 250ms。
    seconds, micros = await redis.time()
    now = int(seconds) + int(micros) / 1_000_000
    previous = float(await redis.get(PREFIX + "next") or 0)
    delay = max(0, previous - now)
    if delay > 1:
        raise BillingError("billing_unavailable")
    if delay:
        await anyio.sleep(delay)
    seconds, micros = await redis.time()
    now = int(seconds) + int(micros) / 1_000_000
    await redis.set(PREFIX + "next", str(now + 0.25), ex=2)


async def _page(client: httpx.AsyncClient, settings: Settings, payload: dict, redis) -> dict:
    await _pace(redis)
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    headers = {"Host": "open.volcengineapi.com", "Content-Type": "application/json"}
    # 仅复用官方签名组件；网络由可取消、有总时限的异步 httpx 执行。
    SignerV4.sign(
        "/",
        "POST",
        headers,
        body,
        None,
        QUERY,
        settings.billing_access_key.get_secret_value(),
        settings.billing_secret_key.get_secret_value(),
        settings.billing_region,
        "billing",
    )
    # 不发送原始 SK，不跟随重定向，不在日志中输出签名头或原始响应。
    async with client.stream(
        "POST", ENDPOINT, params=QUERY, headers=headers, content=body.encode(), follow_redirects=False
    ) as response:
        if response.status_code in {401, 403}:
            raise BillingError("billing_permission_denied")
        if response.status_code != 200:
            raise BillingError("billing_unavailable")
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > 2_000_000:
                raise BillingError("billing_incomplete")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise BillingError("billing_incomplete")
    metadata = data.get("ResponseMetadata")
    if not isinstance(metadata, dict) or metadata.get("Error"):
        raise BillingError("billing_unavailable")
    result = data.get("Result")
    if not isinstance(result, dict) or result.get("Warning"):
        # 官方提示筛选被忽略/降级时，不能把返回的数据当成本产品账单。
        raise BillingError("billing_incomplete")
    return result


async def _collect(client, settings: Settings, redis, app_id: str, resource_id: str, period: str) -> dict:
    payable = paid = Decimal("0")
    offset, expected_total = 0, None
    seen_pages = set()
    for _ in range(MAX_PAGES):
        result = await _page(
            client,
            settings,
            {
                "BillPeriod": period,
                "GroupPeriod": 0,
                "Offset": offset,
                "Limit": PAGE_SIZE,
                "NeedRecordNum": 1,
                "IgnoreZero": 0,
                "Product": [settings.billing_product_code],
                "SplitDimension": json.dumps({settings.billing_product_code: "apikey_id"}, separators=(",", ":")),
                "SplitItemID": resource_id,
            },
            redis,
        )
        rows, total = result.get("List"), result.get("Total")
        if not isinstance(rows, list) or type(total) is not int or total < 0 or len(rows) > PAGE_SIZE:
            raise BillingError("billing_incomplete")
        if expected_total is not None and total != expected_total:
            raise BillingError("billing_incomplete")
        expected_total = total
        fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).digest()
        if rows and fingerprint in seen_pages:
            raise BillingError("billing_incomplete")
        seen_pages.add(fingerprint)
        for row in rows:
            # 再次验证官方返回的产品资源、账期和币种，绝不透出其他 Key 的数据。
            if (
                not isinstance(row, dict)
                or row.get("Product") != settings.billing_product_code
                or row.get("SplitItemID") != resource_id
                or row.get("BillPeriod") != period
                or row.get("Currency") != "CNY"
            ):
                raise BillingError("billing_incomplete")
            with localcontext() as context:
                context.prec = 50
                payable += _money(row.get("PayableAmount"))
                paid += _money(row.get("PaidAmount"))
        offset += len(rows)
        if offset > total or (len(rows) < PAGE_SIZE and offset != total):
            raise BillingError("billing_incomplete")
        if offset == total:
            return {
                "app_id": app_id,
                "ark_resource_id": resource_id,
                "bill_period": period,
                "cost_kind": "official_split_bill",
                "scope": "ark_api_key",
                "product_code": settings.billing_product_code,
                "currency": "CNY",
                "status": "available" if total else "no_data",
                "record_count": total,
                "payable_amount_cny": format(payable, "f") if total else None,
                "paid_amount_cny": format(paid, "f") if total else None,
                "expected_delay_days": 2,
                "settlement_finality": "not_asserted",
                "queried_at": datetime.now(UTC).isoformat(),
                "cached": False,
            }
    # 达到分页/总时限边界时整个查询失败，不发布部分金额。
    raise BillingError("billing_incomplete")


async def _unlock(redis, token: str) -> None:
    for _ in range(3):
        async with redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(PREFIX + "lock")
                if await pipe.get(PREFIX + "lock") != token:
                    return
                pipe.multi()
                pipe.delete(PREFIX + "lock")
                await pipe.execute()
                return
            except WatchError:
                continue


async def official_billing(settings: Settings, caller: CallerIdentity, redis, period: str, *, http_client=None) -> dict:
    product = settings.products.get(caller.app_id)
    if (
        not settings.billing_enabled
        or not settings.billing_access_key.get_secret_value().strip()
        or not settings.billing_secret_key.get_secret_value().strip()
        or product is None
        or not product.ark_resource_id
    ):
        raise BillingError("billing_not_configured")
    validate_period(period)
    # 凭证账号、产品、资源及账期均参与缓存键。仅缓存白名单汇总，不保存原始账单。
    scope = [
        settings.billing_access_key.get_secret_value(),
        settings.billing_region,
        settings.billing_product_code,
        caller.app_id,
        product.ark_resource_id,
        period,
    ]
    cache_key = PREFIX + "report:" + hashlib.sha256(json.dumps(scope).encode()).hexdigest()
    token = secrets.token_hex(16)
    attempted_lock = False
    try:
        with anyio.fail_after(TIMEOUT_SECONDS):
            cached = await redis.get(cache_key)
            if cached:
                return {**json.loads(cached), "cached": True}
            attempted_lock = True
            if not await redis.set(PREFIX + "lock", token, nx=True, ex=math.ceil(TIMEOUT_SECONDS) + 5):
                raise BillingError("billing_busy", 1)
            # 防止与刚完成同一查询的请求重复取账单。
            cached = await redis.get(cache_key)
            if cached:
                return {**json.loads(cached), "cached": True}
            if http_client is None:
                async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                    report = await _collect(client, settings, redis, caller.app_id, product.ark_resource_id, period)
            else:
                report = await _collect(http_client, settings, redis, caller.app_id, product.ark_resource_id, period)
            await redis.set(cache_key, json.dumps(report), ex=CACHE_SECONDS)
            return report
    except BillingError:
        raise
    except Exception as exc:
        logger.warning("official_billing_failed", app_id=caller.app_id, error_type=type(exc).__name__)
        raise BillingError("billing_unavailable") from None
    finally:
        if attempted_lock:
            try:
                with anyio.move_on_after(0.2, shield=True):
                    await _unlock(redis, token)
            except Exception:
                logger.warning("billing_lock_release_failed", app_id=caller.app_id)
