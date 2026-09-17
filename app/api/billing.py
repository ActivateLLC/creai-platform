"""
Credits: balance, packs, in-app payment and the payment webhook.

Only the webhook adds credits. The browser saying a payment went through proves
nothing, so the app waits for the balance to change.
"""

import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from ..core import tenancy as T
from ..core.config import settings
from ..core.db import log_event
from ..services import billing

router = APIRouter(prefix="/v1/billing", tags=["billing"])
log = logging.getLogger("creai.billing")


class CheckoutIn(BaseModel):
    pack: str


def _packs() -> list[dict]:
    return [{"id": k, **v, "price": f"${v['price_cents'] // 100}"}
            for k, v in billing.PACKS.items()]


@router.get("")
async def summary(ctx: T.Ctx = Depends(T.current_ctx)):
    await billing.ensure_signup_grant(ctx.org_id)
    return {"balance": await billing.balance(ctx.org_id),
            "packs": _packs(),
            "can_buy": ctx.may("billing"),
            "payments_enabled": not settings.missing_for("billing"),
            "history": await billing.history(ctx.org_id)}


@router.post("/pay")
async def pay(body: CheckoutIn, ctx: T.Ctx = Depends(T.requires("billing"))):
    try:
        out = await billing.create_payment(ctx.org_id, ctx.user_id, ctx.email, body.pack)
    except billing.BillingError as exc:
        raise HTTPException(400 if "pack" in str(exc) else 503, str(exc))
    await log_event(ctx.org_id, "billing.payment_started", body.pack, None, ctx.user_id)
    return out


@router.post("/domains", include_in_schema=False)
async def register_domain(admin=Depends(T.platform_admin)):
    """Staff: register this deployment's host for Apple Pay / Google Pay."""
    from urllib.parse import urlparse
    host = urlparse(settings.public_url).hostname
    try:
        out = await billing.register_domain(host)
    except billing.BillingError as exc:
        raise HTTPException(400, str(exc))
    await T.log_admin(admin["user_id"], "billing.register_domain", admin["reason"])
    return out


@router.post("/webhook", include_in_schema=False)
async def webhook(request: Request, stripe_signature: str | None = Header(None)):
    payload = await request.body()
    try:
        billing.verify_webhook(payload, stripe_signature, settings.stripe_webhook_secret)
    except billing.BillingError as exc:
        log.warning("rejected stripe webhook: %s", exc)
        raise HTTPException(400, "invalid signature")

    event = json.loads(payload)
    kind = event.get("type")
    if kind == "payment_intent.succeeded":
        intent = (event.get("data") or {}).get("object") or {}
        if await billing.credit_purchase(intent):
            meta = intent.get("metadata") or {}
            await log_event(int(meta["org_id"]), "billing.credited", meta.get("pack", ""))
    obj = (event.get("data") or {}).get("object") or {}
    if kind in ("customer.subscription.created", "customer.subscription.updated",
                "customer.subscription.deleted"):
        from ..services import plans
        if kind.endswith("deleted"):
            obj = {**obj, "_deleted": True}
        org = await plans.apply_subscription(obj)
        if org:
            await log_event(org, "plan." + kind.rsplit(".", 1)[1], obj.get("status", ""))
    elif kind == "invoice.paid":
        from ..services import plans
        await plans.grant_for_invoice(obj)
    elif kind == "invoice.upcoming":
        from ..services import plans
        await plans.remind_upcoming(obj)
    # Always 200 for events we have verified, so Stripe stops retrying.
    return {"received": True}


class CapIn(BaseModel):
    monthly_cap: int | None = None


@router.get("/estimate")
async def estimate(mode: str = "best", kind: str = "site", ctx: T.Ctx = Depends(T.current_ctx)):
    if mode not in ("best", "fast") or kind not in ("site", "app", "chat", "market"):
        raise HTTPException(400, "unknown mode or kind")
    return await billing.estimate(ctx.org_id, mode, kind)


@router.get("/cap")
async def get_cap(ctx: T.Ctx = Depends(T.current_ctx)):
    return await billing.cap_status(ctx.org_id)


@router.post("/cap")
async def update_cap(body: CapIn, ctx: T.Ctx = Depends(T.requires("approve"))):
    if body.monthly_cap is not None and not (10 <= body.monthly_cap <= 1_000_000):
        raise HTTPException(400, "the cap must be between 10 and 1,000,000 credits")
    out = await billing.set_cap(ctx.org_id, body.monthly_cap)
    await log_event(ctx.org_id, "billing.cap", str(body.monthly_cap), None, ctx.user_id)
    return out
