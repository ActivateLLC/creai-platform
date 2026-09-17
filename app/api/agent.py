"""
Agent routes — the conversation beside the live preview.

Anonymous drafts can talk to the agent (that is the product's front door), so they
are rate-limited per draft and per address. Signed-in projects use the same loop,
scoped to a project the caller's workspace owns, and can also queue post drafts.
"""

import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..core import tenancy as T
from ..core.config import settings
from ..core.db import conn, log_event
from ..services import agent, billing, site, webflow
from .drafts import COOKIE, DRAFT_TTL, _token

router = APIRouter(prefix="/v1/agent", tags=["agent"])

DRAFT_TURN_CAP = 5               # free messages before an account is needed
IP_WINDOW, IP_CAP = 600, 30      # per address: 30 turns per 10 minutes
_hits: dict[str, deque] = defaultdict(deque)


class SayIn(BaseModel):
    message: str = Field(min_length=1, max_length=agent.MAX_USER_CHARS)
    mode: str = Field("best", pattern="^(best|fast)$")
    intent: str = Field("build", pattern="^(build|chat|plan)$")


def _model(mode: str, intent: str = "build") -> str:
    # Chat is conversation only, so it always runs on the light model.
    if intent == "chat" or mode == "fast":
        return settings.agent_fast_model
    return settings.agent_model


def _limit(ip: str) -> None:
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > IP_WINDOW:
        q.popleft()
    if len(q) >= IP_CAP:
        raise HTTPException(429, "That's a lot of changes in a short time — "
                                 "give it a few minutes, or create an account to keep going.")
    q.append(now)


def _payload(turn_or_answers, actions=None, log=None, posts=None) -> dict:
    a = turn_or_answers
    return {"messages": agent.visible(a.get("_thread") or []),
            "site": site.merge(a.get("site"), {}),
            "actions": actions or [], "log": log or [], "posts": posts or []}


async def _run(message: str, answers: dict, **kw):
    try:
        return await agent.run(message, answers, **kw)
    except agent.AgentUnavailable as exc:
        raise HTTPException(503, str(exc))
    except webflow.WebflowError as exc:
        raise HTTPException(409, str(exc))


# ---------------------------------------------------------------- anonymous

@router.get("/draft")
async def draft_thread(creai_draft: str | None = Cookie(None)):
    if not creai_draft:
        return _payload({})
    async with conn() as c:
        row = await c.fetchrow(
            "SELECT answers FROM drafts WHERE token=$1 AND expires_at > now()", creai_draft)
    return _payload(row["answers"] if row else {})


@router.post("/draft")
async def draft_say(body: SayIn, request: Request, response: Response,
                    creai_draft: str | None = Cookie(None)):
    _limit(request.client.host if request.client else "unknown")
    tok = creai_draft
    async with conn() as c:
        row = None
        if tok:
            row = await c.fetchrow(
                """SELECT id, answers FROM drafts
                   WHERE token=$1 AND expires_at > now() AND claimed_at IS NULL""", tok)
        if not row:
            tok = _token()
            row = await c.fetchrow(
                """INSERT INTO drafts (token, brief, expires_at) VALUES ($1,$2,$3)
                   RETURNING id, answers""",
                tok, body.message.strip()[:2000],
                datetime.now(timezone.utc) + DRAFT_TTL)
            response.set_cookie(COOKIE, tok, max_age=int(DRAFT_TTL.total_seconds()),
                                httponly=True, samesite="lax",
                                secure=settings.env != "development")

    answers = dict(row["answers"] or {})
    turns = int(answers.get("_turns", 0))
    if turns >= DRAFT_TURN_CAP:
        raise HTTPException(402, "You've used your free messages. Create an account to "
                                 "keep building — your draft is saved and you get "
                                 f"{billing.SIGNUP_CREDITS} free credits.")

    turn = await _run(body.message, answers, model=_model(body.mode, body.intent),
                      intent=body.intent)
    turn.answers["_turns"] = turns + 1
    async with conn() as c:
        await c.execute("UPDATE drafts SET answers=$2, updated_at=now() WHERE id=$1",
                        row["id"], turn.answers)
    return _payload(turn.answers, turn.actions, turn.log)


