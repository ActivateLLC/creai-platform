"""
Upload and download for the people who use an app.

Access follows the same app.json rules as the records beside it — public, user,
own, owner — so there is one model to reason about rather than two. A file in an
"own" collection is reachable by the person who uploaded it and nobody else.

Downloads go through here rather than a public bucket URL, which is what makes
"own" mean anything: a link lifted from one account is refused in another.
"""

import logging

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response

from ..services import appfiles
from .appdata import _allow, _auth, _user

log = logging.getLogger("creai.appfiles")
router = APIRouter(prefix="/v1/appfiles", tags=["appfiles"])


@router.post("/{collection}")
async def upload(collection: str, request: Request, file: UploadFile = File(...),
                 x_app_token: str | None = Header(None),
                 x_app_session: str | None = Header(None)):
    pid, oid, role = _auth(x_app_token, collection)
    uid = _user(pid, x_app_session)
    await _allow(pid, role, collection, "write", uid)
    data = await file.read(appfiles.MAX_BYTES + 1)
    try:
        return await appfiles.save(pid, oid, collection, data,
                                   file.filename or "upload", file.content_type or "", uid)
    except appfiles.FileError as exc:
        raise HTTPException(400, str(exc))


@router.get("/{collection}")
async def listing(collection: str, x_app_token: str | None = Header(None),
                  x_app_session: str | None = Header(None)):
    pid, oid, role = _auth(x_app_token, collection)
    uid = _user(pid, x_app_session)
    level = await _allow(pid, role, collection, "read", uid)
    return {"files": await appfiles.listing(pid, oid, collection, uid, level == "own")}


@router.get("/{file_id}/{token}")
async def download(file_id: int, token: str, x_app_token: str | None = Header(None),
                   x_app_session: str | None = Header(None)):
    """The bytes, if this caller is allowed them. Checked every time, never cached
    publicly: the same URL is a file for its owner and a 404 for anyone else."""
    pid, _oid, role = _auth(x_app_token, "files")
    row = await appfiles.get(pid, file_id, token)
    if not row:
        raise HTTPException(404, "not found")
    uid = _user(pid, x_app_session)
    level = await _allow(pid, role, row["collection"], "read", uid)
    if level == "own" and row["app_user_id"] != uid:
        raise HTTPException(404, "not found")
    data = await appfiles.bytes_of(row["key"])
    if data is None:
        raise HTTPException(404, "not found")
    return Response(data, media_type=row["mime"], headers={
        "Content-Disposition": f'inline; filename="{row["name"]}"',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff"})


@router.delete("/{file_id}/{token}")
async def remove(file_id: int, token: str, x_app_token: str | None = Header(None),
                 x_app_session: str | None = Header(None)):
    pid, _oid, role = _auth(x_app_token, "files")
    row = await appfiles.get(pid, file_id, token)
    if not row:
        raise HTTPException(404, "not found")
    uid = _user(pid, x_app_session)
    level = await _allow(pid, role, row["collection"], "manage", uid)
    if not await appfiles.remove(pid, file_id, uid, level == "own"):
        raise HTTPException(404, "not found")
    return {"ok": True}
