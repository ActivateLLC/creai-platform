"""
Publishing sites on CreAI, and serving customer domains.

A release is the rendered HTML at publish time. Sites never carry script, and are
served with a CSP that says so. Requests that arrive on a customer's own domain
get only that customer's published site or app — never the CreAI app itself.
"""

import re
import secrets
import time
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from ..core import tenancy as T
from ..core.config import settings
from ..core.db import conn, log_event
from ..services import appfs
from ..services import site as site_spec

router = APIRouter(tags=["sites"])
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")

SITE_CSP = ("default-src 'none'; script-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src data: https:; media-src https:; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")
APP_CSP = "sandbox allow-scripts allow-forms allow-popups allow-modals"


def public_url(slug: str) -> str:
    return f"{settings.public_url.rstrip('/')}/s/{slug}"


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40] or "site"


async def _project(c, project_id: int, org_id: int):
    p = await c.fetchrow("SELECT id, name, path, answers FROM projects WHERE id=$1 AND org_id=$2",
                         project_id, org_id)
    if not p:
        raise HTTPException(404, "no such project")
    if p["path"] in ("app", "market") or (p["answers"] or {}).get("source"):
        raise HTTPException(409, "This project isn't a CreAI site.")
    return p


async def _domains(c, project_id: int) -> list[dict]:
    rows = await c.fetch(
        """SELECT name, status FROM domains WHERE project_id=$1 AND status IN ('registered','verifying','live')
           ORDER BY name""", project_id)
    return [{"name": r["name"], "status": r["status"]} for r in rows]


@router.get("/v1/sites/{project_id}/release")
async def release_status(project_id: int, ctx: T.Ctx = Depends(T.current_ctx)):
    async with conn() as c:
        await _project(c, project_id, ctx.org_id)
        r = await c.fetchrow(
            "SELECT slug, live, created_at FROM site_releases WHERE project_id=$1 AND org_id=$2 ORDER BY id DESC LIMIT 1",
            project_id, ctx.org_id)
        doms = await _domains(c, project_id)
    if not r:
        return {"published": False, "domains": doms}
    return {"published": r["live"], "url": public_url(r["slug"]), "at": r["created_at"].isoformat(),
            "domains": doms}


@router.post("/v1/sites/{project_id}/publish")
async def publish(project_id: int, ctx: T.Ctx = Depends(T.requires("approve"))):
    async with conn() as c:
        p = await _project(c, project_id, ctx.org_id)
        spec = (p["answers"] or {}).get("site") or {}
        if not (spec.get("headline") or spec.get("sections")):
            raise HTTPException(409, "Build the site first — there's nothing to publish yet.")
        issues = site_spec.critique(spec)
        slug = await c.fetchval(
            "SELECT slug FROM site_releases WHERE project_id=$1 ORDER BY id DESC LIMIT 1", project_id)
        slug = slug or f"{_slugify(spec.get('business') or p['name'])}-{secrets.token_hex(3)}"
        await c.execute("UPDATE site_releases SET live=false WHERE project_id=$1", project_id)
        await c.execute(
            """INSERT INTO site_releases (org_id, project_id, slug, html, created_by)
               VALUES ($1,$2,$3,$4,$5)""",
            ctx.org_id, project_id, slug, site_spec.render(spec), ctx.user_id)
        doms = await _domains(c, project_id)
    _cache.clear()
    await log_event(ctx.org_id, "site.published", slug, project_id, ctx.user_id)
    return {"published": True, "url": public_url(slug), "domains": doms,
            "quality": issues}


@router.post("/v1/sites/{project_id}/unpublish")
async def unpublish(project_id: int, ctx: T.Ctx = Depends(T.requires("approve"))):
    async with conn() as c:
        await _project(c, project_id, ctx.org_id)
        await c.execute("UPDATE site_releases SET live=false WHERE project_id=$1 AND org_id=$2",
                        project_id, ctx.org_id)
    _cache.clear()
    await log_event(ctx.org_id, "site.unpublished", "", project_id, ctx.user_id)
    return {"published": False}


