"""
Plans: the recurring side of Creai.

  free    a Creai address and credits for building — nothing added to the site
  launch  own domain hosted, monthly credits; yearly includes a domain
  growth  launch + scheduled social publishing, more credits

Money rules:
  * State comes only from verified Stripe webhooks, never from the browser.
  * Monthly credits are granted once per paid invoice (monthly) or once per month
    of a paid year (yearly), keyed so they can never be granted twice.
  * A lapsed plan never takes a site offline: custom domains fall back to the
    free Creai address.
  * Customers are told before renewals, and can cancel in one tap (Stripe portal).
"""

import math
from datetime import datetime, timezone

from ..core.config import settings
from ..core.db import conn
from . import billing

ALL = ("free", "launch", "growth")

PLANS = {
    "free": {"name": "Free", "monthly": 0, "yearly": 0, "credits": 0,
             "features": ["Free Creai address", "Build sites and apps with credits",
                          "No badge on your site"]},
    "launch": {"name": "Launch", "monthly": 1200, "yearly": 12000, "credits": 1000,
               "features": ["Your own domain, hosted with HTTPS",
                            "1,000 credits every month", "Yearly: first-year domain included",
                            "Domain renewals included on yearly"]},
    "growth": {"name": "Growth", "monthly": 3500, "yearly": 35000, "credits": 3000,
               "features": ["Everything in Launch", "Scheduled posting to your social accounts",
                            "3,000 credits every month", "Priority help"]},
}
PAID = ("launch", "growth")
INTERVALS = {"monthly": "month", "yearly": "year"}
ACTIVE = ("active", "trialing", "past_due")          # past_due keeps access while Stripe retries

# Domains: what a customer pays outside an included one. Retail is in line with
# mainstream builders; renewals included while a yearly plan is active.
INCLUDED_DOMAIN_MAX_USD = 15.00


def retail_credits(cost_usd: float) -> int:
    return math.ceil(max(cost_usd * 2, cost_usd + 10) * 100)


def lookup_key(plan: str, interval: str) -> str:
    return f"creai_{plan}_{interval}"


# ---------------------------------------------------------------- state

async def current(org_id: int) -> dict:
    async with conn() as c:
        s = await c.fetchrow("SELECT * FROM subscriptions WHERE org_id=$1", org_id)
    if not s or s["status"] not in ACTIVE or s["plan"] not in PAID:
        return {"plan": "free", "interval": None, "status": s["status"] if s else None,
                "renews_at": None, "cancel_at_period_end": False, "domain_included": False}
    return {"plan": s["plan"], "interval": s["interval"], "status": s["status"],
            "renews_at": s["current_period_end"].isoformat() if s["current_period_end"] else None,
            "cancel_at_period_end": s["cancel_at_period_end"],
            "domain_included": s["interval"] == "yearly" and not s["domain_claimed"]}


async def has(org_id: int, feature: str) -> bool:
    plan = (await current(org_id))["plan"]
    # "no_badge" was here when free sites carried a "Made with Creai" mark. The
    # badge is gone, so every plan has it; kept as a name so any old caller still
    # gets a true answer rather than a KeyError.
    need = {"custom_domain": PAID, "no_badge": ALL, "social_publish": ("growth",)}[feature]
    return plan in need


async def claim_included_domain(org_id: int) -> bool:
    """Use the yearly plan's included domain, once per subscription year."""
    async with conn() as c:
        row = await c.fetchrow(
            """UPDATE subscriptions SET domain_claimed=true
               WHERE org_id=$1 AND interval='yearly' AND plan = ANY($2::text[])
                 AND status = ANY($3::text[]) AND NOT domain_claimed RETURNING org_id""",
            org_id, list(PAID), list(ACTIVE))
    return row is not None


async def release_included_domain(org_id: int) -> None:
    async with conn() as c:
        await c.execute("UPDATE subscriptions SET domain_claimed=false WHERE org_id=$1", org_id)


async def renewals_included(org_id: int) -> bool:
    cur = await current(org_id)
    return cur["plan"] in PAID and cur["interval"] == "yearly"


# ---------------------------------------------------------------- Stripe

async def _price_id(plan: str, interval: str) -> str:
    """The Stripe price for a plan, created once (found by lookup key afterwards)."""
    key = lookup_key(plan, interval)
    found = await billing._stripe("GET", f"/prices?lookup_keys%5B%5D={key}&active=true")
    if found.get("data"):
        return found["data"][0]["id"]
    p = PLANS[plan]
    product = await billing._stripe("POST", "/products", {
        "name": f"Creai {p['name']}", "metadata": {"app": "creai", "plan": plan}})
    price = await billing._stripe("POST", "/prices", {
        "product": product["id"], "currency": "usd", "unit_amount": p[interval],
        "recurring": {"interval": INTERVALS[interval]}, "lookup_key": key,
        "metadata": {"app": "creai", "plan": plan, "interval": interval}})
    return price["id"]


