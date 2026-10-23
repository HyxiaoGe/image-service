from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from src.services.billing import BillingError, official_billing, validate_period

router = APIRouter()


@router.get("/billing")
async def billing(request: Request, bill_period: str = Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")):
    """产品服务密钥只能查询自身方舟 Key 的官方月度分账汇总。"""
    headers = {"Cache-Control": "private, no-store", "Vary": "Authorization"}
    try:
        validate_period(bill_period)
        report = await official_billing(
            request.app.state.settings,
            request.state.image_caller,
            request.app.state.redis,
            bill_period,
            http_client=getattr(request.app.state, "billing_http_client", None),
        )
        return JSONResponse(report, headers=headers)
    except BillingError as exc:
        if exc.retry_after:
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse(
            {"error": exc.code, "message": exc.message, "retryable": exc.retryable},
            status_code=exc.status,
            headers=headers,
        )
