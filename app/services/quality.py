"""
What goes wrong, across every build.

Each reviewer finding is already handled in its own turn: the agent is told and
fixes it before replying. That makes them invisible, which is the problem. In
aggregate they are the most honest description of where this product is weak —
better than any survey, because they come from real work being done for real
businesses.

The loop this closes: a fault that recurs a hundred times a week is either a
prompt that needs rewriting, a rule that is wrong, or a capability that is
missing. Every real fix tonight came from noticing one of those by hand.

Findings are stored as a shape, not a sentence. "Remove the emoji (🔧)" and
"Remove the emoji (💧)" are one fault, not two, or the tally says nothing.
"""

import logging
import re

from ..core.db import conn

log = logging.getLogger("creai.quality")

# The shapes worth counting. First match wins, so order from specific to general.
FAULTS = [
    ("emoji-as-icon", r"\bemoji\b"),
    ("placeholder-on-page", r"\bplaceholder\b"),
    ("dead-sign-in-button", r"way into an app"),
    ("missing-contact", r"way to act|contact details"),
    ("thin-page", r"at least two sections|only one kind"),
    ("stock-phrasing", r"stock phrasing|elevate|seamless"),
    ("greeting-headline", r"headline with what the customer gets"),
    ("shouting", r"exclamation|emoji from the headline"),
    ("jsx-instead-of-templates", r"looks like JSX"),
    ("missing-import", r"without importing|isn't available"),
    ("broken-import", r"doesn't exist|no default export|doesn't export it"),
    ("unclosed-template", r"odd number of backticks"),
    ("entry-missing", r"app\.js is missing|still the starter|never renders"),
    ("sign-in-mismatch", r"expects people to sign in"),
    ("bad-app-json", r"app\.json isn't valid"),
    ("godot-main-scene", r"run/main_scene"),
    ("godot-dangling-ref", r"which doesn't exist|which no file provides"),
    ("godot-scene-header", r"gd_scene"),
]


def shape_of(issue: str) -> str:
    """One fault name for many wordings, so the tally means something."""
    text = issue or ""
    for name, pattern in FAULTS:
        if re.search(pattern, text, re.I):
            return name
    return "other"


async def record(org_id: int, project_id: int | None, surface: str, issues: list[str]) -> None:
    """Note what a reviewer caught. Never raises: this is telemetry, and it must
    not be able to break the build it is observing."""
    rows = [(org_id, project_id, surface, shape_of(i)) for i in (issues or [])[:20]]
    if not rows:
        return
    try:
        async with conn() as c:
            await c.executemany(
                """INSERT INTO quality_findings (org_id, project_id, surface, fault)
                   VALUES ($1,$2,$3,$4)""", rows)
    except Exception:
        log.exception("could not record quality findings")


async def digest(days: int = 7, limit: int = 25) -> dict:
    """What recurs, and where. The working list for the next round of fixes."""
    async with conn() as c:
        top = await c.fetch(
            f"""SELECT fault, surface, count(*) AS hits,
                       count(DISTINCT project_id) AS projects
                FROM quality_findings
                WHERE created_at > now() - interval '{int(days)} days'
                GROUP BY fault, surface ORDER BY hits DESC LIMIT {int(limit)}""")
        total = await c.fetchval(
            f"""SELECT count(*) FROM quality_findings
                WHERE created_at > now() - interval '{int(days)} days'""")
        builds = await c.fetchval(
            f"""SELECT count(DISTINCT project_id) FROM quality_findings
                WHERE created_at > now() - interval '{int(days)} days'""")
        errors = await c.fetch(
            """SELECT message, count(*) AS hits FROM app_errors
               WHERE created_at > now() - interval '7 days'
               GROUP BY message ORDER BY hits DESC LIMIT 10""")
    return {
        "days": days,
        "findings": total or 0,
        "projects_affected": builds or 0,
        "top": [dict(r) for r in top],
        "app_errors": [dict(r) for r in errors],
    }
