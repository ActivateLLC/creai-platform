"""
Domains: register or connect, write records, watch propagation. Phase 0.

Every query is scoped by ctx.org_id. A domain belongs to a workspace, not to the
person who added it.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..core import tenancy as T
from ..core.config import settings
from ..core.db import conn, log_event
from ..services import dns

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


@router.get("/search")
async def search(q: str, ctx: T.Ctx = Depends(T.current_ctx)):
    """Availability across the TLDs we sell.

    Not yet wired to a registrar, so availability is `unknown` rather than
    invented. A fake "available" becomes a failed purchase later, which is worse
    than an honest unknown.
    """
    base = "".join(ch for ch in q.lower() if ch.isalnum())[:32] or "yourbrand"
    tlds = [".com", ".dev", ".io", ".co", ".app", ".studio"]
    return {"query": base, "registrar_connected": False,
            "results": [{"domain": base + t, "availability": "unknown"} for t in tlds]}


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
            """SELECT id, name, source, status, created_at FROM domains
               WHERE org_id=$1 ORDER BY created_at DESC""", ctx.org_id)
    return [dict(r) | {"created_at": r["created_at"].isoformat()} for r in rows]
