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
from datetime import datetime

from ..services import posts as post_svc
from ..services import social_publish, webflow

router = APIRouter(prefix="/v1/approvals", tags=["approvals"])
KINDS = {"post", "dns_change", "filing"}


class DraftIn(BaseModel):
    kind: str
    payload: dict
    project_id: int | None = None
    scheduled_for: str | None = None


@router.get("")
async def queue(state: str = "pending", ctx: T.Ctx = Depends(T.current_ctx)):
    if state not in ("pending", "approved", "held", "scheduled", "failed", "done", "discarded"):
        raise HTTPException(400, "unknown state")
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


class PostEdit(BaseModel):
    text: str | None = None
    scheduled_for: str | None = None
    link: str | None = None
    remove_image: bool = False


@router.patch("/{approval_id}")
async def edit_post(approval_id: int, body: PostEdit, ctx: T.Ctx = Depends(T.requires("write"))):
    """Change a drafted post before it goes out. An edited post needs approving again."""
    when = None
    if body.scheduled_for:
        from ..services.agent import _when
        iso = _when(body.scheduled_for)
        if not iso:
            raise HTTPException(400, "pick a time in the next year")
        when = datetime.fromisoformat(iso)
    await social_publish.cancel(approval_id, ctx.org_id)
    try:
        out = await post_svc.update(ctx.org_id, approval_id, text=body.text, scheduled_for=when,
                                    link=body.link, image_url="" if body.remove_image else None)
    except post_svc.PostError as exc:
        code = 404 if "no such" in str(exc) else 409 if "gone out" in str(exc) else 400
        raise HTTPException(code, str(exc))
    await log_event(ctx.org_id, "post.edited", "", None, ctx.user_id)
    return out


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
    if row["kind"] == "post":
        state = await social_publish.deliver(approval_id) if social_publish.configured() else "approved"
        return {"id": approval_id, "state": state}
    if row["kind"] == "webflow_action":
        # A change to the customer's own site: the person is waiting on it, so
        # it runs now, once, and the outcome is recorded either way.
        try:
            result = await webflow.run_approved(ctx.org_id, row["payload"])
        except webflow.WebflowError as exc:
            async with conn() as c:
                await c.execute("UPDATE approvals SET state='failed' WHERE id=$1 AND org_id=$2",
                                approval_id, ctx.org_id)
            raise HTTPException(409, str(exc))
        failed = result.startswith("ERROR")
        async with conn() as c:
            await c.execute(
                """UPDATE approvals SET state=$3, executed_at=now()
                   WHERE id=$1 AND org_id=$2""",
                approval_id, ctx.org_id, "failed" if failed else "done")
        return {"id": approval_id, "state": "failed" if failed else "done",
                "result": result[:2000]}
    # A worker executes approved rows. Approving records a decision; it does not
    # perform the action inline, so a slow provider can never make the user's
    # click hang or double-fire.
    return {"id": approval_id, "state": "approved"}


@router.post("/{approval_id}/discard")
async def discard(approval_id: int, ctx: T.Ctx = Depends(T.requires("approve"))):
    async with conn() as c:
        # a post can still be pulled after approval, until it has gone out
        row = await c.fetchrow(
            """UPDATE approvals SET state='discarded', decided_at=now(), decided_by=$3
               WHERE id=$1 AND org_id=$2
                 AND (state='pending'
                      OR (kind='post' AND state IN ('approved','held','scheduled','failed')))
               RETURNING id, kind, state""",
            approval_id, ctx.org_id, ctx.user_id)
    if not row:
        raise HTTPException(404, "nothing pending with that id")
    if row["kind"] == "post":
        await social_publish.cancel(approval_id, ctx.org_id)
    return {"id": approval_id, "state": "discarded"}


# ---------------------------------------------------------------- autonomy

@router.get("/autonomy")
async def autonomy(ctx: T.Ctx = Depends(T.current_ctx)):
    """Where this workspace stands: brand confirmed, which channels post on their
    own, whether everything is paused."""
    from ..services import autonomy as auto
    state = await auto.settings_for(ctx.org_id)
    async with conn() as c:
        rows = await c.fetch(
            """SELECT id, network, name, autonomous FROM social_channels
               WHERE org_id=$1 AND status='active' ORDER BY network""", ctx.org_id)
    return {**state, "posted_this_week": await auto.posted_this_week(ctx.org_id),
            "channels": [dict(r) for r in rows]}


@router.post("/autonomy/brand")
async def confirm_brand(on: bool = True, ctx: T.Ctx = Depends(T.requires("approve"))):
    """Say the brand is right — or take it back, which also stops every channel."""
    from ..services import autonomy as auto
    return await auto.confirm_brand(ctx.org_id, on)


@router.post("/autonomy/channel/{channel_id}")
async def channel(channel_id: int, on: bool = True,
                  ctx: T.Ctx = Depends(T.requires("approve"))):
    from ..services import autonomy as auto
    try:
        return await auto.set_channel(ctx.org_id, channel_id, on)
    except PermissionError as exc:
        raise HTTPException(409, str(exc))
    except LookupError:
        raise HTTPException(404, "no such channel")


@router.post("/autonomy/pause")
async def pause(on: bool = True, ctx: T.Ctx = Depends(T.requires("approve"))):
    """Stop everything, now. Held posts stay held until this is turned back off."""
    from ..services import autonomy as auto
    return await auto.pause(ctx.org_id, on)
