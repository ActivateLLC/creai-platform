"""
Videos: plan one, render it, send it to the queue.

Saving a plan is free and rendering is not, so the two are separate calls and the
cost is shown before the second one.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..core import tenancy as T
from ..core.db import conn
from ..services import sound, video

log = logging.getLogger("creai.video")
router = APIRouter(prefix="/v1/videos", tags=["videos"])


class Plan(BaseModel):
    plan: dict = Field(default_factory=dict)
    title: str = ""
    shape: str = "vertical"


@router.get("/sources")
async def sources(for_ads: bool = True):
    """What music may be used, and under what terms. Said before it matters."""
    return {"music": sound.cleared(for_ads)}


@router.post("/{project_id}")
async def save(project_id: int, body: Plan, ctx: T.Ctx = Depends(T.requires("write"))):
    """Keep a plan. Returns what it would cost and anything wrong with it."""
    async with conn() as c:
        ok = await c.fetchval("SELECT 1 FROM projects WHERE id=$1 AND org_id=$2",
                              project_id, ctx.org_id)
    if not ok:
        raise HTTPException(404, "no such project")
    return await video.save(project_id, ctx.org_id, ctx.user_id,
                            body.plan, body.title, body.shape)


@router.get("/{project_id}")
async def listing(project_id: int, ctx: T.Ctx = Depends(T.current_ctx)):
    async with conn() as c:
        rows = await c.fetch(
            """SELECT id, title, shape, state, progress, seconds, credits, error, created_at
               FROM videos WHERE project_id=$1 AND org_id=$2 ORDER BY id DESC LIMIT 40""",
            project_id, ctx.org_id)
    return {"videos": [{**dict(r), "created_at": r["created_at"].isoformat(),
                        "seconds": float(r["seconds"]) if r["seconds"] else None}
                       for r in rows]}


@router.post("/{video_id}/render")
async def render(video_id: int, ctx: T.Ctx = Depends(T.requires("approve"))):
    """Spend the credits and make the film."""
    try:
        return await video.start(video_id, ctx.org_id)
    except video.VideoError as exc:
        raise HTTPException(409, str(exc))
