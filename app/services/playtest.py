"""
The playtester.

A customer asked for realistic characters and got dots. The code compiled, the
export succeeded, every static check passed, and the game was still nothing
like what was asked for — because nothing in the pipeline ever looked at it
with the request in hand.

Sites have had this for months: the `look` tool renders the page and the agent
judges spacing and hierarchy from the picture. Games never did. Every source on
agent-driven game development says the same thing — a blind agent fails
constantly at visual work, and the differentiator is not the tool count but
whether the agent can see what it did.

This runs the built game, captures the moments that matter, and answers three
questions with the original request beside the frames:

  1. Does this look like what was asked for?
  2. Does it work — input answered, no stuck states, no broken text?
  3. Is the thirty-second experience the one that was promised?

Engine-independent on purpose: it takes a URL to a running web build. Godot
today, Unity tomorrow, and it does not care which.

It fails open. A build that could not be playtested is still a build; this is
the second opinion that was missing, not a gate that stops shipping when the
reviewer is unavailable.
"""

import asyncio
import base64
import json
import logging
import shutil
import tempfile
from pathlib import Path

import httpx

from ..core.config import settings

log = logging.getLogger("creai.playtest")

MODEL = "gpt-6-astra"
API = "https://api.openai.com/v1/chat/completions"

# When to look. Weighted toward the opening because that is where a player
# decides, and toward failure states because that is where games break.
MOMENTS = (
    ("start", 1.5, None),                 # the first thing anyone sees
    ("first_input", 3.0, "tap"),          # does a tap do something
    ("playing", 10.0, None),              # ten seconds in — the real game
    ("under_pressure", 20.0, None),       # has difficulty risen at all
)

SYSTEM = """You are playtesting a game somebody asked for, with their request in front of you.
You are the last check before they are told it is done.

Return ONLY JSON:
{
  "verdict": "ship" | "fix",
  "matches_request": true | false,
  "works": true | false,
  "found": [{"what": "...", "why": "...", "fix": "..."}],
  "thirty_seconds": "one sentence: what the first thirty seconds actually are"
}

Three questions, in order of how much they matter:

1. DOES IT LOOK LIKE WHAT WAS ASKED FOR? Compare the frames to the request. If they
   asked for realistic characters and the screen shows dots, that is the finding —
   say it plainly, and say it first. If they asked for a racing game and this is a
   platformer, same. The request is the standard, not the code.

2. DOES IT WORK? Is the start screen a start screen, or a blank field? After a tap,
   did something visibly change? Is any text garbled, cut off, or a placeholder? Is
   anything overlapping or off the edge on a phone-shaped screen? Is there a score,
   and is it readable?

3. IS THE THIRTY-SECOND EXPERIENCE THE ONE PROMISED? From the frames, describe what
   a player actually does in the first half minute. If that description would not
   make anyone want a second run, say why.

Be specific. "The graphics are weak" is useless; "the player is a plain circle with
no face or limbs, and the request asked for a realistic person" is actionable. An
empty `found` list is a fine answer when it is true. Do not flatter."""


class PlaytestError(RuntimeError):
    pass


async def _capture(url: str, work: Path) -> list[tuple[str, Path]]:
    """Run the game in a real browser and photograph the moments that matter."""
    from playwright.async_api import async_playwright

    shots = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await (await browser.new_context(
            viewport={"width": 540, "height": 960}, device_scale_factor=2)).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)[:160]))
        await page.goto(url, wait_until="networkidle", timeout=60000)
        elapsed = 0.0
        for name, at, action in MOMENTS:
            await page.wait_for_timeout(int((at - elapsed) * 1000))
            elapsed = at
            if action == "tap":
                await page.mouse.click(270, 480)
                await page.wait_for_timeout(300)
            shot = work / f"{name}.png"
            await page.screenshot(path=str(shot))
            shots.append((name, shot))
        await browser.close()
    if errors:
        (work / "errors.txt").write_text("\n".join(errors))
    return shots


async def play(url: str, request: str, *, concept: str = "") -> dict:
    """Playtest a running web build against what was asked for.

    Never raises on a review failure — see the module note.
    """
    blank = {"verdict": "unreviewed", "matches_request": None, "works": None,
             "found": [], "thirty_seconds": "", "note": "this build was not playtested"}
    if not settings.openai_key or not url:
        return blank

    work = Path(tempfile.mkdtemp(prefix="creai-playtest-"))
    try:
        try:
            shots = await _capture(url, work)
        except Exception as exc:                      # a browser problem is not a game problem
            log.warning("playtest capture failed: %s", exc)
            return {**blank, "note": f"could not run the build: {type(exc).__name__}"}
        if not shots:
            return {**blank, "note": "no frames were captured"}

        errors_file = work / "errors.txt"
        js_errors = errors_file.read_text() if errors_file.exists() else ""

        content = [{"type": "text", "text":
                    f"THE REQUEST: {request}\n"
                    + (f"THE CONCEPT IT WAS BUILT TO: {concept}\n" if concept else "")
                    + "Frames, in order: " + ", ".join(f"{n} ({MOMENTS[i][1]}s)"
                                                       for i, (n, _) in enumerate(shots))
                    + (f"\nBrowser errors while running: {js_errors[:600]}" if js_errors else "")}]
        for _, shot in shots:
            b64 = base64.b64encode(shot.read_bytes()).decode()
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"}})

        async with httpx.AsyncClient(timeout=180) as x:
            r = await x.post(API,
                             headers={"Authorization": f"Bearer {settings.openai_key}",
                                      "Content-Type": "application/json"},
                             json={"model": MODEL, "max_completion_tokens": 1500,
                                   "response_format": {"type": "json_object"},
                                   "messages": [{"role": "system", "content": SYSTEM},
                                                {"role": "user", "content": content}]})
        if r.status_code >= 400:
            log.warning("playtester unavailable: %s %s", r.status_code, r.text[:160])
            return {**blank, "note": "the playtester was unavailable"}
        out = json.loads(r.json()["choices"][0]["message"]["content"])
        found = [f for f in (out.get("found") or []) if isinstance(f, dict)][:8]
        matches = bool(out.get("matches_request", True))
        works = bool(out.get("works", True))
        return {
            "verdict": "fix" if (found or not matches or not works) else "ship",
            "matches_request": matches,
            "works": works,
            "found": found,
            "thirty_seconds": str(out.get("thirty_seconds") or "")[:300],
            "frames": [n for n, _ in shots],
        }
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.warning("playtest failed: %s", exc)
        return {**blank, "note": "the playtest did not complete"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def summarise(result: dict) -> str:
    """One line for the log and the person, in the order the findings matter."""
    if result.get("verdict") == "unreviewed":
        return result.get("note") or "not playtested"
    if not result.get("matches_request", True):
        first = (result.get("found") or [{}])[0].get("what", "")
        return f"does not match the request — {first}" if first else "does not match the request"
    if not result.get("works", True):
        return "playtested — something is broken"
    if result.get("found"):
        return f"playtested — {len(result['found'])} thing(s) to fix"
    return "playtested — plays as asked"
