"""
Platform administration — you, not your customers.

Three rules this module exists to enforce:

  1. Admin is a separate table, never a flag on a customer account.
  2. Every call requires an X-Reason header and writes an audit row. An
     unexplained look at a customer's data is not a supported operation.
  3. Admin routes read aggregates and metadata. There is no route here that
     reads a tenant's content — no post bodies, no briefs, no credentials.
     Support that genuinely needs content asks the customer.
"""

from fastapi import APIRouter, Depends, HTTPException

from ..core import tenancy as T
from ..core.db import conn

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.get("/overview")
async def overview(admin=Depends(T.platform_admin)):
    async with conn() as c:
        stats = await c.fetchrow("""
            SELECT (SELECT count(*) FROM organizations)              AS orgs,
                   (SELECT count(*) FROM users)                      AS users,
                   (SELECT count(*) FROM projects)                   AS projects,
                   (SELECT count(*) FROM domains WHERE status='live') AS live_domains,
                   (SELECT count(*) FROM approvals WHERE state='pending') AS pending
        """)
        recent = await c.fetch(
            """SELECT o.id, o.name, o.slug, o.plan, o.status, o.created_at,
                      (SELECT count(*) FROM memberships m WHERE m.org_id=o.id) AS members,
                      (SELECT count(*) FROM projects p WHERE p.org_id=o.id)    AS projects
               FROM organizations o ORDER BY o.created_at DESC LIMIT 25""")
    await T.log_admin(admin["user_id"], "admin.overview", admin["reason"])
    return {"totals": dict(stats),
            "workspaces": [dict(r) | {"created_at": r["created_at"].isoformat()}
                           for r in recent]}


@router.get("/workspaces/{org_id}")
async def workspace(org_id: int, admin=Depends(T.platform_admin)):
    """Metadata and health only. Never a tenant's content."""
    async with conn() as c:
        org = await c.fetchrow("SELECT * FROM organizations WHERE id=$1", org_id)
        if not org:
            raise HTTPException(404, "no such workspace")
        members = await c.fetch(
            """SELECT u.email, m.role FROM memberships m JOIN users u ON u.id=m.user_id
               WHERE m.org_id=$1""", org_id)
        # counts and statuses — deliberately not payloads
        domains = await c.fetch(
            "SELECT name, status FROM domains WHERE org_id=$1", org_id)
        counts = await c.fetchrow(
            """SELECT (SELECT count(*) FROM projects WHERE org_id=$1)  AS projects,
                      (SELECT count(*) FROM approvals WHERE org_id=$1 AND state='pending') AS pending,
                      (SELECT count(*) FROM channels WHERE org_id=$1)  AS channels""",
            org_id)
    await T.log_admin(admin["user_id"], "admin.workspace", admin["reason"], org_id)
    return {"workspace": {"id": org["id"], "name": org["name"], "slug": org["slug"],
                          "plan": org["plan"], "status": org["status"],
                          "seats": org["seats"]},
            "members": [dict(m) for m in members],
            "domains": [dict(d) for d in domains],
            "counts": dict(counts)}


@router.post("/workspaces/{org_id}/suspend")
async def suspend(org_id: int, admin=Depends(T.platform_admin)):
    if admin["level"] not in ("engineer", "owner"):
        raise HTTPException(403, "support cannot suspend a workspace")
    async with conn() as c:
        r = await c.execute(
            "UPDATE organizations SET status='suspended' WHERE id=$1", org_id)
    if r.endswith("0"):
        raise HTTPException(404, "no such workspace")
    await T.log_admin(admin["user_id"], "admin.suspend", admin["reason"], org_id)
    return {"org_id": org_id, "status": "suspended"}


@router.post("/workspaces/{org_id}/restore")
async def restore(org_id: int, admin=Depends(T.platform_admin)):
    if admin["level"] not in ("engineer", "owner"):
        raise HTTPException(403, "support cannot restore a workspace")
    async with conn() as c:
        await c.execute("UPDATE organizations SET status='active' WHERE id=$1", org_id)
    await T.log_admin(admin["user_id"], "admin.restore", admin["reason"], org_id)
    return {"org_id": org_id, "status": "active"}


