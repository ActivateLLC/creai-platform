"""
Who you are, which tenant you are acting in, and what you may do there.

Three dependencies, and every route uses one of them:

  current_user   — signed in. Says nothing about a tenant.
  current_ctx    — signed in AND a member of the org in play. The default.
  platform_admin — staff. Separate table, every use logged.

`Ctx` carries `org_id`. Services take `ctx`, never a bare id, so a query cannot
be written without a tenant in scope by accident.
"""

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException

from .db import ROLES, conn

CODE_TTL = timedelta(minutes=15)
INVITE_TTL = timedelta(days=7)

# What each role may do. Checked by require(), never inferred at a call site.
CAN = {
    "owner":  {"read", "write", "approve", "billing", "members", "delete"},
    "admin":  {"read", "write", "approve", "members"},
    "member": {"read", "write", "approve"},
    "viewer": {"read"},
}


def _hash(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    return s or "workspace"


@dataclass(frozen=True)
class Ctx:
    """A request's tenant context. Pass this around, not loose ids."""
    user_id: int
    email: str
    org_id: int
    org_name: str
    role: str

    def may(self, action: str) -> bool:
        return action in CAN.get(self.role, set())

    def require(self, action: str) -> None:
        if not self.may(action):
            raise HTTPException(
                403, f"your role ({self.role}) cannot {action} in this workspace")


# ---------------------------------------------------------------- sign in

async def issue_code(email: str) -> str:
    code = f"{secrets.randbelow(1_000_000):06d}"
    async with conn() as c:
        await c.execute(
            "INSERT INTO login_codes (code_hash, email, expires_at) VALUES ($1,$2,$3)",
            _hash(f"{email.lower().strip()}:{code}"), email.lower().strip(),
            datetime.now(timezone.utc) + CODE_TTL)
    return code


async def redeem_code(email: str, code: str,
                      first_touch: dict | None = None) -> tuple[str, bool]:
    """Returns (session token, created_org). Creates the user and, on first
    sign-in, a personal workspace they own."""
    email = email.lower().strip()
    h = _hash(f"{email}:{code}")
    async with conn() as c:
        row = await c.fetchrow(
            """SELECT 1 FROM login_codes
               WHERE code_hash=$1 AND used_at IS NULL AND expires_at > now()""", h)
        if not row:
            raise HTTPException(401, "that code is wrong or has expired")
        await c.execute("UPDATE login_codes SET used_at=now() WHERE code_hash=$1", h)
        return await start_session(c, email, first_touch=first_touch)


async def start_session(c, email: str, name: str | None = None,
                        first_touch: dict | None = None) -> tuple[str, bool]:
    """Find or create the person and their first workspace; open a session.
    Shared by every way of signing in, so they all land in the same account.

    first_touch records where somebody came from, and is written once — COALESCE
    keeps the original, because whatever introduced them is what earned the
    signup. A later visit through an ad must not take the credit."""
    email = email.lower().strip()
    created = False
    t = first_touch or {}
    user = await c.fetchrow(
        """INSERT INTO users (email, name, source, source_detail, landed_on, referrer)
           VALUES ($1, $2, $3, $4, $5, $6)
           ON CONFLICT (email) DO UPDATE
             SET name = COALESCE(users.name, EXCLUDED.name),
                 source = COALESCE(users.source, EXCLUDED.source),
                 source_detail = COALESCE(users.source_detail, EXCLUDED.source_detail),
                 landed_on = COALESCE(users.landed_on, EXCLUDED.landed_on),
                 referrer = COALESCE(users.referrer, EXCLUDED.referrer)
           RETURNING *""",
        email, name, t.get("channel"), (t.get("detail") or "")[:200] or None,
        (t.get("landed_on") or "")[:300] or None, (t.get("referrer") or "")[:400] or None)

    org_id = await c.fetchval(
        "SELECT org_id FROM memberships WHERE user_id=$1 ORDER BY created_at LIMIT 1",
        user["id"])

    if org_id is None:
        base = slugify(email.split("@")[0])
        slug = base
        while await c.fetchval("SELECT 1 FROM organizations WHERE slug=$1", slug):
            slug = f"{base}-{secrets.token_hex(2)}"
        org = await c.fetchrow(
            "INSERT INTO organizations (name, slug) VALUES ($1,$2) RETURNING *",
            email.split("@")[0], slug)
        await c.execute(
            "INSERT INTO memberships (org_id, user_id, role) VALUES ($1,$2,'owner')",
            org["id"], user["id"])
        org_id = org["id"]
        created = True

    token = "creai_s_" + secrets.token_urlsafe(32)
    await c.execute(
        "INSERT INTO sessions (token_hash, user_id, org_id) VALUES ($1,$2,$3)",
        _hash(token), user["id"], org_id)
    return token, created


# ---------------------------------------------------------------- dependencies

async def _session(authorization: str | None):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "sign in to continue")
    token = authorization.split(None, 1)[1].strip()
    async with conn() as c:
        row = await c.fetchrow(
            """SELECT s.org_id, u.* FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = $1""", _hash(token))
        if not row:
            raise HTTPException(401, "your session has expired")
        await c.execute(
            "UPDATE sessions SET last_seen=now() WHERE token_hash=$1", _hash(token))
    return row


