"""
The scoreboard.

The point of measuring is not to have numbers. It is to know which of the things
we are spending time on actually produces paying customers, and to be able to
prove it to somebody who has every reason to be sceptical.

Two decisions shape this file.

First touch, not last. Whatever introduced somebody is what earned the signup.
Recording the last click before they registered credits the retargeting ad for
work the blog post did, and then the budget moves to the wrong place.

Say when something is not measured. Half of the funnel a growth dashboard usually
shows — visitors, cost per acquisition, lifetime value, referral rate — cannot be
computed from what this platform records. A zero in those rows would be read as a
fact, and a stranger doing diligence would find the gap in an afternoon. Every
figure here is either real or explicitly marked as not yet instrumented.
"""

import logging
import re
from urllib.parse import urlparse

from ..core.db import conn

log = logging.getLogger("creai.metrics")

# Channels that map to how the work is actually budgeted, so the numbers can be
# read against the plan rather than translated first.
CHANNELS = ("content", "partner", "paid", "pr", "referral", "direct")

# Which host means which channel, for the cases a UTM tag is missing — which is
# most of them, because people share links with the tags stripped.
BY_HOST = {
    "google.": "content", "bing.": "content", "duckduckgo.": "content",
    "chatgpt.com": "content", "perplexity.ai": "content", "gemini.google": "content",
    "claude.ai": "content",
    "youtube.": "content", "reddit.": "content", "news.ycombinator": "content",
    "x.com": "content", "twitter.": "content", "linkedin.": "content",
    "facebook.": "content", "instagram.": "content", "tiktok.": "content",
    "producthunt.": "pr", "techcrunch.": "pr",
}

# What counts as having actually used the thing. Registering is not using it: a
# dashboard that counts signups as progress flatters itself.
ACTIVATED_EVENTS = ("site.published", "app.published", "game.published",
                    "site.import_published", "video.rendered")


def classify(utm_source: str | None, utm_medium: str | None,
             referrer: str | None) -> tuple[str, str]:
    """(channel, detail) for a new arrival. Explicit tags win; a referring host is
    the fallback; anything unrecognised is 'direct' rather than a guess."""
    medium = (utm_medium or "").lower().strip()
    source = (utm_source or "").lower().strip()

    if medium in ("cpc", "ppc", "paid", "display"):
        return "paid", source or medium
    if medium in ("affiliate", "partner"):
        return "partner", source or medium
    if medium in ("referral",) and source:
        return "referral", source
    if medium in ("email", "newsletter"):
        return "content", source or "newsletter"
    if source:
        host_channel = _from_host(source)
        return (host_channel or "content"), source

    if referrer:
        host = (urlparse(referrer).hostname or "").lower()
        if host:
            found = _from_host(host)
            if found:
                return found, host
            return "referral", host
    return "direct", ""


def _from_host(host: str) -> str | None:
    for needle, channel in BY_HOST.items():
        if needle in host:
            return channel
    return None


async def remember_source(user_id: int, channel: str, detail: str,
                          landed_on: str | None, referrer: str | None) -> None:
    """Record where somebody came from, once. Later visits never overwrite it."""
    async with conn() as c:
        await c.execute(
            """UPDATE users SET source = COALESCE(source, $2),
                                source_detail = COALESCE(source_detail, $3),
                                landed_on = COALESCE(landed_on, $4),
                                referrer = COALESCE(referrer, $5)
               WHERE id = $1""",
            user_id, channel if channel in CHANNELS else "direct",
            (detail or "")[:200], (landed_on or "")[:300], (referrer or "")[:400])


# ---------------------------------------------------------------- the funnel

# Rows the platform cannot honestly fill in yet, with what each would need. Shown
# on the dashboard as missing rather than as zero.
NOT_INSTRUMENTED = {
    "visitors": "needs analytics on the marketing site — no page-view data reaches this platform",
    "cac": "needs spend per channel; nothing here knows what anything cost",
    "ltv": "needs a few months of churn before the number means anything",
    "referral_rate": "needs invites or shared links to be attributed back to a referrer",
}


async def funnel(days: int = 30) -> dict:
    """What is true, for the last N days and all time."""
    async with conn() as c:
        rows = await c.fetchrow(
            f"""SELECT
                 (SELECT count(*) FROM users) AS registered_all,
                 (SELECT count(*) FROM users
                   WHERE created_at > now() - interval '{int(days)} days') AS registered,
                 (SELECT count(DISTINCT e.org_id) FROM events e
                   WHERE e.kind = ANY($1)) AS activated_all,
                 (SELECT count(DISTINCT e.org_id) FROM events e
                   WHERE e.kind = ANY($1)
                     AND e.at > now() - interval '{int(days)} days') AS activated,
                 (SELECT count(*) FROM subscriptions
                   WHERE status IN ('active','trialing') AND plan <> 'free') AS paying,
                 (SELECT count(*) FROM organizations) AS orgs""",
            list(ACTIVATED_EVENTS))
        plans = await c.fetch(
            """SELECT plan, interval, count(*) AS n FROM subscriptions
               WHERE status IN ('active','trialing') AND plan <> 'free'
               GROUP BY plan, interval""")

    from . import plans as plan_svc
    mrr = 0
    for p in plans:
        price = (plan_svc.PLANS.get(p["plan"], {}) or {}).get("monthly", 0)
        yearly = (plan_svc.PLANS.get(p["plan"], {}) or {}).get("yearly", 0)
        per_month = (yearly / 12) if p["interval"] == "yearly" and yearly else price
        mrr += (per_month or 0) * p["n"]

    return {
        "window_days": days,
        "measured": {
            "registered": rows["registered"],
            "registered_all_time": rows["registered_all"],
            "activated": rows["activated"],
            "activated_all_time": rows["activated_all"],
            "paying": rows["paying"],
            "mrr_cents": int(mrr),
            "workspaces": rows["orgs"],
        },
        "not_instrumented": NOT_INSTRUMENTED,
    }


async def by_channel(days: int = 30) -> list[dict]:
    """Registrations, activation and paying customers by where they came from.
    The row that says which of the effort is working."""
    async with conn() as c:
        rows = await c.fetch(
            f"""SELECT COALESCE(u.source, 'unknown') AS channel,
                       count(DISTINCT u.id) AS registered,
                       count(DISTINCT CASE WHEN a.org_id IS NOT NULL THEN u.id END) AS activated,
                       count(DISTINCT CASE WHEN s.status IN ('active','trialing')
                                            AND s.plan <> 'free' THEN u.id END) AS paying
                FROM users u
                LEFT JOIN memberships m ON m.user_id = u.id
                LEFT JOIN subscriptions s ON s.org_id = m.org_id
                LEFT JOIN (SELECT DISTINCT org_id FROM events WHERE kind = ANY($1)) a
                       ON a.org_id = m.org_id
                WHERE u.created_at > now() - interval '{int(days)} days'
                GROUP BY 1 ORDER BY registered DESC""",
            list(ACTIVATED_EVENTS))
    return [dict(r) for r in rows]


async def coverage() -> dict:
    """How much of the data is any good. A funnel split by channel is worthless if
    most rows say 'unknown', so the dashboard says so out loud."""
    async with conn() as c:
        r = await c.fetchrow(
            """SELECT count(*) AS total,
                      count(*) FILTER (WHERE source IS NOT NULL) AS known
               FROM users""")
    total = r["total"] or 0
    return {"users": total, "with_source": r["known"],
            "share": round((r["known"] / total) if total else 0, 3),
            "note": "Source is recorded from the day it was switched on; anyone who "
                    "registered before that will read as unknown, permanently."}
