"""
Launch readiness: how close a project is to launch, honestly.

Every item is a check on the real project — the spec, the quality gate, releases,
domains — never the model's opinion. Items come in three levels:

  essential    needed to look finished (always counted)
  recommended  makes it better (the person can skip these: they stop counting)
  golive       published, own domain, marketing (skippable too)

Score = 60% essentials + 25% recommended + 15% go-live, over the items that still
count. An essential the person skips is shown as "your call" and still counts as
not done: the number never flatters. The agent's own ideas are listed separately
as suggestions and never change the score.
"""

import json
import re

from ..core.db import conn
from . import appfs
from . import site as site_spec

WEIGHTS = {"essential": 60, "recommended": 25, "golive": 15}
PLACEHOLDER = re.compile(r"\[[A-Z][A-Z0-9 _/-]{1,30}\]")
MAX_SUGGESTIONS = 6


def _item(key, level, label, why, done, request=None, action=None, detail=None):
    return {"key": key, "level": level, "label": label, "why": why, "done": bool(done),
            "request": request, "action": action, "detail": detail}


def _text_of(spec: dict) -> str:
    return json.dumps(spec, ensure_ascii=False)


# ---------------------------------------------------------------- checks

def site_items(spec: dict, published: bool, domain: bool, marketing: bool) -> list[dict]:
    s = site_spec.merge(spec or {}, {})
    kinds = [x["kind"] for x in s["sections"]]
    c = s["contact"]
    holders = sorted(set(PLACEHOLDER.findall(_text_of(s))))
    issues = [i for i in site_spec.critique(s) if "automatic" not in i and "default colours" not in i]
    desc = len(s["subline"])
    has_content = bool(s["headline"] or s["sections"])
    has_identity = bool(s["business"] and s["headline"])
    return [
        _item("identity", "essential", "Name and headline",
              "Visitors should know who you are and what you offer in one glance.",
              s["business"] and s["headline"],
              "Set my business name and write a clear, specific headline for the site."),
        _item("action", "essential", "A clear next step",
              "Every page needs one obvious thing to do: book, call or buy.",
              s["cta"] and ("cta" in kinds or c),
              "Add a clear call to action that fits my business, and a closing section that repeats it."),
        _item("contact", "essential", "A way to reach you",
              "People who are ready to buy need a phone number or email.",
              c.get("email") or c.get("phone"),
              "Add my contact details to the site. Ask me for my phone number or email first."),
        _item("content", "essential", "Enough to go on",
              "Three or more sections cover what you offer, how it works and why you.",
              len(s["sections"]) >= 3,
              "Add sections so the site covers what I offer, how it works and why people should choose me."),
        _item("placeholders", "essential", "No placeholders left",
              "Brackets like [PRICE] look unfinished to visitors.",
              has_content and not holders,
              ("These placeholders are still on my site: " + ", ".join(holders)
               + ". Ask me for the real details, then replace them.") if holders
              else "Build a first version of my site with real details, asking me for anything you don't know.",
              detail=", ".join(holders) or None),
        _item("quality", "essential", "Passes the quality check",
              "No stock AI phrasing, readable contrast, varied sections.",
              has_identity and not issues,
              ("Fix these quality issues on my site: " + " ".join(issues)) if issues
              else "Build a first version of my site so it can be quality-checked.",
              detail=" ".join(issues[:3]) or None),
        _item("design", "recommended", "A design chosen for you",
              "A layout, theme and palette picked for your business, not left on automatic.",
              s["layout"] and s["theme"] and s["palette"] != site_spec.DEFAULT_PALETTE,
              "Choose a layout, theme, motion level and palette that suit my business and explain why."),
        _item("hero", "recommended", "A strong opening image",
              "A real or generated photo makes the first screen feel like yours.",
              s["hero_image"],
              "Create a hero image that fits my business and use it on the site."),
        _item("description", "recommended", "A good search description",
              "The line under your headline doubles as your Google and link-preview text (50–160 characters).",
              50 <= desc <= 160,
              "Rewrite my subline so it works as a search description: specific, 50 to 160 characters."),
        _item("proof", "recommended", "Reasons to trust you",
              "Numbers or reviews you provide help people decide. Never invented.",
              "testimonials" in kinds or "stats" in kinds,
              "Add a section with real reasons to trust me. Ask me for reviews or numbers first; don't invent any."),
        _item("faq", "recommended", "Answers to common questions",
              "An FAQ handles the questions that stop people from getting in touch.",
              "faq" in kinds,
              "Add an FAQ with the questions my customers usually ask. Ask me for any facts you need."),
        _item("published", "golive", "Published",
              "Your site is live on a free Creai address.", published, action="publish"),
        _item("domain", "golive", "Your own domain",
              "A domain like yourbusiness.com looks established.", domain, action="domain"),
        _item("marketing", "golive", "Marketing switched on",
              "Creai drafts posts from your site for you to approve.", marketing, action="marketing"),
    ]


