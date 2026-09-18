"""
Agent routes — the conversation beside the live preview.

Anonymous drafts can talk to the agent (that is the product's front door), so they
are rate-limited per draft and per address. Signed-in projects use the same loop,
scoped to a project the caller's workspace owns, and can also queue post drafts.
"""

import time
from collections import defaultdict, deque
import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..core import tenancy as T
from ..core.config import settings
from ..core.db import conn, log_event
from ..services import agent, appfs, billing, site, webflow
from .drafts import DRAFT_TTL, _token, draft_token, remember

log = logging.getLogger("creai.agent")
router = APIRouter(prefix="/v1/agent", tags=["agent"])

DRAFT_TURN_CAP = 5               # free messages before an account is needed
IP_WINDOW, IP_CAP = 600, 30      # per address: 30 turns per 10 minutes
_hits: dict[str, deque] = defaultdict(deque)


class SayIn(BaseModel):
    message: str = Field(min_length=1, max_length=agent.MAX_USER_CHARS)
    mode: str = Field("best", pattern="^(best|fast)$")
    intent: str = Field("build", pattern="^(build|chat|plan)$")
    asset_ids: list[int] = Field(default_factory=list, max_length=8)
    timezone: str | None = Field(None, max_length=64, pattern=r"^[A-Za-z_]+(/[A-Za-z0-9_+\-]+){0,2}$")


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
async def draft_thread(creai_draft: str | None = Depends(draft_token)):
    if not creai_draft:
        return _payload({})
    async with conn() as c:
        row = await c.fetchrow(
            "SELECT answers FROM drafts WHERE token=$1 AND expires_at > now()", creai_draft)
    return _payload(row["answers"] if row else {})


@router.post("/draft")
async def draft_say(body: SayIn, request: Request, response: Response,
                    creai_draft: str | None = Depends(draft_token)):
    _limit(request.client.host if request.client else "unknown")
    if body.asset_ids:
        raise HTTPException(401, "Sign in to attach photos, videos or files.")
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
            remember(response, tok)

    answers = dict(row["answers"] or {})
    turns = int(answers.get("_turns", 0))
    if turns >= DRAFT_TURN_CAP:
        raise HTTPException(402, "You've used your free messages. Create an account to "
                                 "keep building — your draft is saved and you get "
                                 f"{billing.SIGNUP_CREDITS} free credits.")

    turn = await _run(body.message, answers, model=_model(body.mode, body.intent), tz=body.timezone,
                      intent=body.intent)
    turn.answers["_turns"] = turns + 1
    async with conn() as c:
        await c.execute("UPDATE drafts SET answers=$2, updated_at=now() WHERE id=$1",
                        row["id"], turn.answers)
    return _payload(turn.answers, turn.actions, turn.log)


@router.get("/draft/preview", response_class=HTMLResponse)
async def draft_preview(creai_draft: str | None = Depends(draft_token)):
    answers = {}
    if creai_draft:
        async with conn() as c:
            row = await c.fetchrow(
                "SELECT answers FROM drafts WHERE token=$1 AND expires_at > now()", creai_draft)
        answers = (row["answers"] if row else None) or {}
    return HTMLResponse(site.render(answers.get("site")), headers=_preview_headers())


# ---------------------------------------------------------------- signed in

async def _refresh_thumb(org_id: int, project_id: int, path: str, spec: dict | None):
    """A fresh picture of the project, after the work is done."""
    from ..services import appfs, site as site_spec, thumbs
    try:
        if path == "app":
            files = await appfs.files(project_id, org_id)
            if not files:
                return
            html = appfs.preview(files, spec or {}, appfs.token(project_id, org_id), settings.public_url)
        else:
            html = site_spec.render(spec or {})
        await thumbs.refresh(org_id, project_id, html)
    except Exception:
        log.debug("thumbnail refresh failed", exc_info=True)


FIX_PREFIX = "The preview shows this error, please fix it: "
FREE_FIXES = 3


def p_path(p) -> str:
    return p["path"] if "path" in p.keys() else ""


async def _project(c, project_id: int, org_id: int):
    p = await c.fetchrow(
        "SELECT id, name, path, answers FROM projects WHERE id=$1 AND org_id=$2",
        project_id, org_id)
    if not p:
        raise HTTPException(404, "no such project")
    return p


