"""
A picture of every project.

People recognise their work by sight, not by name. After a build or a publish the
platform renders the project at phone width and keeps the picture with the project,
so the workspace shows what each one actually looks like. Failures are silent: a
missing thumbnail is a cosmetic loss, never a broken build.
"""

import base64
import logging

from ..core.db import conn
from . import assets, review

log = logging.getLogger("creai.thumbs")


async def refresh(org_id: int, project_id: int, html: str) -> str | None:
    if not (review.configured() and assets.configured()):
        return None
    try:
        out = await review.shots(html, ["phone"])
        data = out.get("shots", {}).get("phone")
        if not data:
            return None
        saved = await assets.store_bytes(org_id, name=f"project-{project_id}.png", mime="image/png",
                                         data=base64.b64decode(data), project_id=project_id)
        async with conn() as c:
            old = await c.fetchval(
                "UPDATE projects SET thumb_token=$3 WHERE id=$1 AND org_id=$2 RETURNING thumb_token",
                project_id, org_id, saved["url"].rsplit("/", 1)[1])
            if old:
                stale = await c.fetchval(
                    "SELECT id FROM assets WHERE token=$1 AND org_id=$2 AND project_id=$3", old, org_id, project_id)
        if old and stale:
            await assets.delete(org_id, stale)          # keep one picture per project
        return saved["url"]
    except Exception as exc:                            # noqa: BLE001 — cosmetic only
        log.info("thumbnail skipped for project %s: %s", project_id, exc)
        return None
