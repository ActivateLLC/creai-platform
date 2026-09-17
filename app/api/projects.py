"""Projects — a site, a marketing engagement, a domain, or a company."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..core import tenancy as T
from ..core.db import conn, log_event

router = APIRouter(prefix="/v1/projects", tags=["projects"])
PATHS = {"launch", "market", "domain", "company", "edit"}


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    path: str = "launch"
    brief: str | None = None
    answers: dict = {}


@router.get("")
async def list_projects(ctx: T.Ctx = Depends(T.current_ctx)):
    async with conn() as c:
        rows = await c.fetch(
            """SELECT p.*, d.name AS domain, d.status AS domain_status
               FROM projects p LEFT JOIN domains d ON d.project_id = p.id AND d.org_id = p.org_id
               WHERE p.org_id=$1 ORDER BY p.updated_at DESC""", ctx.org_id)
    return [{"id": r["id"], "name": r["name"], "path": r["path"], "status": r["status"],
             "domain": r["domain"], "domain_status": r["domain_status"],
             "updated_at": r["updated_at"].isoformat()} for r in rows]


@router.post("")
async def create(body: ProjectIn, ctx: T.Ctx = Depends(T.requires("write"))):
    if body.path not in PATHS:
        raise HTTPException(400, f"path must be one of {sorted(PATHS)}")
    async with conn() as c:
        row = await c.fetchrow(
            """INSERT INTO projects (org_id, created_by, name, path, brief, answers)
               VALUES ($1,$2,$3,$4,$5,$6) RETURNING *""",
            ctx.org_id, ctx.user_id, body.name.strip(), body.path, body.brief, body.answers)
    await log_event(ctx.org_id, "project.created", body.name, row["id"], ctx.user_id)
    return {"id": row["id"], "name": row["name"], "path": row["path"], "status": row["status"]}


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
