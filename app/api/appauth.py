"""
Sign-in for the people who use an app a customer built.

Called from the app itself, which holds an app token but no session until somebody
signs in. Rate-limited per app and per address, because an open sign-up endpoint
is an invitation.

Nothing here touches the owner's Creai session: an app's users and Creai's
customers are separate populations that never meet.
"""

import logging
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from ..core import tenancy as T
from ..services import appauth, appfs

log = logging.getLogger("creai.appauth")
router = APIRouter(prefix="/v1/appauth", tags=["appauth"])

RATE = (20, 600)                 # per app+address: 20 attempts per 10 minutes
_hits: dict[str, deque] = defaultdict(deque)


def _app(tok: str | None) -> tuple[int, int, str]:
    if not tok:
        raise HTTPException(401, "missing app token")
    try:
        return appfs.verify(tok)
    except appfs.AppError as exc:
        raise HTTPException(401, str(exc))


def _limit(key: str) -> None:
    now = time.monotonic()
    q = _hits[key]
    while q and now - q[0] > RATE[1]:
        q.popleft()
    if len(q) >= RATE[0]:
        raise HTTPException(429, "Too many attempts. Wait a few minutes and try again.")
    q.append(now)


class SignUpIn(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=appauth.MAX_PASSWORD)
    name: str | None = Field(None, max_length=80)


class SignInIn(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=appauth.MAX_PASSWORD)


@router.post("/signup")
async def signup(body: SignUpIn, request: Request, x_app_token: str | None = Header(None)):
    pid, oid, _role = _app(x_app_token)
    _limit(f"{pid}:{request.client.host if request.client else '?'}")
    try:
        user, session = await appauth.sign_up(pid, oid, body.email, body.password, body.name)
    except appauth.AuthError as exc:
        raise HTTPException(400, str(exc))
    return {"user": user, "session": session}


@router.post("/signin")
async def signin(body: SignInIn, request: Request, x_app_token: str | None = Header(None)):
    pid, oid, _role = _app(x_app_token)
    _limit(f"{pid}:{request.client.host if request.client else '?'}")
    try:
        user, session = await appauth.sign_in(pid, oid, body.email, body.password)
    except appauth.AuthError as exc:
        raise HTTPException(401, str(exc))
    return {"user": user, "session": session}


@router.get("/me")
async def me(x_app_token: str | None = Header(None), x_app_session: str | None = Header(None)):
    """Who is signed in, if anyone. Never an error: not signed in is an answer."""
    pid, _oid, _role = _app(x_app_token)
    if not x_app_session:
        return {"user": None}
    try:
        spid, _o, uid = appauth.open_session(x_app_session)
    except appauth.AuthError:
        return {"user": None}
    if spid != pid:
        return {"user": None}
    return {"user": await appauth.get(pid, uid)}


# ---------------------------------------------------------------- for the owner

@router.get("/users/{project_id}")
async def users(project_id: int, ctx: T.Ctx = Depends(T.requires("write"))):
    """The owner seeing who uses their app. Emails and names, never passwords.
    Scoped by org_id in the query, so another workspace's project returns nothing."""
    return {"users": await appauth.listing(project_id, ctx.org_id)}
