"""Launch readiness: the checklist, the person's decisions, and applying ideas."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..core import tenancy as T
from ..core.db import conn
from ..services import billing, readiness

router = APIRouter(prefix="/v1/readiness", tags=["readiness"])


class DecideIn(BaseModel):
    item: str = Field(min_length=1, max_length=40, pattern=r"^([a-z_]{2,20}|s:\d{1,18})$")
    decision: str | None = Field(None, pattern="^(skip|later)$")


class ApplyIn(BaseModel):
    items: list[str] = Field(min_length=1, max_length=10)
    mode: str = Field("best", pattern="^(best|fast)$")


async def _project(project_id: int, org_id: int) -> dict:
    async with conn() as c:
        p = await c.fetchrow("SELECT id, path, answers FROM projects WHERE id=$1 AND org_id=$2",
                             project_id, org_id)
    if not p:
        raise HTTPException(404, "no such project")
    return dict(p)


@router.get("/{project_id}")
async def get_report(project_id: int, ctx: T.Ctx = Depends(T.current_ctx)):
    return await readiness.report(await _project(project_id, ctx.org_id), ctx.org_id)


@router.post("/{project_id}/decide")
async def decide(project_id: int, body: DecideIn, ctx: T.Ctx = Depends(T.requires("write"))):
    p = await _project(project_id, ctx.org_id)
    await readiness.decide(ctx.org_id, project_id, body.item, body.decision)
    return await readiness.report(p, ctx.org_id)


@router.post("/{project_id}/plan")
async def plan(project_id: int, body: ApplyIn, ctx: T.Ctx = Depends(T.requires("write"))):
    """Turn the selected items into one clear request, with an honest cost estimate.
    The client sends it as a normal message, so it's billed like any other."""
    p = await _project(project_id, ctx.org_id)
    rep = await readiness.report(p, ctx.org_id)
    by_key = {i["key"]: i for i in rep.get("items", []) if not i["done"] and i["request"]}
    by_key.update({f"s:{s['id']}": {"request": s["request"], "label": s["title"]} for s in rep.get("suggestions", [])})
    chosen = [by_key[k] for k in body.items if k in by_key]
    if not chosen:
        raise HTTPException(400, "nothing to apply")
    lines = [f"{n}. {c['request']}" for n, c in enumerate(chosen, 1)]
    message = ("Please make these improvements to my " + ("app" if rep["kind"] == "app" else "site")
               + ":\n" + "\n".join(lines))
    kind = "app" if rep["kind"] == "app" else "market" if rep["kind"] == "market" else "site"
    est = await billing.estimate(ctx.org_id, body.mode, kind)
    extra = max(0, len(chosen) - 1)
    return {"message": message, "items": [c["label"] for c in chosen],
            "estimate": {"low": est["low"], "high": est["high"] + extra * max(1, est["high"] // 3)},
            "suggestion_ids": [int(k[2:]) for k in body.items if k.startswith("s:") and k in by_key]}


@router.post("/{project_id}/applied")
async def applied(project_id: int, body: ApplyIn, ctx: T.Ctx = Depends(T.requires("write"))):
    await _project(project_id, ctx.org_id)
    await readiness.mark_applied(ctx.org_id, project_id,
                                 [int(k[2:]) for k in body.items if k.startswith("s:") and k[2:].isdigit()])
    return {"ok": True}
