"""
The producer.

Writing an ad and directing one are different jobs, and the second was missing
entirely. Every shot so far has been at roughly eye level, which directors will
tell you is the one angle with no point of view — the camera is simply present,
saying nothing about who has power in the frame.

This produces a shot list: what the camera sees, from where, and why. It runs on
the slower, better model on purpose. A voice-to-lead parse lives or dies on
latency; a script is written once and watched thousands of times, so four seconds
of thinking is free.

The angle grammar, which the model is required to use rather than invited to:

  high    the camera above, looking down. The subject shrinks. This is where the
          problem lives — somebody diminished by the thing they have not done yet.
  eye     level with them. Neutral, conversational, the register of UGC. Used for
          the turn, where the ad stops being about the problem.
  low     the camera beneath, looking up. The subject gains authority. This is the
          payoff, and it is the same person the high angle diminished.
  over    over a shoulder, looking at what they are looking at. The product shot,
          because it puts the viewer in their position rather than opposite them.
  macro   very close on hands, a screen, a detail. Where the thing actually happens.

The move that carries an ad is high to low across the same person: the problem
shrinks them, the solution restores them. It is the oldest trick in commercial
direction and it still works because it is not really a trick — it is the story,
stated in where the camera stands.
"""

import json
import logging

import httpx

from ..core.config import settings

log = logging.getLogger("creai.producer")

MODEL = "gpt-6-astra"
API = "https://api.openai.com/v1/chat/completions"

ANGLES = {
    "high": "camera above the subject looking down — they shrink, the problem is bigger than them",
    "eye": "level with the subject — neutral, conversational, the register of real footage",
    "low": "camera below looking up — the subject gains authority and scale",
    "over": "over their shoulder at what they are looking at — puts the viewer in their position",
    "macro": "very close on hands or a screen — where the thing actually happens",
}

MOVES = {
    "still": "locked off, no movement",
    "push": "slow push in — closes distance as the point lands",
    "pull": "slow pull back — reveals the context around what was just shown",
    "handheld": "slight handheld drift — reads as real rather than produced",
    "tilt": "tilt up — used once, on the turn from problem to solution",
}

SYSTEM = """You are the producer of a short vertical advertisement. You write the script AND
direct it: every scene says what the camera sees, from where, and how it moves.

Return ONLY a JSON object: {"scenes": [...]}, each scene having
  line     what is said over it, or "" for a silent beat
  say      who says it: "narrator" or "customer"
  shows    what is on screen, described so somebody could film or generate it
  angle    one of: high, eye, low, over, macro
  move     one of: still, push, pull, handheld, tilt
  seconds  a rough guess; the real length follows the line
  why      one short sentence on why this angle, not another

Direction that is not optional:

- The angle carries the story. Start high on the problem so the person is
  diminished by it; finish low or eye on the payoff so the same person is
  restored. Never shoot the whole thing at eye level — that is the angle with no
  point of view.
- Product moments are 'over' or 'macro'. A screen shot from across the room is
  somebody else's software; over a shoulder it is theirs.
- One tilt at most, on the turn. More than one and it becomes a style rather than
  a moment.
- The first two seconds must move. A locked-off still frame does not stop a thumb.
- The brand name never appears in the opening line.
- The last scene says what to do and why now.
- One idea per scene. Write for sound off.
- Claim nothing the product does not do. No invented prices, awards or customers.
"""


class ProducerError(RuntimeError):
    pass


async def shot_list(concept: dict, *, seconds: int = 20, trade: str = "plumber") -> dict:
    """A script with its direction, or a plain reason it could not be made."""
    if not settings.openai_key:
        raise ProducerError("scripting isn't configured")

    ask = (f"Product: {concept.get('product', 'Creai')}\n"
           f"For: a {trade}\n"
           f"What it shows: {concept.get('shows', '')}\n"
           f"The line the customer says, unchanged: “{concept.get('said', '')}”\n"
           f"Target length: {seconds} seconds.\n"
           f"Angle: {concept.get('angle', 'the problem, sat in before anything is offered')}")

    async with httpx.AsyncClient(timeout=120) as x:
        r = await x.post(API,
                         headers={"Authorization": f"Bearer {settings.openai_key}",
                                  "Content-Type": "application/json"},
                         json={"model": MODEL,
                               # this model rejects max_tokens and ignores temperature
                               "max_completion_tokens": 2000,
                               "response_format": {"type": "json_object"},
                               "messages": [{"role": "system", "content": SYSTEM},
                                            {"role": "user", "content": ask}]})
    if r.status_code >= 400:
        log.error("producer failed: %s %s", r.status_code, r.text[:200])
        raise ProducerError("the script could not be written")
    try:
        out = json.loads(r.json()["choices"][0]["message"]["content"])
    except (KeyError, ValueError, IndexError) as exc:
        raise ProducerError("the script came back unreadable") from exc

    scenes = [s for s in (out.get("scenes") or []) if isinstance(s, dict)]
    if not scenes:
        raise ProducerError("no scenes came back")
    return {"scenes": [_clean(s) for s in scenes], "problems": check(scenes)}


def _clean(s: dict) -> dict:
    return {
        "line": str(s.get("line") or "")[:240],
        "say": "customer" if str(s.get("say", "")).lower().startswith("cust") else "narrator",
        "shows": str(s.get("shows") or "")[:400],
        "angle": s.get("angle") if s.get("angle") in ANGLES else "eye",
        "move": s.get("move") if s.get("move") in MOVES else "still",
        "seconds": float(s.get("seconds") or 3),
        "why": str(s.get("why") or "")[:160],
    }


def check(scenes: list[dict]) -> list[str]:
    """What is wrong with the direction, in words somebody can act on."""
    problems = []
    angles = [s.get("angle") for s in scenes]
    if len(set(angles)) <= 1:
        problems.append("every scene is the same angle — the camera is saying nothing")
    if angles and angles[0] == "low":
        problems.append("it opens low, which makes the problem heroic")
    if "high" in angles and "low" in angles:
        if angles.index("high") > angles.index("low"):
            problems.append("it goes low then high, which ends on the person diminished")
    elif "high" not in angles:
        problems.append("nothing is shot high, so the problem never weighs on anybody")
    if sum(1 for s in scenes if s.get("move") == "tilt") > 1:
        problems.append("more than one tilt — it becomes a style rather than a moment")
    if scenes and scenes[0].get("move") == "still":
        problems.append("the first scene is locked off; the opening has to move")
    if not any(s.get("angle") in ("over", "macro") for s in scenes):
        problems.append("no shot is over the shoulder or macro, so the product is never theirs")
    return problems


def prompt_for(scene: dict) -> str:
    """The scene as a generation prompt.

    Camera movement goes last on purpose: appended after the description it
    reduces drift, where embedding it mid-sentence tends to confuse the subject.
    """
    return (f"{scene['shows']}. {ANGLES[scene['angle']]}. "
            f"Vertical 9:16, photographic, no text, no on-screen writing. "
            f"{MOVES[scene['move']]}.")
