"""Plans: catalog, upgrade (Stripe Checkout) and one-tap manage/cancel (Stripe portal)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..core import tenancy as T
from ..core.config import settings
from ..core.db import log_event
from ..services import billing, plans

router = APIRouter(prefix="/v1/plans", tags=["plans"])


class CheckoutIn(BaseModel):
    plan: str
    interval: str = "yearly"


@router.get("")
async def catalog(ctx: T.Ctx = Depends(T.current_ctx)):
    return {"current": await plans.current(ctx.org_id),
            "plans": [{"id": k, **v} for k, v in plans.PLANS.items()],
            "can_buy": ctx.may("billing"),
            "payments_enabled": not settings.missing_for("billing")}


@router.post("/checkout")
async def checkout(body: CheckoutIn, ctx: T.Ctx = Depends(T.requires("billing"))):
    try:
        url = await plans.checkout(ctx.org_id, ctx.user_id, ctx.email, body.plan, body.interval)
    except billing.BillingError as exc:
        raise HTTPException(400 if "plan" in str(exc) else 503, str(exc))
    await log_event(ctx.org_id, "plan.checkout", f"{body.plan}/{body.interval}", None, ctx.user_id)
    return {"url": url}


@router.post("/portal")
async def portal(ctx: T.Ctx = Depends(T.requires("billing"))):
    try:
        return {"url": await plans.portal(ctx.org_id, ctx.email)}
    except billing.BillingError as exc:
        raise HTTPException(503, str(exc))
