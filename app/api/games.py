"""
Game projects: building Godot exports and serving them.

A build is a job, because exporting takes about a minute: the route starts it and
answers immediately, and the client watches `progress` until it settles. Publishing
points a slug at a finished build, so it costs nothing and is instant.

Serving goes through here rather than a signed bucket URL for one reason: a redirect
to storage can't carry the headers a game needs. Single-threaded games get the same
sandbox CSP as published apps, which gives them an opaque origin. Threaded games need
cross-origin isolation, which an opaque origin can never have, so they are served
only from the separate games host — never from the app's own origin, where an
unsandboxed game could read the session token in the app's browser storage.
"""

import asyncio
import gzip
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..core import tenancy as T
from ..core.config import settings
from ..core.db import conn, log_event
from ..services import billing, godot

router = APIRouter(tags=["games"])

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
GZIP_TYPES = ("text/html", "text/javascript", "application/wasm", "application/json")
GZIP_MIN = 2048


def public_url(slug: str) -> str:
    base = (settings.games_url or settings.public_url).rstrip("/")
    return f"{base}/g/{slug}/"


async def _game_project(c, project_id: int, org_id: int):
    p = await c.fetchrow("SELECT id, name, path, answers FROM projects WHERE id=$1 AND org_id=$2",
                         project_id, org_id)
    if not p or p["path"] != "game":
        raise HTTPException(404, "no such game")
    return p


# ---------------------------------------------------------------- building

@router.post("/v1/games/{project_id}/build")
async def build(project_id: int, threads: bool = False,
                ctx: T.Ctx = Depends(T.requires("write"))):
    """Start an export. Returns at once; watch the job for progress."""
    async with conn() as c:
        await _game_project(c, project_id, ctx.org_id)
    if settings.missing_for("games"):
        raise HTTPException(503, "the game builder isn't switched on yet")

    want = await godot.estimate(project_id, ctx.org_id)
    if await billing.balance(ctx.org_id) < want:
        raise HTTPException(402, f"A build costs about {want} credits and your balance is short. "
                                 "Top up and the game is waiting exactly as you left it.")
    try:
        job = await godot.start(project_id, ctx.org_id, ctx.user_id,
                                threads=threads and godot.threads_allowed())
    except godot.GameError as exc:
        raise HTTPException(409, str(exc))
    asyncio.create_task(godot.run(job["id"], project_id, ctx.org_id))
    await log_event(ctx.org_id, "game.build_started", str(job["id"]), project_id, ctx.user_id)
    return job | {"estimate": want}


@router.get("/v1/games/{project_id}/build/{build_id}")
async def build_status(project_id: int, build_id: int, ctx: T.Ctx = Depends(T.current_ctx)):
    out = await godot.status(build_id, project_id, ctx.org_id)
    if not out:
        raise HTTPException(404, "no such build")
    return out


@router.get("/v1/games/{project_id}/build")
async def latest_build(project_id: int, ctx: T.Ctx = Depends(T.current_ctx)):
    async with conn() as c:
        await _game_project(c, project_id, ctx.org_id)
    return await godot.latest(project_id, ctx.org_id) or {"state": "none"}


@router.get("/v1/games/{project_id}/check")
async def check(project_id: int, ctx: T.Ctx = Depends(T.current_ctx)):
    """What's wrong with the project before a build is spent on it."""
    async with conn() as c:
        await _game_project(c, project_id, ctx.org_id)
    return godot.review(await godot.files(project_id, ctx.org_id))


# ---------------------------------------------------------------- publishing

@router.get("/v1/games/{project_id}/release")
async def release_status(project_id: int, ctx: T.Ctx = Depends(T.current_ctx)):
    async with conn() as c:
        await _game_project(c, project_id, ctx.org_id)
        r = await c.fetchrow(
            """SELECT slug, live, created_at FROM game_releases
               WHERE project_id=$1 AND org_id=$2 ORDER BY id DESC LIMIT 1""",
            project_id, ctx.org_id)
    if not r:
        return {"published": False}
    return {"published": r["live"], "url": public_url(r["slug"]), "at": r["created_at"].isoformat()}


@router.post("/v1/games/{project_id}/publish")
async def publish(project_id: int, ctx: T.Ctx = Depends(T.requires("approve"))):
    """Point a public address at the newest finished build."""
    async with conn() as c:
        p = await _game_project(c, project_id, ctx.org_id)
        b = await c.fetchrow(
            """SELECT id, threads FROM game_builds WHERE project_id=$1 AND org_id=$2
               AND state='done' ORDER BY id DESC LIMIT 1""", project_id, ctx.org_id)
        if not b:
            raise HTTPException(409, "Build the game first — there's nothing exported to publish.")
        if b["threads"] and not godot.threads_allowed():
            raise HTTPException(409, "This build needs a games host to run on. Rebuild it "
                                     "single-threaded, or set GAMES_URL.")
        slug = await c.fetchval(
            "SELECT slug FROM game_releases WHERE project_id=$1 ORDER BY id DESC LIMIT 1", project_id)
        if not slug:
            base = re.sub(r"[^a-z0-9]+", "-", p["name"].lower()).strip("-")[:40] or "game"
            slug = f"{base}-{secrets.token_hex(3)}"
        await c.execute("UPDATE game_releases SET live=false WHERE project_id=$1", project_id)
        await c.execute(
            """INSERT INTO game_releases (org_id, project_id, build_id, slug, title, created_by)
               VALUES ($1,$2,$3,$4,$5,$6)""",
            ctx.org_id, project_id, b["id"], slug, p["name"][:120], ctx.user_id)
    await log_event(ctx.org_id, "game.published", slug, project_id, ctx.user_id)
    return {"published": True, "url": public_url(slug), "threads": b["threads"]}