async def _customer(org_id: int, email: str) -> str:
    async with conn() as c:
        cid = await c.fetchval("SELECT stripe_customer FROM subscriptions WHERE org_id=$1", org_id)
    if cid:
        return cid
    cust = await billing._stripe("POST", "/customers", {
        "email": email, "metadata": {"app": "creai", "org_id": org_id}})
    async with conn() as c:
        await c.execute(
            """INSERT INTO subscriptions (org_id, stripe_customer, plan, status)
               VALUES ($1,$2,'free','none')
               ON CONFLICT (org_id) DO UPDATE SET stripe_customer=EXCLUDED.stripe_customer""",
            org_id, cust["id"])
    return cust["id"]


def _require_stripe():
    if settings.missing_for("billing"):
        raise billing.BillingError("payments are not switched on yet")


async def checkout(org_id: int, user_id: int, email: str, plan: str, interval: str) -> str:
    if plan not in PAID or interval not in INTERVALS:
        raise billing.BillingError("unknown plan")
    _require_stripe()
    cur = await current(org_id)
    if cur["plan"] in PAID:
        raise billing.BillingError("you already have a plan; change it from Manage plan")
    base = settings.public_url.rstrip("/")
    session = await billing._stripe("POST", "/checkout/sessions", {
        "mode": "subscription",
        "customer": await _customer(org_id, email),
        "line_items": [{"price": await _price_id(plan, interval), "quantity": 1}],
        "success_url": f"{base}/?plan=started",
        "cancel_url": f"{base}/?plan=cancelled",
        "allow_promotion_codes": "true",
        "client_reference_id": str(org_id),
        "subscription_data": {"metadata": {"app": "creai", "org_id": org_id, "plan": plan,
                                           "interval": interval, "user_id": user_id}},
    })
    return session["url"]


_portal_config: str | None = None


async def _portal_configuration() -> str:
    """Creai's own portal settings (the account's default may serve other products).
    Found by metadata, created once if missing."""
    global _portal_config
    if _portal_config:
        return _portal_config
    found = await billing._stripe("GET", "/billing_portal/configurations?active=true&limit=100")
    for cfg in found.get("data") or []:
        if (cfg.get("metadata") or {}).get("app") == "creai":
            _portal_config = cfg["id"]
            return _portal_config
    products = []
    for plan in PAID:
        ids = [await _price_id(plan, i) for i in INTERVALS]
        price = await billing._stripe("GET", f"/prices/{ids[0]}")
        products.append({"product": price["product"], "prices": ids})
    base = settings.public_url.rstrip("/")
    cfg = await billing._stripe("POST", "/billing_portal/configurations", {
        "business_profile": {"headline": "Creai — manage your plan",
                             "privacy_policy_url": "https://www.creai.dev/privacy",
                             "terms_of_service_url": "https://www.creai.dev/terms"},
        "default_return_url": f"{base}/?plan=managed",
        "metadata": {"app": "creai"},
        "features": {
            "invoice_history": {"enabled": True},
            "payment_method_update": {"enabled": True},
            "customer_update": {"enabled": True, "allowed_updates": ["email", "address", "tax_id"]},
            "subscription_cancel": {"enabled": True, "mode": "at_period_end",
                                    "cancellation_reason": {"enabled": True, "options": [
                                        "too_expensive", "missing_features", "switched_service", "unused", "other"]}},
            "subscription_update": {"enabled": True, "default_allowed_updates": ["price"],
                                    "proration_behavior": "create_prorations", "products": products},
        }})
    _portal_config = cfg["id"]
    return _portal_config


async def portal(org_id: int, email: str) -> str:
    """Stripe's hosted page: cancel in one tap, change card, switch plan, invoices."""
    _require_stripe()
    params = {"customer": await _customer(org_id, email),
              "return_url": settings.public_url.rstrip("/") + "/?plan=managed"}
    try:
        params["configuration"] = await _portal_configuration()
    except billing.BillingError:
        pass                    # fall back to the account default rather than blocking cancellation
    session = await billing._stripe("POST", "/billing_portal/sessions", params)
    return session["url"]


def _ts(v) -> datetime | None:
    return datetime.fromtimestamp(int(v), tz=timezone.utc) if v else None


