"""
Photos, videos and files people give CreAI as context — and use on their sites.

Storage is a private S3-compatible bucket (Railway Buckets). Rules:

  * Browsers upload straight to the bucket with a presigned POST whose policy pins the
    exact key, content type and a size range, so the bucket itself refuses anything else.
  * After upload the server checks the object's real bytes (magic numbers) against the
    declared type and deletes mismatches. SVG, HTML and scripts are never accepted.
  * Files are served by redirecting an unguessable /f/<token> link to a short-lived
    presigned GET, with the content type and disposition we choose.
  * Every workspace has a storage allowance set by its plan.
"""

import asyncio
import re
import secrets
from functools import lru_cache
from urllib.parse import urlparse

from ..core.config import settings
from ..core.db import conn

TYPES = {
    "image/jpeg": ("image", [b"\xff\xd8\xff"]),
    "image/png": ("image", [b"\x89PNG\r\n\x1a\n"]),
    "image/webp": ("image", [b"RIFF"]),
    "image/gif": ("image", [b"GIF87a", b"GIF89a"]),
    "video/mp4": ("video", []),
    "video/quicktime": ("video", []),
    "video/webm": ("video", [b"\x1a\x45\xdf\xa3"]),
    "application/pdf": ("pdf", [b"%PDF-"]),
}
MAX_BYTES = {"image": 15 * 1024 * 1024, "video": 250 * 1024 * 1024, "pdf": 25 * 1024 * 1024}
QUOTA = {"free": 1 * 1024 ** 3, "launch": 10 * 1024 ** 3, "growth": 50 * 1024 ** 3}
UPLOAD_TTL = 900
GET_TTL = 3600
MAX_ATTACH = 8
MODEL_IMAGE_BYTES = 5 * 1024 * 1024        # the API's per-image limit
MODEL_PDF_BYTES = 25 * 1024 * 1024
NAME = re.compile(r"[^A-Za-z0-9._ -]+")


class AssetError(ValueError):
    pass


def configured() -> bool:
    return all(bool(getattr(settings, k, "")) for k in
               ("assets_bucket", "assets_key_id", "assets_secret", "assets_endpoint"))


@lru_cache(maxsize=1)
def _s3():
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3", endpoint_url=settings.assets_endpoint, aws_access_key_id=settings.assets_key_id,
        aws_secret_access_key=settings.assets_secret, region_name=settings.assets_region or "auto",
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"},
                      retries={"max_attempts": 3}))


async def _run(fn, *a, **kw):
    return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*a, **kw))


def public_url(token: str) -> str:
    return f"{settings.public_url.rstrip('/')}/f/{token}"


def is_our_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    ours = urlparse(settings.public_url)
    return p.scheme == "https" and p.hostname == ours.hostname and re.fullmatch(r"/f/[A-Za-z0-9_-]{16,64}", p.path or "") is not None


def clean_name(name: str) -> str:
    base = NAME.sub("", (name or "").strip())[-80:].strip(" .") or "file"
    return base


async def usage(org_id: int) -> int:
    async with conn() as c:
        return int(await c.fetchval(
            "SELECT COALESCE(SUM(size),0) FROM assets WHERE org_id=$1 AND status <> 'deleted'", org_id))


async def quota(org_id: int) -> int:
    from . import plans
    return QUOTA.get((await plans.current(org_id))["plan"], QUOTA["free"])


# ---------------------------------------------------------------- upload

async def start_upload(org_id: int, user_id: int, *, name: str, mime: str, size: int,
                       project_id: int | None = None, parent_id: int | None = None,
                       width: int | None = None, height: int | None = None,
                       duration: float | None = None) -> dict:
    if not configured():
        raise AssetError("uploads aren't switched on yet")
    if mime not in TYPES:
        raise AssetError("CreAI accepts photos (JPEG, PNG, WebP, GIF), videos (MP4, MOV, WebM) and PDFs")
    kind = TYPES[mime][0]
    if not 0 < size <= MAX_BYTES[kind]:
        raise AssetError(f"{kind.capitalize()}s can be up to {MAX_BYTES[kind] // (1024 * 1024)} MB")
    if await usage(org_id) + size > await quota(org_id):
        raise AssetError("This workspace is out of file storage. Delete some files or upgrade your plan.")
    async with conn() as c:
        if project_id and not await c.fetchval(
                "SELECT 1 FROM projects WHERE id=$1 AND org_id=$2", project_id, org_id):
            raise AssetError("no such project")
        if parent_id and not await c.fetchval(
                "SELECT 1 FROM assets WHERE id=$1 AND org_id=$2 AND kind='video'", parent_id, org_id):
            raise AssetError("no such video")
        token = secrets.token_urlsafe(18)
        key = f"org/{org_id}/{secrets.token_hex(12)}/{clean_name(name)}"
        aid = await c.fetchval(
            """INSERT INTO assets (org_id, project_id, parent_id, created_by, kind, mime, name, size,
                                   width, height, duration, key, token)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING id""",
            org_id, project_id, parent_id, user_id, kind, mime, clean_name(name), size,
            width, height, duration, key, token)
    post = await _run(_s3().generate_presigned_post, settings.assets_bucket, key,
                      Fields={"Content-Type": mime},
                      Conditions=[{"Content-Type": mime}, ["content-length-range", 1, MAX_BYTES[kind]]],
                      ExpiresIn=UPLOAD_TTL)
    return {"id": aid, "kind": kind, "upload": {"url": post["url"], "fields": post["fields"]}}


