"""
Domains: register or connect, write records, watch propagation. Phase 0.

Every query is scoped by ctx.org_id. A domain belongs to a workspace, not to the
person who added it.
"""

import re
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from ..core import tenancy as T
from ..core.config import settings
from ..core.db import conn, log_event
from ..services import billing, dns, hosting, registrar, vault

router = APIRouter(prefix="/v1/domains", tags=["domains"])


class ConnectIn(BaseModel):
    name: str = Field(min_length=3, max_length=253)
    project_id: int | None = None


def _clean(name: str) -> str:
    n = name.strip().lower()
    for p in ("https://", "http://"):
        if n.startswith(p):
            n = n[len(p):]
    return n.split("/")[0].removeprefix("www.")


NAME = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
QUOTE_MINUTES = 10


class ContactIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    organization: str = Field("", max_length=100)
    email: EmailStr
    phone: str = Field(pattern=r"^\+[0-9]{1,3}\.[0-9]{4,14}$")
    street: str = Field(min_length=3, max_length=200)
    city: str = Field(min_length=2, max_length=100)
    state: str = Field("", max_length=100)
    postal_code: str = Field(min_length=2, max_length=20)
    country_code: str = Field(pattern=r"^[A-Z]{2}$")


class QuoteIn(BaseModel):
    domain: str = Field(min_length=4, max_length=253)


class PurchaseIn(BaseModel):
    quote_id: str
    project_id: int | None = None
    contact: ContactIn | None = None
    accept_terms: bool = False


@router.get("/search")
async def search(q: str, ctx: T.Ctx = Depends(T.current_ctx)):
    """Candidate names with indicative prices. Search results are cached by the
    registry side, so buying always goes through /quote first."""
    q = q.strip()[:63]
    if len(q) < 2:
        raise HTTPException(400, "type at least two characters")
    if not registrar.configured():
        return {"query": q, "registrar_connected": False, "results": []}
    try:
        results = await registrar.search(q)
    except registrar.RegistrarError as exc:
        raise HTTPException(503, str(exc))
    return {"query": q, "registrar_connected": True, "results": results}


@router.post("/quote")
async def quote(body: QuoteIn, ctx: T.Ctx = Depends(T.requires("billing"))):
    name = _clean(body.domain)
    if not NAME.match(name):
        raise HTTPException(400, "that isn't a valid domain name")
    try:
        d = await registrar.check(name)
    except registrar.RegistrarError as exc:
        raise HTTPException(503, str(exc))
    if not d["available"]:
        reasons = {"domain_unavailable": "That domain is already taken.",
                   "premium": "That's a premium domain, which CreAI can't sell yet.",
                   "extension_not_supported_via_api": "CreAI can't register that ending yet.",
                   "extension_not_supported": "That ending isn't supported.",
                   "extension_disallows_registration": "That ending doesn't allow new registrations."}
        return {"domain": name, "available": False, "message": reasons.get(d["reason"], "That domain isn't available.")}
    qid = secrets.token_urlsafe(16)
    async with conn() as c:
        await c.execute(
            """INSERT INTO domain_quotes (id, org_id, domain, cost_usd, credits, renewal_credits, expires_at)
               VALUES ($1,$2,$3,$4,$5,$6, now() + make_interval(mins => $7))""",
            qid, ctx.org_id, name, d["cost_usd"], d["credits"], d["renewal_credits"], QUOTE_MINUTES)
    return {"domain": name, "available": True, "quote_id": qid, "credits": d["credits"],
            "renewal_credits": d["renewal_credits"], "cost_usd": d["cost_usd"],
            "expires_in_minutes": QUOTE_MINUTES,
            "terms": "Registered in your name for one year. Registrations can't be refunded once "
                     "complete. CreAI renews it yearly from your credits and emails you first; you can "
                     "move it to another registrar any time."}


