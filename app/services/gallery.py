"""
Things people made here, and making one of your own from them.

Two ideas in one place. The gallery is proof: a stranger deciding whether this
works can look at real businesses instead of a landing page. Remix is the way in:
they see something close to what they want, take it, and it is theirs in a tap.

What travels, and what does not. A remix copies the shape of a thing — the page,
the code, the look. It never copies what belongs to the business that made it:
no customers, no records, no uploaded files, no payment account, no domain, no
takings. That line is absolute, and it is enforced by copying a short list of
fields rather than by cloning a row and then deleting things, because the second
approach leaks the day somebody adds a column.

Appearing here is always the owner's choice, and always reversible.
"""

import logging

from ..core.db import conn, log_event
from . import appfs, godot

log = logging.getLogger("creai.gallery")

KINDS = {"launch": "Site", "edit": "Site", "app": "App", "game": "Game"}


class GalleryError(ValueError):
    """Something the person should be told plainly."""


def _card(r) -> dict:
    """What a stranger may see: the work, never the workspace."""
    site = (r["answers"] or {}).get("site") or {}
    return {
        "id": r["id"],
        "name": site.get("business") or r["name"],
        "kind": KINDS.get(r["path"], "Site"),
        "headline": site.get("headline") or "",
        "thumb": f"/f/{r['thumb_token']}" if r["thumb_token"] else None,
        "remixes": r["remixes"],
        "url": r["public"],
    }


async def listing(limit: int = 60, kind: str | None = None) -> list[dict]:
    """The gallery. Nothing about who made it, or from where."""
    async with conn() as c:
        rows = await c.fetch(
            """SELECT p.id, p.name, p.path, p.answers, p.thumb_token, p.remixes,
                      COALESCE(s.slug, a.slug, g.slug) AS public
               FROM projects p
               LEFT JOIN site_releases s ON s.project_id = p.id AND s.live
               LEFT JOIN app_releases  a ON a.project_id = p.id AND a.live
               LEFT JOIN game_releases g ON g.project_id = p.id AND g.live
               WHERE p.showcase AND NOT p.showcase_hidden AND p.archived_at IS NULL
                 AND ($1::text IS NULL OR p.path = $1)
               ORDER BY p.updated_at DESC LIMIT $2""",
            {"Site": "launch", "App": "app", "Game": "game"}.get(kind or ""), limit)
    return [_card(r) for r in rows]


async def show(project_id: int, org_id: int, on: bool, user_id: int | None) -> dict:
    """The owner choosing whether their work appears. Only something published can
    be shown: a gallery of drafts would be a gallery of broken links."""
    async with conn() as c:
        live = await c.fetchval(
            """SELECT EXISTS (SELECT 1 FROM site_releases WHERE project_id=$1 AND live)
                   OR EXISTS (SELECT 1 FROM app_releases  WHERE project_id=$1 AND live)
                   OR EXISTS (SELECT 1 FROM game_releases WHERE project_id=$1 AND live)""",
            project_id)
        if on and not live:
            raise GalleryError("publish it first — the gallery only shows things people can open")
        done = await c.execute(
            "UPDATE projects SET showcase=$1 WHERE id=$2 AND org_id=$3", on, project_id, org_id)
    if done.endswith("0"):
        raise GalleryError("no such project")
    await log_event(org_id, "gallery.shown" if on else "gallery.hidden", "", project_id, user_id)
    return {"showcase": on}


async def remix(source_id: int, org_id: int, user_id: int | None) -> dict:
    """Make somebody's work your own to change. The shape travels; their business
    does not."""
    async with conn() as c:
        src = await c.fetchrow(
            """SELECT id, name, path, answers FROM projects
               WHERE id=$1 AND showcase AND NOT showcase_hidden AND archived_at IS NULL""",
            source_id)
        if not src:
            raise GalleryError("that isn't in the gallery any more")

        # Only the look and the words. Anything a business accumulated is left behind.
        site = (src["answers"] or {}).get("site") or {}
        answers = {"site": site} if site else {}

        new_id = await c.fetchval(
            """INSERT INTO projects (org_id, created_by, name, path, answers)
               VALUES ($1,$2,$3,$4,$5) RETURNING id""",
            org_id, user_id, f"{site.get('business') or src['name']} (remix)",
            src["path"], answers)
        await c.execute("UPDATE projects SET remixes = remixes + 1 WHERE id=$1", source_id)

    if src["path"] == "app":
        files = await appfs.files(source_id, await _org_of(source_id))
        if files:
            await appfs.write(new_id, org_id, files)
    elif src["path"] == "game":
        files = await godot.files(source_id, await _org_of(source_id))
        if files:
            await godot.write(new_id, org_id, files)

    await log_event(org_id, "gallery.remixed", str(source_id), new_id, user_id)
    return {"project_id": new_id}


async def _org_of(project_id: int) -> int:
    async with conn() as c:
        return await c.fetchval("SELECT org_id FROM projects WHERE id=$1", project_id)
