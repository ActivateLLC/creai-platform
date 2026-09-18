"""
Speech to text — so voice works the same everywhere.

The browser's own Web Speech API is a different engine on every platform: Google's
in Chrome, Apple's in Safari, absent in Firefox, and exposed-but-dead inside an iOS
WKWebView. One implementation here replaces all of that, and it is the same on a
laptop, a phone browser and the app.

Audio is recorded by the browser, posted once, transcribed and thrown away. Nothing
is stored: a voice prompt is a way of typing, not a file the customer owns.
"""

import logging

import httpx

from ..core.config import settings

log = logging.getLogger("creai.speech")

API = "https://api.openai.com/v1/audio/transcriptions"
MODEL = "gpt-4o-mini-transcribe"

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