@router.post("/v1/games/{project_id}/unpublish")
async def unpublish(project_id: int, ctx: T.Ctx = Depends(T.requires("approve"))):
    async with conn() as c:
        await _game_project(c, project_id, ctx.org_id)
        await c.execute("UPDATE game_releases SET live=false WHERE project_id=$1 AND org_id=$2",
                        project_id, ctx.org_id)
    await log_event(ctx.org_id, "game.unpublished", "", project_id, ctx.user_id)
    return {"published": False}


# ---------------------------------------------------------------- serving

async def _live(slug: str):
    async with conn() as c:
        return await c.fetchrow(
            """SELECT r.slug, r.title, b.prefix, b.names, b.threads
               FROM game_releases r JOIN game_builds b ON b.id = r.build_id
               WHERE r.slug=$1 AND r.live ORDER BY r.id DESC LIMIT 1""", slug)


def _headers(threads: bool, on_games_host: bool) -> dict:
    """Isolation for threaded games, an opaque origin for everything else.

    These are alternatives, not a scale: COOP and COEP are both ignored on an opaque
    origin, so a page cannot be sandboxed and cross-origin isolated at once.
    """
    if threads and on_games_host:
        return {"Cross-Origin-Opener-Policy": "same-origin",
                "Cross-Origin-Embedder-Policy": "require-corp",
                "Cross-Origin-Resource-Policy": "same-origin",
                "Content-Security-Policy": "frame-ancestors 'self'"}
    return {"Content-Security-Policy":
            "sandbox allow-scripts allow-pointer-lock allow-popups allow-modals; frame-ancestors 'self'",
            "Cross-Origin-Resource-Policy": "same-origin"}


def _on_games_host(request: Request) -> bool:
    if not settings.games_url:
        return False
    host = (request.headers.get("host") or "").split(":")[0].lower()
    return host == settings.games_url.split("//")[-1].split("/")[0].split(":")[0].lower()


def _body(data: bytes, mime: str, request: Request) -> tuple[bytes, dict]:
    """Godot ships 37 MB of wasm for a trivial game; it compresses to about nine."""
    accepts = "gzip" in (request.headers.get("accept-encoding") or "")
    if accepts and len(data) >= GZIP_MIN and mime.split(";")[0] in GZIP_TYPES:
        return gzip.compress(data, 6), {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
    return data, {}


@router.get("/g/{slug}", include_in_schema=False)
async def game_index_redirect(slug: str):
    # The export's own HTML uses relative paths, so it has to live in a directory.
    if not SLUG.match(slug):
        raise HTTPException(404)
    return RedirectResponse(f"/g/{slug}/", status_code=308)


@router.get("/g/{slug}/", include_in_schema=False)
async def game_index(slug: str, request: Request):
    if not SLUG.match(slug):
        raise HTTPException(404)
    r = await _live(slug)
    if not r:
        return HTMLResponse("<!doctype html><title>Not found</title><p>This game isn't published.</p>",
                            status_code=404)
    if r["threads"] and not _on_games_host(request):
        return HTMLResponse(
            "<!doctype html><title>Wrong address</title>"
            f"<p>This game runs at <a href=\"{public_url(slug)}\">its own address</a>.</p>",
            status_code=404)
    return await _file(slug, "index.html", request, r)


@router.get("/g/{slug}/{name}", include_in_schema=False)
async def game_file(slug: str, name: str, request: Request):
    if not SLUG.match(slug) or not NAME.match(name):
        raise HTTPException(404)
    r = await _live(slug)
    if not r:
        raise HTTPException(404)
    return await _file(slug, name, request, r)


async def _file(slug: str, name: str, request: Request, r) -> Response:
    from ..services import assets
    if name not in (r["names"] or []):
        raise HTTPException(404)
    data = await assets.blob(f"{r['prefix']}/{name}")
    if data is None:
        raise HTTPException(404)
    mime = godot.mime_for(name)
    body, enc = _body(data, mime, request)
    return Response(body, media_type=mime, headers={
        **_headers(bool(r["threads"]), _on_games_host(request)), **enc,
        "Cache-Control": "public, max-age=300", "Referrer-Policy": "no-referrer",
        "X-Robots-Tag": "all"})
