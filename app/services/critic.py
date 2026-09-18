"""
The critic.

Apps get reviewed before they ship here; videos did not. Every fault in tonight's
cuts — a caption in a box, white text invisible on a pale screen, a read with no
arc, an opening that did not move — was found by a person watching it. That is a
critic doing a job nothing in the pipeline was doing.

So this watches the finished file. Frames are pulled at intervals and looked at,
which is the only honest way to review a video: the plan can be perfect and the
render still wrong, and the difference is invisible to anything that only reads
the plan.

Two things keep it useful rather than decorative.

It reports what it can see, and says when it cannot. A critic that invents faults
gets ignored, and one that invents praise is worse. Audio is not looked at here,
so it says so instead of guessing.

It fails open. A render that could not be reviewed still ships — the critic is a
second opinion, not a gate. Nothing about a marketing pipeline should be able to
stop a video existing because a reviewer was unavailable.
"""

import asyncio
import base64
import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

from ..core.config import settings

log = logging.getLogger("creai.critic")

MODEL = "gpt-6-astra"
API = "https://api.openai.com/v1/chat/completions"
FRAMES = 6

SYSTEM = """You are reviewing a vertical advertisement, frame by frame. You are the last
person to see it before money is spent showing it to strangers.

Return ONLY JSON: {"verdict": "ship" | "fix", "found": [...], "working": [...]}
where each entry in `found` is {"what": "...", "why": "...", "fix": "..."} — what is wrong,
why it costs something, and the specific change.

What to look for, in the order it costs money:

- The first frame. Does it move, and is the product or a person in it? A still frame with
  text on it does not stop a thumb, and roughly three quarters of viewers never reach the
  fourth second.
- Legibility. Any caption you cannot read instantly — white on a pale screen, text over the
  thing it describes, too small, too long — is a caption that is not there.
- Dated craft. A black rounded box behind captions, a thick stroke, a yellow highlighted
  word, emoji punctuation. These read as 2021 and undercut anything selling software.
- The camera. If every frame is the same distance and height, the camera is saying nothing.
  A problem shot from above and a payoff shot from below is the arc; flat throughout is not.
- Claims. Any figure, award, rating or customer count on screen that the product cannot
  support. Flag it even if it looks plausible — especially then.
- The end. Is there a name, an address and a reason to act, and can all three be read?

Be specific and be brief. "The caption is hard to read" is useless; "the caption at frame 4
is white on a cream app screen" is actionable. If a frame is fine, say nothing about it.
Only put something in `working` if it is genuinely good — an empty list is an acceptable
answer and better than flattery.
"""


class CriticError(RuntimeError):
    pass


def _grab(video: Path, at: float, out: Path) -> bool:
    done = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(at), "-i", str(video),
         "-frames:v", "1", "-vf", "scale=540:-2", str(out)],
        capture_output=True)
    return done.returncode == 0 and out.exists() and out.stat().st_size > 0


def _length(video: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(video)], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


async def review(video: Path | str) -> dict:
    """Watch a finished ad and say what is wrong with it.

    Never raises on a review failure: a second opinion that can block a render is
    no longer a second opinion.
    """
    video = Path(video)
    blank = {"verdict": "unreviewed", "found": [], "working": [],
             "note": "this render was not reviewed"}
    if not settings.openai_key or not video.exists():
        return blank

    seconds = _length(video)
    if seconds <= 0:
        return {**blank, "note": "the file could not be read"}

    work = Path(tempfile.mkdtemp(prefix="creai-critic-"))
    try:
        # weighted toward the opening, because that is where the money is lost
        points = [0.2, 0.9, 1.8] + [seconds * f for f in (0.4, 0.7, 0.95)]
        shots = []
        for i, at in enumerate(points[:FRAMES]):
            p = work / f"{i}.jpg"
            if _grab(video, min(at, max(seconds - 0.1, 0)), p):
                shots.append((round(at, 2), p))
        if not shots:
            return {**blank, "note": "no frames could be read from the file"}

        content = [{"type": "text",
                    "text": f"A {seconds:.0f}-second vertical ad. Frames at "
                            + ", ".join(f"{t}s" for t, _ in shots)
                            + ". Audio is not included, so do not comment on it."}]
        for _, p in shots:
            b64 = base64.b64encode(p.read_bytes()).decode()
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

        async with httpx.AsyncClient(timeout=180) as x:
            r = await x.post(API,
                             headers={"Authorization": f"Bearer {settings.openai_key}",
                                      "Content-Type": "application/json"},
                             json={"model": MODEL, "max_completion_tokens": 1500,
                                   "response_format": {"type": "json_object"},
                                   "messages": [{"role": "system", "content": SYSTEM},
                                                {"role": "user", "content": content}]})
        if r.status_code >= 400:
            log.warning("critic unavailable: %s %s", r.status_code, r.text[:160])
            return {**blank, "note": "the reviewer was unavailable"}
        out = json.loads(r.json()["choices"][0]["message"]["content"])
        found = [f for f in (out.get("found") or []) if isinstance(f, dict)][:8]
        return {"verdict": "fix" if found else "ship",
                "found": found,
                "working": [str(w)[:160] for w in (out.get("working") or [])][:5],
                "frames": [t for t, _ in shots]}
    except (httpx.HTTPError, ValueError, KeyError, OSError) as exc:
        log.warning("critic failed: %s", exc)
        return {**blank, "note": "the review did not complete"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def review_many(videos: list[Path | str], at_once: int = 3) -> dict:
    """Review a batch of variants. Used before a set goes out, so twenty ads are
    not published with the same fault twenty times."""
    gate = asyncio.Semaphore(at_once)

    async def one(v):
        async with gate:
            return str(v), await review(v)

    return dict(await asyncio.gather(*(one(v) for v in videos)))
