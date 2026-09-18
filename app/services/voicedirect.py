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
    """One line's direction: what it is doing emotionally, and how it should land.

    `hold` is the silence left after the line, in seconds. Silence does more for
    drama than volume does: a beat after "Six forty-seven" lands harder than the
    same words said louder, and a model asked to pause inside a sentence will
    usually ignore it. So the pause is cut in, not requested.
    """
    name: str
    feeling: str
    delivery: str
    tags: tuple[str, ...] = ()
    hold: float = 0.0


# The arc of a problem-angle ad. Named beats rather than positions, so a cut that
# drops a scene keeps the shape instead of shifting every direction one along.
# The arc has to be wider than feels comfortable when read one line at a time.
# Each line is generated separately, so a model never hears the contrast — the
# range has to be written in, or every beat drifts back toward the same middle
# and the whole thing lands as one narration bed.
ARCS: dict[str, tuple[Beat, ...]] = {
    "problem": (
        Beat("open", "exhausted — the end of a long physical day, said half to himself",
             "very low and close, almost under the breath, markedly slower than "
             "feels natural, no projection whatsoever",
             ("tired", "quiet"), hold=0.55),
        Beat("agitate", "sharp, genuinely annoyed — not weary any more",
             "clipped and harder, a shade faster and louder, the sentence of "
             "somebody who has had enough of it",
             ("frustrated",), hold=0.35),
        Beat("turn", "caught off guard — wait, what just happened",
             "audibly brighter and quicker, lifts through the line, a touch of "
             "disbelief", ("curious", "faster"), hold=0.3),
        Beat("demo", "impressed, gathering pace",
             "noticeably quicker and cleaner than anything before it, energised, "
             "no hesitation", ("impressed",), hold=0.25),
        Beat("payoff", "quiet delight — a small vocal smile, almost laughing at how "
                       "easy that was",
             "warm and unhurried again, smiling audibly, land the last word",
             ("satisfied",), hold=0.45),
        Beat("cta", "calm certainty — it does not need selling",
             "drop the energy slightly, quieter than the line before, short and "
             "inevitable, downward inflection, never an announcer lift", (), hold=0.0),
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

# Who speaks. A single voice doing every emotion is why an ad sounds like one
# person reading; the tonal change should be structural, not manufactured. When
# the camera cuts to the customer's phone, the voice cuts too.
#
# The narrator and the customer are different people, so they get different
# voices — and the customer's line is the one the ad is demonstrating, which is
# precisely the line that must not sound performed.
CAST = {
    "narrator": {"openai": "echo",
                 "why": "the person showing you this — warm, grounded, not selling"},
    "customer": {"openai": "verse",
                 "why": "the tradesperson in the van — a different person entirely"},
}


# Never cast again. Kept by name so nobody reaches for it out of habit, and so
# the reason outlives the person who made the call.
RETIRED = {"ash": "used in an early cut and rejected — do not cast"}

# Anyone else who speaks gets the next unused voice rather than doubling up. Two
# characters sharing a voice is exactly what a second speaker exists to avoid.
SPARE = ("ballad", "sage", "coral", "alloy", "shimmer")


def voice_for(line: dict) -> str:
    """Which cast member says this line."""
    return "customer" if line.get("in_character") else "narrator"


def cast_voice(role: str) -> str:
    """The voice for any role, casting one when the role is new.

    A third or fourth speaker gets a spare rather than reusing the narrator's:
    the point of more than one voice is that they sound like more than one person.
    """
    role = (role or "narrator").strip().lower()
    known = CAST.get(role)
    if known:
        return known["openai"]
    taken = {c["openai"] for c in CAST.values()} | set(RETIRED)
    for v in SPARE:
        if v not in taken:
            CAST[role] = {"openai": v, "why": f"cast for {role}"}
            return v
    raise DirectionError("every voice is cast or retired — reuse one deliberately")


# Lines said by the customer rather than the narrator get their own direction:
# somebody talking into a phone is not performing, and a performed one is the
# fastest way to lose the thing the ad is demonstrating.
IN_CHARACTER = Beat(
    "said", "not performing — a person recording a note for themselves",
    "clipped, slightly tired, trailing off at the end, no emphasis on any word",
    ("tired",), hold=0.3)

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


# A pause inside a line is the one a model will not honour. Asked to leave half a
# second after "Six forty-seven", it reads straight through — so the line is split
# and the silence is cut in between the two halves. Each half is generated on its
# own, which also lets the second half be directed differently from the first.
SPLIT_AFTER = 0.45


def split_for_pause(text: str) -> list[str]:
    """A line broken where a dramatic pause belongs.

    Only splits a short first sentence away from what follows: "Six forty-seven.
    You are not done." becomes two. A long sentence is left alone, because a pause
    mid-thought reads as a fault rather than a choice.
    """
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if p.strip()]
    if len(parts) == 2 and len(parts[0].split()) <= 4:
        return parts
    return [text.strip()] if text and text.strip() else []


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
        who = voice_for(l)
        chunks = split_for_pause(say)
        out.append({**l, "say": say, "name_note": note, "who": who,
                    "chunks": chunks,
                    "gap": SPLIT_AFTER if len(chunks) > 1 else 0.0,
                    "voice": cast_voice(who), "hold": beat.hold,
                    "feeling": beat.feeling, "delivery": beat.delivery,
                    "tags": list(beat.tags), "says_name": bool(note),
                    "openai": for_openai(beat, bool(note)) + (" " + note if note else ""),
                    "gemini": for_gemini(beat, bool(note)) + (note or ""),
                    "elevenlabs": {**for_elevenlabs(beat, bool(note)), "note": note}})
    return out
