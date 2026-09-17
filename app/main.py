"""
CreAI Platform API.

Build-to-launch: generate a site, register a domain, deploy it, market it.
Phase 0 is the launch chain — domains, DNS and deploy — because if that cannot be
made reliable, the product's whole wedge does not hold.
"""

import asyncio

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from .api import (admin, agent, approvals, auth_routes, billing, connections, dashboard,
                  domains, drafts, marketing, orgs, projects, channels, appdata)
from .core import db
from .services import social_publish
from .core.config import settings

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    sweeper = None
    if social_publish.configured() and settings.env != "development":
        sweeper = asyncio.create_task(social_publish.sweeper())
    yield
    if sweeper:
        sweeper.cancel()
    await db.disconnect()


app = FastAPI(title="CreAI Platform", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # the web app, local dev, and the CreAI mobile app (Capacitor on iOS / Android)
    allow_origins=[settings.public_url, "http://localhost:3000",
                   "capacitor://localhost", "https://localhost"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Draft-Token"],
)

class AppDataCORS:
    """Sandboxed app previews have an opaque origin. /v1/appdata authenticates by
    app token only, so it answers any origin without credentials; every other
    route keeps the strict CORS policy above."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/v1/appdata"):
            return await self.app(scope, receive, send)
        headers = [(b"access-control-allow-origin", b"*"),
                   (b"access-control-allow-methods", b"GET, POST, PATCH, DELETE, OPTIONS"),
                   (b"access-control-allow-headers", b"content-type, x-app-token"),
                   (b"access-control-max-age", b"600")]
        if scope["method"] == "OPTIONS":
            await send({"type": "http.response.start", "status": 204, "headers": headers})
            return await send({"type": "http.response.body", "body": b""})

        async def with_cors(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [h for h in message.get("headers", [])
                                                  if not h[0].startswith(b"access-control-")] + headers}
            await send(message)
        # strip Origin so the strict middleware below doesn't interfere
        scope = {**scope, "headers": [h for h in scope["headers"] if h[0] != b"origin"]}
        return await self.app(scope, receive, with_cors)


app.add_middleware(AppDataCORS)

for r in (appdata.router, agent.router, billing.router, connections.router, marketing.router, channels.router, drafts.router, auth_routes.router, orgs.router, projects.router, domains.router,
          approvals.router, dashboard.router, admin.router):
    app.include_router(r)


@app.get("/health")
async def health():
    """Names what is configured rather than claiming to be fine.

    A deployment missing its DNS credential is not healthy in any useful sense,
    and finding that out here beats finding it out mid-launch.
    """
    return {"ok": True, "env": settings.env, "configured": settings.configured}


# ---------------------------------------------------------------- the app itself
# Served from the same origin as the API, so the draft cookie and sign-in work
# without cross-site configuration.
WEB = Path(__file__).parent / "web"


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/logo-mark.png", include_in_schema=False)
async def logo_mark():
    return FileResponse(WEB / "logo-mark.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/logo.svg", include_in_schema=False)
async def logo():
    return FileResponse(WEB / "logo.svg", media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/.well-known/apple-developer-merchantid-domain-association", include_in_schema=False)
async def apple_pay_domain():
    """Apple Pay domain verification for the in-app payment panel."""
    return FileResponse(WEB / "apple-developer-merchantid-domain-association",
                        media_type="text/plain")
