"""Uploading photos, videos and PDFs, listing them, and serving them by link."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ..core import tenancy as T
from ..core.db import log_event
from ..services import assets

router = APIRouter(tags=["assets"])


class StartIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    mime: str = Field(max_length=60)
    size: int = Field(gt=0)
    project_id: int | None = None
    parent_id: int | None = None
    width: int | None = Field(None, ge=1, le=20000)
    height: int | None = Field(None, ge=1, le=20000)
    duration: float | None = Field(None, ge=0, le=36000)


def _err(exc: assets.AssetError) -> HTTPException:
    msg = str(exc)
    code = 404 if msg.startswith("no such") else 503 if "switched on" in msg else 413 if "up to" in msg or "storage" in msg else 400
    return HTTPException(code, msg)


@router.post("/v1/assets")
async def start(body: StartIn, ctx: T.Ctx = Depends(T.requires("write"))):
    try:
        return await assets.start_upload(ctx.org_id, ctx.user_id, **body.model_dump())
    except assets.AssetError as exc:
        raise _err(exc)


@router.post("/v1/assets/{asset_id}/complete")
async def complete(asset_id: int, ctx: T.Ctx = Depends(T.requires("write"))):
    try:
        out = await assets.complete(ctx.org_id, asset_id)
    except assets.AssetError as exc:
        raise _err(exc)
    if not out["parent_id"]:
        await log_event(ctx.org_id, "asset.uploaded", out["kind"], out["project_id"], ctx.user_id)
    return out


@router.get("/v1/assets")
async def listing(project_id: int | None = None, ctx: T.Ctx = Depends(T.current_ctx)):
    return {"assets": await assets.listing(ctx.org_id, project_id),
            "used": await assets.usage(ctx.org_id), "quota": await assets.quota(ctx.org_id),
            "enabled": assets.configured()}


@router.delete("/v1/assets/{asset_id}")
async def remove(asset_id: int, ctx: T.Ctx = Depends(T.requires("write"))):
    if not await assets.delete(ctx.org_id, asset_id):
        raise HTTPException(404, "no such file")
    return {"deleted": True}


@router.get("/f/{token}", include_in_schema=False)
async def serve(token: str):
    url = await assets.signed_get(token)
    if not url:
        raise HTTPException(404)
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "public, max-age=1800",
                                                           "Referrer-Policy": "no-referrer"})
