"""Workspaces: create, rename, invite, change roles, remove."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from ..core import tenancy as T
from ..core.db import ROLES, conn, log_event
from ..services import mailer

router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])


class OrgIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.post("")
async def create(body: OrgIn, u=Depends(T.current_user)):
    import secrets
    base = T.slugify(body.name)
    async with conn() as c:
        slug = base
        while await c.fetchval("SELECT 1 FROM organizations WHERE slug=$1", slug):
            slug = f"{base}-{secrets.token_hex(2)}"
        org = await c.fetchrow(
            "INSERT INTO organizations (name, slug) VALUES ($1,$2) RETURNING *",
            body.name.strip(), slug)
        await c.execute(
            "INSERT INTO memberships (org_id, user_id, role) VALUES ($1,$2,'owner')",
            org["id"], u["id"])
    return {"id": org["id"], "name": org["name"], "slug": org["slug"], "role": "owner"}


@router.get("/members")
async def members(ctx: T.Ctx = Depends(T.current_ctx)):
    async with conn() as c:
        rows = await c.fetch(
            """SELECT u.id, u.email, u.name, m.role, m.created_at
               FROM memberships m JOIN users u ON u.id = m.user_id
               WHERE m.org_id=$1 ORDER BY m.created_at""", ctx.org_id)
        pending = await c.fetch(
            """SELECT id, email, role, created_at FROM invitations
               WHERE org_id=$1 AND accepted_at IS NULL AND expires_at > now()""",
            ctx.org_id)
    return {
        "members": [dict(r) | {"created_at": r["created_at"].isoformat()} for r in rows],
        "invited": [dict(r) | {"created_at": r["created_at"].isoformat()} for r in pending],
        "your_role": ctx.role,
    }


class InviteIn(BaseModel):
    email: EmailStr
    role: str = "member"


@router.post("/invite")
async def invite(body: InviteIn, ctx: T.Ctx = Depends(T.requires("members"))):
    if body.role not in ROLES or body.role == "owner":
        raise HTTPException(400, "role must be admin, member or viewer")
    async with conn() as c:
        seats = await c.fetchval(
            "SELECT seats FROM organizations WHERE id=$1", ctx.org_id)
        used = await c.fetchval(
            "SELECT count(*) FROM memberships WHERE org_id=$1", ctx.org_id)
        if used >= seats:
            raise HTTPException(
                402, f"this workspace has {seats} seats and all are in use")

        tok = T.new_invite_token()
        await c.execute(
            """INSERT INTO invitations (org_id, email, role, token_hash, invited_by, expires_at)
               VALUES ($1,$2,$3,$4,$5,$6)""",
            ctx.org_id, str(body.email).lower(), body.role, T.hash_invite(tok),
            ctx.user_id, datetime.now(timezone.utc) + T.INVITE_TTL)

    mailer.send_invite(str(body.email), ctx.org_name, tok)
    await log_event(ctx.org_id, "member.invited", str(body.email), actor_id=ctx.user_id)
    return {"invited": str(body.email), "role": body.role, "expires_in_days": 7}


class AcceptIn(BaseModel):
    token: str


@router.post("/accept")
async def accept(body: AcceptIn, u=Depends(T.current_user)):
    async with conn() as c:
        inv = await c.fetchrow(
            """SELECT * FROM invitations
               WHERE token_hash=$1 AND accepted_at IS NULL AND expires_at > now()""",
            T.hash_invite(body.token.strip()))
        if not inv:
            raise HTTPException(404, "that invitation is invalid or has expired")
        if inv["email"].lower() != u["email"].lower():
            raise HTTPException(403, "this invitation was sent to a different address")
        await c.execute(
            """INSERT INTO memberships (org_id, user_id, role) VALUES ($1,$2,$3)
               ON CONFLICT (org_id, user_id) DO NOTHING""",
            inv["org_id"], u["id"], inv["role"])
        await c.execute(
            "UPDATE invitations SET accepted_at=now() WHERE id=$1", inv["id"])
    await log_event(inv["org_id"], "member.joined", u["email"], actor_id=u["id"])
    return {"org_id": inv["org_id"], "role": inv["role"]}


class RoleIn(BaseModel):
    user_id: int
    role: str


@router.post("/role")
async def set_role(body: RoleIn, ctx: T.Ctx = Depends(T.requires("members"))):
    if body.role not in ROLES:
        raise HTTPException(400, f"role must be one of {list(ROLES)}")
    if body.user_id == ctx.user_id:
        raise HTTPException(400, "you cannot change your own role")
    async with conn() as c:
        if body.role != "owner":
            owners = await c.fetchval(
                "SELECT count(*) FROM memberships WHERE org_id=$1 AND role='owner'",
                ctx.org_id)
            target = await c.fetchval(
                "SELECT role FROM memberships WHERE org_id=$1 AND user_id=$2",
                ctx.org_id, body.user_id)
            if target == "owner" and owners <= 1:
                raise HTTPException(400, "a workspace must keep at least one owner")
        r = await c.execute(
            "UPDATE memberships SET role=$1 WHERE org_id=$2 AND user_id=$3",
            body.role, ctx.org_id, body.user_id)
    if r.endswith("0"):
        raise HTTPException(404, "that person is not in this workspace")
    await log_event(ctx.org_id, "member.role", f"{body.user_id} → {body.role}",
                    actor_id=ctx.user_id)
    return {"user_id": body.user_id, "role": body.role}


@router.delete("/members/{user_id}")
async def remove(user_id: int, ctx: T.Ctx = Depends(T.requires("members"))):
    async with conn() as c:
        role = await c.fetchval(
            "SELECT role FROM memberships WHERE org_id=$1 AND user_id=$2",
            ctx.org_id, user_id)
        if not role:
            raise HTTPException(404, "that person is not in this workspace")
        if role == "owner":
            owners = await c.fetchval(
                "SELECT count(*) FROM memberships WHERE org_id=$1 AND role='owner'",
                ctx.org_id)
            if owners <= 1:
                raise HTTPException(400, "a workspace must keep at least one owner")
        await c.execute(
            "DELETE FROM memberships WHERE org_id=$1 AND user_id=$2", ctx.org_id, user_id)
    await log_event(ctx.org_id, "member.removed", str(user_id), actor_id=ctx.user_id)
    return {"removed": user_id}
