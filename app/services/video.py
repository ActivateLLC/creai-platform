"""
Video: a script, cut into scenes, made into a film.

Some people do not want a website. They want a channel — ads, shorts, a comedy
feed — and the thing they need built is the video itself. So video is a kind of
project here, beside a site, an app and a game, rather than a button hidden
inside marketing.

The plan is kept apart from the render, deliberately. Generating a picture and a
voice costs real money, so changing one line should re-render one scene, not the
whole film. Every scene carries its own asset keys and a hash of what produced
them; if the words and the look have not changed, the scene is reused.

What the pipeline does, in order:
  1. write the scenes from the brand kit and the brief
  2. generate a still for each scene that needs one
  3. give each still motion, or capture real footage where the product is the point
  4. speak each line, and time the scene to the line rather than the other way round
  5. lay a bed under it, and a sound on each cut
  6. cut to shape, burn the captions, and hand it to the approval queue

Rendering itself happens in the builder, which has ffmpeg and a browser. This
module owns the plan, the money and the state.
"""

import hashlib
import json
import logging
import re
from typing import Any

from ..core.db import conn, log_event

log = logging.getLogger("creai.video")

SHAPES = {"vertical": (1080, 1920), "square": (1080, 1080), "wide": (1920, 1080)}

# What a scene can be. Generated pictures cost money; the others do not, which is
# why a plan that leans on real footage is both cheaper and more convincing.
SOURCES = ("generated", "footage", "card", "upload")

# Roughly what each stage costs us, in credits, so the estimate shown before a
# render is honest rather than a guess.
COST = {"still": 1, "motion": 6, "voice": 1, "music": 2, "render": 3}

MAX_SCENES = 12
MAX_SECONDS = 180


class VideoError(ValueError):
    """Something the person should be told plainly."""


def shape_of(name: str) -> tuple[int, int]:
    return SHAPES.get(name or "vertical", SHAPES["vertical"])


def _fingerprint(scene: dict) -> str:
    """What this scene's pictures depend on. Change the words or the look and it
    re-renders; change the caption position and it does not."""
    seed = json.dumps({k: scene.get(k) for k in ("line", "source", "prompt", "footage", "seconds")},
                      sort_keys=True)
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def check(plan: dict) -> list[str]:
    """Everything wrong with a plan, in words a person can act on."""
    problems = []
    scenes = plan.get("scenes") or []
    if not scenes:
        problems.append("there are no scenes yet — say what the video should show")
    if len(scenes) > MAX_SCENES:
        problems.append(f"that is more than {MAX_SCENES} scenes; cut it down or split it in two")
    total = sum(float(s.get("seconds") or 0) for s in scenes)
    if total > MAX_SECONDS:
        problems.append(f"that runs to {int(total)} seconds; the limit is {MAX_SECONDS}")

    for i, s in enumerate(scenes, 1):
        if s.get("source") not in SOURCES:
            problems.append(f"scene {i} has no source — generated, footage, card or upload")
        if s.get("source") == "generated" and not (s.get("prompt") or "").strip():
            problems.append(f"scene {i} is generated but has nothing to generate from")
        if s.get("source") == "footage" and not s.get("footage"):
            problems.append(f"scene {i} is footage but names no clip")

    # The things that actually lose money on a feed, checked before we spend any.
    first = scenes[0] if scenes else {}
    if first and first.get("source") == "card":
        problems.append("the first scene is a title card — open on the thing itself, "
                        "or most people never see the second scene")
    brand = (plan.get("brand_name") or "").strip().lower()
    if brand and first:
        opening = (first.get("line") or "").lower()
        if brand and brand in opening:
            problems.append("the opening line says the brand name — that reads as an "
                            "advertisement and costs the scroll; save it for the end")
    if not any((s.get("line") or "").strip() for s in scenes):
        problems.append("nothing is said in any scene")
    return problems


def estimate(plan: dict) -> int:
    """Credits this render will cost, counted the same way it will be charged."""
    scenes = plan.get("scenes") or []
    n = COST["render"]
    for s in scenes:
        if s.get("source") == "generated":
            n += COST["still"] + (COST["motion"] if s.get("motion", True) else 0)
        if (s.get("line") or "").strip():
            n += COST["voice"]
    if plan.get("music"):
        n += COST["music"]
    return n


