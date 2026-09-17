"""Projects — a site, a marketing engagement, a domain, or a company."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..core import tenancy as T
from ..core.config import settings
from ..core.db import conn, log_event

router = APIRouter(prefix="/v1/projects", tags=["projects"])
PATHS = {"launch", "market", "domain", "company", "edit", "app"}


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    path: str = "launch"
    brief: str | None = None
    answers: dict = {}


KINDS = {"launch": "Site", "edit": "Site", "app": "App", "market": "Marketing",
         "domain": "Domain", "company": "Company"}


@router.get("")
async def list_projects(archived: bool = False, ctx: T.Ctx = Depends(T.current_ctx)):
    """Everything in the workspace, newest activity first, with enough to recognise
    each one at a glance: what it is, whether it's live, and what it looks like."""
    async with conn() as c:
        rows = await c.fetch(
            """SELECT p.id, p.name, p.path, p.status, p.answers, p.thumb_token, p.archived_at,
                      p.updated_at, p.last_opened_at,
                      (SELECT d.name FROM domains d WHERE d.project_id = p.id AND d.org_id = p.org_id
                       AND d.status IN ('registered','verifying','live') ORDER BY d.id LIMIT 1) AS domain,
                      (SELECT s.slug FROM site_releases s WHERE s.project_id = p.id AND s.live
                       ORDER BY s.id DESC LIMIT 1) AS site_slug,
                      (SELECT a.slug FROM app_releases a WHERE a.project_id = p.id AND a.live
                       ORDER BY a.id DESC LIMIT 1) AS app_slug
               FROM projects p
               WHERE p.org_id=$1 AND (p.archived_at IS NULL) <> $2
               ORDER BY GREATEST(p.updated_at, COALESCE(p.last_opened_at, p.updated_at)) DESC""",
            ctx.org_id, archived)
    out = []
    for r in rows:
        answers = r["answers"] or {}
        spec = answers.get("site") or {}
        slug = r["site_slug"] or r["app_slug"]
        base = settings.public_url.rstrip("/")
        out.append({
            "id": r["id"], "name": r["name"], "path": r["path"],
            "kind": "Site" if answers.get("source") == "webflow" else KINDS.get(r["path"], "Project"),
            "external": bool(answers.get("source")),
            "status": r["status"], "archived": bool(r["archived_at"]),
            "business": spec.get("business") or None,
            "headline": spec.get("headline") or None,
            "domain": r["domain"],
            "url": (f"https://{r['domain']}" if r["domain"] and r["status"] == "live"
                    else f"{base}/{'a' if r['app_slug'] else 's'}/{slug}" if slug else None),
            "published": bool(slug),
            "thumb": f"{base}/f/{r['thumb_token']}" if r["thumb_token"] else None,
            "updated_at": r["updated_at"].isoformat(),
            "last_opened_at": r["last_opened_at"].isoformat() if r["last_opened_at"] else None,
        })
    return out


@router.post("")
async def create(body: ProjectIn, ctx: T.Ctx = Depends(T.requires("write"))):
    if body.path not in PATHS:
        raise HTTPException(400, f"path must be one of {sorted(PATHS)}")
    async with conn() as c:
        row = await c.fetchrow(
            """INSERT INTO projects (org_id, created_by, name, path, brief, answers)
               VALUES ($1,$2,$3,$4,$5,$6) RETURNING *""",
            ctx.org_id, ctx.user_id, body.name.strip(), body.path, body.brief, body.answers)
    if body.path == "app":
        from ..services import appfs
        await appfs.seed(row["id"], ctx.org_id)
    await log_event(ctx.org_id, "project.created", body.name, row["id"], ctx.user_id)
    return {"id": row["id"], "name": row["name"], "path": row["path"], "status": row["status"]}


class ProjectPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    archived: bool | None = None


@router.patch("/{project_id}")
async def update(project_id: int, body: ProjectPatch, ctx: T.Ctx = Depends(T.requires("write"))):
    sets, args = [], []
    if body.name is not None:
        args.append(body.name.strip())
        sets.append(f"name=${len(args) + 2}")
    if body.archived is not None:
        args.append(None if not body.archived else "now")
        sets.append(f"archived_at={'now()' if body.archived else 'NULL'}")
        args.pop()
    if not sets:
        raise HTTPException(400, "nothing to change")
    async with conn() as c:
        row = await c.fetchrow(
            f"UPDATE projects SET {', '.join(sets)}, updated_at=now() WHERE id=$1 AND org_id=$2 RETURNING id, name, archived_at",
            project_id, ctx.org_id, *args)
    if not row:
        raise HTTPException(404, "no such project")
    await log_event(ctx.org_id, "project.updated", row["name"], project_id, ctx.user_id)
    return {"id": row["id"], "name": row["name"], "archived": bool(row["archived_at"])}


@router.post("/{project_id}/opened")
async def opened(project_id: int, ctx: T.Ctx = Depends(T.current_ctx)):
    """Remember where the person was, so signing in returns them to it."""
    async with conn() as c:
        row = await c.fetchrow(
            "UPDATE projects SET last_opened_at=now() WHERE id=$1 AND org_id=$2 RETURNING id",
            project_id, ctx.org_id)
    if not row:
        raise HTTPException(404, "no such project")
    return {"ok": True}


@router.delete("/{project_id}")
async def remove(project_id: int, ctx: T.Ctx = Depends(T.requires("approve"))):
    """Permanent, and only for a project that isn't live anywhere."""
    async with conn() as c:
        p = await c.fetchrow("SELECT name FROM projects WHERE id=$1 AND org_id=$2", project_id, ctx.org_id)
        if not p:
            raise HTTPException(404, "no such project")
        live = await c.fetchval(
            """SELECT 1 FROM domains WHERE project_id=$1 AND status IN ('registered','verifying','live')
               UNION ALL SELECT 1 FROM site_releases WHERE project_id=$1 AND live
               UNION ALL SELECT 1 FROM app_releases WHERE project_id=$1 AND live LIMIT 1""", project_id)
        if live:
            raise HTTPException(409, "This is published or has a domain. Unpublish it first, or archive it instead.")
        await c.execute("DELETE FROM projects WHERE id=$1 AND org_id=$2", project_id, ctx.org_id)
    await log_event(ctx.org_id, "project.deleted", p["name"], None, ctx.user_id)
    return {"deleted": True}


@router.get("/{project_id}")
async def get(project_id: int, ctx: T.Ctx = Depends(T.current_ctx)):
    async with conn() as c:
        p = await c.fetchrow(
            "SELECT * FROM projects WHERE id=$1 AND org_id=$2", project_id, ctx.org_id)
        if not p:
            raise HTTPException(404, "no such project")
        domains = await c.fetch(
            "SELECT id, name, status FROM domains WHERE project_id=$1 AND org_id=$2",
            project_id, ctx.org_id)
        deploys = await c.fetch(
            """SELECT id, status, url, created_at FROM deployments
               WHERE project_id=$1 AND org_id=$2 ORDER BY created_at DESC LIMIT 5""",
            project_id, ctx.org_id)
    return {"id": p["id"], "name": p["name"], "path": p["path"], "status": p["status"],
            "brief": p["brief"], "answers": p["answers"],
            "domains": [dict(d) for d in domains],
            "deployments": [dict(d) | {"created_at": d["created_at"].isoformat()}
                            for d in deploys]}
