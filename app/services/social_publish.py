"""
Social publishing through CreAI's self-hosted Postiz.

CreAI holds one Postiz organisation. Each connected social account (a Postiz
"integration") is mapped to exactly one CreAI workspace in our own database, and
a post is only ever sent to a channel mapped to the workspace that approved it.

Delivery is deliberately conservative: a post that a platform would reject
(Instagram without an image, TikTok without a video) is held with a plain
reason instead of being sent to fail.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from ..core.config import settings
from ..core.db import conn, log_event

log = logging.getLogger("creai.social")

# CreAI network name → Postiz integration identifiers that can carry it
IDENTIFIERS = {
    "instagram": ("instagram", "instagram-standalone"),
    "facebook": ("facebook",),
    "google_business": ("gmb",),
    "linkedin": ("linkedin-page", "linkedin"),
    "x": ("x",),
    "threads": ("threads",),
    "tiktok": ("tiktok",),
    "youtube": ("youtube",),
    "pinterest": ("pinterest",),
}
NEEDS_MEDIA = {"instagram": "an image", "tiktok": "a video", "youtube": "a video",
               "pinterest": "an image and a board"}
SWEEP_SECONDS = 300


class PostizError(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.postiz_api_key and settings.postiz_url)


async def _call(method: str, path: str, **kw):
    if not configured():
        raise PostizError("social publishing isn't configured")
    async with httpx.AsyncClient(timeout=30) as x:
        r = await x.request(method, settings.postiz_url.rstrip("/") + path,
                            headers={"Authorization": settings.postiz_api_key}, **kw)
    if r.status_code >= 400:
        raise PostizError(f"Postiz {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


async def is_connected() -> bool:
    try:
        return bool((await _call("GET", "/is-connected")).get("connected"))
    except (PostizError, httpx.HTTPError):
        return False


async def integrations() -> list[dict]:
    return await _call("GET", "/integrations")


# ---------------------------------------------------------------- channel mapping

async def assign(integration_id: str, org_id: int) -> dict:
    found = next((i for i in await integrations() if i["id"] == integration_id), None)
    if not found:
        raise PostizError("no such channel in Postiz")
    network = next((k for k, ids in IDENTIFIERS.items() if found["identifier"] in ids), None)
    if not network:
        raise PostizError(f"CreAI doesn't publish to {found['identifier']} yet")
    async with conn() as c:
        row = await c.fetchrow(
            """INSERT INTO social_channels (org_id, postiz_id, network, identifier, name, picture)
               VALUES ($1,$2,$3,$4,$5,$6)
               ON CONFLICT (postiz_id) DO UPDATE
                 SET org_id=EXCLUDED.org_id, name=EXCLUDED.name, picture=EXCLUDED.picture,
                     status='active', updated_at=now()
               RETURNING *""",
            org_id, integration_id, network, found["identifier"],
            found.get("name"), found.get("picture"))
    await log_event(org_id, "channel.connected", network, None, None)
    return dict(row)


async def unassign(integration_id: str) -> None:
    async with conn() as c:
        await c.execute("DELETE FROM social_channels WHERE postiz_id=$1", integration_id)


async def channels(org_id: int) -> list[dict]:
    async with conn() as c:
        rows = await c.fetch(
            """SELECT network, identifier, name, picture, status, created_at
               FROM social_channels WHERE org_id=$1 ORDER BY network""", org_id)
    return [dict(r) | {"created_at": r["created_at"].isoformat()} for r in rows]


# ---------------------------------------------------------------- delivery

def _settings(network: str, identifier: str, link: str) -> dict:
    s = {"__type": identifier}
    if identifier == "facebook" and link:
        s["url"] = link
    elif identifier == "gmb":
        s["topicType"] = "STANDARD"
        if link:
            s.update(callToActionType="LEARN_MORE", callToActionUrl=link)
    elif identifier in ("instagram", "instagram-standalone"):
        s.update(post_type="post", collaborators=[])
    elif identifier == "x":
        s.update(who_can_reply_post="everyone", community="")
    return s


def _content(network: str, text: str, link: str) -> str:
    if link and link not in text and network not in ("facebook", "google_business"):
        return f"{text}\n\n{link}"
    return text


async def _mark(c, approval_id: int, state: str, delivery: dict) -> None:
    await c.execute(
        """UPDATE approvals SET state=$2, payload = payload || jsonb_build_object('delivery', $3::jsonb)
           WHERE id=$1""", approval_id, state, delivery)


async def deliver(approval_id: int) -> str:
    """Try to hand one approved post to Postiz. Returns the resulting state."""
    async with conn() as c:
        # claim it: only one worker can move a post into 'sending'
        row = await c.fetchrow(
            """UPDATE approvals SET state='sending'
               WHERE id=$1 AND kind='post' AND state IN ('approved','held') RETURNING *""",
            approval_id)
        if not row:
            return "skipped"
        p = row["payload"] or {}
        network = p.get("network", "")
        has_image = any(x.get("type") == "image" for x in p.get("media") or [])
        needs = NEEDS_MEDIA.get(network)
        if needs and not (network == "instagram" and has_image):
            await _mark(c, approval_id, "held",
                        {"reason": f"{network.replace('_', ' ').title()} posts need {NEEDS_MEDIA[network]}."})
            return "held"
        ch = await c.fetchrow(
            """SELECT * FROM social_channels WHERE org_id=$1 AND network=$2 AND status='active'
               ORDER BY created_at LIMIT 1""", row["org_id"], network)
        if not ch:
            await _mark(c, approval_id, "held",
                        {"reason": f"Connect {network.replace('_', ' ').title()} to publish this."})
            return "held"

    uploaded = []
    for item in (p.get("media") or [])[:4]:
        if item.get("type") != "image" or not str(item.get("url", "")).startswith("https://"):
            continue
        try:
            up = await _call("POST", "/upload-from-url", json={"url": item["url"]})
            uploaded.append({"id": up["id"], "path": up["path"]})
        except (PostizError, httpx.HTTPError, KeyError) as exc:
            async with conn() as c:
                await _mark(c, approval_id, "failed", {"reason": "The image couldn't be attached.",
                                                       "detail": str(exc)[:300]})
            return "failed"

    now = datetime.now(timezone.utc)
    when = row["scheduled_for"] if row["scheduled_for"] and row["scheduled_for"] > now + timedelta(minutes=2) else None
    body = {
        "type": "schedule" if when else "now",
        "date": (when or now).isoformat().replace("+00:00", "Z"),
        "shortLink": False,
        "tags": [],
        "posts": [{
            "integration": {"id": ch["postiz_id"]},
            "value": [{"content": _content(network, p.get("text", ""), p.get("link", "")), "image": uploaded}],
            "settings": _settings(network, ch["identifier"], p.get("link", "")),
        }],
    }
    try:
        out = await _call("POST", "/posts", json=body)
    except (PostizError, httpx.HTTPError) as exc:
        async with conn() as c:
            await _mark(c, approval_id, "failed", {"reason": "The platform didn't accept this post.",
                                                   "detail": str(exc)[:300]})
        log.warning("post %s failed: %s", approval_id, exc)
        return "failed"
    post_id = (out[0] if isinstance(out, list) and out else {}).get("postId")
    async with conn() as c:
        await _mark(c, approval_id, "scheduled",
                    {"postiz_post_id": post_id, "channel": ch["name"],
                     "at": (when or now).isoformat()})
        await c.execute("UPDATE approvals SET executed_at=now() WHERE id=$1", approval_id)
    await log_event(row["org_id"], "post.scheduled", network, row["project_id"], None)
    return "scheduled"


async def cancel(approval_id: int, org_id: int) -> None:
    async with conn() as c:
        row = await c.fetchrow(
            "SELECT payload FROM approvals WHERE id=$1 AND org_id=$2 AND kind='post'",
            approval_id, org_id)
    post_id = ((row and row["payload"]) or {}).get("delivery", {}).get("postiz_post_id")
    if post_id:
        try:
            await _call("DELETE", f"/posts/{post_id}")
        except (PostizError, httpx.HTTPError) as exc:
            log.warning("could not cancel postiz post %s: %s", post_id, exc)


async def sweep_once() -> int:
    """Deliver anything approved or held whose channel may now exist."""
    if not configured():
        return 0
    async with conn() as c:
        if not await c.fetchval("SELECT pg_try_advisory_lock(424242)"):
            return 0
        try:
            ids = await c.fetch(
                """SELECT id FROM approvals WHERE kind='post' AND state IN ('approved','held')
                   ORDER BY scheduled_for NULLS FIRST LIMIT 50""")
        finally:
            await c.execute("SELECT pg_advisory_unlock(424242)")
    done = 0
    for r in ids:
        if await deliver(r["id"]) == "scheduled":
            done += 1
    return done


async def sweeper() -> None:
    while True:
        try:
            await sweep_once()
        except Exception:                     # keep the loop alive
            log.exception("social sweep failed")
        await asyncio.sleep(SWEEP_SECONDS)
