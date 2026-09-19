"""
The concept, formed before a line of code.

A game gets built from a description the way an ad used to get written from a
brief: straight to the thing, with no argument behind it. The result is a game
that is technically what was asked for and gives nobody a reason to play it
twice — which, given 17,889 games shipped on Steam last year and a median
lifetime take of $570, is a game that does not exist.

So this is the hypothesis step for games, the same discipline the ad pipeline
has: who plays it, the one mechanic, the hook in a sentence, what the first
thirty seconds actually are, and the question nobody asks — why this one and
not the thousands like it.

Two disciplines it keeps.

It refuses a clone by saying so. "Flappy Bird but blue" gets named as a clone
and handed back with the twist that would make it a reason to play. Silence
here is how a platform ships the same game ten thousand times and gets a
store's spam rejection for all of them.

It says what it cannot know. Asked whether anyone wants this, it has no data,
and it says so with a low confidence rather than a guess dressed as a finding.
"""

import json
import logging

import httpx

from ..core.config import settings

log = logging.getLogger("creai.gameconcept")

MODEL = "claude-fable-5-1"

SYSTEM = """You are working out what a game IS before anyone builds it. You have the person's
description and nothing else.

Return ONLY JSON:
{
  "title":       a working title, two or three words, not generic,
  "player":      who plays this, specifically — "someone with three minutes on a bus",
                 not "casual gamers",
  "mechanic":    the ONE verb the game is built on, in five words or fewer,
  "hook":        one sentence that would make that player want to try it,
  "thirty_seconds": what the first half minute actually is, as a player experiences it,
  "twist":       what makes this one different from the games it resembles — must be
                 concrete and buildable, not "better graphics",
  "resembles":   the closest existing game or genre, named honestly,
  "is_clone":    true if without the twist this would be indistinguishable from `resembles`,
  "ceiling":     "" or the one sentence that names anything asked for that cannot be
                 built here — no photorealism, no online multiplayer, no licensed worlds,
  "confidence":  "high" | "medium" | "low"
}

Rules:
- ONE mechanic. If the description has three, pick the one the others depend on and
  say the rest are later.
- If it is a clone, say so in `is_clone` and put a real twist in `twist`. Do not soften
  it. A platform that ships clones gets every one of them rejected as spam.
- The twist must be something a builder can make: a rule, a constraint, a reversal.
  "Unique art style" is not a twist.
- Use the person's language for the player, not marketing language. No "engaging",
  "immersive", "addictive".
- `confidence` is low when you are guessing about whether anyone wants this. You have
  no data. Say so rather than dress it up."""


class ConceptError(RuntimeError):
    pass


async def form(description: str) -> dict:
    """The concept, or a plain reason it could not be formed."""
    if not settings.anthropic_key:
        raise ConceptError("the concept brain isn't configured")
    description = (description or "").strip()
    if not description:
        raise ConceptError("there is nothing to form a concept from")

    from . import models
    try:
        out = await models.complete(MODEL, [{"role": "user", "content": description}],
                                    tools=[], system=SYSTEM, max_tokens=1200)
    except (models.ModelRejected, models.ModelUnavailable, models.ModelAccountProblem) as exc:
        raise ConceptError(str(exc)) from exc

    text = (out.get("text") or "").strip().strip("`")
    if text.startswith("json"):
        text = text[4:]
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ConceptError("the concept came back unreadable") from exc
    c = _clean(raw)
    return {**c, "problems": check(c)}


def _clean(r: dict) -> dict:
    keep = ("title", "player", "mechanic", "hook", "thirty_seconds", "twist",
            "resembles", "ceiling")
    out = {k: str(r.get(k) or "")[:300] for k in keep}
    out["is_clone"] = bool(r.get("is_clone"))
    out["confidence"] = r.get("confidence") if r.get("confidence") in ("high", "medium", "low") \
        else "low"
    return out


HOLLOW = ("engaging", "immersive", "addictive", "innovative", "unique art style",
          "better graphics", "next-level", "fun for all ages")


def check(c: dict) -> list[str]:
    """What is weak about a concept, before a build is spent on it."""
    problems = []
    if len((c.get("mechanic") or "").split()) > 6:
        problems.append("the mechanic is not one verb — pick the one the others depend on")
    if c.get("is_clone") and not (c.get("twist") or "").strip():
        problems.append("this is a clone with no twist; it will not be built as is")
    for w in HOLLOW:
        for field in ("hook", "twist", "player"):
            if w in (c.get(field) or "").lower():
                problems.append(f"'{w}' is marketing language, not a concept")
                break
    if not (c.get("thirty_seconds") or "").strip():
        problems.append("no thirty-second experience described, so there is nothing to build to")
    return problems


def brief_from(c: dict) -> str:
    """What the builder is told, from the concept rather than from the raw ask."""
    lines = [
        f"Title: {c.get('title', '')}",
        f"Who plays it: {c.get('player', '')}",
        f"The one mechanic: {c.get('mechanic', '')}",
        f"The hook: {c.get('hook', '')}",
        f"The first thirty seconds: {c.get('thirty_seconds', '')}",
        f"What makes it not {c.get('resembles', 'the obvious one')}: {c.get('twist', '')}",
    ]
    if c.get("ceiling"):
        lines.append(f"Say this to the person before building: {c['ceiling']}")
    lines.append("Build the mechanic first and make it feel good before anything else exists.")
    return "\n".join(lines)
