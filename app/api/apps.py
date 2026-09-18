"""
Publishing apps Creai built.

A release is a snapshot of the app's files. The public page serves the latest live
release under a sandbox CSP, so app code runs with an opaque origin even as a
top-level page, and visitors get a public app token limited by the app's rules.
"""

import re
import secrets

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from ..core import tenancy as T
from ..core.config import settings
from ..core.db import conn, log_event
from ..services import appfs

router = APIRouter(tags=["apps"])

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")


def public_url(slug: str) -> str:
    return f"{settings.public_url.rstrip('/')}/a/{slug}"


async def _app_project(c, project_id: int, org_id: int):
    p = await c.fetchrow("SELECT id, name, path, answers FROM projects WHERE id=$1 AND org_id=$2",
                         project_id, org_id)
    if not p or p["path"] != "app":
        raise HTTPException(404, "no such app")
    return p


@router.get("/v1/apps/{project_id}/release")
async def release_status(project_id: int, ctx: T.Ctx = Depends(T.current_ctx)):
    async with conn() as c:
        await _app_project(c, project_id, ctx.org_id)
        r = await c.fetchrow(
            """SELECT slug, live, created_at FROM app_releases WHERE project_id=$1 AND org_id=$2
               ORDER BY id DESC LIMIT 1""", project_id, ctx.org_id)
    if not r:
        return {"published": False}
    return {"published": r["live"], "url": public_url(r["slug"]), "at": r["created_at"].isoformat()}


@router.post("/v1/apps/{project_id}/publish")
async def publish(project_id: int, ctx: T.Ctx = Depends(T.requires("approve"))):
    async with conn() as c:
        p = await _app_project(c, project_id, ctx.org_id)
        files = await appfs.files(project_id, ctx.org_id)
        if files.get(appfs.ENTRY, "") == appfs.STARTER[appfs.ENTRY]:
            raise HTTPException(409, "Build the app first — it's still the starter screen.")
        errors = await c.fetchval(
            """SELECT count(*) FROM app_errors WHERE project_id=$1
               AND created_at > (SELECT COALESCE(MAX(updated_at), 'epoch') FROM project_files WHERE project_id=$1)""",
            project_id)
        if errors:
            raise HTTPException(409, "The app reported an error since its last change. Fix it before publishing.")
        slug = await c.fetchval(
            "SELECT slug FROM app_releases WHERE project_id=$1 ORDER BY id DESC LIMIT 1", project_id)
        if not slug:
            base = re.sub(r"[^a-z0-9]+", "-", (p["answers"] or {}).get("site", {}).get("business", "")
                          .lower() or p["name"].lower()).strip("-")[:40] or "app"
            slug = f"{base}-{secrets.token_hex(3)}"
        await c.execute("UPDATE app_releases SET live=false WHERE project_id=$1", project_id)
        await c.execute(
            """INSERT INTO app_releases (org_id, project_id, slug, files, site, created_by)
               VALUES ($1,$2,$3,$4,$5,$6)""",
            ctx.org_id, project_id, slug, files, (p["answers"] or {}).get("site") or {}, ctx.user_id)
    await log_event(ctx.org_id, "app.published", slug, project_id, ctx.user_id)
    return {"published": True, "url": public_url(slug),
            "rules": appfs.rules(files)}


@router.post("/v1/apps/{project_id}/unpublish")
async def unpublish(project_id: int, ctx: T.Ctx = Depends(T.requires("approve"))):
    async with conn() as c:
        await _app_project(c, project_id, ctx.org_id)
        await c.execute("UPDATE app_releases SET live=false WHERE project_id=$1 AND org_id=$2",
                        project_id, ctx.org_id)
    await log_event(ctx.org_id, "app.unpublished", "", project_id, ctx.user_id)
    return {"published": False}


@router.get("/a/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def serve(slug: str):
    if not SLUG.match(slug):
        raise HTTPException(404)
    async with conn() as c:
        r = await c.fetchrow(
            """SELECT org_id, project_id, files, site FROM app_releases
               WHERE slug=$1 AND live ORDER BY id DESC LIMIT 1""", slug)
    if not r:
        return HTMLResponse("<!doctype html><title>Not found</title><p>This app isn't published.</p>",
                            status_code=404)
    page = appfs.preview(r["files"], r["site"], appfs.token(r["project_id"], r["org_id"], "public"),
                         settings.public_url)
    return HTMLResponse(page, headers={
        # Opaque origin even as a top-level page: app code never acts as app.creai.dev.
        "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups allow-modals",
        "Cache-Control": "public, max-age=60",
        "X-Robots-Tag": "noindex" if not r["site"].get("business") else "all",
        "Referrer-Policy": "no-referrer",
    })
