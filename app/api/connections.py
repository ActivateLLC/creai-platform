"""
Connections to tools a customer already uses. Webflow first.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ..core import tenancy as T
from ..core.config import settings
from ..core.db import conn, log_event
from ..services import vault, webflow

router = APIRouter(prefix="/v1/connections", tags=["connections"])

COMING = [{"provider": p, "name": n, "available": False}
          for p, n in (("wordpress", "WordPress"), ("shopify", "Shopify"), ("framer", "Framer"))]


class SelectIn(BaseModel):
    site_id: str


class SettingsIn(BaseModel):
    auto_publish: bool


def _fail(exc: Exception):
    if isinstance(exc, (webflow.WebflowError, vault.VaultError)):
        raise HTTPException(409, str(exc))
    raise exc


@router.get("")
async def list_connections(ctx: T.Ctx = Depends(T.current_ctx)):
    wf = await webflow.status(ctx.org_id)
    return [{"provider": "webflow", "name": "Webflow", "available": True, **wf}] + COMING


@router.post("/webflow/start")
async def start(ctx: T.Ctx = Depends(T.requires("write"))):
    try:
        url = await webflow.start_auth(ctx.org_id, ctx.user_id)
    except Exception as exc:
        _fail(exc)
    return {"url": url}


@router.get("/webflow/callback", include_in_schema=False)
async def callback(code: str | None = None, state: str | None = None, error: str | None = None):
    base = settings.public_url.rstrip("/")
    if error or not code or not state:
        return RedirectResponse(f"{base}/?connect=webflow&status=cancelled", status_code=303)
    try:
        await webflow.finish_auth(code, state)
    except (webflow.WebflowError, vault.VaultError):
        return RedirectResponse(f"{base}/?connect=webflow&status=failed", status_code=303)
    return RedirectResponse(f"{base}/?connect=webflow&status=ok", status_code=303)


@router.get("/webflow/sites")
async def sites(ctx: T.Ctx = Depends(T.current_ctx)):
    try:
        text = await webflow.call(ctx.org_id, "data_sites_tool",
                                  {"actions": [{"label": "sites", "list_sites": {"detail": "summary"}}]}, {})
    except Exception as exc:
        _fail(exc)
    return {"sites": _parse_sites(text)}


def _parse_sites(text: str) -> list[dict]:
    """Pull id/name/shortName out of the tool's JSON, wherever it sits."""
    import json
    import re
    found, seen = [], set()

    def walk(v):
        if isinstance(v, dict):
            if isinstance(v.get("id"), str) and ("displayName" in v or "shortName" in v or "name" in v):
                if v["id"] not in seen:
                    seen.add(v["id"])
                    found.append({"id": v["id"],
                                  "name": v.get("displayName") or v.get("name") or v.get("shortName"),
                                  "short_name": v.get("shortName"),
                                  "last_published": v.get("lastPublished")})
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    for chunk in re.findall(r"\{.*\}", text, flags=re.S) or [text]:
        try:
            walk(json.loads(chunk))
        except ValueError:
            continue
    return found


@router.post("/webflow/select")
async def select(body: SelectIn, ctx: T.Ctx = Depends(T.requires("write"))):
    listed = (await sites(ctx))["sites"]
    site = next((s for s in listed if s["id"] == body.site_id), None)
    if not site:
        raise HTTPException(404, "that site isn't in your connected Webflow workspace")
    site["staging_url"] = f"https://{site['short_name']}.webflow.io" if site.get("short_name") else None
    site["designer_url"] = f"https://webflow.com/design/{site['short_name']}" if site.get("short_name") else None
    await webflow.update_meta(ctx.org_id, {"site": site})
    async with conn() as c:
        pid = await c.fetchval(
            """SELECT id FROM projects WHERE org_id=$1 AND path='edit'
               AND answers->>'source'='webflow' AND answers->'site'->>'id'=$2""",
            ctx.org_id, site["id"])
        if not pid:
            pid = await c.fetchval(
                """INSERT INTO projects (org_id, created_by, name, path, answers)
                   VALUES ($1,$2,$3,'edit',$4) RETURNING id""",
                ctx.org_id, ctx.user_id, site["name"] or "Webflow site",
                {"source": "webflow", "site": site})
    await log_event(ctx.org_id, "connection.webflow.site", site["name"] or "", pid, ctx.user_id)
    return {"project_id": pid, "site": site}


@router.post("/webflow/settings")
async def update_settings(body: SettingsIn, ctx: T.Ctx = Depends(T.requires("approve"))):
    try:
        meta = await webflow.update_meta(ctx.org_id, {"auto_publish": body.auto_publish})
    except Exception as exc:
        _fail(exc)
    await log_event(ctx.org_id, "connection.webflow.auto_publish",
                    "on" if body.auto_publish else "off", None, ctx.user_id)
    return {"auto_publish": bool(meta.get("auto_publish"))}


@router.delete("/webflow")
async def remove(ctx: T.Ctx = Depends(T.requires("write"))):
    await webflow.disconnect(ctx.org_id)
    await log_event(ctx.org_id, "connection.webflow", "disconnected", None, ctx.user_id)
    return {"connected": False}
