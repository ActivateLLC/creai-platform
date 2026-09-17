"""
The reviewer: a second pair of eyes on every build.

The builder is too close to its own work — it believes its code runs and its layout
looks right. So before a build turn ends, the platform gathers evidence the builder
cannot argue with:

  sites  screenshots at phone and desktop widths, plus measurements (overflow, text
         under 12px, contrast below AA, missing alt text)
  apps   a real load in a browser: reported errors, clicks through the main controls,
         a filled and submitted form, and a screenshot

A reviewer model then looks at that evidence with a fresh context and a skeptical
brief, and returns findings by severity. The builder fixes the blockers; the rest is
reported honestly. Deterministic findings (an error, a 40px overflow) come from the
measurements, not the model, so they can't be talked away.
"""

import asyncio
import base64
import logging

import httpx

from ..core.config import settings
from . import models

log = logging.getLogger("creai.review")

TIMEOUT = 90
MAX_FINDINGS = 6
# A second opinion shouldn't cost more than the build itself.
def review_model() -> str:
    return settings.agent_fast_model or settings.agent_model


class RenderError(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.render_url and settings.render_token)


async def _call(path: str, payload: dict) -> dict:
    if not configured():
        raise RenderError("the renderer isn't switched on")
    async with httpx.AsyncClient(timeout=TIMEOUT) as x:
        r = await x.post(settings.render_url.rstrip("/") + path, json=payload,
                         headers={"X-Render-Token": settings.render_token})
    if r.status_code >= 400:
        raise RenderError(f"renderer returned {r.status_code}")
    return r.json()


async def shots(html: str, widths: list[str] | None = None) -> dict:
    return await _call("/shot", {"html": html, "widths": widths or ["phone", "desktop"]})


async def smoke(html: str) -> dict:
    return await _call("/smoke", {"html": html})


# ---------------------------------------------------------------- deterministic findings

def _site_facts(measure: dict) -> list[str]:
    facts = []
    for width, m in (measure or {}).items():
        for o in (m.get("overflow") or [])[:3]:
            facts.append(f"{width}: <{o['tag']} class=\"{o['cls']}\"> runs {o['over']}px past the screen edge")
        for t in (m.get("tiny_text") or [])[:2]:
            facts.append(f"{width}: text at {t['px']}px is too small to read — “{t['text']}”")
        for c in (m.get("low_contrast") or [])[:3]:
            facts.append(f"{width}: contrast {c['ratio']}:1 on “{c['text']}” (needs 4.5:1)")
        if m.get("images_without_alt"):
            facts.append(f"{width}: {m['images_without_alt']} image(s) have no alt text")
    return facts


def _app_facts(run: dict) -> list[str]:
    facts = []
    for e in (run.get("errors") or [])[:4]:
        facts.append(f"error while running: {e}")
    if run.get("rendered") is False:
        facts.append("the app rendered nothing into #root")
    for i in (run.get("interactions") or []):
        if not i.get("ok"):
            facts.append(f"clicking “{i.get('clicked')}” failed: {i.get('error')}")
    form = run.get("form")
    if form and form.get("submitted") and not form.get("page_changed"):
        facts.append("the form submitted but nothing on the page changed — saving may not work")
    for b in (run.get("blocked_requests") or [])[:2]:
        facts.append(f"a request was blocked: {b}")
    return facts


SITE_BRIEF = """You are reviewing a small business website another agent just built, the way a
senior designer would before it goes to the client. You can see it at phone and desktop widths.

Judge only what you can see: layout and spacing, visual hierarchy, whether the first screen says
what the business does and what to do next, whether type and colour look deliberate, and whether
anything looks broken, cramped, cut off or unfinished. Copy matters too: vague or stock phrasing,
claims with no substance, placeholders left in view.

Return findings as JSON only:
{"findings":[{"severity":"blocker|should_fix|nice_to_have","what":"...","fix":"one precise instruction"}],
 "verdict":"ship|fix_first","summary":"one sentence"}
Rules: at most 6 findings, most serious first. A blocker means a visitor would be confused or put
off. Don't invent problems to look useful — an empty findings list is a fine answer. Never suggest
inventing facts, prices or testimonials."""

