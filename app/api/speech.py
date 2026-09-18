"""
Voice typing — one short recording becomes text in the message box.

This is the front door for anonymous drafts too, the same as the agent, so it is
rate-limited per address. It costs a fraction of a cent per use, but an open
transcription endpoint is somebody else's free API, so the limit matters.

The text is returned, not sent: the person reads it and presses send, exactly as
if they had typed it. Voice never commits anything on its own.
"""

import logging
import time
from collections import defaultdict, deque

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from ..services import speech

log = logging.getLogger("creai.speech")
router = APIRouter(prefix="/v1/speech", tags=["speech"])

IP_WINDOW, IP_CAP = 600, 40      # per address: 40 recordings per 10 minutes
_hits: dict[str, deque] = defaultdict(deque)


def _limit(ip: str) -> None:
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > IP_WINDOW:
        q.popleft()
    if len(q) >= IP_CAP:
        raise HTTPException(429, "That's a lot of recordings in a short time — "
                                 "give it a few minutes, or type instead.")
    q.append(now)


@router.post("")
async def transcribe(request: Request,
                     audio: UploadFile = File(...),
                     language: str | None = Form(None)):
    """Spoken audio in, text back. Nothing is stored."""
    if not speech.configured():
        raise HTTPException(503, "Voice typing isn't switched on yet.")
    _limit(request.client.host if request.client else "unknown")

    data = await audio.read(speech.MAX_BYTES + 1)
    if len(data) > speech.MAX_BYTES:
        raise HTTPException(413, "That recording is too long — try again in a sentence or two.")

    try:
        text = await speech.transcribe(data, audio.content_type, language)
    except speech.SpeechError as exc:
        # These messages are written to be read by the person, so pass them through.
        raise HTTPException(400, str(exc))
    return {"text": text}
