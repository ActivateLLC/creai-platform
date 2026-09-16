"""
The approval queue.

Every generated artefact — a post, a DNS change, a filing — lands here and waits.
Nothing sends, posts, spends or files without an explicit decision by a member
whose role permits it. There is no code path that publishes a pending row.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..core import tenancy as T
from ..core.db import conn, log_event

router = APIRouter(prefix="/v1/approvals", tags=["approvals"])
KINDS = {"post", "dns_change", "filing"}


class DraftIn(BaseModel):
    kind: str
    payload: dict
    project_id: int | None = None
    scheduled_for: str | None = None


@router.get("")
async def queue(state: str = "pending", ctx: T.Ctx = Depends(T.current_ctx)):
    async with conn() as c:
        rows = await c.fetch(
            """SELECT id, kind, payload, state, scheduled_for, created_at
               FROM approvals WHERE org_id=$1 AND state=$2
               ORDER BY created_at DESC LIMIT 100""", ctx.org_id, state)
    return [{"id": r["id"], "kind": r["kind"], "payload": r["payload"], "state": r["state"],
             "scheduled_for": r["scheduled_for"].isoformat() if r["scheduled_for"] else None,
             "created_at": r["created_at"].isoformat()} for r in rows]


@router.post("")
async def draft(body: DraftIn, ctx: T.Ctx = Depends(T.requires("write"))):
    """Agents write here. They have no route that publishes."""
    if body.kind not in KINDS:
        raise HTTPException(400, f"kind must be one of {sorted(KINDS)}")
    async with conn() as c:
        if body.project_id:
            owns = await c.fetchval(
                "SELECT 1 FROM projects WHERE id=$1 AND org_id=$2",
                body.project_id, ctx.org_id)
            if not owns:
                raise HTTPException(404, "no such project")
        row = await c.fetchrow(
            """INSERT INTO approvals (org_id, project_id, kind, payload, scheduled_for)
               VALUES ($1,$2,$3,$4,$5) RETURNING id""",
            ctx.org_id, body.project_id, body.kind, body.payload, body.scheduled_for)
    return {"id": row["id"], "state": "pending"}


@router.post("/{approval_id}/approve")
async def approve(approval_id: int, ctx: T.Ctx = Depends(T.requires("approve"))):
    async with conn() as c:
        row = await c.fetchrow(
            """UPDATE approvals SET state='approved', decided_at=now(), decided_by=$3
               WHERE id=$1 AND org_id=$2 AND state='pending' RETURNING *""",
            approval_id, ctx.org_id, ctx.user_id)
    if not row:
        raise HTTPException(404, "nothing pending with that id")
    await log_event(ctx.org_id, f"{row['kind']}.approved", "", row["project_id"], ctx.user_id)
    # A worker executes approved rows. Approving records a decision; it does not
    # perform the action inline, so a slow provider can never make the user's
    # click hang or double-fire.
    return {"id": approval_id, "state": "approved"}


@router.post("/{approval_id}/discard")
async def discard(approval_id: int, ctx: T.Ctx = Depends(T.requires("approve"))):
    async with conn() as c:
        row = await c.fetchrow(
            """UPDATE approvals SET state='discarded', decided_at=now(), decided_by=$3
               WHERE id=$1 AND org_id=$2 AND state='pending' RETURNING id""",
            approval_id, ctx.org_id, ctx.user_id)
    if not row:
        raise HTTPException(404, "nothing pending with that id")
    return {"id": approval_id, "state": "discarded"}
