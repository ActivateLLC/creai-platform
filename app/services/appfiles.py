"""
Files belonging to the people who use an app a customer built.

The gap this closes: a client portal could list an invoice but not hand over the
PDF, a booking could not carry a photo of the job, an application could not take
a CV. Records held text and numbers and nothing else.

A file here belongs to one app and, when the collection is private, to one person
in it — the same four access levels the records use, enforced the same way. Bytes
live in the same bucket as everything else; the row remembers who may read them.

Nothing is public by URL. Every read goes through the API with the app's token and
the person's session, so a link copied out of one account is worthless in another.
"""

import logging
import secrets

from ..core.db import conn
from . import assets

log = logging.getLogger("creai.appfiles")

# What an app's users may hand over. Images and PDFs cover receipts, photos of a
# leak, a signed form, a CV. Video is deliberately absent: it is a different cost
# and a different product decision.
TYPES = {
    "image/jpeg": "image", "image/png": "image", "image/webp": "image",
    "application/pdf": "pdf",
}
MAX_BYTES = 12 * 1024 * 1024
MAX_PER_APP = 2_000


class FileError(ValueError):
    """Something the person uploading should be told, in plain words."""


def kind_of(mime: str) -> str:
    kind = TYPES.get((mime or "").split(";")[0].strip().lower())
    if not kind:
        raise FileError("that file type isn't accepted — images and PDFs only")
    return kind


def describe(r) -> dict:
    return {"id": r["id"], "name": r["name"], "mime": r["mime"], "size": r["size"],
            "collection": r["collection"], "created_at": r["created_at"].isoformat(),
            "url": f"/v1/appfiles/{r['id']}/{r['token']}"}


async def save(project_id: int, org_id: int, collection: str, data: bytes, name: str,
               mime: str, app_user_id: int | None) -> dict:
    """Store one file against an app, and against a person when there is one."""
    if not assets.configured():
        raise FileError("file uploads aren't switched on yet")
    if not data:
        raise FileError("that file was empty")
    if len(data) > MAX_BYTES:
        raise FileError(f"files can be up to {MAX_BYTES // (1024 * 1024)} MB")
    kind = kind_of(mime)
    head = data[:16]
    sniffs = assets.TYPES.get(mime, (None, []))[1]
    if sniffs and not any(head.startswith(sig) for sig in sniffs):
        raise FileError("that file isn't what its name says it is, so it wasn't saved")

    async with conn() as c:
        n = await c.fetchval("SELECT count(*) FROM app_files WHERE project_id=$1", project_id)
    if n >= MAX_PER_APP:
        raise FileError("this app has reached its file limit")

    token = secrets.token_urlsafe(18)
    key = f"app/{project_id}/{secrets.token_hex(12)}/{assets.clean_name(name)}"
    await assets.put_blob(key, data, mime)
    async with conn() as c:
        row = await c.fetchrow(
            """INSERT INTO app_files (org_id, project_id, collection, app_user_id,
                                      name, mime, kind, size, key, token)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *""",
            org_id, project_id, collection, app_user_id, assets.clean_name(name),
            mime, kind, len(data), key, token)
    return describe(row)


async def get(project_id: int, file_id: int, token: str):
    """The row, if the id and its token agree. The token stops one file's link
    being guessed from another's."""
    async with conn() as c:
        return await c.fetchrow(
            "SELECT * FROM app_files WHERE id=$1 AND project_id=$2 AND token=$3",
            file_id, project_id, token)


async def listing(project_id: int, org_id: int, collection: str,
                  app_user_id: int | None, only_own: bool) -> list[dict]:
    async with conn() as c:
        if only_own:
            rows = await c.fetch(
                """SELECT * FROM app_files WHERE project_id=$1 AND org_id=$2 AND collection=$3
                   AND app_user_id=$4 ORDER BY id DESC LIMIT 200""",
                project_id, org_id, collection, app_user_id)
        else:
            rows = await c.fetch(
                """SELECT * FROM app_files WHERE project_id=$1 AND org_id=$2 AND collection=$3
                   ORDER BY id DESC LIMIT 200""", project_id, org_id, collection)
    return [describe(r) for r in rows]


async def remove(project_id: int, file_id: int, app_user_id: int | None, only_own: bool) -> bool:
    async with conn() as c:
        if only_own:
            r = await c.execute(
                "DELETE FROM app_files WHERE id=$1 AND project_id=$2 AND app_user_id=$3",
                file_id, project_id, app_user_id)
        else:
            r = await c.execute("DELETE FROM app_files WHERE id=$1 AND project_id=$2",
                                file_id, project_id)
    return not r.endswith("0")


async def bytes_of(key: str) -> bytes | None:
    return await assets.blob(key)
