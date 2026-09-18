"""
Beds and sounds — and which of them a customer may legally ship.

The trap this module exists to close: the most obvious open music model,
MusicGen, is CC-BY-NC. Its output cannot go in a paid advertisement, and nothing
about using it warns you. A customer who runs an ad with it is exposed, and they
would be exposed because of a default we chose for them. So the licence is a
property of the source here, checked before a track is attached, rather than a
line in documentation nobody reads.

What is cleared, and why these:
  · ACE-Step — Apache 2.0. Beds, instrumental, light enough to self-host.
  · Stable Audio Small SFX — commercial-safe community licence. Whooshes, stings.
  · ElevenLabs Music — licensed training data, but advertising rights only unlock
    on their higher tiers, so it is offered as a choice with that said plainly.
  · Uploads — the customer's own music, their problem and their right.

Effects are composited rather than prompted. A generated clip will not reliably
produce a whoosh on the cut or a sting under the outro; a sound file and an
ffmpeg filter will, every time, for nothing.
"""

import logging
from dataclasses import dataclass

log = logging.getLogger("creai.sound")


@dataclass(frozen=True)
class Source:
    name: str
    licence: str
    ads_ok: bool           # may the output run in a paid advertisement
    self_hosted: bool
    note: str


SOURCES = {
    "ace-step": Source("ACE-Step", "Apache-2.0", True, True,
                       "Instrumental beds. Ours to run, clear to ship."),
    "stable-sfx": Source("Stable Audio Small SFX", "Stability Community", True, True,
                         "Short effects: whooshes, clicks, stings."),
    "elevenlabs": Source("ElevenLabs Music", "Licensed (tiered)", True, False,
                         "Cleared training data, but advertising rights need their "
                         "higher tier — check the plan before running ads."),
    "upload": Source("Your own track", "Yours", True, False,
                     "Whatever you already have the right to use."),
    # Present so it can be refused by name rather than silently missing.
    "musicgen": Source("MusicGen", "CC-BY-NC-4.0", False, True,
                       "Non-commercial only. Fine for a draft, never for an ad."),
}

# A bed should sit under a voice, not beside it.
DUCK_DB = -16          # how far the music drops while someone is speaking
BED_DB = -22           # the bed's resting level under a voice track
SFX_DB = -12


class SoundError(ValueError):
    """Something the person should be told plainly."""


def pick(source: str, *, for_ads: bool) -> Source:
    """The source, if it may be used this way. Refuses by licence, with the reason."""
    s = SOURCES.get((source or "").lower())
    if not s:
        raise SoundError(f"unknown music source: {source}")
    if for_ads and not s.ads_ok:
        raise SoundError(
            f"{s.name} is {s.licence} — non-commercial, so it can't go in an ad. "
            "ACE-Step is clear for this and sounds close.")
    return s


def cleared(for_ads: bool = True) -> list[dict]:
    """What a person may choose from, with the terms said out loud."""
    return [{"id": k, "name": s.name, "licence": s.licence, "self_hosted": s.self_hosted,
             "note": s.note}
            for k, s in SOURCES.items() if s.ads_ok or not for_ads]


def bed_prompt(kit: dict, mood: str = "") -> str:
    """A bed that suits the business, described for a music model. Kept plain:
    a bed that draws attention to itself is working against the voice."""
    voice = (kit or {}).get("voice") or "plain and direct"
    return (f"Instrumental background bed, {mood or 'calm and steady'}, "
            f"matching a brand that sounds {voice}. Sparse, warm, no vocals, "
            "no dramatic swells, sits under a speaking voice. Loopable.")


def mix(voice_track: str, bed: str | None, cuts: list[float], out: str) -> list[str]:
    """The ffmpeg arguments to lay a bed under a voice and put a sound on each cut.

    Returned rather than run, so the renderer owns the process and this stays
    testable without ffmpeg present.
    """
    if not bed:
        return ["-i", voice_track, "-c:a", "aac", "-b:a", "160k", out]
    # sidechaincompress ducks the bed whenever the voice is present, which is what
    # makes a bed feel deliberate instead of loud.
    chain = (f"[1:a]volume={BED_DB}dB[bed];"
             f"[bed][0:a]sidechaincompress=threshold=0.03:ratio=8:attack=8:release=320[ducked];"
             f"[0:a][ducked]amix=inputs=2:duration=first:dropout_transition=0[mixed]")
    return ["-i", voice_track, "-stream_loop", "-1", "-i", bed,
            "-filter_complex", chain, "-map", "[mixed]",
            "-c:a", "aac", "-b:a", "160k", "-shortest", out]
