"""
Directing a read.

The fault this fixes is not the model. It is that every line in an ad was handed
the same instruction — "warm, grounded, unhurried" — which is a voice setting, not
a performance. A read with no arc sounds like a script being read, because that is
exactly what it is.

An ad has emotional movement: tired and observational at the open, frustration as
the problem is named, curiosity at the turn, quickening through the demonstration,
relief at the payoff, conviction at the ask. Give each line its own direction and
the same model produces a different thing.

Written model-agnostically on purpose. The current generation of speech models all
take director-style prompting — OpenAI through an `instructions` field, Gemini
through natural-language stage direction, ElevenLabs through inline tags and a
stability setting that trades consistency for range. The arc belongs here; the
dialect belongs in an adapter, so changing supplier is a new adapter rather than a
rewrite of every script.
"""

import logging
import re
from dataclasses import dataclass

log = logging.getLogger("creai.voicedirect")


@dataclass(frozen=True)
class Beat:
    """One line's direction: what it is doing emotionally, and how it should land."""
    name: str
    feeling: str
    delivery: str
    tags: tuple[str, ...] = ()


# The arc of a problem-angle ad. Named beats rather than positions, so a cut that
# drops a scene keeps the shape instead of shifting every direction one along.
ARCS: dict[str, tuple[Beat, ...]] = {
    "problem": (
        Beat("open", "weary, observational — almost talking to himself",
             "low and close, slower than feels natural, no projection",
             ("tired",)),
        Beat("agitate", "recognition turning to mild frustration",
             "conversational, clipped, the sentence of someone who has said it before",
             ("frustrated",)),
        Beat("turn", "curiosity — something might be different",
             "lifts slightly, a fraction quicker", ("curious",)),
        Beat("demo", "growing confidence as it works",
             "quicker, cleaner, no hesitation", ()),
        Beat("payoff", "relief, faintly amused that it was that easy",
             "warmer, a smile in the voice, unhurried again", ("pleased",)),
        Beat("cta", "conviction, not salesmanship",
             "short, decisive, downward inflection, no announcer lift", ()),
    ),
    "demo": (
        Beat("open", "matter-of-fact, mid-thought", "plain, no setup", ()),
        Beat("demo", "quiet confidence", "even, unhurried, let the picture work", ()),
        Beat("payoff", "understatement", "flat and certain — the opposite of a claim", ()),
        Beat("cta", "conviction", "short, decisive", ()),
    ),
    "outcome": (
        Beat("open", "calm, after the fact", "settled, low", ()),
        Beat("how", "explaining, not selling", "conversational", ()),
        Beat("payoff", "quiet satisfaction", "warmer", ("pleased",)),
        Beat("cta", "conviction", "short, decisive", ()),
    ),
}

# Lines said by the customer rather than the narrator get their own direction:
# somebody talking into a phone is not performing, and a performed one is the
# fastest way to lose the thing the ad is demonstrating.
IN_CHARACTER = Beat(
    "said", "not performing — a person recording a note for themselves",
    "clipped, slightly tired, trailing off at the end, no emphasis on any word",
    ("tired",))

# Brand names a speech model cannot get from the spelling. The phonetic version
# goes into the text the model reads; nobody ever sees it, they only hear it.
# Kept as data so a second product is an entry rather than an edit.
SAY_AS = {
    "creai": ("Kree-aye", "the stress pattern of 'today': light first syllable, strong second"),
    "arbi":  ("AR-bee", "stress on the first syllable, like 'army'"),
}


def phonetic(text: str) -> tuple[str, str]:
    """(text the model should read, note about how to stress it).

    Substitution happens in the input, not in the caption: the viewer reads the
    brand spelled properly and hears it said properly.
    """
    said, notes = text or "", []
    for name, (spoken, how) in SAY_AS.items():
        pattern = re.compile(rf"\b{name}\b", re.I)
        if pattern.search(said):
            said = pattern.sub(spoken, said)
            notes.append(f"Say '{spoken}' with {how}.")
    return said, " ".join(notes)


NAME_NOTE = ("Where a brand name appears spelled phonetically, say it exactly as "
             "written rather than as it would be spelled.")


class DirectionError(ValueError):
    pass


def arc(name: str) -> tuple[Beat, ...]:
    a = ARCS.get((name or "").lower())
    if not a:
        raise DirectionError(f"no arc called {name!r}")
    return a


def direct(angle: str, beat_name: str, *, in_character: bool = False,
           says_name: bool = False) -> Beat:
    """The direction for one line."""
    if in_character:
        beat = IN_CHARACTER
    else:
        found = [b for b in arc(angle) if b.name == beat_name]
        if not found:
            raise DirectionError(f"{angle!r} has no beat {beat_name!r}")
        beat = found[0]
    return beat


# ---------------------------------------------------------------- adapters

def for_openai(beat: Beat, says_name: bool = False) -> str:
    """OpenAI takes a plain-language instruction, so the direction goes in whole."""
    out = (f"{beat.feeling}. Delivery: {beat.delivery}. "
           "Never sound like an advertisement.")
    if says_name:
        out += " " + NAME_NOTE
    return out


def for_gemini(beat: Beat, says_name: bool = False) -> str:
    """Gemini takes stage direction and reads inline cues, so the tags go inline."""
    cues = "".join(f"[{t}] " for t in beat.tags)
    out = (f"Read as a voice actor would: {beat.feeling}. {beat.delivery}. "
           f"Not an advertisement read.\n{cues}")
    if says_name:
        out += NAME_NOTE + " "
    return out


def for_elevenlabs(beat: Beat, says_name: bool = False) -> dict:
    """ElevenLabs takes audio tags in the text and a stability number.

    Lower stability gives more range and less consistency. Beats carrying a
    feeling want range; the call to action wants to land the same way every time.
    """
    stability = 0.65 if beat.name == "cta" else 0.35
    return {"prefix": "".join(f"[{t}] " for t in beat.tags),
            "stability": stability,
            "note": NAME_NOTE if says_name else ""}


def plan_read(angle: str, lines: list[dict]) -> list[dict]:
    """Attach direction to every line of a script, for any product.

    lines: [{beat, text, in_character?}]
    Returns each line with `say` — what the model should read, which may differ
    from `text` where a brand name needs spelling for the ear.
    """
    out = []
    for l in lines:
        say, note = phonetic(l.get("text") or "")
        beat = direct(angle, l.get("beat", ""), in_character=bool(l.get("in_character")))
        out.append({**l, "say": say, "name_note": note,
                    "feeling": beat.feeling, "delivery": beat.delivery,
                    "tags": list(beat.tags), "says_name": bool(note),
                    "openai": for_openai(beat, bool(note)) + (" " + note if note else ""),
                    "gemini": for_gemini(beat, bool(note)) + (note or ""),
                    "elevenlabs": {**for_elevenlabs(beat, bool(note)), "note": note}})
    return out
