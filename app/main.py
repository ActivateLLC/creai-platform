"""
CreAI Platform API.

Build-to-launch: generate a site, register a domain, deploy it, market it.
Phase 0 is the launch chain — domains, DNS and deploy — because if that cannot be
made reliable, the product's whole wedge does not hold.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import admin, approvals, auth_routes, dashboard, domains, orgs, projects
from .core import db
from .core.config import settings

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    yield
    await db.disconnect()


app = FastAPI(title="CreAI Platform", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.public_url, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth_routes.router, orgs.router, projects.router, domains.router,
          approvals.router, dashboard.router, admin.router):
    app.include_router(r)


@app.get("/health")
async def health():
    """Names what is configured rather than claiming to be fine.

    A deployment missing its DNS credential is not healthy in any useful sense,
    and finding that out here beats finding it out mid-launch.
    """
    return {"ok": True, "env": settings.env, "configured": settings.configured}
