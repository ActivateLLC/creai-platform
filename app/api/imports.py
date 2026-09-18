"""
Bring your own site: upload a folder or point at a repo, and get hosting plus a
domain.

The rule this file exists to enforce: an imported site runs its own JavaScript,
so it is never served from the app's origin. It goes on a custom domain, or on a
separate hosting host if one is configured. Asking to publish without either is
refused, with the reason.
"""

import logging
import secrets

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

from ..core import tenancy as T
from ..core.config import settings
from ..core.db import conn, log_event
from ..services import assets, imports

log = logging.getLogger("creai.imports")
router = APIRouter(tags=["imports"])


async def _owned(c, project_id: int, org_id: int):
    p = await c.fetchrow("SELECT id, name FROM projects WHERE id=$1 AND org_id=$2",
                         project_id, org_id)
    if not p:
        raise HTTPException(404, "no such project")
    return p


@router.post("/v1/imports/{project_id}")
async def upload(project_id: int, request: Request,
                 ctx: T.Ctx = Depends(T.requires("write"))):
    """Take a site: a zipped folder, or the files themselves. Most people have
    never zipped anything, so choosing a folder has to work just as well."""
    if not assets.configured():
        raise HTTPException(503, "Hosting isn't switched on yet.")
    async with conn() as c:
        await _owned(c, project_id, ctx.org_id)

    form = await request.form()
    uploads = [v for v in form.getlist("file") + form.getlist("files")
               if isinstance(v, UploadFile)]
    if not uploads:
        raise HTTPException(400, "Choose the folder your website is in, or a zip of it.")
    # Browsers send the path each file had on the person's machine alongside it.
    paths = [str(p) for p in form.getlist("paths")]

    try:
        if len(uploads) == 1 and (uploads[0].filename or "").lower().endswith(".zip"):
            files = imports.read_zip(await uploads[0].read(imports.MAX_TOTAL + 1))
        else:
            named = []
            for i, up in enumerate(uploads):
                name = paths[i] if i < len(paths) else (up.filename or f"file{i}")
                named.append((name, await up.read(imports.MAX_FILE + 1)))
            files = imports.read_loose(named)
    except imports.ImportError_ as exc:
        raise HTTPException(400, str(exc))

    slug = secrets.token_hex(4)
    manifest, total = {}, 0
    for path, blob in files.items():
        key = f"import/{project_id}/{slug}/{path}"
        mime = imports.mime_for(path)
        await assets.put_blob(key, blob, mime)
        manifest[path] = {"key": key, "mime": mime, "size": len(blob)}
        total += len(blob)

    async with conn() as c:
        await c.execute(
            """INSERT INTO site_imports (org_id, project_id, slug, source, manifest, files, bytes)
               VALUES ($1,$2,$3,'upload',$4,$5,$6)""",
            ctx.org_id, project_id, slug, manifest, len(manifest), total)
    await log_event(ctx.org_id, "site.imported", f"{len(manifest)} files", project_id, ctx.user_id)
    return {"slug": slug, "files": len(manifest), "bytes": total,
            "can_publish_free": imports.can_serve_free_address(),
            "note": None if imports.can_serve_free_address() else
                    "Ready. Connect a domain to put it live — imported sites run their own "
                    "code, so they are never served from Creai's own address."}


@router.get("/v1/imports/{project_id}")
async def latest(project_id: int, ctx: T.Ctx = Depends(T.requires("write"))):
    async with conn() as c:
        await _owned(c, project_id, ctx.org_id)
        r = await c.fetchrow(
            """SELECT slug, source, origin, files, bytes, live, created_at
               FROM site_imports WHERE project_id=$1 ORDER BY id DESC LIMIT 1""", project_id)
    if not r:
        return {"import": None, "can_publish_free": imports.can_serve_free_address()}
    return {"import": {**dict(r), "created_at": r["created_at"].isoformat()},
            "can_publish_free": imports.can_serve_free_address()}


@router.post("/v1/imports/{project_id}/publish")
async def publish(project_id: int, request: Request,
                  ctx: T.Ctx = Depends(T.requires("approve"))):
    """Put an imported site live — but only where its own JavaScript cannot reach
    anybody's Creai session."""
    async with conn() as c:
        await _owned(c, project_id, ctx.org_id)
        found = await c.fetchrow(
            "SELECT slug FROM site_imports WHERE project_id=$1 ORDER BY id DESC LIMIT 1",
            project_id)
        if not found:
            raise HTTPException(409, "Import a folder first.")
        domain = await c.fetchval(
            "SELECT name FROM domains WHERE project_id=$1 AND org_id=$2 LIMIT 1",
            project_id, ctx.org_id)

    if not domain and not imports.can_serve_free_address():
        raise HTTPException(409,
            "An imported site runs its own code, so Creai won't serve it from its own address "
            "— a script on that address could read a signed-in visitor's account. Connect your "
            "domain and it goes live there.")

    async with conn() as c:
        await c.execute("UPDATE site_imports SET live=false WHERE project_id=$1", project_id)
        await c.execute("UPDATE site_imports SET live=true WHERE project_id=$1 AND slug=$2",
                        project_id, found["slug"])
    await log_event(ctx.org_id, "site.import_published", domain or found["slug"],
                    project_id, ctx.user_id)
    return {"published": True, "domain": domain,
            "url": f"https://{domain}" if domain
                   else f"{imports.hosting_host()}/i/{found['slug']}/"}


@router.get("/i/{slug}/{path:path}", include_in_schema=False)
async def serve(slug: str, path: str, request: Request):
    """Serve an imported site. Refuses on the app's own origin, whatever the
    route: the check is here rather than in configuration, because this is the
    thing that must never be got wrong."""
    host = (request.headers.get("host") or "").split(":")[0].lower()
    app_host = (settings.public_url or "").split("//")[-1].split("/")[0].lower()
    if host == app_host:
        raise HTTPException(404, "not found")

    async with conn() as c:
        r = await c.fetchrow(
            "SELECT manifest FROM site_imports WHERE slug=$1 AND live ORDER BY id DESC LIMIT 1",
            slug)
    if not r:
        raise HTTPException(404, "not found")
    manifest = r["manifest"] or {}
    entry = imports.entry_for(path, manifest)
    if not entry:
        entry = imports.entry_for("", manifest)          # single-page apps
        if not entry:
            raise HTTPException(404, "not found")
    meta = manifest[entry]
    body = await assets.blob(meta["key"])
    if body is None:
        raise HTTPException(404, "not found")
    return Response(body, media_type=meta["mime"], headers={
        "Content-Security-Policy": imports.SERVE_CSP,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "public, max-age=300"})