def _sniff(mime: str, head: bytes) -> bool:
    kind, magics = TYPES[mime]
    if mime == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if mime in ("video/mp4", "video/quicktime"):
        return head[4:8] == b"ftyp"
    return any(head.startswith(m) for m in magics)


async def complete(org_id: int, asset_id: int) -> dict:
    async with conn() as c:
        a = await c.fetchrow("SELECT * FROM assets WHERE id=$1 AND org_id=$2", asset_id, org_id)
    if not a:
        raise AssetError("no such file")
    if a["status"] == "ready":
        return describe(a)
    try:
        head = await _run(_s3().head_object, Bucket=settings.assets_bucket, Key=a["key"])
        first = await _run(_s3().get_object, Bucket=settings.assets_bucket, Key=a["key"], Range="bytes=0-15")
        magic = first["Body"].read()
    except Exception:
        raise AssetError("the upload didn't arrive; try again")
    size = int(head.get("ContentLength") or 0)
    if not _sniff(a["mime"], magic) or size > MAX_BYTES[a["kind"]] or size <= 0:
        await _run(_s3().delete_object, Bucket=settings.assets_bucket, Key=a["key"])
        async with conn() as c:
            await c.execute("UPDATE assets SET status='deleted' WHERE id=$1", asset_id)
        raise AssetError("that file isn't what its name says it is, so it was removed")
    async with conn() as c:
        a = await c.fetchrow(
            "UPDATE assets SET status='ready', size=$2 WHERE id=$1 AND org_id=$3 RETURNING *",
            asset_id, size, org_id)
    return describe(a)


def describe(a, frames: list | None = None) -> dict:
    return {"id": a["id"], "kind": a["kind"], "name": a["name"], "mime": a["mime"], "size": a["size"],
            "width": a["width"], "height": a["height"], "duration": a["duration"],
            "status": a["status"], "project_id": a["project_id"], "parent_id": a["parent_id"],
            "url": public_url(a["token"]), "created_at": a["created_at"].isoformat(),
            "frames": frames or []}


async def listing(org_id: int, project_id: int | None = None, limit: int = 100) -> list[dict]:
    async with conn() as c:
        rows = await c.fetch(
            """SELECT * FROM assets WHERE org_id=$1 AND status='ready'
               AND ($2::bigint IS NULL OR project_id=$2 OR project_id IS NULL)
               ORDER BY id DESC LIMIT $3""", org_id, project_id, limit)
    frames = {}
    for r in rows:
        if r["parent_id"]:
            frames.setdefault(r["parent_id"], []).append(public_url(r["token"]))
    return [describe(r, frames.get(r["id"])) for r in rows if not r["parent_id"]]


async def delete(org_id: int, asset_id: int) -> bool:
    async with conn() as c:
        rows = await c.fetch(
            """UPDATE assets SET status='deleted' WHERE org_id=$1 AND (id=$2 OR parent_id=$2)
               AND status <> 'deleted' RETURNING key""", org_id, asset_id)
    for r in rows:
        try:
            await _run(_s3().delete_object, Bucket=settings.assets_bucket, Key=r["key"])
        except Exception:
            pass
    return bool(rows)


async def store_bytes(org_id: int, *, name: str, mime: str, data: bytes,
                      project_id: int | None = None) -> dict:
    """Save a file the server made itself (a project thumbnail, say)."""
    if not configured():
        raise AssetError("uploads aren't switched on yet")
    if mime not in TYPES or not data:
        raise AssetError("unsupported file")
    token = secrets.token_urlsafe(18)
    key = f"org/{org_id}/{secrets.token_hex(12)}/{clean_name(name)}"
    await _run(_s3().put_object, Bucket=settings.assets_bucket, Key=key, Body=data, ContentType=mime)
    async with conn() as c:
        row = await c.fetchrow(
            """INSERT INTO assets (org_id, project_id, kind, mime, name, size, key, token, status)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'ready') RETURNING *""",
            org_id, project_id, TYPES[mime][0], mime, clean_name(name), len(data), key, token)
    return describe(row)


