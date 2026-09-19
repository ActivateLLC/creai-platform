"""
Saying what is happening, while it happens.

The agent already narrates its work — it writes files, checks them, looks at the
result on a phone and a desktop, fixes what it finds. Every one of those steps was
recorded and then delivered in a single lump when the turn finished. So a build
that takes ninety seconds showed one motionless line and a pulsing dot, and the
honest reading of that screen is that the thing has frozen.

This is the channel that carries those steps out as they happen.

Three decisions worth keeping.

It says what it did, not what it is. "Wrote the sign-in screen" is worth reading;
"Processing" is a spinner with words on it. Anything that cannot be said
concretely is better left unsaid, and the dot can carry the waiting.

It is memory only, and losing it costs nothing. Progress is not a record — it is
a courtesy during a wait. Keeping it in the database would mean a write per step
for something nobody reads twice.

It expires. A turn that died leaves its last line behind; without a clock that
line sits on a screen forever, describing something that stopped happening
minutes ago.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("creai.progress")

KEEP = 600.0          # a finished turn's steps stay readable for ten minutes
MAX_STEPS = 40        # a turn that says more than this is looping, not working


@dataclass
class Run:
    """What one turn has done so far."""
    started: float = field(default_factory=time.monotonic)
    touched: float = field(default_factory=time.monotonic)
    steps: list[dict] = field(default_factory=list)
    done: bool = False


_runs: dict[str, Run] = {}
_lock = asyncio.Lock()


async def start(key: str) -> None:
    async with _lock:
        _sweep()
        _runs[key] = Run()


async def say(key: str, step: str) -> None:
    """Record one thing that just happened.

    Called from the agent as it works, so the wording is the agent's own rather
    than a stage name invented here — it knows what it just did, and a summary
    written elsewhere drifts from the truth the moment the agent changes.
    """
    step = (step or "").strip()
    if not step:
        return
    async with _lock:
        run = _runs.get(key)
        if run is None:
            run = _runs[key] = Run()
        if run.steps and run.steps[-1]["step"] == step:
            return                                  # never repeat a line at somebody
        if len(run.steps) >= MAX_STEPS:
            return
        run.steps.append({"step": step[:160],
                          "at": round(time.monotonic() - run.started, 1)})
        run.touched = time.monotonic()


async def finish(key: str) -> None:
    async with _lock:
        run = _runs.get(key)
        if run:
            run.done = True
            run.touched = time.monotonic()


async def read(key: str, since: int = 0) -> dict:
    """The steps a waiting screen has not seen yet.

    `since` is how many it already has, so a poll returns the new ones rather
    than the whole list every second and a half.
    """
    async with _lock:
        _sweep()
        run = _runs.get(key)
        if run is None:
            return {"steps": [], "count": 0, "done": True, "seconds": 0}
        return {"steps": run.steps[max(0, since):],
                "count": len(run.steps),
                "done": run.done,
                "seconds": round(time.monotonic() - run.started, 1)}


def _sweep() -> None:
    """Drop runs nobody is waiting on. Called on the way past rather than on a
    timer: a background task for this would outlive its usefulness."""
    now = time.monotonic()
    for key in [k for k, r in _runs.items() if now - r.touched > KEEP]:
        _runs.pop(key, None)


# What a step should sound like. Kept here as guidance rather than enforced,
# because the agent writes its own lines and a validator would only teach it to
# write past one.
WELL_SAID = (
    "wrote the sign-in screen",
    "checked the app · all clear",
    "looked at it on a phone and a desktop",
    "fixed the button that went nowhere",
    "drew the icons",
)
BADLY_SAID = (
    "processing",
    "working",
    "step 3 of 7",
    "analyzing requirements",
)