@router.get("/draft/preview", response_class=HTMLResponse)
async def draft_preview(creai_draft: str | None = Cookie(None)):
    answers = {}
    if creai_draft:
        async with conn() as c:
            row = await c.fetchrow(
                "SELECT answers FROM drafts WHERE token=$1 AND expires_at > now()", creai_draft)
        answers = (row["answers"] if row else None) or {}
    return HTMLResponse(site.render(answers.get("site")), headers=_preview_headers())


# ---------------------------------------------------------------- signed in

async def _project(c, project_id: int, org_id: int):
    p = await c.fetchrow(
        "SELECT id, name, answers FROM projects WHERE id=$1 AND org_id=$2",
        project_id, org_id)
    if not p:
        raise HTTPException(404, "no such project")
    return p


@router.get("/projects/{project_id}")
async def project_thread(project_id: int, ctx: T.Ctx = Depends(T.current_ctx)):
    async with conn() as c:
        p = await _project(c, project_id, ctx.org_id)
    a = p["answers"] or {}
    return _payload(a) | {"project": {"id": p["id"], "name": p["name"],
                                      "source": a.get("source"), "site": a.get("site")}}


@router.post("/projects/{project_id}")
async def project_say(project_id: int, body: SayIn,
                      ctx: T.Ctx = Depends(T.requires("write"))):
    async with conn() as c:
        p = await _project(c, project_id, ctx.org_id)

    await billing.ensure_signup_grant(ctx.org_id)
    have = await billing.balance(ctx.org_id)
    tier = "fast" if body.intent == "chat" else body.mode
    if have < billing.MIN_TO_START[tier]:
        raise HTTPException(402, "You're out of credits. Top up to keep building — "
                                 "everything you've made is saved.")

    async def queue_posts(posts: list[dict]) -> list[int]:
        # Bound to this workspace and project before the model ever runs.
        ids = []
        async with conn() as c:
            for post in posts:
                ids.append(await c.fetchval(
                    """INSERT INTO approvals (org_id, project_id, kind, payload)
                       VALUES ($1,$2,'post',$3) RETURNING id""",
                    ctx.org_id, project_id, post))
        return ids

    answers = p["answers"] or {}
    bridge = None
    if answers.get("source") == webflow.PROVIDER:
        st = await webflow.status(ctx.org_id)
        if not st["connected"]:
            raise HTTPException(409, "Webflow isn't connected for this workspace — reconnect to keep editing.")

        async def queue_approval(payload: dict) -> int:
            async with conn() as c:
                return await c.fetchval(
                    """INSERT INTO approvals (org_id, project_id, kind, payload)
                       VALUES ($1,$2,'webflow_action',$3) RETURNING id""",
                    ctx.org_id, project_id, payload)
        bridge = webflow.Bridge(ctx.org_id, project_id, answers.get("site") or {},
                                st["auto_publish"], queue_approval)

    turn = await _run(body.message, answers, project=True,
                      queue_posts=queue_posts, model=_model(body.mode, body.intent),
                      intent=body.intent, bridge=bridge)
    spent, left = await billing.charge_usage(ctx.org_id, ctx.user_id, project_id, turn.calls)
    async with conn() as c:
        await c.execute(
            "UPDATE projects SET answers=$3, updated_at=now() WHERE id=$1 AND org_id=$2",
            project_id, ctx.org_id, turn.answers)
    if turn.posts:
        await log_event(ctx.org_id, "agent.posts_drafted", str(len(turn.posts)),
                        project_id, ctx.user_id)
    return _payload(turn.answers, turn.actions, turn.log, turn.posts) | {
        "credits": {"spent": spent, "balance": left}}


@router.get("/projects/{project_id}/preview", response_class=HTMLResponse)
async def project_preview(project_id: int, ctx: T.Ctx = Depends(T.current_ctx)):
    async with conn() as c:
        p = await _project(c, project_id, ctx.org_id)
    return HTMLResponse(site.render((p["answers"] or {}).get("site")),
                        headers=_preview_headers())


def _preview_headers() -> dict:
    # Rendered pages carry no script; this makes that a rule, not a habit.
    return {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                       "img-src 'self' data:; frame-ancestors 'self'",
            "Cache-Control": "no-store"}