# ---------------------------------------------------------------- serving

async def put_blob(key: str, data: bytes, mime: str) -> None:
    """Store a file the platform owns outright (a game export), outside the asset
    library: it has no owner-facing entry, and the API serves it, not a signed URL."""
    if not configured():
        raise AssetError("uploads aren't switched on yet")
    await _run(_s3().put_object, Bucket=settings.assets_bucket, Key=key, Body=data,
               ContentType=mime)


async def blob(key: str) -> bytes | None:
    try:
        return await _bytes(key)
    except Exception:
        return None


async def signed_get(token: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token or ""):
        return None
    async with conn() as c:
        a = await c.fetchrow("SELECT key, mime, name FROM assets WHERE token=$1 AND status='ready'", token)
    if not a:
        return None
    return await _run(_s3().generate_presigned_url, "get_object",
                      Params={"Bucket": settings.assets_bucket, "Key": a["key"],
                              "ResponseContentType": a["mime"],
                              "ResponseContentDisposition": f'inline; filename="{a["name"]}"'},
                      ExpiresIn=GET_TTL)


async def ensure_cors() -> None:
    """Let the app's own pages (web and mobile) upload straight to the bucket."""
    origins = sorted({settings.public_url.rstrip("/"), "capacitor://localhost", "https://localhost"})
    await _run(_s3().put_bucket_cors, Bucket=settings.assets_bucket, CORSConfiguration={"CORSRules": [{
        "AllowedOrigins": origins, "AllowedMethods": ["POST", "PUT"], "AllowedHeaders": ["*"],
        "ExposeHeaders": ["ETag"], "MaxAgeSeconds": 3000}]})


# ---------------------------------------------------------------- model context

async def _bytes(key: str) -> bytes:
    obj = await _run(_s3().get_object, Bucket=settings.assets_bucket, Key=key)
    return await _run(obj["Body"].read)


async def context(org_id: int, ids: list[int]) -> tuple[list[dict], str, list[dict]]:
    """Content blocks for the model, a text note listing the files, and a light record
    for the saved conversation (no file bytes are ever stored in the thread)."""
    import base64
    ids = list(dict.fromkeys(int(i) for i in ids))[:MAX_ATTACH]
    if not ids:
        return [], "", []
    async with conn() as c:
        rows = await c.fetch(
            "SELECT * FROM assets WHERE org_id=$1 AND status='ready' AND id = ANY($2::bigint[])", org_id, ids)
        frames = await c.fetch(
            """SELECT * FROM assets WHERE org_id=$1 AND status='ready' AND parent_id = ANY($2::bigint[])
               ORDER BY id""", org_id, ids)
    by_id = {r["id"]: r for r in rows}
    fr = {}
    for f in frames:
        fr.setdefault(f["parent_id"], []).append(f)
    blocks, lines, record = [], [], []
    for aid in ids:
        a = by_id.get(aid)
        if not a or a["parent_id"]:
            continue
        url = public_url(a["token"])
        dims = f" {a['width']}×{a['height']}" if a["width"] and a["height"] else ""
        if a["kind"] == "image":
            if a["size"] <= MODEL_IMAGE_BYTES:
                data = await _bytes(a["key"])
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": a["mime"],
                                                           "data": base64.b64encode(data).decode()}})
            lines.append(f"- photo #{a['id']} “{a['name']}”{dims} — use on the site as {url}")
        elif a["kind"] == "pdf":
            if a["size"] <= MODEL_PDF_BYTES:
                data = await _bytes(a["key"])
                blocks.append({"type": "document", "title": a["name"], "source": {
                    "type": "base64", "media_type": "application/pdf", "data": base64.b64encode(data).decode()}})
            lines.append(f"- PDF #{a['id']} “{a['name']}” — read it; link to it as {url}")
        else:
            secs = f" {int(a['duration'] // 60)}:{int(a['duration'] % 60):02d}" if a["duration"] else ""
            for f in fr.get(aid, [])[:4]:
                data = await _bytes(f["key"])
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": f["mime"],
                                                           "data": base64.b64encode(data).decode()}})
            lines.append(f"- video #{a['id']} “{a['name']}”{dims}{secs} — stills from it are attached; "
                         f"use it on the site as hero_video {url}")
        record.append({"id": a["id"], "kind": a["kind"], "name": a["name"], "url": url,
                       "thumb": public_url(fr[aid][0]["token"]) if fr.get(aid) else (url if a["kind"] == "image" else None)})
    note = ("The person attached these files (they are theirs to use; describe only what you can see):\n"
            + "\n".join(lines)) if lines else ""
    return blocks, note, record
