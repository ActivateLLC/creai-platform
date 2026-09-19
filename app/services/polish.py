"""
A bounded pass at making something look better, handed to a second model.

The research that justified adding a second provider was specific about what
Gemini is good at: fast, polished, single-shot visual output — "clean layouts,
reasonable component hierarchy, CSS that doesn't embarrass you" — not holding
together a multi-step build. Every independent comparison of the two models on
real coding tasks put Claude ahead by its widest margin on exactly the kind of
multi-step, tool-calling, stateful work this platform's build loop is made of.

So this file is the entire boundary. It is the only place in the codebase
allowed to call the "gemini-2.5-flash" model, and it is built so it cannot
become anything more than a single-shot advisor:

  - No `tools` are ever passed to it (the registry entry sets tools=False,
    and _gemini in models.py doesn't build Gemini's function-calling schema
    at all — it would need to be added deliberately, not fall out by default).
  - It never touches the filesystem, never calls check_app/check_game/
    build_game, and never decides whether a change ships.
  - Its output is always a *suggestion* structure that the calling orchestrator
    (Claude, inside agent.py's tool loop) reads and decides whether to apply.

If Gemini is ever wanted for something bigger than this, that is a deliberate
new decision — not a natural extension of what's here.
"""

import json
import logging

from . import models

log = logging.getLogger("creai.polish")

MODEL_KEY = "gemini-2.5-flash"

SYSTEM = """You are a UI/visual design assistant. You are shown either a description of a
screen or the actual markup/CSS for one, and you suggest concrete visual improvements:
layout, spacing, hierarchy, color, typography, and (for games) art-direction notes for
SVG assets.

You do not write final production code. You do not decide whether your suggestion is
used. Return a short, concrete list of specific changes — "increase spacing between the
score and the timer to 24px", not "improve the spacing" — so another model can apply or
reject each one individually.

Never suggest anything that requires a binary asset, a font file, or an external URL:
everything here ships as inline CSS/SVG/text. If you don't have enough information to
give a concrete suggestion, say so plainly instead of inventing detail."""


async def suggest(description: str, *, current_markup: str = "") -> dict:
    """One bounded polish suggestion. Never mutates anything; the caller decides.

    `description` — what the screen is for and who it's for.
    `current_markup` — optional current HTML/CSS/SVG, if there is something to
    react to rather than propose from scratch.
    """
    if not models.configured(MODEL_KEY):
        return {"ok": False, "reason": "gemini isn't configured", "suggestions": []}

    user_msg = description if not current_markup else (
        f"{description}\n\n---current markup---\n{current_markup[:6000]}")

    try:
        out = await models.complete(MODEL_KEY, [{"role": "user", "content": user_msg}],
                                    tools=[], system=SYSTEM, max_tokens=1200)
    except (models.ModelRejected, models.ModelUnavailable, models.ModelAccountProblem) as exc:
        # A polish pass failing is never worth surfacing as an error to the
        # person — it's an optional second opinion, not a required step.
        log.warning("polish suggestion unavailable: %s", exc)
        return {"ok": False, "reason": "unavailable", "suggestions": []}

    text = out.get("text", "").strip()
    return {"ok": True, "suggestions": _split(text), "raw": text,
            "usage": out.get("usage", {})}


def _split(text: str) -> list[str]:
    """Turn a free-text response into a list a caller can iterate and apply
    individually, rather than one undifferentiated blob."""
    lines = [l.strip(" -*\u2022\t") for l in text.splitlines()]
    return [l for l in lines if l]
