"""
Buying domains for customers, through Cloudflare Registrar (API beta).

Cloudflare sells at cost and charges CreAI's account, so CreAI takes payment
first (in credits, at cost plus a small fee), registers the domain in the
customer's own name, and refunds the credits if the registration doesn't go
through. Registrations are non-refundable once they succeed, so nothing is
bought without an explicit, priced confirmation.
"""

import asyncio
import math

import httpx

from ..core.config import settings

API = "https://api.cloudflare.com/client/v4"
# Retail: about what mainstream builders charge (a .com lands near $20/year).
RETAIL_MULTIPLE = 2.0
RETAIL_MIN_MARKUP_USD = 10.00
POLL = 2.0
POLL_LIMIT = 60


class RegistrarError(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.cloudflare_token and settings.cloudflare_account_id)


def credits_for(cost_usd: float) -> int:
    return math.ceil(round(max(cost_usd * RETAIL_MULTIPLE, cost_usd + RETAIL_MIN_MARKUP_USD) * 100, 6))


def _url(path: str) -> str:
    return f"{API}/accounts/{settings.cloudflare_account_id}/registrar{path}"


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.cloudflare_token}", "Content-Type": "application/json"}


def _shape(d: dict) -> dict:
    pricing = d.get("pricing") or {}
    cost = float(pricing.get("registration_cost") or 0)
    renew = float(pricing.get("renewal_cost") or 0)
    ok = bool(d.get("registrable")) and d.get("tier", "standard") == "standard" and cost > 0
    return {"domain": d.get("name"), "available": ok,
            "reason": None if ok else (d.get("reason") or ("premium" if d.get("tier") == "premium" else "unavailable")),
            "cost_usd": cost, "renewal_usd": renew,
            "credits": credits_for(cost) if ok else None,
            "renewal_credits": credits_for(renew) if ok and renew else None}


async def _call(method: str, path: str, **kw) -> httpx.Response:
    if not configured():
        raise RegistrarError("domain registration isn't switched on yet")
    async with httpx.AsyncClient(timeout=40) as x:
        return await x.request(method, _url(path), headers={**_headers(), **kw.pop("headers", {})}, **kw)


async def search(q: str, limit: int = 8) -> list[dict]:
    r = await _call("GET", "/domain-search", params={"q": q[:63], "limit": limit})
    data = r.json()
    if not data.get("success"):
        detail = "; ".join(e.get("message", "") for e in data.get("errors") or [])
        raise RegistrarError("domain search is unavailable right now" + (f" ({detail})" if detail else ""))
    return [_shape(d) for d in (data.get("result") or {}).get("domains", [])]


async def check(domain: str) -> dict:
    """Authoritative availability and price, straight from the registry."""
    r = await _call("POST", "/domain-check", json={"domains": [domain]})
    data = r.json()
    if not data.get("success") or not (data.get("result") or {}).get("domains"):
        raise RegistrarError("couldn't check that domain right now")
    return _shape(data["result"]["domains"][0])


def contact_payload(c: dict) -> dict:
    return {"registrant": {
        "email": c["email"], "phone": c["phone"],
        "postal_info": {"name": c["name"], "organization": c.get("organization") or "",
                        "address": {"street": c["street"], "city": c["city"], "state": c.get("state") or "",
                                    "postal_code": c["postal_code"], "country_code": c["country_code"]}}}}


async def register(domain: str, contact: dict) -> dict:
    """Register and wait for a terminal state. Returns the registration context."""
    body = {"domain_name": domain, "contacts": contact_payload(contact), "auto_renew": True}
    r = await _call("POST", "/registrations", json=body)
    data = r.json()
    if r.status_code == 400 and "auto_renew" in str(data.get("errors")).lower():
        body.pop("auto_renew")          # beta may not accept it yet; renewal is then handled by staff
        r = await _call("POST", "/registrations", json=body)
        data = r.json()
    if r.status_code >= 400 or not data.get("success"):
        msg = "; ".join(e.get("message", "") for e in data.get("errors") or []) or f"status {r.status_code}"
        raise RegistrarError(f"registration was refused: {msg}")
    result = data["result"]
    for _ in range(POLL_LIMIT):
        state = result.get("state")
        if state == "succeeded":
            return (result.get("context") or {}).get("registration") or {}
        if state in ("failed", "blocked"):
            raise RegistrarError(f"registration {state}: {(result.get('error') or {}).get('message', '')}")
        if state == "action_required":
            raise RegistrarError("the registry needs an extra step for this domain; our team will follow up")
        await asyncio.sleep(POLL)
        s = await _call("GET", f"/registrations/{domain}/registration-status")
        result = s.json().get("result") or {}
    raise RegistrarError("registration is still in progress; we'll keep checking")


# ---------------------------------------------------------------- renewals

async def renewal_sweep() -> dict:
    """Charge renewal credits for domains within a day of renewing, once per term.
    The registrar renews on CreAI's account; this keeps the customer's side square."""
    from datetime import timedelta
    from ..core.db import conn, log_event
    from . import billing
    charged = short = 0
    async with conn() as c:
        due = await c.fetch(
            """SELECT id, org_id, name, expires_at, renewal_credits FROM domains
               WHERE source='registered' AND expires_at IS NOT NULL AND renewal_credits IS NOT NULL
                 AND expires_at < now() + interval '1 day'""")
    from . import plans
    for d in due:
        term = d["expires_at"].isoformat()
        ref = f"renew:{d['id']}:{term}"
        if await plans.renewals_included(d["org_id"]):
            async with conn() as c:
                await c.execute(
                    """INSERT INTO credit_ledger (org_id, delta, reason, ref, detail)
                       VALUES ($1, 0, 'waived', $2, $3) ON CONFLICT (ref) DO NOTHING""",
                    d["org_id"], ref, {"domain": d["name"], "credits": d["renewal_credits"],
                                       "waived": "domain renewal included in your yearly plan"})
            ok = True
        else:
            ok = await billing.spend(d["org_id"], None, d["renewal_credits"], "domain",
                                     ref, {"domain": d["name"], "renewal": term})
        if ok:
            async with conn() as c:
                await c.execute("UPDATE domains SET expires_at = expires_at + interval '1 year' WHERE id=$1", d["id"])
            await log_event(d["org_id"], "domain.renewed", d["name"])
            charged += 1
        else:
            await log_event(d["org_id"], "domain.renewal_unpaid", d["name"])
            short += 1
    return {"charged": charged, "short": short}
