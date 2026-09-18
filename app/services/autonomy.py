"""
Posting without being asked, once the owner has said the brand is right.

Approving every post forever is friction that quietly kills the feature: people
stop opening the queue, the queue goes stale, and the marketing stops. So a
channel can be set to post on its own.

Three things make that safe enough to offer.

It is gated on the brand. Autonomy cannot be switched on until the owner has
confirmed the brand kit — the voice, the audience, the words. Posting in
somebody's voice before they have agreed what their voice is is the one mistake
that cannot be taken back, and it is a deleted post with a screenshot of it still
going around.

It means "goes out unless you stop it", not "you never see it". An autonomous
post is scheduled with a window, visible the whole time, cancelled by one tap. A
trapdoor rather than a cliff.

And some posts never qualify, however trusted the channel. Anything carrying a
price, an offer, or a claim about an award, a rating or a review waits for a
human, because those are the ones with consequences beyond embarrassment.
"""

import logging
import re
from datetime import datetime, timedelta, timezone

from ..core.db import conn

log = logging.getLogger("creai.autonomy")

# How long an autonomous post sits where it can still be stopped.
WINDOW = timedelta(hours=4)

# Claims a machine should not make on a business's behalf unprompted. Prices and
# offers create obligations; ratings and awards are checkable facts that a model
# can get wrong in a way that looks like lying.
NEEDS_A_HUMAN = re.compile(
    r"""(?xi)
    (?:[£$€]\s?\d) |                                  # a price
    \b\d+\s?% \s?(?:off|discount) |                   # a discount
    \b(?:sale|offer|deal|coupon|promo|voucher)\b |
    \b(?:free|half[- ]price|bogo)\b |
    \b(?:award|award[- ]winning|rated|rating|stars?|reviews?|testimonial)\b |
    \b(?:guarantee[d]?|certified|licensed|insured|accredited)\b |
    \b(?:best|number\s?one|no\.?\s?1)\b | \#\s?1\b   # \b can't precede a #
    """)


def why_a_human(text: str) -> str | None:
    """The reason a post must wait, or None when it may go on its own."""
    m = NEEDS_A_HUMAN.search(text or "")
    if not m:
        return None
    return (f"it mentions “{m.group(0).strip()}” — prices, offers and claims about awards "
            "or reviews always wait for you")


async def settings_for(org_id: int) -> dict:
    async with conn() as c:
        r = await c.fetchrow(
            """SELECT brand_confirmed_at, social_paused, weekly_post_cap
               FROM org_settings WHERE org_id=$1""", org_id)
    return {"brand_confirmed": bool(r and r["brand_confirmed_at"]),
            "paused": bool(r and r["social_paused"]),
            "cap": (r["weekly_post_cap"] if r and r["weekly_post_cap"] else 14)}


async def confirm_brand(org_id: int, on: bool = True) -> dict:
    """The owner saying the brand is right. Turning it off also takes every channel
    off autonomy: if the voice is in question, nothing should be speaking in it."""
    when = datetime.now(timezone.utc) if on else None
    async with conn() as c:
        await c.execute(
            """INSERT INTO org_settings (org_id, brand_confirmed_at) VALUES ($1,$2)
               ON CONFLICT (org_id) DO UPDATE SET brand_confirmed_at=EXCLUDED.brand_confirmed_at""",
            org_id, when)
        if not on:
            await c.execute("UPDATE social_channels SET autonomous=false WHERE org_id=$1", org_id)
    return {"brand_confirmed": on}


async def set_channel(org_id: int, channel_id: int, on: bool) -> dict:
    """Turn a single channel autonomous. Refused until the brand is confirmed."""
    state = await settings_for(org_id)
    if on and not state["brand_confirmed"]:
        raise PermissionError(
            "Confirm your brand first — the voice, the audience and the words. Creai won't "
            "post as you until you've agreed that's you.")
    async with conn() as c:
        done = await c.execute(
            "UPDATE social_channels SET autonomous=$1 WHERE id=$2 AND org_id=$3",
            on, channel_id, org_id)
    if done.endswith("0"):
        raise LookupError("no such channel")
    return {"autonomous": on}


async def pause(org_id: int, on: bool) -> dict:
    """The one control that stops everything at once, without unpicking settings.
    Someone reaching for this is having a bad day; it should not ask questions."""
    async with conn() as c:
        await c.execute(
            """INSERT INTO org_settings (org_id, social_paused) VALUES ($1,$2)
               ON CONFLICT (org_id) DO UPDATE SET social_paused=EXCLUDED.social_paused""",
            org_id, on)
        if on:
            await c.execute(
                """UPDATE approvals SET state='held'
                   WHERE org_id=$1 AND kind='post' AND state IN ('pending','scheduled')
                     AND autonomous""", org_id)
    return {"paused": on}


async def posted_this_week(org_id: int) -> int:
    async with conn() as c:
        return await c.fetchval(
            """SELECT count(*) FROM approvals
               WHERE org_id=$1 AND kind='post' AND executed_at > now() - interval '7 days'""",
            org_id) or 0


async def decide(org_id: int, network: str, text: str) -> dict:
    """Whether this post goes out on its own, and if not, why not. The reason is
    written to be shown to the owner, not logged and forgotten."""
    state = await settings_for(org_id)
    if state["paused"]:
        return {"autonomous": False, "reason": "posting is paused"}
    if not state["brand_confirmed"]:
        return {"autonomous": False, "reason": "your brand isn't confirmed yet"}

    async with conn() as c:
        allowed = await c.fetchval(
            """SELECT autonomous FROM social_channels
               WHERE org_id=$1 AND network=$2 AND status='active' LIMIT 1""", org_id, network)
    if not allowed:
        return {"autonomous": False, "reason": f"{network} still asks you first"}

    held = why_a_human(text)
    if held:
        return {"autonomous": False, "reason": held}

    if await posted_this_week(org_id) >= state["cap"]:
        return {"autonomous": False,
                "reason": f"that's {state['cap']} posts this week already, which is your limit"}

    return {"autonomous": True, "holds_until": datetime.now(timezone.utc) + WINDOW}
