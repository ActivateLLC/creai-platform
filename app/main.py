"""
CreAI Platform API.

Build-to-launch: generate a site, register a domain, deploy it, market it.
Phase 0 is the launch chain — domains, DNS and deploy — because if that cannot be
made reliable, the product's whole wedge does not hold.
"""

import asyncio

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from .api import (admin, agent, approvals, auth_routes, billing, connections, dashboard,
                  domains, drafts, marketing, orgs, projects, channels, appdata, apps, sites, plans)
from .core import db
from .services import social_publish
from .core.config import settings

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


async def _stripe_self_check():
    """On start: prove the Stripe key works and make sure wallets are on for our domain."""
    from urllib.parse import urlparse
    from .services import billing, plans
    log = logging.getLogger("creai.stripe")
    try:
        ids = [await plans._price_id(p, i) for p in plans.PAID for i in plans.INTERVALS]
        log.info("stripe prices ready: %s", ", ".join(ids))
    except Exception as exc:
        log.error("stripe price check failed: %s", exc)
        if "Invalid API Key" in str(exc) or "expired" in str(exc).lower():
            from .core import config
            config.STRIPE_REJECTED = True
            log.error("stripe rejected the key; billing is switched off until it is replaced")
            return
    try:
        out = await billing.register_domain(urlparse(settings.public_url).hostname)
        log.info("stripe wallet domain: %s", out)
    except Exception as exc:
        log.error("stripe wallet domain failed: %s", exc)
    try:
        cfg = await plans._portal_configuration()
        log.info("stripe portal configuration: %s", cfg)
    except Exception as exc:
        log.error("stripe portal configuration failed: %s", exc)


async def _renewals():
    from .services import registrar
    log = logging.getLogger("creai.renewals")
    while True:
        try:
            out = await registrar.renewal_sweep()
            from .services import plans as plan_svc
            await plan_svc.monthly_sweep()
            if out["charged"] or out["short"]:
                log.info("domain renewals: %s", out)
        except Exception:
            log.exception("renewal sweep failed")
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    tasks = []
    if social_publish.configured() and settings.env != "development":
        tasks.append(asyncio.create_task(social_publish.sweeper()))
    if settings.env != "development":
        tasks.append(asyncio.create_task(_renewals()))
        if not settings.missing_for("billing"):
            tasks.append(asyncio.create_task(_stripe_self_check()))
        elif settings.stripe_key or settings.stripe_publishable_key:
            logging.getLogger("creai.stripe").error(
                "stripe keys look wrong: STRIPE_SECRET_KEY should start with sk_ or rk_ (has %s…), "
                "STRIPE_PUBLISHABLE_KEY with pk_ (has %s…); billing stays off",
                settings.stripe_key[:3], settings.stripe_publishable_key[:3])
    yield
    for t in tasks:
        t.cancel()
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
app.add_middleware(sites.CustomDomains)

for r in (appdata.router, apps.router, sites.router, plans.router, agent.router, billing.router, connections.router, marketing.router, channels.router, drafts.router, auth_routes.router, orgs.router, projects.router, domains.router,
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
    # CreAI never runs inside a frame: not a preview, not anyone else's page.
    return FileResponse(WEB / "index.html", headers={
        "Cache-Control": "no-cache", "X-Frame-Options": "DENY",
        "Content-Security-Policy": "frame-ancestors 'none'"})


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
