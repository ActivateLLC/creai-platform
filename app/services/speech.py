"""
Speech, both ways — so voice works the same everywhere.

The browser's own Web Speech API is a different engine on every platform: Google's
in Chrome, Apple's in Safari, absent in Firefox, and exposed-but-dead inside an iOS
WKWebView. One implementation here replaces all of that, and it is the same on a
laptop, a phone browser and the app.

Audio is recorded by the browser, posted once, transcribed and thrown away. Nothing
is stored: a voice prompt is a way of typing, not a file the customer owns.
"""

import logging
import re

import httpx

from ..core.config import settings

log = logging.getLogger("creai.speech")

API = "https://api.openai.com/v1/audio/transcriptions"
MODEL = "gpt-4o-mini-transcribe"

# Reading a line aloud, for videos. One voice per brand, chosen once and kept, so
# a channel sounds like itself from one video to the next.
SPEAK_API = "https://api.openai.com/v1/audio/speech"
SPEAK_MODEL = "gpt-4o-mini-tts"
VOICES = ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")
MAX_LINE = 900

# A spoken prompt is a sentence or two. The cap is generous for that and still far
# under the provider's own 25 MB limit, so a runaway recording fails here, cheaply,
# rather than after an upload.
MAX_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 120

# What browsers actually produce from MediaRecorder.
KINDS = {
    "audio/webm": "speech.webm",
    "audio/ogg": "speech.ogg",
    "audio/mp4": "speech.mp4",
    "audio/mpeg": "speech.mp3",
    "audio/wav": "speech.wav",
    "audio/x-m4a": "speech.m4a",
}

# The words this product hears constantly and that a general model mangles.
HINT = ("Creai, creai.dev, Godot, Webflow, Stripe, Postiz, Railway, Cloudflare, "
        "landing page, domain, deploy, SEO.")


class SpeechError(Exception):
    """Something the person should be told in plain words."""


def configured() -> bool:
    return bool(settings.openai_key)


def kind_of(content_type: str | None) -> str:
    """The filename to send, by media type. Extension is how the API sniffs format."""
    base = (content_type or "").split(";")[0].strip().lower()
    if base not in KINDS:
        raise SpeechError("that recording format isn't supported")
    return KINDS[base]


async def transcribe(audio: bytes, content_type: str | None, language: str | None = None) -> str:
    """Spoken audio in, text out. Raises SpeechError with something sayable."""
    if not configured():
        raise SpeechError("voice typing isn't switched on yet")
    if not audio:
        raise SpeechError("nothing was recorded")
    if len(audio) > MAX_BYTES:
        raise SpeechError("that recording is too long — try again in a sentence or two")

    name = kind_of(content_type)
    data = {"model": MODEL, "prompt": HINT}
    # Only pass a language when the browser is confident; a wrong hint is worse
    # than none, and the model detects language well on its own.
    if language and len(language) >= 2:
        data["language"] = language[:2].lower()

    try:
        async with httpx.AsyncClient(timeout=60) as x:
            r = await x.post(API, headers={"Authorization": f"Bearer {settings.openai_key}"},
                             files={"file": (name, audio, content_type or "audio/webm")}, data=data)
    except httpx.HTTPError as exc:
        log.error("transcription unreachable: %s", exc)
        raise SpeechError("voice typing is unavailable right now — type it instead")

    if r.status_code == 429:
        raise SpeechError("voice typing is busy — try again in a moment, or type it")
    if r.status_code >= 400:
        detail = ""
        try:
            detail = (r.json().get("error") or {}).get("message", "")[:200]
        except Exception:
            detail = r.text[:200]
        log.error("transcription failed: %s %s", r.status_code, detail)
        raise SpeechError("that didn't come through — try again, or type it instead")

    text = (r.json().get("text") or "").strip()
    if not text:
        raise SpeechError("nothing was heard — try again, or type it instead")
    return text


# ---------------------------------------------------------------- text to speech

def _say_the_name(text: str) -> str:
    """Creai is said "Kree-aye", and a speech model will not guess that from the
    spelling. Spelling it phonetically in the input is the only lever that works,
    and it never reaches anyone's eyes — only their ears."""
    return re.sub(r"\bCreai\b", "Kree-aye", text or "")


class SpeechError(RuntimeError):
    """Something the person should be told plainly."""


async def speak(line: str, voice: str = "ash", *, instructions: str = "") -> bytes:
    """One spoken line, as mp3. Lines are spoken one at a time on purpose: it is
    how a scene can be timed to its own words, and how one line can be changed
    later without paying to say the rest again."""
    if not settings.openai_key:
        raise SpeechError("speech isn't configured")
    text = _say_the_name((line or "").strip())
    if not text:
        raise SpeechError("there is nothing to say")
    if len(text) > MAX_LINE:
        raise SpeechError("that line is too long to say in one breath — split the scene")
    if voice not in VOICES:
        voice = "ash"
    async with httpx.AsyncClient(timeout=90) as x:
        r = await x.post(SPEAK_API,
                         headers={"Authorization": f"Bearer {settings.openai_key}",
                                  "Content-Type": "application/json"},
                         json={"model": SPEAK_MODEL, "voice": voice, "input": text,
                               "instructions": instructions or DELIVERY,
                               "response_format": "mp3"})
    if r.status_code >= 400:
        log.error("speech failed: %s %s", r.status_code, r.text[:200])
        raise SpeechError("the voice could not be generated")
    return r.content


# How a line should land. Read aloud, an advertisement voice is the fastest way to
# be scrolled past; a person explaining something useful is not.
DELIVERY = ("Warm, grounded, unhurried \u2014 a capable person explaining something useful to a "
            "friend, not an advertisement. Slight downward inflection at the end of each "
            "sentence. Where the word 'Kree-aye' appears, say it with the stress pattern of "
            "'today': light first syllable, strong second syllable.")