def app_items(files: dict, spec: dict, errors: int, published: bool, domain: bool) -> list[dict]:
    built = files.get(appfs.ENTRY, "") not in ("", appfs.STARTER[appfs.ENTRY])
    code = "\n".join(v for k, v in files.items() if k.endswith(".js"))
    used = sorted(set(re.findall(r"collection\(\s*['\"]([a-z][a-z0-9_]{0,40})['\"]", code)))
    ruled = set(appfs.rules(files))
    missing = [c for c in used if c not in ruled]
    s = site_spec.merge(spec or {}, {})
    return [
        _item("built", "essential", "The app is built",
              "Real screens for the main thing people come to do.", built,
              "Build the core screens of my app. Ask me what people need to do first if it's unclear."),
        _item("errors", "essential", "Runs without errors",
              "The preview hasn't reported an error since the last change.", built and not errors,
              "The preview reported an error. Find and fix it." if errors else None),
        _item("named", "essential", "Has a name",
              "Shown at the top of the app and in the browser tab.", s["business"],
              "Give my app a short, clear name and show it in the top bar."),
        _item("rules", "essential", "Visitor access decided",
              "Each kind of data states who can see and add it once the app is public.",
              built and not missing,
              ("Add access rules in app.json for: " + ", ".join(missing)
               + ". Keep personal data private; ask me if unsure.") if missing else None,
              detail=", ".join(missing) or None),
        _item("design", "recommended", "Styled for your brand",
              "A theme and colours chosen for you rather than the defaults.",
              s["theme"] and s["palette"] != site_spec.DEFAULT_PALETTE,
              "Choose a theme and colours for my app that suit my brand."),
        _item("published", "golive", "Published",
              "Your app is live at its own Creai address.", published, action="publish_app"),
        _item("domain", "golive", "Your own domain",
              "Put the app on a domain you own.", domain, action="domain"),
    ]


def market_items(brand: dict, drafted: int, approved: int, channels: int) -> list[dict]:
    return [
        _item("brand", "essential", "Brand kit saved",
              "Your voice, audience and colours, read from your site.", brand,
              "Read my website and save my brand kit."),
        _item("drafts", "essential", "Posts drafted",
              "A couple of weeks of posts ready for your review.", drafted,
              "Plan my next two weeks of posts."),
        _item("approved", "essential", "Posts approved",
              "Nothing goes out until you approve it.", approved, action="review"),
        _item("channels", "golive", "Social accounts connected",
              "Approved posts go out on schedule to accounts you connect.", channels, action="channels"),
    ]


# ---------------------------------------------------------------- scoring

def score(items: list[dict], decisions: dict) -> dict:
    for it in items:
        d = decisions.get(it["key"])
        it["decision"] = d
        it["counts"] = not (d == "skip" and it["level"] != "essential")
    totals = {}
    for level, weight in WEIGHTS.items():
        counted = [i for i in items if i["level"] == level and i["counts"]]
        if counted:
            totals[level] = (weight, sum(i["done"] for i in counted) / len(counted))
    total_w = sum(w for w, _ in totals.values()) or 1
    pct = round(100 * sum(w * f for w, f in totals.values()) / total_w)
    segments = [{"level": lv, "weight": round(100 * totals[lv][0] / total_w, 2),
                 "filled": round(totals[lv][1], 4)} for lv in WEIGHTS if lv in totals]
    essentials_left = [i for i in items if i["level"] == "essential" and not i["done"]]
    return {"percent": pct, "segments": segments, "essentials_left": len(essentials_left),
            "essentials_left_labels": [i["label"] for i in essentials_left]}


def stage(essentials_left: int, published: bool, domain: bool) -> dict:
    if published and domain and not essentials_left:
        return {"id": "live_domain", "title": "Live on your domain", "note": "Keep improving whenever you like."}
    if published:
        return {"id": "live", "title": "Live", "note": "Published. You can keep editing and republish any time."}
    if not essentials_left:
        return {"id": "ready", "title": "Ready to launch", "note": "The essentials are done. Launch now, or polish first."}
    return {"id": "building", "title": "Getting there",
            "note": f"{essentials_left} essential{'s' if essentials_left != 1 else ''} left. You can still launch now and edit later."}


# ---------------------------------------------------------------- assembling

