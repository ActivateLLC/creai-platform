"""
Anonymous drafts — the work someone does before they have an account.

A stranger describes their idea, we generate a site, they iterate and see it on a
preview URL. None of that needs an account, and demanding one first is where most
of them leave.

The account is required at the **commitment boundary**: the first action that
spends money, is irreversible, or touches something they own. Buying a domain,
connecting a domain they already have, deploying to a real address, connecting a
social account, filing an entity. You cannot register a domain to an anonymous
session, so that is where the wall belongs — and by then they have seen their own
business on screen, which is why it converts.

A draft is keyed to a random token held in a cookie, has no org_id, and expires.
Signing up claims it into the new workspace.
"""

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from ..core import tenancy as T
from ..core.config import settings
from ..core.db import conn, log_event

router = APIRouter(prefix="/v1/drafts", tags=["drafts"])

DRAFT_TTL = timedelta(days=30)
COOKIE = "creai_draft"

# Actions that require an account. Everything else is free to try.
COMMITMENT = {
    "buy_domain": "Registering a domain needs an account — it is registered in your name.",
    "connect_domain": "Connecting a domain you own needs an account so we can verify it is yours.",
    "deploy": "Publishing to a real address needs an account.",
    "connect_channel": "Connecting a social account needs an account of your own first.",
    "file_entity": "Forming a company needs an account — we file it in your name.",
}


class StartIn(BaseModel):
    brief: str = Field(min_length=3, max_length=2000)


class AnswerIn(BaseModel):
    key: str
    value: str


def _token() -> str:
    return "d_" + secrets.token_urlsafe(24)


@router.post("/start")
async def start(body: StartIn, response: Response,
                creai_draft: str | None = Cookie(None)):
    """No auth. One sentence in, a draft project out."""
    tok = creai_draft or _token()
    async with conn() as c:
        row = await c.fetchrow(
            """INSERT INTO drafts (token, brief, expires_at)
               VALUES ($1,$2,$3)
               ON CONFLICT (token) DO UPDATE SET brief=EXCLUDED.brief, updated_at=now()
               RETURNING id, token, brief, answers, created_at""",
            tok, body.brief.strip(), datetime.now(timezone.utc) + DRAFT_TTL)
    # secure only outside development: a Secure cookie is dropped over plain
    # HTTP, which would silently break every local dev session.
    response.set_cookie(COOKIE, tok, max_age=int(DRAFT_TTL.total_seconds()),
                        httponly=True, samesite="lax",
                        secure=settings.env != "development")
    return {"draft_id": row["id"], "brief": row["brief"], "answers": row["answers"],
            "signed_in": False}


@router.get("")
async def read(creai_draft: str | None = Cookie(None)):
    """What the client calls on load: is there work in progress?"""
    if not creai_draft:
        return {"draft": None}
    async with conn() as c:
        row = await c.fetchrow(
            "SELECT id, brief, answers, created_at FROM drafts WHERE token=$1 AND expires_at > now()",
            creai_draft)
    if not row:
        return {"draft": None}
    return {"draft": {"id": row["id"], "brief": row["brief"], "answers": row["answers"],
                      "created_at": row["created_at"].isoformat()}}


@router.post("/answer")
async def answer(body: AnswerIn, creai_draft: str | None = Cookie(None)):
    """The narrowing questions, asked beside a result rather than before one.

    Same information the old flow demanded up front, gathered when it is
    obviously useful because the answer visibly changes what is on screen.
    """
    if not creai_draft:
        raise HTTPException(404, "no draft in progress")
    async with conn() as c:
        row = await c.fetchrow(
            """UPDATE drafts SET answers = answers || $2::jsonb, updated_at=now()
               WHERE token=$1 AND expires_at > now() RETURNING answers""",
            creai_draft, {body.key: body.value})
    if not row:
        raise HTTPException(404, "that draft has expired")
    return {"answers": row["answers"]}


@router.get("/gate/{action}")
async def gate(action: str):
    """Why the wall is here. The client shows this above the sign-in form."""
    reason = COMMITMENT.get(action)
    if not reason:
        raise HTTPException(404, "unknown action")
    return {"action": action, "requires_account": True, "reason": reason}


@router.post("/claim")
async def claim(ctx: T.Ctx = Depends(T.current_ctx),
                creai_draft: str | None = Cookie(None)):
    """Called immediately after sign-up. Moves the anonymous work into the
    workspace so nothing done before the wall is lost."""
    if not creai_draft:
        return {"claimed": False}
    async with conn() as c:
        d = await c.fetchrow(
            "SELECT * FROM drafts WHERE token=$1 AND claimed_at IS NULL AND expires_at > now()",
            creai_draft)
        if not d:
            return {"claimed": False}
        name = (d["brief"].split(".")[0] or "Untitled")[:120]
        p = await c.fetchrow(
            """INSERT INTO projects (org_id, created_by, name, path, brief, answers)
               VALUES ($1,$2,$3,'launch',$4,$5) RETURNING id""",
            ctx.org_id, ctx.user_id, name, d["brief"], d["answers"])
        await c.execute(
            "UPDATE drafts SET claimed_at=now(), claimed_by=$2 WHERE id=$1",
            d["id"], ctx.user_id)
    await log_event(ctx.org_id, "draft.claimed", name, p["id"], ctx.user_id)
    return {"claimed": True, "project_id": p["id"], "name": name}
