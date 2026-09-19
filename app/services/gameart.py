"""
The art director, single-shot, on the model that is best at it.

The most common reason a set of drawings looks amateur is not that any one of
them is bad. It is that they do not belong to each other: a different stroke
weight here, a different light direction there, a colour that is in no other
sprite. Each was drawn well and the game looks assembled from parts.

So this produces a visual bible for one game before anything is drawn — the
palette, the construction rules every sprite shares, the light, the reference
feel — and the builder draws to it. One decision made once, instead of a
hundred small ones drifting.

Gemini, because the research is consistent that it has the better eye for
visual work and this is exactly one bounded, single-shot job. It never touches
a file. It returns a document; the orchestrator draws.
"""

import json
import logging

log = logging.getLogger("creai.gameart")

MODEL_KEY = "gemini-2.5-flash"

SYSTEM = """You are the art director for one small game. You are given its concept and you
return the visual rules everything in it will follow. You do not draw; you decide.

Return ONLY JSON:
{
  "palette": {
    "background": hex,     dark and desaturated, or light and warm — pick one, say which
    "neutral":    hex,     the mid tone for everything that is not important
    "accent":     hex,     ONE saturated colour, reserved for the player and the danger
    "accent_2":   hex or "" — a second only if the game genuinely needs two teams/states
  },
  "style":       one line — "flat geometric, no outlines", "chunky pixel with 2px stroke",
                 "paper cut-out with grain", etc. Must be achievable in SVG and shader code.
  "light":       where the light comes from, for every sprite — "top-left, soft" — so
                 shading is consistent across the whole set
  "construction": the rules every sprite follows: stroke weight, corner radius, how many
                 colours per object, silhouette-first. Three to five short rules.
  "player":      how the player character reads at a glance — the silhouette in one phrase
  "danger":      how a threat reads at a glance, and how it differs from the player
  "feel":        the reference in one line — "like a screen-printed poster", "like a
                 well-made toy" — something a builder can hold in mind
  "avoid":       two or three things that would break the look
}

Rules:
- The accent goes on no more than a tenth of the screen. If it is everywhere it means nothing.
- Everything must be drawable as SVG paths and shader code. No textures, no photos, no
  fonts other than the engine default.
- Silhouette first: the player and the danger must be distinguishable as black shapes.
- Prefer restraint. Two colours used well beat six used badly."""


class ArtError(RuntimeError):
    pass


async def direct(concept: dict, theme: str = "") -> dict:
    """A visual bible for this game, or a plain reason there is none.

    Returns {"ok": False, ...} rather than raising when the model is unavailable:
    a game without an art bible is still buildable — the builder falls back to
    the craft rules in its brief — so this must not stop a build.
    """
    from . import models
    if not models.configured(MODEL_KEY):
        return {"ok": False, "reason": "the art director isn't configured", "bible": None}

    ask = "\n".join(f"{k}: {v}" for k, v in concept.items()
                    if k in ("title", "player", "mechanic", "hook", "thirty_seconds", "twist")
                    and v)
    if theme:
        ask += f"\nThe person asked for this look: {theme}"

    try:
        out = await models.complete(MODEL_KEY, [{"role": "user", "content": ask}],
                                    tools=[], system=SYSTEM, max_tokens=1000)
    except (models.ModelRejected, models.ModelUnavailable, models.ModelAccountProblem) as exc:
        log.warning("art director unavailable: %s", exc)
        return {"ok": False, "reason": "unavailable", "bible": None}

    text = (out.get("text") or "").strip().strip("`")
    if text.startswith("json"):
        text = text[4:]
    try:
        bible = json.loads(text)
    except ValueError:
        return {"ok": False, "reason": "unreadable", "bible": None}
    return {"ok": True, "bible": _clean(bible), "problems": check(bible)}


def _clean(b: dict) -> dict:
    pal = b.get("palette") or {}
    return {
        "palette": {k: str(pal.get(k) or "")[:9] for k in ("background", "neutral", "accent", "accent_2")},
        "style": str(b.get("style") or "")[:160],
        "light": str(b.get("light") or "")[:80],
        "construction": [str(r)[:120] for r in (b.get("construction") or [])][:5],
        "player": str(b.get("player") or "")[:120],
        "danger": str(b.get("danger") or "")[:120],
        "feel": str(b.get("feel") or "")[:120],
        "avoid": [str(a)[:100] for a in (b.get("avoid") or [])][:3],
    }


def check(b: dict) -> list[str]:
    problems = []
    pal = b.get("palette") or {}
    for k in ("background", "neutral", "accent"):
        v = str(pal.get(k) or "")
        if not (v.startswith("#") and len(v) in (4, 7)):
            problems.append(f"palette.{k} is not a hex colour")
    if not (b.get("construction") or []):
        problems.append("no construction rules, so sprites will drift apart")
    if not (b.get("light") or "").strip():
        problems.append("no light direction, so shading will be inconsistent")
    style = (b.get("style") or "").lower()
    for word in ("texture", "photo", "realistic", "3d render"):
        if word in style:
            problems.append(f"style asks for '{word}', which cannot be drawn as SVG and shader")
    return problems


def brief_from(bible: dict) -> str:
    """What the builder is told to draw to."""
    p = bible.get("palette") or {}
    lines = [
        f"Palette: background {p.get('background')}, neutral {p.get('neutral')}, "
        f"accent {p.get('accent')}" + (f", second accent {p['accent_2']}" if p.get("accent_2") else "")
        + ". The accent on no more than a tenth of the screen.",
        f"Style: {bible.get('style', '')}",
        f"Light, for every sprite: {bible.get('light', '')}",
        "Construction, for every sprite: " + "; ".join(bible.get("construction") or []),
        f"The player reads as: {bible.get('player', '')}",
        f"A threat reads as: {bible.get('danger', '')}",
        f"The feel: {bible.get('feel', '')}",
        "Avoid: " + "; ".join(bible.get("avoid") or []),
        "Every SVG you write follows all of this. A sprite that does not belong to the set is "
        "worse than no sprite.",
    ]
    return "\n".join(lines)
