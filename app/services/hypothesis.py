"""
The hypothesis, formed before anything is written.

The difference between a generator and an agent is what happens before the first
word. A generator is handed an audience, a pain and a promise and writes to them.
An agent works out what they are, states them as a position it could be wrong
about, and then writes several arguments for that position rather than one.

So this produces a creative hypothesis: who buys, what hurts, the insight that is
not obvious, what can actually be proved, the emotional arc, the promise, and the
one moment that carries it. Then the angles are arguments for that hypothesis,
and the test is between them rather than between phrasings of the same idea.

Two disciplines it keeps.

Proof is what exists, not what would help. The field is filled from what the
business actually has — a working demonstration, a real customer sentence, a real
number. If there is nothing, it says so, and the angles that need proof drop out
of the set rather than being written around.

The insight has to be arguable. "People want to save time" is not an insight, it
is a category. "They hate the CRM more than they hate the job" is one, because
somebody could disagree with it — and because it tells you what the ad opens on.
"""

import json
import logging

import httpx

from ..core.config import settings

log = logging.getLogger("creai.hypothesis")

MODEL = "gpt-6-astra"
API = "https://api.openai.com/v1/chat/completions"

SYSTEM = """You are working out what an advertisement should argue, before anyone writes one.

Return ONLY JSON:
{
  "audience":  who actually buys this, specifically — "owner-operator contractors", not
               "small businesses",
  "pain":      the thing they already resent, in their words,
  "insight":   the non-obvious thing that is true about them. It must be arguable: somebody
               should be able to disagree with it. "They want to save time" is a category,
               not an insight. "They hate the paperwork more than they hate the job" is one,
  "emotion":   the arc, as four to six words in order, e.g. "fatigue, irritation, curiosity,
               surprise, relief",
  "promise":   the core promise in one plain sentence, in their language,
  "proof":     the single moment that makes it believable — what is on screen when they
               stop doubting,
  "objection": the first thing they will think that stops them acting,
  "cta":       what to do, in four words or fewer,
  "confidence": "high" | "medium" | "low" — how sure you are, given what you were told
}

Rules:
- Use only what you are told. If you were not given a real customer quote or a real number,
  do not invent one; say what proof is missing in `proof` instead.
- Write in the audience's language, not marketing language. No "solutions", "streamline",
  "empower", "seamless".
- The objection must be a real one. "It's too expensive" when the product is free is not.
- Say `low` confidence when you are guessing. A stated guess is useful; a guess dressed as
  a finding is not."""


class HypothesisError(RuntimeError):
    pass


async def form(product: str, *, known: dict | None = None) -> dict:
    """The creative position, before any script exists."""
    if not settings.openai_key:
        raise HypothesisError("the creative brain isn't configured")

    known = known or {}
    told = [f"The product: {product}"]
    if known.get("audience"):
        told.append(f"Who it is for: {known['audience']}")
    if known.get("shows"):
        told.append(f"What can be shown on screen: {known['shows']}")
    if known.get("said"):
        told.append(f"A real thing a customer says to it: “{known['said']}”")
    if (known.get("proof") or {}).get("quote"):
        p = known["proof"]
        told.append(f"A real customer said: “{p['quote']}” — {p.get('who', 'unattributed')}")
    else:
        told.append("There is NO customer quote and NO usage numbers available.")

    async with httpx.AsyncClient(timeout=120) as x:
        r = await x.post(API,
                         headers={"Authorization": f"Bearer {settings.openai_key}",
                                  "Content-Type": "application/json"},
                         json={"model": MODEL, "max_completion_tokens": 1200,
                               "response_format": {"type": "json_object"},
                               "messages": [{"role": "system", "content": SYSTEM},
                                            {"role": "user", "content": "\n".join(told)}]})
    if r.status_code >= 400:
        log.error("hypothesis failed: %s %s", r.status_code, r.text[:200])
        raise HypothesisError("the hypothesis could not be formed")
    try:
        out = json.loads(r.json()["choices"][0]["message"]["content"])
    except (KeyError, ValueError, IndexError) as exc:
        raise HypothesisError("the hypothesis came back unreadable") from exc

    return {**_clean(out), "problems": check(out)}


def _clean(h: dict) -> dict:
    keep = ("audience", "pain", "insight", "emotion", "promise", "proof", "objection", "cta")
    out = {k: str(h.get(k) or "")[:300] for k in keep}
    out["confidence"] = h.get("confidence") if h.get("confidence") in ("high", "medium", "low") \
        else "low"
    return out


# Words that mean nothing to the person being sold to, and signal that the
# hypothesis was written in marketing language rather than theirs.
HOLLOW = ("solution", "streamline", "empower", "seamless", "leverage", "robust",
          "cutting-edge", "game-chang", "revolutionise", "revolutioniz", "synerg")


def check(h: dict) -> list[str]:
    """What is weak about a hypothesis, before anything is built on it."""
    problems = []
    insight = (h.get("insight") or "").lower()
    if not insight:
        problems.append("there is no insight, so every angle will argue the same thing")
    elif len(insight.split()) < 5:
        problems.append("the insight is too short to be arguable")
    for word in HOLLOW:
        for field in ("promise", "insight", "pain"):
            if word in (h.get(field) or "").lower():
                problems.append(f"'{word}' is marketing language, not the audience's")
                break
    emotion = [e for e in (h.get("emotion") or "").replace(",", " ").split() if e]
    if len(set(emotion)) < 3:
        problems.append("the emotional arc does not move enough to direct a read")
    if not (h.get("objection") or "").strip():
        problems.append("no objection named, so nothing in the ad answers one")
    if (h.get("cta") or "").count(" ") > 4:
        problems.append("the call to action is too long to act on")
    return problems


def brief_from(h: dict, angle: str) -> str:
    """One angle's brief, argued from the hypothesis rather than from nothing."""
    from .admutate import ANGLES
    return "\n".join([
        f"Audience: {h.get('audience', '')}",
        f"What hurts: {h.get('pain', '')}",
        f"The insight this argues: {h.get('insight', '')}",
        f"Emotional arc: {h.get('emotion', '')}",
        f"The promise: {h.get('promise', '')}",
        f"What makes it believable: {h.get('proof', '')}",
        f"What they will think that stops them: {h.get('objection', '')}",
        f"Do this: {h.get('cta', '')}",
        "",
        f"Argue it this way: {ANGLES.get(angle, '')}",
        "",
        "The ad must answer the objection somewhere, without naming it.",
    ])
