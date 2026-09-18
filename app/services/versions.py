"""
Going back to how it was.

Every publish has always been kept — the page, the files, the moment — and none
of it was ever readable. So the fear that stops people editing a live site had no
answer: change something, break it, and there was no way back.

This is the answer, and it is deliberately not version control. No branches, no
diffs, no commit messages. A list of moments with a picture of each, and a button
that puts one back. That is the shape of the idea for somebody who does not write
code, and it is the part of a repository they actually needed.

Restoring never destroys. Putting Tuesday back makes a new version, so Wednesday
is still there to return to. Somebody who restores by mistake must not be punished
for it.
"""

import logging

from ..core.db import conn, log_event
from . import appfs, godot

log = logging.getLogger("creai.versions")

TABLES = {
    "site": ("site_releases", "html"),
    "app": ("app_releases", "files"),
    "game": ("game_releases", "files"),
}


class VersionError(ValueError):
    """Something the person should be told plainly."""


def kind_of(path: str) -> str:
    return {"app": "app", "game": "game"}.get(path or "", "site")


async def listing(project_id: int, org_id: int, path: str, limit: int = 30) -> list[dict]:
    """Every version, newest first, with the moment it was published."""
    table, _ = TABLES[kind_of(path)]
    async with conn() as c:
        rows = await c.fetch(
            f"""SELECT r.id, r.slug, r.live, r.created_at, u.email AS by
                FROM {table} r LEFT JOIN users u ON u.id = r.created_by
                WHERE r.project_id=$1 AND r.org_id=$2
                ORDER BY r.id DESC LIMIT $3""", project_id, org_id, limit)
    return [{"id": r["id"], "live": r["live"], "by": r["by"],
             "at": r["created_at"].isoformat()} for r in rows]


async def restore(project_id: int, org_id: int, path: str, version_id: int,
                  user_id: int | None) -> dict:
    """Put a version back, as a new version. Nothing is overwritten or lost."""
    kind = kind_of(path)
    table, payload = TABLES[kind]
    async with conn() as c:
        old = await c.fetchrow(
            f"SELECT * FROM {table} WHERE id=$1 AND project_id=$2 AND org_id=$3",
            version_id, project_id, org_id)
        if not old:
            raise VersionError("that version isn't there any more")
        if old["live"]:
            raise VersionError("that version is already the live one")

        async with c.transaction():
            await c.execute(f"UPDATE {table} SET live=false WHERE project_id=$1", project_id)
            if kind == "site":
                await c.execute(
                    f"""INSERT INTO {table} (org_id, project_id, slug, html, live, created_by)
                        VALUES ($1,$2,$3,$4,true,$5)""",
                    org_id, project_id, old["slug"], old["html"], user_id)
            else:
                await c.execute(
                    f"""INSERT INTO {table} (org_id, project_id, slug, files, live, created_by)
                        VALUES ($1,$2,$3,$4,true,$5)""",
                    org_id, project_id, old["slug"], old[payload], user_id)

    # The editor has to follow the site, or the next change would be made against
    # the newer code and quietly undo the restore.
    if kind == "app":
        await appfs.write(project_id, org_id, dict(old["files"] or {}))
    elif kind == "game":
        await godot.write(project_id, org_id, dict(old["files"] or {}))

    await log_event(org_id, f"{kind}.restored", old["created_at"].strftime("%d %b %H:%M"),
                    project_id, user_id)
    return {"restored": True, "from": old["created_at"].isoformat()}


async def preview_html(project_id: int, org_id: int, version_id: int) -> str | None:
    """The page as it was, for looking before restoring."""
    async with conn() as c:
        return await c.fetchval(
            """SELECT html FROM site_releases
               WHERE id=$1 AND project_id=$2 AND org_id=$3""",
            version_id, project_id, org_id)