@router.get("/contact")
async def get_contact(ctx: T.Ctx = Depends(T.requires("billing"))):
    c = await _contact(ctx.org_id)
    return {"contact": c}


async def _contact(org_id: int) -> dict | None:
    async with conn() as c:
        enc = await c.fetchval("SELECT registrant_enc FROM org_settings WHERE org_id=$1", org_id)
    return vault.open_(enc.encode()) if enc else None


async def _save_contact(org_id: int, contact: dict) -> None:
    async with conn() as c:
        await c.execute(
            """INSERT INTO org_settings (org_id, registrant_enc) VALUES ($1,$2)
               ON CONFLICT (org_id) DO UPDATE SET registrant_enc=EXCLUDED.registrant_enc, updated_at=now()""",
            org_id, vault.seal(contact).decode())


@router.post("/purchase")
async def purchase(body: PurchaseIn, ctx: T.Ctx = Depends(T.requires("billing"))):
    if not body.accept_terms:
        raise HTTPException(400, "Please confirm the price and that registrations can't be refunded.")
    contact = body.contact.model_dump() if body.contact else await _contact(ctx.org_id)
    if not contact:
        raise HTTPException(400, "We need the owner's contact details to register a domain in your name.")
    async with conn() as c:
        if body.project_id and not await c.fetchval(
                "SELECT 1 FROM projects WHERE id=$1 AND org_id=$2", body.project_id, ctx.org_id):
            raise HTTPException(404, "no such project")
        # Claim the quote exactly once; a double tap can't buy twice.
        q = await c.fetchrow(
            """UPDATE domain_quotes SET state='buying'
               WHERE id=$1 AND org_id=$2 AND state='open' AND expires_at > now() RETURNING *""",
            body.quote_id, ctx.org_id)
    if not q:
        raise HTTPException(409, "That price has expired or was already used. Check the domain again.")
    name, cost = q["domain"], q["credits"]

    # Re-check right before paying: availability and price can change in seconds.
    try:
        fresh = await registrar.check(name)
    except registrar.RegistrarError as exc:
        await _quote_state(q["id"], "failed")
        raise HTTPException(503, str(exc))
    if not fresh["available"] or fresh["credits"] > cost:
        await _quote_state(q["id"], "failed")
        raise HTTPException(409, "That domain's availability or price just changed. Check it again.")

    ref = f"domain:{q['id']}"
    if not await billing.spend(ctx.org_id, ctx.user_id, cost, "domain", ref, {"domain": name}):
        await _quote_state(q["id"], "failed")
        raise HTTPException(402, f"This domain costs {cost:,} credits. Top up to buy it.")
    try:
        reg = await registrar.register(name, contact)
    except registrar.RegistrarError as exc:
        await billing.refund(ctx.org_id, cost, f"refund:{q['id']}", {"domain": name, "why": str(exc)})
        await _quote_state(q["id"], "failed")
        await log_event(ctx.org_id, "domain.purchase_failed", name, body.project_id, ctx.user_id)
        raise HTTPException(502, f"{exc}. Your {cost:,} credits have been returned.")

    await _quote_state(q["id"], "bought")
    if body.contact:
        await _save_contact(ctx.org_id, contact)
    async with conn() as c:
        did = await c.fetchval(
            """INSERT INTO domains (org_id, project_id, name, source, verify_token, status, expires_at, renewal_credits)
               VALUES ($1,$2,$3,'registered',$4,'registered',$5,$6)
               ON CONFLICT (org_id, name) DO UPDATE SET status='registered', source='registered',
                 project_id=COALESCE(EXCLUDED.project_id, domains.project_id), expires_at=EXCLUDED.expires_at
               RETURNING id""",
            ctx.org_id, body.project_id, name, dns.verify_token(),
            _ts(reg.get("expires_at")), q["renewal_credits"])
    await log_event(ctx.org_id, "domain.purchased", name, body.project_id, ctx.user_id)
    hosted = await _host(did, ctx.org_id)
    return {"domain": name, "id": did, "status": "registered", "credits_spent": cost,
            "expires_at": reg.get("expires_at"), "hosting": hosted}


