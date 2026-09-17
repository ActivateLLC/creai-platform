"""A workspace's connected social accounts."""

from fastapi import APIRouter, Depends

from ..core import tenancy as T
from ..services import social_publish

router = APIRouter(prefix="/v1/channels", tags=["channels"])


@router.get("")
async def list_channels(ctx: T.Ctx = Depends(T.current_ctx)):
    return {"publishing": social_publish.configured(),
            "channels": await social_publish.channels(ctx.org_id)}
