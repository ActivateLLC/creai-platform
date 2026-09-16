"""Sign in, read yourself, switch workspace."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr

from ..core import tenancy as T
from ..core.config import settings
from ..core.db import conn
from ..services import mailer

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class StartIn(BaseModel):
    email: EmailStr


class CodeIn(BaseModel):
    email: EmailStr
    code: str


@router.post("/start")
async def start(body: StartIn):
    code = await T.issue_code(str(body.email))
    sent = mailer.send_code(str(body.email), code)
    out = {"sent": sent, "expires_in_minutes": 15}
    if settings.env == "development" and not sent:
        out["code"] = code          # development only, and only when unconfigured
    return out


@router.post("/code")
async def redeem(body: CodeIn):
    token, created = await T.redeem_code(str(body.email), body.code.strip())
    return {"token": token, "token_type": "bearer", "workspace_created": created}


@router.get("/me")
async def me(u=Depends(T.current_user)):
    """The user and every workspace they belong to. The client uses this to
    populate the switcher; `active` is what X-Org defaults to."""
    async with conn() as c:
        orgs = await c.fetch(
            """SELECT o.id, o.name, o.slug, o.plan, m.role
               FROM memberships m JOIN organizations o ON o.id = m.org_id
               WHERE m.user_id=$1 ORDER BY m.created_at""", u["id"])
        is_admin = await c.fetchval(
            "SELECT level FROM platform_admins WHERE user_id=$1", u["id"])
    return {
        "email": u["email"], "name": u["name"], "surface": u["surface"],
        "active_org": u["org_id"],
        "workspaces": [dict(o) for o in orgs],
        "platform_admin": is_admin,      # null for every customer
    }


class SwitchIn(BaseModel):
    org_id: int


@router.post("/switch")
async def switch(body: SwitchIn, u=Depends(T.current_user)):
    async with conn() as c:
        m = await c.fetchrow(
            "SELECT 1 FROM memberships WHERE org_id=$1 AND user_id=$2",
            body.org_id, u["id"])
        if not m:
            raise HTTPException(404, "no such workspace")
        await c.execute(
            "UPDATE sessions SET org_id=$1 WHERE user_id=$2", body.org_id, u["id"])
    return {"active_org": body.org_id}


class SurfaceIn(BaseModel):
    surface: str


@router.post("/surface")
async def set_surface(body: SurfaceIn, u=Depends(T.current_user)):
    if body.surface not in ("guided", "developer"):
        raise HTTPException(400, "surface must be guided or developer")
    async with conn() as c:
        await c.execute("UPDATE users SET surface=$1 WHERE id=$2", body.surface, u["id"])
    return {"surface": body.surface}
