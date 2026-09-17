"""
Credits and payments.

Customers buy credit packs inside the app, in a CreAI-styled payment panel
(Stripe's Payment Element underneath: cards, Apple Pay, Google Pay, Klarna).
Card details go straight from the browser to the processor; they never touch
this server. Every agent turn is metered on the tokens it actually used and
deducted from the workspace's balance.

Money rules, enforced here:

  * 1 credit = $0.01 of retail value.
  * A turn costs its real Anthropic cost × MARKUP, rounded up. The margin holds
    whichever model ran.
  * The ledger is append-only. A balance is a sum; nothing is edited in place.
  * Credits are added only by a verified webhook, never by the browser saying
    it paid, and each payment can be credited exactly once.
"""

import hashlib
import hmac
import math
import time
from urllib.parse import urlencode

import httpx

from ..core.config import settings
from ..core.db import conn

MARKUP = 3
SIGNUP_CREDITS = 150
MIN_TO_START = {"best": 25, "fast": 6}     # roughly one turn's worth

# USD per million tokens: input, output, 5-minute cache write, cache read.
# Source: platform.claude.com/docs/en/about-claude/pricing (Sept 2026).
PRICES = {
    "claude-fable-5-1":          (10.0, 50.0, 12.50, 0.25),
    "claude-opus-5":             (5.0, 25.0, 6.25, 0.50),
    "claude-sonnet-5":           (2.0, 10.0, 2.50, 0.20),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 1.25, 0.10),
}
# An unknown model is billed as the most expensive one, never for free.
_FALLBACK = max(PRICES.values(), key=lambda p: p[1])

PACKS = {
    "starter": {"name": "Starter", "price_cents": 1000,  "credits": 1000,  "bonus": 0},
    "builder": {"name": "Builder", "price_cents": 2500,  "credits": 2750,  "bonus": 10},
    "pro":     {"name": "Pro",     "price_cents": 5000,  "credits": 6000,  "bonus": 20},
    "studio":  {"name": "Studio",  "price_cents": 10000, "credits": 13000, "bonus": 30},
}

STRIPE = "https://api.stripe.com/v1"
# Wallets (Apple Pay, Google Pay) ride on "card". Link is deliberately left out
# so the panel stays CreAI-branded.
PAYMENT_METHODS = ["card", "klarna"]
STATEMENT_SUFFIX = "CREDITS"
WEBHOOK_TOLERANCE = 300


class BillingError(RuntimeError):
    pass


# ---------------------------------------------------------------- metering

def usage_cost(model: str, usage: dict) -> float:
    """Dollars for one API call, from the usage block the API returned."""
    i, o, w, r = PRICES.get(model, _FALLBACK)
    u = usage or {}
    return (int(u.get("input_tokens") or 0) * i
            + int(u.get("output_tokens") or 0) * o
            + int(u.get("cache_creation_input_tokens") or 0) * w
            + int(u.get("cache_read_input_tokens") or 0) * r) / 1_000_000


def credits_for(calls: list[tuple[str, dict]]) -> tuple[int, float]:
    """(credits to deduct, raw dollar cost) for a turn's model calls."""
    cost = sum(usage_cost(m, u) for m, u in calls)
    if cost <= 0:
        return 0, 0.0
    return max(1, math.ceil(cost * 100 * MARKUP)), cost


# ---------------------------------------------------------------- ledger

async def ensure_signup_grant(org_id: int) -> None:
    async with conn() as c:
        await c.execute(
            """INSERT INTO credit_ledger (org_id, delta, reason, ref)
               VALUES ($1, $2, 'signup', $3) ON CONFLICT (ref) DO NOTHING""",
            org_id, SIGNUP_CREDITS, f"signup:{org_id}")


async def balance(org_id: int) -> int:
    async with conn() as c:
        return int(await c.fetchval(
            "SELECT COALESCE(SUM(delta), 0) FROM credit_ledger WHERE org_id=$1", org_id))


async def charge_usage(org_id: int, actor_id: int, project_id: int,
                       calls: list[tuple[str, dict]]) -> tuple[int, int]:
    """Deduct a turn's usage. Returns (credits charged, new balance)."""
    credits, cost = credits_for(calls)
    if credits:
        detail = {"project_id": project_id, "cost_usd": round(cost, 6),
                  "models": sorted({m for m, _ in calls})}
        async with conn() as c:
            await c.execute(
                """INSERT INTO credit_ledger (org_id, delta, reason, detail, actor_id)
                   VALUES ($1, $2, 'usage', $3, $4)""",
                org_id, -credits, detail, actor_id)
    return credits, await balance(org_id)


