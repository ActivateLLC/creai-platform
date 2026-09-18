"""
Data saved by apps Creai builds.

Called from app previews, which run in a sandbox with an opaque origin. Access is
by an app token (HMAC, scoped to one project, expiring), never by the owner's
session. Each app is capped so a runaway loop can't flood the database.
"""

import re
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from ..core.db import conn
from ..services import appfs

router = APIRouter(prefix="/v1/appdata", tags=["appdata"])

NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
MAX_RECORD = 20_000
MAX_RECORDS = 5_000
RATE = (120, 60)            # requests per window, seconds
_hits: dict[int, deque] = defaultdict(deque)


class RecordIn(BaseModel):
    data: dict


def _auth(tok: str | None, collection: str) -> tuple[int, int, str]:
    if not tok:
        raise HTTPException(401, "missing app token")
    try:
        pid, oid, role = appfs.verify(tok)
    except appfs.AppError as exc:
        raise HTTPException(401, str(exc))
    if not NAME.match(collection):
        raise HTTPException(400, "collection names are lowercase letters, digits and _")
    q, now = _hits[pid], time.monotonic()
    while q and now - q[0] > RATE[1]:
        q.popleft()
    if len(q) >= RATE[0]:
        raise HTTPException(429, "too many requests from this app; slow down")
    q.append(now)
    return pid, oid, role


async def _allow(pid: int, role: str, collection: str, action: str) -> None:
    """Owners (the preview) can do anything. Visitors to a published app can only
    do what the app's app.json allows for that collection."""
    if role == "owner":
        return
    async with conn() as c:
        files = await c.fetchval(
            "SELECT files FROM app_releases WHERE project_id=$1 AND live ORDER BY id DESC LIMIT 1", pid)
    rule = appfs.rules(files or {}).get(collection, {"read": "owner", "write": "owner", "manage": "owner"})
    if rule[action] != "public":
        raise HTTPException(403, f"visitors can't {action} {collection} in this app")


class ReportIn(BaseModel):
    message: str


@router.post("/_report")
async def report_error(body: ReportIn, x_app_token: str | None = Header(None)):
    """The preview reports an error; fixing it later can be free."""
    pid, oid, role = _auth(x_app_token, "errors")
    if role != "owner":
        return {"ok": True}
    async with conn() as c:
        n = await c.fetchval(
            "SELECT count(*) FROM app_errors WHERE project_id=$1 AND created_at > now() - interval '1 day'", pid)
        if n < 30:
            await c.execute(
                """INSERT INTO app_errors (org_id, project_id, message) VALUES ($1,$2,$3)
                   ON CONFLICT (project_id, message) DO NOTHING""",
                oid, pid, body.message.strip()[:500])
    return {"ok": True}


def _row(r) -> dict:
    return {"id": r["id"], **(r["data"] or {}), "created_at": r["created_at"].isoformat()}


def _size(data: dict) -> None:
    import json
    if len(json.dumps(data)) > MAX_RECORD:
        raise HTTPException(413, "record too large")


@router.get("/{collection}")
async def list_records(collection: str, x_app_token: str | None = Header(None)):
    pid, oid, role = _auth(x_app_token, collection)
    await _allow(pid, role, collection, "read")
    async with conn() as c:
        rows = await c.fetch(
            """SELECT id, data, created_at FROM app_records
               WHERE project_id=$1 AND org_id=$2 AND collection=$3 ORDER BY id DESC LIMIT 500""",
            pid, oid, collection)
    return {"items": [_row(r) for r in rows]}


@router.post("/{collection}")
async def add_record(collection: str, body: RecordIn, x_app_token: str | None = Header(None)):
    pid, oid, role = _auth(x_app_token, collection)
    await _allow(pid, role, collection, "write")
    _size(body.data)
    async with conn() as c:
        n = await c.fetchval("SELECT count(*) FROM app_records WHERE project_id=$1", pid)
        if n >= MAX_RECORDS:
            raise HTTPException(409, "this app has reached its record limit")
        r = await c.fetchrow(
            """INSERT INTO app_records (org_id, project_id, collection, data)
               VALUES ($1,$2,$3,$4) RETURNING id, data, created_at""",
            oid, pid, collection, body.data)
    return _row(r)


@router.get("/{collection}/{record_id}")
async def get_record(collection: str, record_id: int, x_app_token: str | None = Header(None)):
    pid, oid, role = _auth(x_app_token, collection)
    await _allow(pid, role, collection, "read")
    async with conn() as c:
        r = await c.fetchrow(
            """SELECT id, data, created_at FROM app_records
               WHERE id=$1 AND project_id=$2 AND org_id=$3 AND collection=$4""",
            record_id, pid, oid, collection)
    if not r:
        raise HTTPException(404, "not found")
    return _row(r)


@router.patch("/{collection}/{record_id}")
async def update_record(collection: str, record_id: int, body: RecordIn,
                        x_app_token: str | None = Header(None)):
    pid, oid, role = _auth(x_app_token, collection)
    await _allow(pid, role, collection, "manage")
    _size(body.data)
    async with conn() as c:
        r = await c.fetchrow(
            """UPDATE app_records SET data = data || $5::jsonb, updated_at=now()
               WHERE id=$1 AND project_id=$2 AND org_id=$3 AND collection=$4
               RETURNING id, data, created_at""",
            record_id, pid, oid, collection, body.data)
    if not r:
        raise HTTPException(404, "not found")
    return _row(r)


@router.delete("/{collection}/{record_id}")
async def delete_record(collection: str, record_id: int, x_app_token: str | None = Header(None)):
    pid, oid, role = _auth(x_app_token, collection)
    await _allow(pid, role, collection, "manage")
    async with conn() as c:
        res = await c.execute(
            "DELETE FROM app_records WHERE id=$1 AND project_id=$2 AND org_id=$3 AND collection=$4",
            record_id, pid, oid, collection)
    if res.endswith("0"):
        raise HTTPException(404, "not found")
    return {"ok": True}
