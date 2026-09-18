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

import httpx
from dataclasses import dataclass

from ..core.config import settings

log = logging.getLogger("creai.sound")


@dataclass(frozen=True)
class Source:
    name: str
    licence: str
    ads_ok: bool           # may the output run in a paid advertisement
    self_hosted: bool
    note: str


# Hosted now rather than self-hosted: fal runs ACE-Step and Stable Audio, which
# removes the GPU that was blocking this and keeps the licences that made them
# the right choice. Self-hosting stays the cheaper answer at volume; it is no
# longer the only answer.
ENDPOINTS = {
    "ace-step": "fal-ai/ace-step",                          # beds, Apache-2.0
    "stable-sfx": "fal-ai/stable-audio-25/text-to-audio",   # effects, community licence
}

SOURCES = {
    "ace-step": Source("ACE-Step", "Apache-2.0", True, True,
                       "Instrumental beds. Clear to ship, and cheap to run ourselves later."),
    "stable-sfx": Source("Stable Audio", "Stability Community", True, True,
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


# ---------------------------------------------------------------- making it

async def make(source: str, prompt: str, seconds: float = 8, *, for_ads: bool = True) -> bytes:
    """Generate a bed or an effect, refusing by licence before spending anything.

    The licence check happens first on purpose: discovering afterwards that a
    track cannot run in an advertisement means the money is already gone and the
    cut is already built around it.
    """
    picked = pick(source, for_ads=for_ads)          # raises on a licence problem
    if not settings.fal_key:
        raise SoundError("sound generation isn't configured")
    endpoint = ENDPOINTS.get(source)
    if not endpoint:
        raise SoundError(f"{picked.name} has no endpoint wired up")

    body = {"prompt": prompt}
    if source == "ace-step":
        body["duration"] = max(4, min(int(seconds), 120))
    else:
        body["seconds_total"] = max(1, min(int(seconds), 47))

    async with httpx.AsyncClient(timeout=300) as x:
        r = await x.post(f"https://fal.run/{endpoint}",
                         headers={"Authorization": f"Key {settings.fal_key}",
                                  "Content-Type": "application/json"},
                         json=body)
    if r.status_code >= 400:
        log.error("sound failed: %s %s", r.status_code, r.text[:200])
        raise SoundError("that sound could not be made")
    out = r.json()
    url = ((out.get("audio") or out.get("audio_file") or {}) or {}).get("url")
    if not url:
        raise SoundError("no audio came back")
    async with httpx.AsyncClient(timeout=300) as x:
        return (await x.get(url)).content


async def bed_for(kit: dict, mood: str = "", seconds: float = 30) -> bytes:
    """The bed for an ad, described from the brand rather than from a genre."""
    return await make("ace-step", bed_prompt(kit, mood), seconds)


async def effect(what: str, seconds: float = 2) -> bytes:
    """One effect. Short on purpose: an effect that outlasts its moment is music."""
    return await make("stable-sfx", what, seconds)
