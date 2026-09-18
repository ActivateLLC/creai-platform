"""
Taking money for the businesses Creai builds for.

A customer connects their own Stripe account. Their buyers pay them directly:
the money never sits in Creai's balance, and the customer owns the disputes,
the refunds and the tax position, as they should. Creai takes an application fee
on each charge — revenue that scales with their success rather than a cost that
scales with their traffic.

That is Stripe Connect with direct charges. The alternative, holding the money
and paying out, would make Creai a payment facilitator with the liability that
carries. This way the customer's name is on the statement, which is also what a
buyer expects to see.
"""

import logging

from ..core.config import settings
from ..core.db import conn
from .billing import BillingError, _stripe

log = logging.getLogger("creai.payments")

# Creai's cut, in basis points, taken from each charge. 2% alongside Stripe's own
# fee leaves the seller with the large majority and is well under what a booking
# or storefront platform normally charges.
FEE_BPS = 200
MIN_CHARGE = 50                    # 50 cents; below this the fees swallow it
MAX_CHARGE = 2_000_000             # $20,000, so a typo can't become a disaster


def configured() -> bool:
    return settings.stripe_keys_ok()


def fee_for(amount: int) -> int:
    """What Creai takes from a charge, never more than the charge itself."""
    return max(0, min(amount - 1, amount * FEE_BPS // 10_000))


async def self_check() -> str:
    """Prove Connect is actually enabled on this platform account, rather than
    assuming it because a key exists. A read-only list is enough: if Connect is
    off, Stripe refuses it, and we would otherwise find out when a customer
    pressed Connect and got a stack trace."""
    out = await _stripe("GET", "/accounts?limit=1")
    n = len(out.get("data") or [])
    return f"ok ({n} connected account{'' if n == 1 else 's'} visible)"


async def account_for(org_id: int, project_id: int) -> dict | None:
    async with conn() as c:
        r = await c.fetchrow(
            """SELECT * FROM payment_accounts WHERE org_id=$1 AND project_id=$2""",
            org_id, project_id)
    return dict(r) if r else None


async def connect(org_id: int, project_id: int, email: str, return_url: str) -> dict:
    """Start or resume connecting the customer's own Stripe account. Returns the
    hosted onboarding link — Stripe collects the bank details and the identity
    checks, never Creai."""
    if not configured():
        raise BillingError("payments aren't switched on yet")
    row = await account_for(org_id, project_id)
    acct = row["stripe_account"] if row else None
    if not acct:
        made = await _stripe("POST", "/accounts", {
            "type": "standard", "email": email,
            "metadata": {"app": "creai", "org_id": org_id, "project_id": project_id}})
        acct = made["id"]
        async with conn() as c:
            await c.execute(
                """INSERT INTO payment_accounts (org_id, project_id, stripe_account)
                   VALUES ($1,$2,$3)
                   ON CONFLICT (project_id) DO UPDATE SET stripe_account=EXCLUDED.stripe_account""",
                org_id, project_id, acct)
    link = await _stripe("POST", "/account_links", {
        "account": acct, "type": "account_onboarding",
        "refresh_url": return_url, "return_url": return_url})
    return {"account": acct, "url": link["url"]}


async def refresh(org_id: int, project_id: int) -> dict:
    """Ask Stripe whether this account can actually charge yet. Onboarding can be
    abandoned halfway, and a half-connected account looks connected from here."""
    row = await account_for(org_id, project_id)
    if not row:
        return {"connected": False, "ready": False}
    acct = await _stripe("GET", f"/accounts/{row['stripe_account']}")
    ready = bool(acct.get("charges_enabled"))
    async with conn() as c:
        await c.execute(
            "UPDATE payment_accounts SET ready=$1, checked_at=now() WHERE project_id=$2",
            ready, project_id)
    return {"connected": True, "ready": ready,
            "needs": (acct.get("requirements") or {}).get("currently_due") or [],
            "account": row["stripe_account"]}


async def checkout(org_id: int, project_id: int, *, amount: int, currency: str,
                   label: str, success_url: str, cancel_url: str,
                   app_user_id: int | None = None, reference: str = "") -> dict:
    """A payment page for one thing, charged on the customer's account with Creai's
    fee taken from it."""
    row = await account_for(org_id, project_id)
    if not row or not row["ready"]:
        raise BillingError("this business hasn't finished connecting its payment account yet")
    amount = int(amount)
    if amount < MIN_CHARGE or amount > MAX_CHARGE:
        raise BillingError(f"the amount must be between {MIN_CHARGE} and {MAX_CHARGE} cents")
    currency = (currency or "usd").lower()[:3]

    session = await _stripe("POST", "/checkout/sessions", {
        "mode": "payment",
        "line_items": [{"quantity": 1, "price_data": {
            "currency": currency, "unit_amount": amount,
            "product_data": {"name": (label or "Payment")[:120]}}}],
        "payment_intent_data": {"application_fee_amount": fee_for(amount)},
        "success_url": success_url, "cancel_url": cancel_url,
        "metadata": {"app": "creai", "project_id": project_id,
                     "app_user_id": app_user_id or "", "reference": reference[:80]},
    }, stripe_account=row["stripe_account"])

    async with conn() as c:
        await c.execute(
            """INSERT INTO payments (org_id, project_id, app_user_id, session_id, amount,
                                     currency, fee, label, reference, status)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'open')""",
            org_id, project_id, app_user_id, session["id"], amount, currency,
            fee_for(amount), (label or "")[:120], reference[:80])
    return {"id": session["id"], "url": session["url"], "amount": amount,
            "currency": currency, "fee": fee_for(amount)}


async def settle(session_id: str, project_id: int) -> dict | None:
    """Ask Stripe whether a payment went through. Used when the buyer comes back,
    so a paid thing shows as paid without waiting on a webhook."""
    row = await account_for_session(session_id)
    if not row:
        return None
    s = await _stripe("GET", f"/checkout/sessions/{session_id}",
                      stripe_account=row["stripe_account"])
    paid = s.get("payment_status") == "paid"
    async with conn() as c:
        await c.execute(
            "UPDATE payments SET status=$1, settled_at=now() WHERE session_id=$2",
            "paid" if paid else "open", session_id)
    return {"paid": paid, "amount": s.get("amount_total"), "currency": s.get("currency")}


async def account_for_session(session_id: str) -> dict | None:
    async with conn() as c:
        r = await c.fetchrow(
            """SELECT a.* FROM payments p JOIN payment_accounts a ON a.project_id = p.project_id
               WHERE p.session_id = $1""", session_id)
    return dict(r) if r else None


async def listing(org_id: int, project_id: int, limit: int = 100) -> list[dict]:
    """What the owner has taken. Amounts in cents, as Stripe reports them."""
    async with conn() as c:
        rows = await c.fetch(
            """SELECT amount, currency, fee, label, reference, status, created_at
               FROM payments WHERE org_id=$1 AND project_id=$2 ORDER BY id DESC LIMIT $3""",
            org_id, project_id, limit)
    return [{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows]
