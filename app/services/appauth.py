"""
Accounts for the people who use an app a customer built.

These are not Creai accounts. Somebody who books a class, files a timesheet or
checks their order is a user of that one app, and nothing else: their sign-in is
scoped to a single project, and it disappears with the project.

Passwords are scrypt-hashed with a per-password salt. Nobody reads them back —
not the app, not the owner, not us. A session is a signed token carrying the user
id, verified in the same way as the app tokens beside it in appfs.

Why this lives here and not in a hosted identity service: an app that succeeds
would be billed per monthly active user, forever, by somebody else. Rows in a
table we already run cost nothing and keep the margin.
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
import time

from ..core.config import settings
from ..core.db import conn

SESSION_TTL = 30 * 24 * 3600        # a month, then sign in again
MIN_PASSWORD = 8
MAX_PASSWORD = 200                  # hashing is the expensive part; cap the input
MAX_USERS = 5_000                   # per app, alongside the record cap
EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")

# scrypt, per OWASP's cheap-and-sane end: ~64 MB, well under a worker's memory.
N, R, P = 2 ** 14, 8, 1


class AuthError(ValueError):
    """Something the person signing in should be told, in words they can act on."""


# ---------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"choose a password of at least {MIN_PASSWORD} characters")
    if len(password) > MAX_PASSWORD:
        raise AuthError("that password is too long")
    salt = os.urandom(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(key).decode()


def check_password(password: str, stored: str) -> bool:
    try:
        kind, salt_b64, key_b64 = stored.split("$")
        if kind != "scrypt":
            return False
        salt, key = base64.b64decode(salt_b64), base64.b64decode(key_b64)
    except (ValueError, TypeError):
        return False
    got = hashlib.scrypt(password.encode()[:MAX_PASSWORD], salt=salt, n=N, r=R, p=P, dklen=32)
    return hmac.compare_digest(got, key)


# ---------------------------------------------------------------- sessions

def session(project_id: int, org_id: int, user_id: int) -> str:
    """A signed session for one user of one app."""
    body = f"{project_id}.{org_id}.{user_id}.{int(time.time()) + SESSION_TTL}"
    sig = hmac.new(settings.secret_key.encode(), b"appuser:" + body.encode(),
                   hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{body}.{sig}".encode()).decode().rstrip("=")


def open_session(tok: str) -> tuple[int, int, int]:
    """(project_id, org_id, user_id), or AuthError."""
    try:
        raw = base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4)).decode()
        pid, oid, uid, exp, sig = raw.split(".")
    except (ValueError, UnicodeDecodeError):
        raise AuthError("please sign in again")
    want = hmac.new(settings.secret_key.encode(), f"appuser:{pid}.{oid}.{uid}.{exp}".encode(),
                    hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, want) or int(exp) < time.time():
        raise AuthError("please sign in again")
    return int(pid), int(oid), int(uid)


# ---------------------------------------------------------------- accounts

def _public(r) -> dict:
    return {"id": r["id"], "email": r["email"], "name": r["name"],
            "created_at": r["created_at"].isoformat()}


async def sign_up(project_id: int, org_id: int, email: str, password: str,
                  name: str | None = None) -> tuple[dict, str]:
    email = (email or "").strip().lower()
    if not EMAIL.match(email):
        raise AuthError("that doesn't look like an email address")
    pw = hash_password(password)
    async with conn() as c:
        n = await c.fetchval("SELECT count(*) FROM app_users WHERE project_id=$1", project_id)
        if n >= MAX_USERS:
            raise AuthError("this app has reached its sign-up limit")
        exists = await c.fetchval(
            "SELECT 1 FROM app_users WHERE project_id=$1 AND lower(email)=$2", project_id, email)
        if exists:
            # Says the same thing as a wrong password on sign-in would, so this
            # endpoint can't be used to discover who has an account.
            raise AuthError("that email is already registered — sign in instead")
        r = await c.fetchrow(
            """INSERT INTO app_users (org_id, project_id, email, name, pw)
               VALUES ($1,$2,$3,$4,$5) RETURNING id, email, name, created_at""",
            org_id, project_id, email, (name or "").strip()[:80] or None, pw)
    return _public(r), session(project_id, org_id, r["id"])


async def sign_in(project_id: int, org_id: int, email: str, password: str) -> tuple[dict, str]:
    email = (email or "").strip().lower()
    async with conn() as c:
        r = await c.fetchrow(
            """SELECT id, email, name, pw, blocked, created_at FROM app_users
               WHERE project_id=$1 AND lower(email)=$2""", project_id, email)
    # Hash anyway when the account is missing, so a wrong address and a wrong
    # password take the same time and neither can be told apart from outside.
    stored = r["pw"] if r else hash_password(secrets.token_urlsafe(16))
    ok = check_password(password or "", stored)
    if not r or not ok:
        raise AuthError("that email and password don't match")
    if r["blocked"]:
        raise AuthError("this account has been turned off by the app's owner")
    async with conn() as c:
        await c.execute("UPDATE app_users SET seen_at=now() WHERE id=$1", r["id"])
    return _public(r), session(project_id, org_id, r["id"])


async def get(project_id: int, user_id: int) -> dict | None:
    async with conn() as c:
        r = await c.fetchrow(
            """SELECT id, email, name, blocked, created_at FROM app_users
               WHERE id=$1 AND project_id=$2""", user_id, project_id)
    if not r or r["blocked"]:
        return None
    return _public(r)


async def listing(project_id: int, org_id: int, limit: int = 200) -> list[dict]:
    """For the owner: who uses their app. Never password material."""
    async with conn() as c:
        rows = await c.fetch(
            """SELECT id, email, name, created_at, seen_at, blocked FROM app_users
               WHERE project_id=$1 AND org_id=$2 ORDER BY id DESC LIMIT $3""",
            project_id, org_id, limit)
    return [{**_public(r), "blocked": r["blocked"],
             "seen_at": r["seen_at"].isoformat() if r["seen_at"] else None} for r in rows]
