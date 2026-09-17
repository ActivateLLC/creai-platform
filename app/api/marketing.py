"""
Marketing: for a business that only wants growth, or on top of a site CreAI
built or a site the customer connected. Every project can have it.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..core import tenancy as T
from ..core.db import conn, log_event
from ..services import brand

router = APIRouter(prefix="/v1/marketing", tags=["marketing"])


class StartIn(BaseModel):
    website: str = Field(min_length=3, max_length=300)
    name: str | None = Field(None, max_length=120)


class EnableIn(BaseModel):
    project_id: int


@router.post("/start")
async def start(body: StartIn, ctx: T.Ctx = Depends(T.requires("write"))):
    try:
        url = brand.normalise(body.website)
    except brand.FetchError as exc:
        raise HTTPException(400, str(exc))
    host = url.split("//", 1)[1].split("/", 1)[0].removeprefix("www.")
    async with conn() as c:
        pid = await c.fetchval(
            """SELECT id FROM projects WHERE org_id=$1 AND path='market'
               AND answers->>'website'=$2""", ctx.org_id, url)
        if not pid:
            pid = await c.fetchval(
                """INSERT INTO projects (org_id, created_by, name, path, answers)
                   VALUES ($1,$2,$3,'market',$4) RETURNING id""",
                ctx.org_id, ctx.user_id, (body.name or host)[:120],
                {"source": "marketing", "marketing": True, "website": url})
    await log_event(ctx.org_id, "marketing.started", host, pid, ctx.user_id)
    return {"project_id": pid, "website": url}


@router.post("/enable")
async def enable(body: EnableIn, ctx: T.Ctx = Depends(T.requires("write"))):
    async with conn() as c:
        row = await c.fetchrow(
            """UPDATE projects SET answers = answers || '{"marketing": true}'::jsonb, updated_at=now()
               WHERE id=$1 AND org_id=$2 RETURNING id""", body.project_id, ctx.org_id)
    if not row:
        raise HTTPException(404, "no such project")
    return {"project_id": body.project_id, "marketing": True}


@router.get("/calendar")
async def calendar(project_id: int | None = None, days: int = 60,
                   ctx: T.Ctx = Depends(T.current_ctx)):
    days = max(1, min(days, 366))
    until = datetime.now(timezone.utc) + timedelta(days=days)
    async with conn() as c:
        rows = await c.fetch(
            """SELECT id, project_id, payload, state, scheduled_for, created_at FROM approvals
               WHERE org_id=$1 AND kind='post'
                 AND state IN ('pending','approved','held','sending','scheduled','failed')
                 AND ($2::bigint IS NULL OR project_id=$2)
                 AND (scheduled_for IS NULL OR scheduled_for <= $3)
               ORDER BY scheduled_for NULLS LAST, created_at""",
            ctx.org_id, project_id, until)
    return {"posts": [{
        "id": r["id"], "project_id": r["project_id"], "state": r["state"],
        "network": (r["payload"] or {}).get("network"),
        "text": (r["payload"] or {}).get("text"),
        "link": (r["payload"] or {}).get("link"),
        "image": next((m.get("url") for m in (r["payload"] or {}).get("media") or []
                       if m.get("type") == "image"), None),
        "scheduled_for": r["scheduled_for"].isoformat() if r["scheduled_for"] else None,
        "delivery": {k: v for k, v in ((r["payload"] or {}).get("delivery") or {}).items()
                     if k in ("reason", "channel", "at", "upgrade")},
    } for r in rows]}