@router.get("/projects/{project_id}")
async def project_thread(project_id: int, ctx: T.Ctx = Depends(T.current_ctx)):
    async with conn() as c:
        p = await _project(c, project_id, ctx.org_id)
    a = p["answers"] or {}
    return _payload(a) | {"project": {"id": p["id"], "name": p["name"], "path": p["path"],
                                      "source": a.get("source"), "site": a.get("site"),
                                      "website": a.get("website"), "brand": a.get("brand"),
                                      "marketing": bool(a.get("marketing"))}}


@router.post("/projects/{project_id}")
async def project_say(project_id: int, body: SayIn,
                      ctx: T.Ctx = Depends(T.requires("write"))):
    async with conn() as c:
        p = await _project(c, project_id, ctx.org_id)

    await billing.ensure_signup_grant(ctx.org_id)
    tier = "fast" if body.intent == "chat" else body.mode
    kind = ("chat" if body.intent == "chat" else "app" if p_path(p) == "app"
            else "game" if p_path(p) == "game"
            else "market" if p_path(p) == "market" else "site")

    # Fixing an error the preview reported is on us — a few times per error.
    waived = None
    fix = FIX_PREFIX and body.message.startswith(FIX_PREFIX) and p_path(p) == "app"
    if fix:
        err = body.message[len(FIX_PREFIX):]
        async with conn() as c:
            row = await c.fetchrow(
                """UPDATE app_errors SET free_fixes = free_fixes + 1
                   WHERE project_id=$1 AND org_id=$2 AND message=$3 AND free_fixes < $4
                     AND created_at > now() - interval '1 day' RETURNING id""",
                project_id, ctx.org_id, err, FREE_FIXES)
        if row:
            waived = "fixing an error in the app Creai built"

    have = await billing.balance(ctx.org_id)
    if not waived and have < billing.MIN_TO_START[tier]:
        from ..services import credit_alerts
        st = await credit_alerts.status(ctx.org_id, tier, kind)
        refill = (f" Your plan credits refill on {st['refill_at'][:10]}." if st["refill_at"] else "")
        raise HTTPException(402, "You're out of credits. Top up to keep building — "
                                 "everything you've made is saved." + refill)
    cap = await billing.cap_status(ctx.org_id)
    if not waived and cap["monthly_cap"] is not None:
        est = await billing.estimate(ctx.org_id, tier, kind)
        if cap["remaining"] < est["low"]:
            raise HTTPException(402, f"This workspace has reached its monthly limit of "
                                     f"{cap['monthly_cap']:,} credits. An owner can raise it under Credits.")

    async def queue_posts(posts: list[dict]) -> list[int]:
        # Bound to this workspace and project before the model ever runs.
        ids = []
        async with conn() as c:
            for post in posts:
                when = post.get("scheduled_for")
                when = datetime.fromisoformat(when) if when else None
                ids.append(await c.fetchval(
                    """INSERT INTO approvals (org_id, project_id, kind, payload, scheduled_for)
                       VALUES ($1,$2,'post',$3,$4) RETURNING id""",
                    ctx.org_id, project_id, post, when))
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

    app_ctx = (project_id, ctx.org_id) if p_path(p) == "app" else None
    game_ctx = (project_id, ctx.org_id) if p_path(p) == "game" else None
    attachments = None
    if body.asset_ids:
        from ..services import assets as asset_svc
        attachments = await asset_svc.context(ctx.org_id, body.asset_ids)

    class Reviewer:
        """Renders what was just built, runs it, and asks a fresh model what's wrong."""
        kind = "app" if p_path(p) == "app" else "site"

        async def __call__(self, turn):
            from ..services import appfs, review, site as site_spec
            if not review.configured():
                return None
            spec = turn.site or (turn.answers or {}).get("site") or {}   # what was just built, not what's stored
            brief = json.dumps({"asked_for": body.message[:400],
                                "business": spec.get("business"), "headline": spec.get("headline")})
            if self.kind == "app":
                files = await appfs.files(project_id, ctx.org_id)
                if not files:
                    return None
                html = appfs.preview(files, spec, appfs.token(project_id, ctx.org_id), settings.public_url)
            else:
                html = site_spec.render(spec)
            return await review.safe_review(self.kind, html, brief)

        def instruction(self, found):
            from ..services import review
            return review.as_instruction(found, self.kind)

    class Ideas:
        """Improvement ideas for this project's launch checklist."""
        async def add(self, items):
            from ..services import readiness
            return await readiness.add_suggestions(ctx.org_id, project_id, items)

        async def skipped(self):
            from ..services import readiness
            return await readiness.skipped_titles(project_id)

    class Drafts:
        """This project's drafted posts, bound before the model runs."""
        async def list(self):
            from ..services import posts as post_svc
            return await post_svc.pending(ctx.org_id, project_id)

        async def update(self, post_id, **changes):
            from ..services import posts as post_svc
            from ..services import social_publish
            await social_publish.cancel(post_id, ctx.org_id)
            return await post_svc.update(ctx.org_id, post_id, project_id=project_id, editor="Creai", **changes)

        async def discard(self, post_id):
            async with conn() as c:
                row = await c.fetchrow(
                    """UPDATE approvals SET state='discarded', decided_at=now(), decided_by=$4
                       WHERE id=$1 AND org_id=$2 AND project_id=$3 AND kind='post'
                         AND state IN ('pending','held','failed') RETURNING id""",
                    post_id, ctx.org_id, project_id, ctx.user_id)
            return row is not None
    turn = await _run(body.message, answers, project=True, tz=body.timezone,
                      queue_posts=queue_posts, model=_model(body.mode, body.intent),
                      intent=body.intent, bridge=bridge, app=app_ctx, game=game_ctx,
                      video=p_path(p) == "video",
                      drafts=Drafts(),
                      ideas=None if answers.get("source") or p_path(p) in ("market", "game") else Ideas(),
                      attachments=attachments,
                      reviewer=None if answers.get("source") or p_path(p) in ("market", "game") else Reviewer(),
                      marketing=bool(answers.get("marketing")) or p_path(p) == "market",
                      marketing_only=p_path(p) == "market")
    spent, left = await billing.charge_usage(ctx.org_id, ctx.user_id, project_id, turn.calls,
                                             kind=f"{tier}:{kind}", waived=waived)
    business = ((turn.answers or {}).get("site") or {}).get("business")
    async with conn() as c:
        await c.execute(
            """UPDATE projects SET answers=$3, updated_at=now(),
                 name = CASE WHEN name IN ('New app', 'Site') AND $4 <> '' THEN $4 ELSE name END
               WHERE id=$1 AND org_id=$2""",
            project_id, ctx.org_id, turn.answers, business or "")
    if turn.posts:
        await log_event(ctx.org_id, "agent.posts_drafted", str(len(turn.posts)),
                        project_id, ctx.user_id)
    if turn.site_changed or turn.app_changed:
        asyncio.create_task(_refresh_thumb(ctx.org_id, project_id, p_path(p), turn.site))
    from ..services import credit_alerts
    try:
        await credit_alerts.check(ctx.org_id)
    except Exception:                                  # an email problem never fails a turn
        log.exception("credit alert check failed")
    status = await credit_alerts.status(ctx.org_id, tier, kind)
    return _payload(turn.answers, turn.actions, turn.log, turn.posts) | {
        "credits": {"spent": spent, "balance": left, "waived": bool(waived), "status": status},
        "review": turn.review}