async def history(org_id: int, limit: int = 20) -> list[dict]:
    async with conn() as c:
        rows = await c.fetch(
            """SELECT delta, reason, detail, created_at FROM credit_ledger
               WHERE org_id=$1 ORDER BY created_at DESC, id DESC LIMIT $2""", org_id, limit)
    return [{"delta": r["delta"], "reason": r["reason"],
             "pack": (r["detail"] or {}).get("pack"),
             "at": r["created_at"].isoformat()} for r in rows]


async def credit_purchase(intent: dict) -> bool:
    """Credit a succeeded PaymentIntent. True if newly credited; False if it was
    already credited, not succeeded, or not one of ours."""
    if intent.get("object") != "payment_intent" or intent.get("status") != "succeeded":
        return False
    meta = intent.get("metadata") or {}
    pack = PACKS.get(meta.get("pack", ""))
    try:
        org_id = int(meta.get("org_id", ""))
    except ValueError:
        return False
    if not pack or meta.get("app") != "creai":
        return False
    # What was actually received must cover the pack.
    if int(intent.get("amount_received") or 0) < pack["price_cents"] \
            or intent.get("currency") != "usd":
        return False
    async with conn() as c:
        row = await c.fetchrow(
            """INSERT INTO credit_ledger (org_id, delta, reason, ref, detail)
               VALUES ($1, $2, 'purchase', $3, $4)
               ON CONFLICT (ref) DO NOTHING RETURNING id""",
            org_id, pack["credits"], f"pay:{intent['id']}",
            {"pack": meta.get("pack"), "amount": intent.get("amount_received"),
             "method": (intent.get("payment_method_types") or [None])[0]})
    return row is not None


# ---------------------------------------------------------------- Stripe

def _form(data: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten nested dicts/lists into Stripe's bracketed form encoding."""
    out = []
    for k, v in data.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            out += _form(v, key)
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    out += _form(item, f"{key}[{i}]")
                else:
                    out.append((f"{key}[{i}]", str(item)))
        elif v is not None:
            out.append((key, str(v).lower() if isinstance(v, bool) else str(v)))
    return out


async def _stripe(method: str, path: str, data: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as x:
        r = await x.request(
            method, f"{STRIPE}{path}", content=urlencode(_form(data or {})),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            auth=(settings.stripe_key, ""))
    out = r.json()
    if r.status_code >= 400:
        raise BillingError((out.get("error") or {}).get("message", "payment failed"))
    return out


async def create_payment(org_id: int, user_id: int, email: str, pack_id: str) -> dict:
    """Start an in-app payment for a pack. Returns what the payment panel needs."""
    pack = PACKS.get(pack_id)
    if not pack:
        raise BillingError("unknown pack")
    if settings.missing_for("billing") or not settings.stripe_publishable_key:
        raise BillingError("payments are not switched on yet")
    intent = await _stripe("POST", "/payment_intents", {
        "amount": pack["price_cents"],
        "currency": "usd",
        "payment_method_types": PAYMENT_METHODS,
        "description": f"CreAI {pack['name']} — {pack['credits']:,} credits",
        "receipt_email": email,
        "statement_descriptor_suffix": STATEMENT_SUFFIX,
        "metadata": {"app": "creai", "org_id": org_id, "pack": pack_id,
                     "user_id": user_id, "credits": pack["credits"]},
    })
    return {"client_secret": intent["client_secret"],
            "publishable_key": settings.stripe_publishable_key,
            "amount": pack["price_cents"], "credits": pack["credits"],
            "name": pack["name"]}


async def register_domain(host: str) -> dict:
    """Register a web domain so wallets (Apple Pay, Google Pay) can appear there."""
    try:
        d = await _stripe("POST", "/payment_method_domains", {"domain_name": host})
    except BillingError as exc:
        if "already" not in str(exc).lower():
            raise
        found = await _stripe("GET", f"/payment_method_domains?domain_name={host}")
        d = (found.get("data") or [{}])[0]
        if d.get("id"):
            d = await _stripe("POST", f"/payment_method_domains/{d['id']}/validate")
    return {"domain": d.get("domain_name"), "enabled": d.get("enabled"),
            "apple_pay": (d.get("apple_pay") or {}).get("status"),
            "google_pay": (d.get("google_pay") or {}).get("status")}


def verify_webhook(payload: bytes, header: str | None, secret: str,
                   now: float | None = None) -> None:
    """Stripe's signature scheme: HMAC-SHA256 over "timestamp.payload"."""
    if not secret or not header:
        raise BillingError("missing signature")
    parts = {}
    for item in header.split(","):
        k, _, v = item.partition("=")
        parts.setdefault(k.strip(), []).append(v.strip())
    try:
        ts = int(parts["t"][0])
    except (KeyError, ValueError):
        raise BillingError("bad signature header")
    if abs((now or time.time()) - ts) > WEBHOOK_TOLERANCE:
        raise BillingError("signature too old")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload,
                        hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in parts.get("v1", [])):
        raise BillingError("signature mismatch")
