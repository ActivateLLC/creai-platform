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