@router.get("/projects/{project_id}/preview", response_class=HTMLResponse)
async def project_preview(project_id: int, ctx: T.Ctx = Depends(T.current_ctx)):
    async with conn() as c:
        p = await _project(c, project_id, ctx.org_id)
    answers = p["answers"] or {}
    if p_path(p) == "app":
        page = appfs.preview(await appfs.files(project_id, ctx.org_id), answers.get("site"),
                             appfs.token(project_id, ctx.org_id), settings.public_url)
        # Even if opened directly, app code never runs with Creai's origin.
        return HTMLResponse(page, headers={
            "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-modals; frame-ancestors 'self'",
            "Cache-Control": "no-store"})
    return HTMLResponse(site.render(answers.get("site")), headers=_preview_headers())


def _preview_headers() -> dict:
    # The preview must match what a visitor will get, or motion looks broken here
    # and works in production. Only Creai's own motion script may run, by its hash.
    from ..services import site as site_spec
    return {"Content-Security-Policy": f"default-src 'none'; script-src {site_spec.motion_hash()}; "
                                       "style-src 'unsafe-inline' https://fonts.googleapis.com; "
                                       "font-src https://fonts.gstatic.com; img-src data: https:; media-src https:; "
                                       "frame-ancestors 'self'",
            "Cache-Control": "no-store"}