def reusable(old: dict, new: dict) -> set[str]:
    """Scene ids whose pictures can be kept. Re-rendering what did not change is
    the difference between a cheap edit and an expensive one."""
    was = {s.get("id"): _fingerprint(s) for s in (old.get("scenes") or []) if s.get("assets")}
    return {sid for sid, fp in was.items()
            if any(s.get("id") == sid and _fingerprint(s) == fp for s in (new.get("scenes") or []))}


async def save(project_id: int, org_id: int, user_id: int | None, plan: dict,
               title: str = "", shape: str = "vertical") -> dict:
    """Keep the plan. Saving never costs anything; only rendering does."""
    problems = check(plan)
    for s in plan.get("scenes") or []:
        s.setdefault("id", hashlib.sha256(json.dumps(s, sort_keys=True).encode()).hexdigest()[:10])
    async with conn() as c:
        row = await c.fetchrow(
            """INSERT INTO videos (org_id, project_id, created_by, title, shape, plan)
               VALUES ($1,$2,$3,$4,$5,$6) RETURNING id, created_at""",
            org_id, project_id, user_id, title[:160], shape if shape in SHAPES else "vertical",
            plan)
    return {"id": row["id"], "problems": problems, "credits": estimate(plan),
            "at": row["created_at"].isoformat()}


async def start(video_id: int, org_id: int) -> dict:
    """Move a plan into the render queue. Refuses on the problems that would waste
    the person's money, rather than rendering something nobody will watch."""
    async with conn() as c:
        v = await c.fetchrow("SELECT id, plan, state FROM videos WHERE id=$1 AND org_id=$2",
                             video_id, org_id)
        if not v:
            raise VideoError("no such video")
        if v["state"] == "rendering":
            raise VideoError("that video is already rendering")
        problems = check(v["plan"] or {})
        if problems:
            raise VideoError("; ".join(problems[:3]))
        await c.execute("UPDATE videos SET state='rendering', progress=0, error=NULL, "
                        "updated_at=now() WHERE id=$1", video_id)
    return {"id": video_id, "state": "rendering", "credits": estimate(v["plan"] or {})}


async def finish(video_id: int, *, asset_key: str, poster_key: str | None,
                 seconds: float, credits: int) -> None:
    async with conn() as c:
        await c.execute(
            """UPDATE videos SET state='ready', progress=100, asset_key=$2, poster_key=$3,
               seconds=$4, credits=$5, updated_at=now() WHERE id=$1""",
            video_id, asset_key, poster_key, seconds, credits)


async def fail(video_id: int, why: str) -> None:
    """A failed render says what happened. 'Something went wrong' teaches nobody."""
    async with conn() as c:
        await c.execute("UPDATE videos SET state='failed', error=$2, updated_at=now() WHERE id=$1",
                        video_id, str(why)[:400])


# ---------------------------------------------------------------- writing it

HOOKS = (
    "open on the thing itself, mid-use, with no preamble",
    "open on the problem the customer already has",
    "open on a number or a result, stated flatly",
)


def brief_for_agent(kit: dict, ask: str, shape: str = "vertical") -> str:
    """What the agent is told before it writes. Everything here is a rule that
    earns its place from how these are actually watched, not a style preference."""
    voice = (kit or {}).get("voice") or "plain and direct"
    who = (kit or {}).get("audience") or "the people this business serves"
    name = (kit or {}).get("name") or "the business"
    w, h = shape_of(shape)
    return (
        f"Write a short video for {name}. It sounds like this: {voice}. It is for {who}.\n"
        f"The ask: {ask}\n\n"
        f"Shape {w}x{h}. Aim for 15 to 30 seconds, and never more than 45.\n"
        "Rules, in order of how much they cost when broken:\n"
        f"- The first scene shows the thing itself. {HOOKS[0]}. A title card first loses "
        "most of the audience before the second scene.\n"
        "- Do not say the brand name in the opening line. It reads as an advertisement.\n"
        "- One idea per scene. A line with two ideas has none.\n"
        "- Write for sound off: the line and the picture must carry it with no audio.\n"
        "- Specific beats clever. Say the actual number, the actual job, the actual result.\n"
        "- Claim nothing the business cannot do. No invented prices, awards or reviews.\n"
        "- The name and what to do next belong in the last scene, not the first.\n\n"
        "Return scenes: each with a line to speak, a source (generated, footage, card), "
        "a prompt when generated, and roughly how long it runs."
    )