async def report(project: dict, org_id: int) -> dict:
    pid = project["id"]
    answers = project.get("answers") or {}
    path = project.get("path")
    async with conn() as c:
        decisions = {r["item"]: r["decision"] for r in await c.fetch(
            "SELECT item, decision FROM readiness_decisions WHERE project_id=$1 AND org_id=$2", pid, org_id)}
        domain = bool(await c.fetchval(
            """SELECT 1 FROM domains WHERE project_id=$1 AND org_id=$2
               AND status IN ('registered','verifying','live')""", pid, org_id))
        suggestions = [dict(r) for r in await c.fetch(
            """SELECT id, title, why, request FROM readiness_suggestions
               WHERE project_id=$1 AND org_id=$2 AND status='open' ORDER BY id DESC LIMIT $3""",
            pid, org_id, MAX_SUGGESTIONS)]
        if path == "app":
            files = await appfs.files(pid, org_id)
            published = bool(await c.fetchval(
                "SELECT 1 FROM app_releases WHERE project_id=$1 AND org_id=$2 AND live", pid, org_id))
            errors = await c.fetchval(
                """SELECT count(*) FROM app_errors WHERE project_id=$1
                   AND created_at > (SELECT COALESCE(MAX(updated_at),'epoch') FROM project_files WHERE project_id=$1)""",
                pid)
            items, kind = app_items(files, answers.get("site") or {}, errors, published, domain), "app"
        elif path == "market":
            drafted = await c.fetchval(
                "SELECT count(*) FROM approvals WHERE project_id=$1 AND org_id=$2 AND kind='post'", pid, org_id)
            approved = await c.fetchval(
                """SELECT count(*) FROM approvals WHERE project_id=$1 AND org_id=$2 AND kind='post'
                   AND state IN ('approved','held','sending','scheduled','done')""", pid, org_id)
            channels = await c.fetchval(
                "SELECT count(*) FROM social_channels WHERE org_id=$1 AND status='active'", org_id)
            items, kind = market_items(answers.get("brand") or {}, drafted, approved, channels), "market"
            published = bool(approved)
        elif answers.get("source"):
            return {"kind": "external", "supported": False}
        else:
            published = bool(await c.fetchval(
                "SELECT 1 FROM site_releases WHERE project_id=$1 AND org_id=$2 AND live", pid, org_id))
            items = site_items(answers.get("site") or {}, published, domain, bool(answers.get("marketing")))
            kind = "site"
    s = score(items, decisions)
    return {"kind": kind, "supported": True, "items": items, "suggestions": suggestions,
            "published": published, "domain": domain,
            "stage": stage(s["essentials_left"], published, domain), **s}


async def decide(org_id: int, project_id: int, item: str, decision: str | None) -> None:
    if item.startswith("s:"):
        sid = int(item[2:])
        async with conn() as c:
            await c.execute(
                """UPDATE readiness_suggestions SET status=$4
                   WHERE id=$1 AND project_id=$2 AND org_id=$3""",
                sid, project_id, org_id, "skipped" if decision == "skip" else "open")
        return
    async with conn() as c:
        if decision is None:
            await c.execute("DELETE FROM readiness_decisions WHERE project_id=$1 AND org_id=$2 AND item=$3",
                            project_id, org_id, item)
        else:
            await c.execute(
                """INSERT INTO readiness_decisions (org_id, project_id, item, decision) VALUES ($1,$2,$3,$4)
                   ON CONFLICT (project_id, item) DO UPDATE SET decision=EXCLUDED.decision, updated_at=now()""",
                org_id, project_id, item, decision)


async def add_suggestions(org_id: int, project_id: int, ideas: list[dict]) -> int:
    """Store up to three new ideas from the agent, skipping repeats (including ones the person skipped)."""
    async with conn() as c:
        seen = {r["title"].strip().lower() for r in await c.fetch(
            "SELECT title FROM readiness_suggestions WHERE project_id=$1", project_id)}
        added = 0
        for idea in ideas[:3]:
            title = str(idea.get("title", "")).strip()[:60]
            why = str(idea.get("why", "")).strip()[:160]
            req = str(idea.get("request", "")).strip()[:500]
            if not (title and why and req) or title.lower() in seen:
                continue
            await c.execute(
                """INSERT INTO readiness_suggestions (org_id, project_id, title, why, request)
                   VALUES ($1,$2,$3,$4,$5)""", org_id, project_id, title, why, req)
            seen.add(title.lower())
            added += 1
        # keep the open list short: oldest open ideas beyond the limit are retired
        await c.execute(
            """UPDATE readiness_suggestions SET status='skipped'
               WHERE project_id=$1 AND status='open' AND id NOT IN (
                 SELECT id FROM readiness_suggestions WHERE project_id=$1 AND status='open'
                 ORDER BY id DESC LIMIT $2)""", project_id, MAX_SUGGESTIONS)
    return added


async def mark_applied(org_id: int, project_id: int, ids: list[int]) -> None:
    if ids:
        async with conn() as c:
            await c.execute(
                """UPDATE readiness_suggestions SET status='applied'
                   WHERE id = ANY($1::bigint[]) AND project_id=$2 AND org_id=$3""", ids, project_id, org_id)


async def skipped_titles(project_id: int) -> list[str]:
    async with conn() as c:
        return [r["title"] for r in await c.fetch(
            "SELECT title FROM readiness_suggestions WHERE project_id=$1 AND status<>'open' ORDER BY id DESC LIMIT 20",
            project_id)]