async def current_user(authorization: str = Header(None)):
    return await _session(authorization)


async def current_ctx(authorization: str = Header(None),
                      x_org: str | None = Header(None)) -> Ctx:
    """The default dependency. Resolves the active workspace and asserts
    membership — a user may only ever act inside an org they belong to."""
    u = await _session(authorization)
    org_id = int(x_org) if x_org and x_org.isdigit() else u["org_id"]
    if org_id is None:
        raise HTTPException(400, "no workspace selected")

    async with conn() as c:
        m = await c.fetchrow(
            """SELECT m.role, o.name, o.status FROM memberships m
               JOIN organizations o ON o.id = m.org_id
               WHERE m.org_id=$1 AND m.user_id=$2""", org_id, u["id"])
    if not m:
        # Deliberately 404, not 403: an outsider learns nothing about whether
        # this workspace exists.
        raise HTTPException(404, "no such workspace")
    if m["status"] != "active":
        raise HTTPException(403, "this workspace is suspended")

    return Ctx(user_id=u["id"], email=u["email"], org_id=org_id,
               org_name=m["name"], role=m["role"])


def requires(action: str):
    """Route-level permission: `dep = Depends(requires('approve'))`."""
    async def dep(ctx: Ctx = Depends(current_ctx)) -> Ctx:
        ctx.require(action)
        return ctx
    return dep


async def platform_admin(authorization: str = Header(None),
                         x_reason: str | None = Header(None)):
    """Staff access. Separate table, and a reason header is required — an
    unexplained look at a customer's data is not a supported operation."""
    u = await _session(authorization)
    async with conn() as c:
        row = await c.fetchrow(
            "SELECT * FROM platform_admins WHERE user_id=$1", u["id"])
    if not row:
        raise HTTPException(404, "not found")
    if not x_reason or len(x_reason.strip()) < 8:
        raise HTTPException(
            400, "an X-Reason header explaining this access is required")
    return {"user_id": u["id"], "email": u["email"], "level": row["level"],
            "reason": x_reason.strip()}


async def log_admin(admin_id: int, action: str, reason: str,
                    org_id: int | None = None) -> None:
    async with conn() as c:
        await c.execute(
            """INSERT INTO admin_access_log (admin_id, org_id, action, reason)
               VALUES ($1,$2,$3,$4)""", admin_id, org_id, action, reason)


def new_invite_token() -> str:
    return secrets.token_urlsafe(24)


def hash_invite(tok: str) -> str:
    return _hash(tok)