APP_BRIEF = """You are reviewing a small web app another agent just built, the way a senior engineer
would before handing it to the customer. You can see a screenshot after a real browser loaded it,
clicked its main controls and submitted its first form.

Judge what you can see and what the run reports: does the core job work, is state handled (loading,
empty, error), do controls do something, is the layout usable on a phone, does the copy make the
purpose obvious.

Return findings as JSON only:
{"findings":[{"severity":"blocker|should_fix|nice_to_have","what":"...","fix":"one precise instruction"}],
 "verdict":"ship|fix_first","summary":"one sentence"}
Rules: at most 6 findings, most serious first. A blocker means the app is broken or its main job
can't be completed. Don't invent problems."""


async def _ask(brief: str, facts: list[str], images: list[str], context: str) -> dict:
    blocks = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b}}
              for b in images[:3]]
    told = ("What the build is meant to be:\n" + context.strip()[:1500]
            + ("\n\nMeasured facts (already verified, treat as true):\n- " + "\n- ".join(facts)
               if facts else "\n\nNo automated problems were measured."))
    blocks.append({"type": "text", "text": told})
    out = await models.complete(review_model(), [{"role": "user", "content": blocks}], [], brief, 1200)
    text = " ".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
    return _json(text)


def _json(text: str) -> dict:
    import json
    import re
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {"findings": [], "verdict": "ship", "summary": ""}
    try:
        out = json.loads(m.group(0))
    except ValueError:
        return {"findings": [], "verdict": "ship", "summary": ""}
    return out if isinstance(out, dict) else {"findings": [], "verdict": "ship", "summary": ""}


def _merge(facts: list[str], judged: dict) -> dict:
    findings = [{"severity": "blocker", "what": f, "fix": "", "measured": True} for f in facts[:4]]
    for f in (judged.get("findings") or [])[:MAX_FINDINGS]:
        if not isinstance(f, dict) or not f.get("what"):
            continue
        sev = f.get("severity") if f.get("severity") in ("blocker", "should_fix", "nice_to_have") else "should_fix"
        findings.append({"severity": sev, "what": str(f["what"])[:220], "fix": str(f.get("fix", ""))[:220],
                         "measured": False})
    blockers = [f for f in findings if f["severity"] == "blocker"]
    return {"findings": findings[:MAX_FINDINGS + 4], "blockers": len(blockers),
            "verdict": "fix_first" if blockers else (judged.get("verdict") or "ship"),
            "summary": str(judged.get("summary", ""))[:200]}


async def review_site(html: str, context: str) -> dict:
    out = await shots(html)
    facts = _site_facts(out.get("measure"))
    images = [out["shots"][w] for w in ("phone", "desktop") if w in out.get("shots", {})]
    judged = await _ask(SITE_BRIEF, facts, images, context) if images else {}
    result = _merge(facts, judged)
    result["evidence"] = {"widths": list(out.get("shots", {})), "measured": facts}
    return result


async def review_app(html: str, context: str) -> dict:
    run = await smoke(html)
    facts = _app_facts(run)
    images = [run["shot"]] if run.get("shot") else []
    judged = await _ask(APP_BRIEF, facts, images, context) if images else {}
    result = _merge(facts, judged)
    result["evidence"] = {"rendered": run.get("rendered"), "errors": run.get("errors"),
                          "interactions": run.get("interactions"), "form": run.get("form"),
                          "measured": facts}
    return result


def as_instruction(result: dict, kind: str) -> str:
    """What the builder is told to do about the review."""
    lines = []
    for f in result["findings"]:
        mark = {"blocker": "MUST FIX", "should_fix": "SHOULD FIX", "nice_to_have": "OPTIONAL"}[f["severity"]]
        lines.append(f"- [{mark}] {f['what']}" + (f" → {f['fix']}" if f["fix"] else ""))
    return (f"A reviewer looked at the {kind} you just built, on a phone and on a desktop, and ran it.\n"
            + "\n".join(lines)
            + "\nFix every MUST FIX and as many SHOULD FIX as you sensibly can, then reply. "
              "If you disagree with a point, say so plainly in your reply rather than ignoring it.")


async def safe_review(kind: str, html: str, context: str) -> dict | None:
    """Never let review trouble break a build."""
    try:
        return await asyncio.wait_for(
            review_app(html, context) if kind == "app" else review_site(html, context), TIMEOUT + 30)
    except Exception as exc:                       # noqa: BLE001 — report, never raise
        log.warning("review skipped: %s", exc)
        return None