async def _quote_state(qid: str, state: str) -> None:
    async with conn() as c:
        await c.execute("UPDATE domain_quotes SET state=$2 WHERE id=$1", qid, state)


def _ts(v):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None
    except ValueError:
        return None


async def _host(domain_id: int, org_id: int) -> dict:
    """Attach apex and www to the service and write the records the host asks for.
    Best effort: a failure here leaves the domain owned and retryable."""
    async with conn() as c:
        d = await c.fetchrow("SELECT * FROM domains WHERE id=$1 AND org_id=$2", domain_id, org_id)
    if not hosting.configured():
        return {"ok": False, "message": "Hosting on your own domain is being switched on; your domain is saved."}
    try:
        apex = await hosting.attach(d["name"])
        www = await hosting.attach("www." + d["name"])
        records = dns.hosting_records(d["name"], apex["records"] + www["records"])
        written = []
        if settings.cloudflare_token:
            zone = d["zone_id"] or await dns.ensure_zone(d["name"])
            written = await dns.write_records(zone, d["name"], d["verify_token"], records)
            async with conn() as c:
                await c.execute("UPDATE domains SET zone_id=$1 WHERE id=$2", zone, domain_id)
    except (hosting.HostingError, RuntimeError) as exc:
        return {"ok": False, "message": str(exc)}
    info = {"apex": apex["id"], "www": www["id"], "records": records,
            "certificate": apex["certificate"], "verified": apex["verified"] and www["verified"]}
    async with conn() as c:
        await c.execute("UPDATE domains SET hosting=$1 WHERE id=$2", info, domain_id)
    return {"ok": True, "records": records, "written": [w for w in written if w["ok"]]}


@router.post("/connect")
async def connect_domain(body: ConnectIn, ctx: T.Ctx = Depends(T.requires("write"))):
    name = _clean(body.name)
    token = dns.verify_token()
    async with conn() as c:
        if body.project_id:
            owns = await c.fetchval(
                "SELECT 1 FROM projects WHERE id=$1 AND org_id=$2",
                body.project_id, ctx.org_id)
            if not owns:
                raise HTTPException(404, "no such project")
        await c.execute(
            """INSERT INTO domains (org_id, project_id, name, source, verify_token, status)
               VALUES ($1,$2,$3,'connected',$4,'pending')
               ON CONFLICT (org_id, name) DO UPDATE
                 SET verify_token = EXCLUDED.verify_token, status='pending'""",
            ctx.org_id, body.project_id, name, token)
    await log_event(ctx.org_id, "domain.connect", name, body.project_id, ctx.user_id)
    return {
        "domain": name, "status": "pending",
        "verify": {"type": "TXT", "name": f"{settings.verify_prefix}.{name}", "value": token},
        "instructions": "Add this TXT record at your registrar. We check every 30 "
                        "seconds and email you when it resolves. Your existing "
                        "records, including email, are never modified.",
    }


async def _own(c, domain_id: int, org_id: int):
    d = await c.fetchrow(
        "SELECT * FROM domains WHERE id=$1 AND org_id=$2", domain_id, org_id)
    if not d:
        raise HTTPException(404, "no such domain")
    return d


@router.post("/{domain_id}/verify")
async def verify(domain_id: int, ctx: T.Ctx = Depends(T.requires("write"))):
    async with conn() as c:
        d = await _own(c, domain_id, ctx.org_id)
    if await dns.verify_ownership(d["name"], d["verify_token"]):
        async with conn() as c:
            await c.execute(
                "UPDATE domains SET status='verifying', verified_at=now() WHERE id=$1",
                domain_id)
        await log_event(ctx.org_id, "domain.verified", d["name"], d["project_id"], ctx.user_id)
        return {"verified": True}
    return {"verified": False,
            "hint": "Not visible yet. Registrars can take up to an hour. You can "
                    "close this page — we'll email you."}


