"""
Telling people before their credits run out.

In the app: every reply carries a credit status (ok / low / out) sized to what the
next message will likely cost. By email: one note when a workspace drops below 20%
of its last top-up or plan grant, and one when it reaches zero — each sent once per
refill, never repeated until credits are added again.
"""

import asyncio
from datetime import datetime, timezone

from ..core.config import settings
from ..core.db import conn
from . import billing, mailer, plans

LOW_SHARE = 0.20
MIN_LOW = 20
MESSAGES_WARN = 3


async def status(org_id: int, mode: str = "best", kind: str = "site") -> dict:
    bal = await billing.balance(org_id)
    tier = mode if mode in billing.MIN_TO_START else "best"
    est = await billing.estimate(org_id, tier, kind)
    left = max(0, bal) // max(1, est["high"])
    if bal < billing.MIN_TO_START[tier]:
        state = "out"
    elif left < MESSAGES_WARN:
        state = "low"
    else:
        state = "ok"
    cur = await plans.current(org_id)
    refill = None
    if cur["plan"] in plans.PAID:
        if cur["interval"] == "monthly":
            refill = cur["renews_at"]
        else:
            now = datetime.now(timezone.utc)
            refill = (datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=timezone.utc)).isoformat()
    return {"balance": bal, "state": state, "messages_left": left,
            "next_message": {"low": est["low"], "high": est["high"]},
            "plan": cur["plan"], "refill_at": refill,
            "refill_credits": plans.PLANS[cur["plan"]]["credits"] if cur["plan"] in plans.PAID else 0}


async def _owners(c, org_id: int) -> list[str]:
    rows = await c.fetch(
        """SELECT u.email FROM memberships m JOIN users u ON u.id = m.user_id
           WHERE m.org_id=$1 AND m.role IN ('owner','admin')""", org_id)
    return [r["email"] for r in rows]


def _send_all(emails: list[str], subject: str, body: str) -> bool:
    return all(mailer.send_notice(e, subject, body) for e in emails) if emails else False


async def check(org_id: int) -> str | None:
    """Send the low / empty email if it's due. Returns which one was sent."""
    bal = await billing.balance(org_id)
    async with conn() as c:
        last_add = await c.fetchrow(
            """SELECT delta, created_at FROM credit_ledger
               WHERE org_id=$1 AND delta > 0 ORDER BY id DESC LIMIT 1""", org_id)
        if not last_add:
            return None
        biggest = await c.fetchval(
            """SELECT COALESCE(MAX(delta), 0) FROM credit_ledger
               WHERE org_id=$1 AND delta > 0 AND created_at > now() - interval '90 days'""", org_id)
        s = await c.fetchrow("SELECT low_alert_at, empty_alert_at FROM org_settings WHERE org_id=$1", org_id)
        emails = await _owners(c, org_id)
    since = last_add["created_at"]
    low_at, empty_at = (s["low_alert_at"], s["empty_alert_at"]) if s else (None, None)
    threshold = max(MIN_LOW, int(biggest * LOW_SHARE))
    cur = await plans.current(org_id)
    base = settings.public_url.rstrip("/")
    refill = ""
    if cur["plan"] in plans.PAID:
        refill = f"Your {plans.PLANS[cur['plan']]['credits']:,} plan credits refill at the start of your next period. "

    kind = None
    if bal <= 0 and (empty_at is None or empty_at < since):
        kind, col = "empty", "empty_alert_at"
        subject = "You're out of CreAI credits"
        body = (f"Your workspace has used all its credits, so new messages are paused. Everything you've "
                f"built is saved, and published sites stay online.\n\n{refill}"
                f"To keep building now, top up at {base} (Credits).\n")
    elif 0 < bal <= threshold and (low_at is None or low_at < since):
        kind, col = "low", "low_alert_at"
        subject = f"{bal:,} CreAI credits left"
        body = (f"Heads up: your workspace has {bal:,} credits left. {refill}"
                f"You can top up or change your monthly limit any time at {base} (Credits).\n")
    if not kind:
        return None
    sent = await asyncio.get_running_loop().run_in_executor(None, _send_all, emails, subject, body)
    if sent:
        async with conn() as c:
            await c.execute(
                f"""INSERT INTO org_settings (org_id, {col}) VALUES ($1, now())
                    ON CONFLICT (org_id) DO UPDATE SET {col}=now(), updated_at=now()""", org_id)
        return kind
    return None