async def apply_subscription(sub: dict) -> int | None:
    """customer.subscription.created/updated/deleted → our record. Returns org_id."""
    meta = sub.get("metadata") or {}
    if meta.get("app") != "creai":
        return None
    try:
        org_id = int(meta["org_id"])
    except (KeyError, ValueError):
        return None
    items = ((sub.get("items") or {}).get("data") or [{}])
    price = items[0].get("price") or {}
    pm = price.get("metadata") or {}
    plan = pm.get("plan") or meta.get("plan")
    interval = pm.get("interval") or meta.get("interval")
    if plan not in PAID or interval not in INTERVALS:
        return None
    period_end = sub.get("current_period_end") or items[0].get("current_period_end")
    status = sub.get("status", "incomplete")
    async with conn() as c:
        prev = await c.fetchrow("SELECT stripe_subscription, current_period_end FROM subscriptions WHERE org_id=$1", org_id)
        new_year = bool(prev and prev["current_period_end"] and period_end
                        and _ts(period_end) > prev["current_period_end"] and interval == "yearly")
        await c.execute(
            """INSERT INTO subscriptions (org_id, stripe_customer, stripe_subscription, plan, interval, status,
                                          current_period_end, cancel_at_period_end, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8, now())
               ON CONFLICT (org_id) DO UPDATE SET
                 stripe_customer=EXCLUDED.stripe_customer, stripe_subscription=EXCLUDED.stripe_subscription,
                 plan=EXCLUDED.plan, interval=EXCLUDED.interval, status=EXCLUDED.status,
                 current_period_end=EXCLUDED.current_period_end,
                 cancel_at_period_end=EXCLUDED.cancel_at_period_end, updated_at=now(),
                 domain_claimed = CASE WHEN $9 THEN false ELSE subscriptions.domain_claimed END""",
            org_id, sub.get("customer"), sub.get("id"), plan, interval,
            "canceled" if sub.get("_deleted") else status,
            _ts(period_end), bool(sub.get("cancel_at_period_end")), new_year)
    return org_id


async def grant_for_invoice(invoice: dict) -> int:
    """invoice.paid → monthly credits. Yearly invoices grant the first month now;
    the monthly sweep grants the rest. Returns credits granted."""
    lines = ((invoice.get("lines") or {}).get("data") or [])
    meta = {}
    for ln in lines:
        price = ln.get("price") or ((ln.get("pricing") or {}).get("price_details") or {})
        meta = (ln.get("metadata") or {}) or (price.get("metadata") or {})
        if meta.get("plan"):
            break
    sub_details = (invoice.get("subscription_details")
                   or ((invoice.get("parent") or {}).get("subscription_details")) or {})
    sub_meta = sub_details.get("metadata") or {}
    plan = meta.get("plan") or sub_meta.get("plan")
    org = sub_meta.get("org_id") or meta.get("org_id")
    if plan not in PAID or not org or invoice.get("status") != "paid":
        return 0
    org_id = int(org)
    period_start = (lines[0].get("period") or {}).get("start") if lines else None
    month = _ts(period_start or invoice.get("created")).strftime("%Y-%m")
    return await _grant_month(org_id, plan, month, invoice.get("id"))


async def _grant_month(org_id: int, plan: str, month: str, source: str | None) -> int:
    credits = PLANS[plan]["credits"]
    async with conn() as c:
        row = await c.fetchrow(
            """INSERT INTO credit_ledger (org_id, delta, reason, ref, detail)
               VALUES ($1,$2,'plan',$3,$4) ON CONFLICT (ref) DO NOTHING RETURNING id""",
            org_id, credits, f"plan:{org_id}:{month}", {"plan": plan, "month": month, "source": source})
    return credits if row else 0


async def monthly_sweep() -> int:
    """Yearly subscribers receive their credits every month of the paid year."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    n = 0
    async with conn() as c:
        rows = await c.fetch(
            """SELECT org_id, plan FROM subscriptions WHERE interval='yearly'
               AND status = ANY($1::text[]) AND plan = ANY($2::text[])
               AND current_period_end > now()""", list(ACTIVE), list(PAID))
    for r in rows:
        if await _grant_month(r["org_id"], r["plan"], month, "yearly"):
            n += 1
    return n


async def remind_upcoming(invoice: dict) -> str | None:
    """invoice.upcoming → a heads-up email before any renewal charge."""
    from . import mailer
    email = invoice.get("customer_email")
    amount = (invoice.get("amount_due") or 0) / 100
    when = _ts(invoice.get("next_payment_attempt") or invoice.get("period_end"))
    if not email or not when:
        return None
    body = (f"Your Creai plan renews on {when:%B %d, %Y} for ${amount:,.2f}.\n\n"
            f"Nothing to do if you'd like to keep it. To change or cancel, open Creai, go to "
            f"Credits → Manage plan. It takes one tap.\n")
    import asyncio
    await asyncio.get_running_loop().run_in_executor(
        None, mailer.send_notice, email, "Your Creai plan renews soon", body)
    return email
