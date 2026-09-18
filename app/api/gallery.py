"""
The public gallery, and remixing from it.

The listing needs no account: a stranger deciding whether Creai works should not
have to sign up to look. Remixing does need one, because it makes something that
belongs to somebody.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from ..core import tenancy as T
from ..services import gallery

log = logging.getLogger("creai.gallery")
router = APIRouter(tags=["gallery"])


@router.get("/v1/gallery")
async def listing(kind: str | None = None, limit: int = 60):
    """Real things people built here. No account needed to look."""
    return {"items": await gallery.listing(limit=max(1, min(int(limit), 100)), kind=kind)}


@router.post("/v1/gallery/{project_id}/remix")
async def remix(project_id: int, ctx: T.Ctx = Depends(T.requires("write"))):
    """Take it and make it yours. Their customers, records and takings stay theirs."""
    try:
        return await gallery.remix(project_id, ctx.org_id, ctx.user_id)
    except gallery.GalleryError as exc:
        raise HTTPException(404, str(exc))


@router.post("/v1/projects/{project_id}/showcase")
async def showcase(project_id: int, on: bool = True,
                   ctx: T.Ctx = Depends(T.requires("approve"))):
    """Show this in the gallery, or stop showing it. Always the owner's call."""
    try:
        return await gallery.show(project_id, ctx.org_id, on, ctx.user_id)
    except gallery.GalleryError as exc:
        raise HTTPException(409, str(exc))
