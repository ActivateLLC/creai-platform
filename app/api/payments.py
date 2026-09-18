"""
Payments: the owner connects their Stripe account, and their app can charge.

Two audiences on one router. The owner connects and reviews takings, signed in to
Creai. The app asks for a payment page, holding only an app token and, usually, a
signed-in person's session.

An app may only charge on its own project's connected account, and the amount is
bounded, so a compromised app token can start a payment the owner can see and
refund — not move money anywhere else.
"""

import logging
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from ..core import tenancy as T
from ..core.config import settings
from ..services import payments
from ..services.billing import BillingError
from .appdata import _allow, _auth, _user

log = logging.getLogger("creai.payments")
router = APIRouter(tags=["payments"])

RATE = (30, 600)                  # per app+address: 30 payment pages per 10 minutes
_hits: dict[str, deque] = defaultdict(deque)


def _limit(key: str) -> None:
    now = time.monotonic()
    q = _hits[key]
    while q and now - q[0] > RATE[1]:
        q.popleft()
    if len(q) >= RATE[0]:
        raise HTTPException(429, "Too many payment attempts. Wait a few minutes.")
    q.append(now)


# ---------------------------------------------------------------- the owner


@router.post("/v1/payments/{project_id}/connect")
async def connect(project_id: int, ctx: T.Ctx = Depends(T.requires("approve"))):
    """Hand back Stripe's own onboarding link. Creai never sees a bank detail."""
    base = settings.public_url.rstrip("/")
    try:
        return await payments.connect(ctx.org_id, project_id, ctx.email or "",
                                      f"{base}/?payments=connected")
    except BillingError as exc:
        raise HTTPException(400, str(exc))


@router.get("/v1/payments/{project_id}")
async def status(project_id: int, ctx: T.Ctx = Depends(T.requires("write"))):
    """Whether this project can actually take money, asked of Stripe rather than
    assumed from a row."""
    try:
        state = await payments.refresh(ctx.org_id, project_id)
    except BillingError as exc:
        raise HTTPException(400, str(exc))
    return {**state, "fee_bps": payments.FEE_BPS,
            "payments": await payments.listing(ctx.org_id, project_id)}


# ---------------------------------------------------------------- the app


class ChargeIn(BaseModel):
    amount: int = Field(ge=payments.MIN_CHARGE, le=payments.MAX_CHARGE,
                        description="in cents")
    label: str = Field(max_length=120)
    currency: str = Field("usd", max_length=3)
    reference: str = Field("", max_length=80)


@router.post("/v1/apppay/{collection}")
async def charge(collection: str, body: ChargeIn, request: Request,
                 x_app_token: str | None = Header(None),
                 x_app_session: str | None = Header(None)):
    """A payment page for a thing in this app. The collection decides who may ask,
    with the same rules as the records — so "own" means a person can only pay for
    their own things."""
    pid, oid, role = _auth(x_app_token, collection)
    uid = _user(pid, x_app_session)
    await _allow(pid, role, collection, "write", uid)
    _limit(f"{pid}:{request.client.host if request.client else '?'}")
    base = settings.public_url.rstrip("/")
    try:
        return await payments.checkout(
            oid, pid, amount=body.amount, currency=body.currency, label=body.label,
            success_url=f"{base}/paid?s={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/paid?cancelled=1",
            app_user_id=uid, reference=body.reference)
    except BillingError as exc:
        raise HTTPException(400, str(exc))


@router.get("/v1/apppay/session/{session_id}")
async def settled(session_id: str, x_app_token: str | None = Header(None)):
    """Did it go through? Asked when the buyer returns, so a paid thing reads as
    paid without waiting on a webhook."""
    pid, _oid, _role = _auth(x_app_token, "payments")
    out = await payments.settle(session_id, pid)
    if out is None:
        raise HTTPException(404, "not found")
    return out