def _site_response(html: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(html, status_code=status, headers={
        "Content-Security-Policy": SITE_CSP, "Cache-Control": "public, max-age=60",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "strict-origin-when-cross-origin"})


BADGE = ('<a href="https://www.creai.dev/?ref=badge" rel="noopener" style="position:fixed;right:14px;bottom:14px;'
         'z-index:99;display:inline-flex;gap:6px;align-items:center;padding:7px 12px;border-radius:999px;'
         'background:#141414;color:#F4F1EA;font:600 12px/1 system-ui,sans-serif;text-decoration:none;'
         'box-shadow:0 4px 16px #0003">Made with CreAI</a>')


@router.get("/s/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def serve(slug: str):
    if not SLUG.match(slug):
        raise HTTPException(404)
    async with conn() as c:
        r = await c.fetchrow(
            "SELECT html, org_id FROM site_releases WHERE slug=$1 AND live ORDER BY id DESC LIMIT 1", slug)
    if not r:
        return _site_response(PLACEHOLDER.replace("{msg}", "This site isn't published."), 404)
    from ..services import plans
    html = r["html"]
    if not await plans.has(r["org_id"], "no_badge"):
        html = html.replace("</body>", BADGE + "</body>", 1)
    return _site_response(html)


PLACEHOLDER = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Coming soon</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;font:17px/1.5 system-ui,sans-serif;
background:#F4F1EA;color:#1b1b1b}p{max-width:30ch;text-align:center}</style><p>{msg}</p></html>"""


# ---------------------------------------------------------------- custom domains

_cache: dict[str, tuple[float, tuple]] = {}
CACHE_SECONDS = 30


def _own_hosts() -> set[str]:
    return {urlparse(settings.public_url).hostname or "", "localhost", "127.0.0.1", "test", "testserver"}


async def _release_for_host(host: str):
    name = host.removeprefix("www.")
    hit = _cache.get(name)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    from ..services import plans
    async with conn() as c:
        d = await c.fetchrow(
            """SELECT org_id, project_id FROM domains WHERE name=$1
               AND status IN ('registered','verifying','live')
               ORDER BY (status = 'live') DESC, id DESC LIMIT 1""", name)
        out = None
        if d and not await plans.has(d["org_id"], "custom_domain"):
            # No plan: never go dark — send visitors to the free CreAI address.
            slug = await c.fetchval(
                """SELECT slug FROM (SELECT slug, id FROM site_releases WHERE project_id=$1 AND live
                   UNION ALL SELECT slug, id FROM app_releases WHERE project_id=$1 AND live) r
                   ORDER BY id DESC LIMIT 1""", d["project_id"]) if d["project_id"] else None
            kind = await c.fetchval("SELECT path FROM projects WHERE id=$1", d["project_id"]) if slug else None
            out = ("redirect", f"{settings.public_url.rstrip('/')}/{'a' if kind == 'app' else 's'}/{slug}") \
                if slug else ("none",)
        elif d:
            out = ("none",)
            if d["project_id"]:
                site = await c.fetchval(
                    "SELECT html FROM site_releases WHERE project_id=$1 AND org_id=$2 AND live ORDER BY id DESC LIMIT 1",
                    d["project_id"], d["org_id"])
                if site:
                    out = ("site", site)
                else:
                    app = await c.fetchrow(
                        """SELECT files, site FROM app_releases WHERE project_id=$1 AND org_id=$2 AND live
                           ORDER BY id DESC LIMIT 1""", d["project_id"], d["org_id"])
                    if app:
                        out = ("app", app["files"], app["site"], d["project_id"], d["org_id"])
    _cache[name] = (time.monotonic() + CACHE_SECONDS, out)
    return out


class CustomDomains:
    """Requests for a customer's domain never reach the platform's routes."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        host = next((v.decode().split(":")[0].lower() for k, v in scope["headers"] if k == b"host"), "")
        if (not host or host in _own_hosts() or host.endswith((".railway.app", ".railway.internal"))
                or scope["path"].startswith("/health")):
            return await self.app(scope, receive, send)
        try:
            release = await _release_for_host(host)
        except Exception:
            release = None
        if release is None:
            return await self.app(scope, receive, send)
        if scope["path"] not in ("/", "/index.html"):
            resp = HTMLResponse("", status_code=404 if scope["path"] != "/robots.txt" else 200)
        elif release[0] == "site":
            resp = _site_response(release[1])
        elif release[0] == "redirect":
            from fastapi.responses import RedirectResponse
            resp = RedirectResponse(release[1], status_code=302)
        elif release[0] == "app":
            _, files, spec, pid, oid = release
            page = appfs.preview(files, spec, appfs.token(pid, oid, "public"), settings.public_url)
            resp = HTMLResponse(page, headers={"Content-Security-Policy": APP_CSP, "Cache-Control": "no-store"})
        else:
            resp = _site_response(PLACEHOLDER.replace("{msg}", f"{host} is set up with CreAI. The site is on its way."))
        return await resp(scope, receive, send)
