"""
Editing drafted posts — by the person (Review tab) or by the agent on request.

Only posts that haven't gone out can change: pending drafts, and approved posts
still waiting (held). Editing a waiting post sends it back for approval, so
nothing is published in a form the person didn't see.
"""

from ..core.db import conn

EDITABLE = ("pending", "held", "failed")
MAX_TEXT = 3000


class PostError(ValueError):
    pass


def _clean_link(v: str) -> str:
    v = (v or "").strip()
    if v and not (v.startswith("https://") and len(v) <= 500 and " " not in v):
        raise PostError("links must start with https://")
    return v


async def pending(org_id: int, project_id: int | None, limit: int = 30) -> list[dict]:
    async with conn() as c:
        rows = await c.fetch(
            """SELECT id, payload, scheduled_for, state FROM approvals
               WHERE org_id=$1 AND kind='post' AND state = ANY($2::text[])
                 AND ($3::bigint IS NULL OR project_id=$3)
               ORDER BY scheduled_for NULLS LAST, id LIMIT $4""",
            org_id, list(EDITABLE), project_id, limit)
    return [{"id": r["id"], "network": (r["payload"] or {}).get("network"),
             "text": (r["payload"] or {}).get("text", ""), "state": r["state"],
             "scheduled_for": r["scheduled_for"].isoformat() if r["scheduled_for"] else None,
             "has_image": any(m.get("type") == "image" for m in (r["payload"] or {}).get("media") or [])}
            for r in rows]


async def update(org_id: int, post_id: int, *, project_id: int | None = None, text: str | None = None,
                 scheduled_for=None, link: str | None = None, image_url: str | None = None,
                 editor: str = "you") -> dict:
    async with conn() as c:
        row = await c.fetchrow(
            """SELECT id, payload, state, scheduled_for FROM approvals
               WHERE id=$1 AND org_id=$2 AND kind='post'
                 AND ($3::bigint IS NULL OR project_id=$3)""",
            post_id, org_id, project_id)
        if not row:
            raise PostError("no such post")
        if row["state"] not in EDITABLE:
            raise PostError("this post has already gone out or been scheduled; cancel it and draft a new one")
        payload = dict(row["payload"] or {})
        if text is not None:
            text = text.strip()
            if not text:
                raise PostError("a post needs some text")
            payload["text"] = text[:MAX_TEXT]
        if link is not None:
            payload["link"] = _clean_link(link)
        if image_url is not None:
            payload["media"] = [{"type": "image", "url": image_url}] if image_url else []
        payload.pop("delivery", None)
        payload["edited_by"] = editor
        when = row["scheduled_for"] if scheduled_for is None else scheduled_for
        await c.execute(
            """UPDATE approvals SET payload=$3, scheduled_for=$4,
                 state='pending', decided_at=NULL, decided_by=NULL
               WHERE id=$1 AND org_id=$2""",
            post_id, org_id, payload, when)
    return {"id": post_id, "state": "pending", "text": payload.get("text"),
            "scheduled_for": when.isoformat() if when else None}