@router.post("/{domain_id}/host")
async def host_domain(domain_id: int, ctx: T.Ctx = Depends(T.requires("write"))):
    """Point a verified or purchased domain at CreAI hosting (safe to repeat)."""
    async with conn() as c:
        d = await _own(c, domain_id, ctx.org_id)
        taken = await c.fetchval(
            """SELECT 1 FROM domains WHERE name=$1 AND org_id<>$2
               AND status IN ('registered','verifying','live')""", d["name"], ctx.org_id)
    if taken:
        raise HTTPException(409, "Another workspace already uses this domain.")
    if d["source"] == "connected" and d["status"] == "pending":
        raise HTTPException(409, "Verify you own this domain first.")
    out = await _host(domain_id, ctx.org_id)
    await log_event(ctx.org_id, "domain.hosted", d["name"], d["project_id"], ctx.user_id)
    return out


@router.post("/{domain_id}/records")
async def write_records(domain_id: int, ctx: T.Ctx = Depends(T.requires("write"))):
    if settings.missing_for("dns"):
        raise HTTPException(503, "DNS is not configured on this deployment")
    async with conn() as c:
        d = await _own(c, domain_id, ctx.org_id)

    zone = d["zone_id"] or await dns.ensure_zone(d["name"])
    written = await dns.write_records(zone, d["name"], d["verify_token"])

    async with conn() as c:
        await c.execute("UPDATE domains SET zone_id=$1 WHERE id=$2", zone, domain_id)
        await c.execute(
            "DELETE FROM dns_records WHERE domain_id=$1 AND org_id=$2", domain_id, ctx.org_id)
        for r in written:
            await c.execute(
                """INSERT INTO dns_records (org_id, domain_id, type, name, value, status, provider_id)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                ctx.org_id, domain_id, r["type"], r["name"], r["value"],
                "pending" if r["ok"] else "failed", r["provider_id"])
    await log_event(ctx.org_id, "dns.written", d["name"], d["project_id"], ctx.user_id)
    return {"domain": d["name"], "records": written}


@router.get("/{domain_id}/status")
async def status(domain_id: int, ctx: T.Ctx = Depends(T.current_ctx)):
    """Resolved from outside, not from our own write — what the customer sees."""
    async with conn() as c:
        d = await _own(c, domain_id, ctx.org_id)
        recs = await c.fetch(
            "SELECT * FROM dns_records WHERE domain_id=$1 AND org_id=$2 ORDER BY id",
            domain_id, ctx.org_id)

    live = await dns.check_live(d["name"])
    resolved = live["apex"] and live["www"]

    if resolved and d["status"] != "live":
        async with conn() as c:
            await c.execute("UPDATE domains SET status='live' WHERE id=$1", domain_id)
            await c.execute(
                """UPDATE dns_records SET status='live', checked_at=now()
                   WHERE domain_id=$1 AND org_id=$2""", domain_id, ctx.org_id)
        await log_event(ctx.org_id, "domain.live", d["name"], d["project_id"])

    return {"domain": d["name"], "status": "live" if resolved else d["status"],
            "resolves": live,
            "records": [{"type": r["type"], "name": r["name"], "value": r["value"],
                         "status": "live" if resolved else r["status"]} for r in recs],
            "note": "Propagation is usually minutes but registrars can take an hour."}


@router.get("")
async def list_domains(ctx: T.Ctx = Depends(T.current_ctx)):
    async with conn() as c:
        rows = await c.fetch(
            """SELECT id, name, source, status, project_id, expires_at, created_at FROM domains
               WHERE org_id=$1 ORDER BY created_at DESC""", ctx.org_id)
    return [dict(r) | {"created_at": r["created_at"].isoformat(),
                       "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None}
            for r in rows]