@router.get("/audit")
async def audit(limit: int = 100, admin=Depends(T.platform_admin)):
    """The log of staff access. Readable by staff, deliberately: an audit trail
    nobody reads is decoration."""
    async with conn() as c:
        rows = await c.fetch(
            """SELECT l.id, u.email, l.org_id, l.action, l.reason, l.at
               FROM admin_access_log l JOIN users u ON u.id = l.admin_id
               ORDER BY l.at DESC LIMIT $1""", min(limit, 500))
    return [dict(r) | {"at": r["at"].isoformat()} for r in rows]


# ---------------------------------------------------------------- social channels

from pydantic import BaseModel  # noqa: E402

from ..services import social_publish  # noqa: E402


class AssignIn(BaseModel):
    integration_id: str
    org_id: int


@router.get("/social")
async def social_overview(admin=Depends(T.platform_admin)):
    """Postiz health and which connected account belongs to which workspace."""
    try:
        listed = await social_publish.integrations()
    except social_publish.PostizError as exc:
        raise HTTPException(503, str(exc))
    async with conn() as c:
        owned = {r["postiz_id"]: dict(r) for r in await c.fetch(
            """SELECT s.postiz_id, s.org_id, o.name AS workspace FROM social_channels s
               JOIN organizations o ON o.id=s.org_id""")}
    await T.log_admin(admin["user_id"], "admin.social", admin["reason"])
    return {"connected": True, "channels": [{
        "id": i["id"], "platform": i.get("identifier"), "name": i.get("name"),
        "disabled": i.get("disabled"), "owner": owned.get(i["id"]),
    } for i in listed]}


@router.post("/social/assign")
async def social_assign(body: AssignIn, admin=Depends(T.platform_admin)):
    async with conn() as c:
        if not await c.fetchval("SELECT 1 FROM organizations WHERE id=$1", body.org_id):
            raise HTTPException(404, "no such workspace")
    try:
        row = await social_publish.assign(body.integration_id, body.org_id)
    except social_publish.PostizError as exc:
        raise HTTPException(400, str(exc))
    await T.log_admin(admin["user_id"], "admin.social.assign",
                      f"{admin['reason']} · {body.integration_id} → {body.org_id}")
    return {"network": row["network"], "org_id": row["org_id"]}


@router.post("/social/unassign")
async def social_unassign(body: AssignIn, admin=Depends(T.platform_admin)):
    await social_publish.unassign(body.integration_id)
    await T.log_admin(admin["user_id"], "admin.social.unassign",
                      f"{admin['reason']} · {body.integration_id}")
    return {"ok": True}


@router.get("/probe")
async def probe(admin=Depends(T.platform_admin)):
    """Staff: check that go-live integrations actually work, not just that keys exist."""
    from ..services import hosting, registrar
    out = {}
    try:
        found = await registrar.search("creai probe", limit=1)
        out["registrar"] = {"ok": True, "sample": found[0]["domain"] if found else None}
    except Exception as exc:                        # noqa: BLE001 — report, don't raise
        out["registrar"] = {"ok": False, "error": str(exc)[:300]}
    try:
        doms = await hosting.domains()
        out["hosting"] = {"ok": True, "custom_domains": len(doms)}
    except Exception as exc:                        # noqa: BLE001
        out["hosting"] = {"ok": False, "error": str(exc)[:300]}
    await T.log_admin(admin["user_id"], "admin.probe", admin["reason"])
    return out


@router.get("/quality")
async def quality(days: int = 7, admin=Depends(T.platform_admin)):
    """What the reviewers keep catching, across every build.

    The working list: a fault appearing hundreds of times is a prompt to rewrite,
    a rule that is wrong, or a capability that is missing. Read it weekly.
    """
    from ..services import quality as quality_svc
    return await quality_svc.digest(days=max(1, min(int(days), 90)))
