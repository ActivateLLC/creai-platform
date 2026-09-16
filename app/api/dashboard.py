"""
The dashboard payload — organised around what needs a human, not around metrics.

The user is a business owner deciding what to sign off, not an analyst studying a
funnel. So the queue comes first and anything healthy simply says it is running.
"""

from fastapi import APIRouter, Depends

from ..core import tenancy as T
from ..core.db import conn

router = APIRouter(prefix="/v1/dashboard", tags=["dashboard"])


@router.get("")
async def overview(ctx: T.Ctx = Depends(T.current_ctx)):
    async with conn() as c:
        pending = await c.fetch(
            """SELECT id, kind, payload, created_at FROM approvals
               WHERE org_id=$1 AND state='pending' ORDER BY created_at DESC LIMIT 20""",
            ctx.org_id)
        waiting = await c.fetch(
            """SELECT id, name, status FROM domains
               WHERE org_id=$1 AND status IN ('pending','verifying')""", ctx.org_id)
        projects = await c.fetch(
            """SELECT p.id, p.name, p.status, d.name AS domain
               FROM projects p LEFT JOIN domains d ON d.project_id=p.id AND d.org_id=p.org_id
               WHERE p.org_id=$1 ORDER BY p.updated_at DESC LIMIT 12""", ctx.org_id)
        events = await c.fetch(
            """SELECT kind, detail, at FROM events
               WHERE org_id=$1 ORDER BY at DESC LIMIT 12""", ctx.org_id)

    needs_you = [
        {"source": "approval", "id": r["id"], "kind": r["kind"], "payload": r["payload"],
         "at": r["created_at"].isoformat()} for r in pending
    ] + [
        {"source": "domain", "id": r["id"], "kind": "dns_pending",
         "payload": {"domain": r["name"], "status": r["status"]}, "at": None}
        for r in waiting
    ]

    return {"workspace": {"id": ctx.org_id, "name": ctx.org_name, "your_role": ctx.role},
            "needs_you": needs_you,
            "projects": [dict(p) for p in projects],
            "activity": [{"kind": e["kind"], "detail": e["detail"], "at": e["at"].isoformat()}
                         for e in events]}
