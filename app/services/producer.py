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

# Everything else a moment is made of. Each of these maps to something the
# pipeline can actually do — a generation prompt, a sound file, a voice
# direction, a caption setting. An attribute nothing can execute is a note, not
# a specification, and notes are how production documents rot.
LENSES = {
    "wide": "wide lens, some distortion, the room around them — for establishing",
    "normal": "normal lens, how the eye sees it — for anything conversational",
    "long": "long lens, compressed, background falling away — isolates the subject",
    "macro": "macro, very close, shallow — hands, a screen, a detail",
}

LIGHT = {
    "dusk": "low warm sun, long shadows, end of the day",
    "lamp": "one practical lamp, warm pool, dark around it — night, indoors",
    "grey": "flat overcast daylight, no drama — the ordinary version of the day",
    "screen": "lit by the phone itself, cool on the face, dark behind",
    "clean": "even bright light, no mood — for product screens",
}

# The sounds that exist, generated rather than licensed, so a spec can name one
# and the render can find it.
SOUNDS = {
    "pop": "a name landing in a field",
    "tick": "money landing — heavier than the others",
    "snap": "a state changing",
    "chime": "a date landing in a calendar",
    "none": "silence, deliberately",
}

# Music is the one attribute not yet wired: ACE-Step needs a GPU and has none
# yet. Specifying it is still worth doing, because the spec outlives the gap.
MUSIC = {
    "none": "no bed — the voice and the room carry it",
    "under": "a sparse bed, well under the voice, ducked when anyone speaks",
    "lift": "the bed opens slightly as the product appears",
    "out": "the bed drops away entirely, leaving the last line dry",
}

SYSTEM = """You are the producer of a short vertical advertisement. You write the script AND
direct it: every scene says what the camera sees, from where, and how it moves.

Return ONLY a JSON object: {"scenes": [...]}, each scene having
  line     what is said over it, or "" for a silent beat
  say      who says it: "narrator" or "customer"
  shows    what is on screen, described so somebody could film or generate it
  angle    one of: high, eye, low, over, macro
  move     one of: still, push, pull, handheld, tilt
  lens     one of: wide, normal, long, macro
  light    one of: dusk, lamp, grey, screen, clean
  tone     the emotional direction for the voice on this line, in a few words
  sound    one of: pop, tick, snap, chime, none — a single effect, on the beat it marks
  music    one of: none, under, lift, out
  caption  what is SEEN on screen. Two to five words, and NOT a summary of the line —
           the viewer is already hearing that. Prefer the hard thing: a time, an
           amount, a day, a name. A voice says "eighteen four"; the screen says
           "$18,400". A voice says "six forty-seven"; the screen says "6:47".
           If the scene has no such thing in it, use "" — a caption that only
           narrates is worse than none.
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

On the other attributes:
- Light carries time of day and mood. The problem is dusk or lamp; the product is clean.
  Do not light a working screen dramatically — it reads as an advertisement for itself.
- Lens is distance felt rather than measured. Long for isolation, macro for the moment a
  thing happens, wide only to establish.
- Sound marks a beat; it does not decorate one. At most one effect per scene, and silence
  is a legitimate choice — four identical clicks are worse than nothing.
- Music is under everything or it is not there. It lifts once, when the product arrives,
  and drops out for the last line so the ask lands dry.
- Captions are not subtitles. On at most half the scenes, and never on the scene where
  the customer speaks — the picture is already carrying that moment.
- Tone is the emotional direction, and it must MOVE across the ad. Exhausted, then sharp,
  then curious, then quick, then relieved, then certain. A single tone across six scenes
  is a voice setting, not a performance.
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
        "lens": s.get("lens") if s.get("lens") in LENSES else "normal",
        "light": s.get("light") if s.get("light") in LIGHT else "grey",
        "tone": str(s.get("tone") or "")[:120],
        "sound": s.get("sound") if s.get("sound") in SOUNDS else "none",
        "music": s.get("music") if s.get("music") in MUSIC else "under",
        "caption": str(s.get("caption") or "")[:80],
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

    tones = [(s.get("tone") or "").lower() for s in scenes if s.get("tone")]
    if len(tones) > 2 and len(set(tones)) <= 1:
        problems.append("every scene has the same tone — that is a voice setting, not a read")
    if sum(1 for s in scenes if s.get("sound") not in (None, "none")) > len(scenes) * 0.6:
        problems.append("almost every scene has an effect; sound marks a beat, it does not "
                        "decorate one")
    # Only judge an attribute that was actually specified. Complaining about a
    # missing one turns the checker into noise, and a checker people learn to
    # skim is worse than none.
    music = [s.get("music") for s in scenes if s.get("music")]
    if music.count("lift") > 1:
        problems.append("the bed lifts more than once, so no lift means anything")
    if music and music[-1] not in ("out", "none"):
        problems.append("the bed is still running under the last line; let the ask land dry")
    captioned = sum(1 for s in scenes if (s.get("caption") or "").strip())
    if captioned > max(1, len(scenes) // 2):
        problems.append("most scenes carry a caption — the viewer is already hearing the line")
    return problems


def prompt_for(scene: dict) -> str:
    """The scene as a generation prompt.

    Camera movement goes last on purpose: appended after the description it
    reduces drift, where embedding it mid-sentence tends to confuse the subject.
    """
    return (f"{scene['shows']}. {ANGLES[scene['angle']]}. "
            f"{LENSES.get(scene.get('lens', 'normal'), '')}. "
            f"{LIGHT.get(scene.get('light', 'grey'), '')}. "
            f"Vertical 9:16, photographic, no text, no on-screen writing. "
            f"{MOVES[scene['move']]}.")
