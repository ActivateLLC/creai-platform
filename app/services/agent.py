"""
The conversational agent: a message in, tool calls against the platform, a reply
and an updated site out.

Two rules shape everything here.

1. **The agent drafts; the person decides.** Its tools edit the site spec, record
   answers and queue drafts. Anything that spends money, touches DNS, publishes or
   connects an account is returned as a suggested *action* the person taps — the
   agent has no tool that performs it.
2. **The tenant is fixed before the model runs.** A turn is bound to one draft or
   one project the caller already owns. No tool takes an org or project id from
   the model, so a prompt cannot move the agent into someone else's workspace.
"""

import json
import logging
import re
from dataclasses import dataclass, field

import httpx

from ..core.config import settings
from . import site as site_spec

log = logging.getLogger("creai.agent")

API = "https://api.anthropic.com/v1/messages"
MAX_STEPS = 6
APP_MAX_STEPS = 12
MAX_THREAD = 40          # messages kept per draft or project
MAX_USER_CHARS = 4000

ACTIONS = {
    "build_plan": "Build the plan just proposed",
    "sign_in": "Create an account (needed before anything is registered or published)",
    "connect_domain": "Connect a domain the person already owns",
    "review_posts": "Open the post drafts waiting for approval",
}

SYSTEM = """You are CreAI, the build-to-launch agent inside the CreAI platform. You help a \
business owner go from an idea to a live site and a first marketing campaign.

How you work:
- The person sees a live preview of their site beside this chat. Change it with \
update_site; every call re-renders the preview, so make real edits rather than \
describing them.
- On the first message, build a complete first version straight away: business name, \
headline, subline, a call to action, and two to four sections that fit the business. \
Then ask ONE short question that would most improve it.
- Never invent facts about the business: prices, addresses, phone numbers, reviews, \
licences, years in business. Where one is needed, use a placeholder like [PRICE] and \
say so. Testimonials only if the person supplies them.
- Use save_answer for facts the person tells you (audience, location, services, tone).
- You cannot register domains, publish, spend money or connect accounts yourself. When \
one of those is the natural next step, call suggest_action so the person can do it with \
a tap, and say what it will do and what it costs if known.
- Publishing to a live domain is not switched on yet. If asked, say it is coming and \
offer what is available now.
- Design like a senior designer, not a template. For each business choose a layout, theme, \
motion level and a palette of its own (bg, ink, accent with strong contrast) that fit what it \
sells and who buys it; two different businesses should never look alike. Use the section kinds \
that tell this business's story (steps for a process, stats only with real numbers the person \
gave, gallery with generated images). Write specific, concrete copy in the owner's voice; avoid \
stock phrases such as elevate, unlock, seamless, one-stop, welcome to.
- update_site returns a quality list. If it is not empty, fix every item with another \
update_site call before replying.
- Style: plain, specific and calm. Short replies — two or three sentences. No \
exclamation marks. No markdown headings or bullet lists.
"""

CHAT_EXTRA = """
Mode: CHAT. The person wants to talk things through. Answer questions and give advice \
about their business, site or marketing. You have no tools in this mode, so do not claim \
to have changed anything; if they want a change, tell them to switch to Build.
"""

PLAN_EXTRA = """
Mode: PLAN. Do not change the site. Reply with a short numbered plan (three to seven steps) \
of exactly what you would change and why, in plain language, then ask whether to build it. \
Numbered lines are allowed in this mode.
"""

MARKET_ALSO = """
Marketing is switched on for this project, but the site is still being built. Keep
working on the site whenever the person asks about it. Only read their brand or draft
posts when they ask for marketing or posts. When they ask to change drafted posts, use
revise_post on the ids listed below rather than drafting new ones.
"""

MARKET_EXTRA = """
This project is about MARKETING the person's business. Their website is the source of
truth for what they sell and how they sound. If there is no brand kit yet, call
read_website on their site first, then save_brand_kit with their voice, audience,
services, colours and key links. Then propose and draft posts with draft_posts, each with
a scheduled_for date and time in the coming weeks, spread sensibly (for example 3 to 5 a
week), each linking back to a relevant page. Never invent prices, offers, awards or
reviews; use only what is on their site or what they tell you.
"""

INTENTS = ("build", "chat", "plan")

PROJECT_EXTRA = """
The person is signed in and working on a project in their own workspace. You can also \
draft social posts with draft_posts; they wait in the approval queue and nothing posts \
until the person approves each one.
"""

TOOL_UPDATE_SITE = {
    "name": "update_site",
    "description": "Edit the site. Only the fields you pass change. Passing `sections` "
                   "replaces the whole section list, so include every section you want kept.",
    "input_schema": {
        "type": "object",
        "properties": {
            "business": {"type": "string"},
            "headline": {"type": "string"},
            "subline": {"type": "string"},
            "cta": {"type": "string", "description": "Main button label"},
            "tone": {"type": "string", "enum": list(site_spec.TONES)},
            "layout": {"type": "string", "enum": list(site_spec.LAYOUTS),
                       "description": "Page structure. editorial: big type, numbered sections, calm "
                                      "authority. split: copy beside a visual, clear and practical. "
                                      "centered: classic, warm. bento: tiles, modern and product-like. "
                                      "poster: loud, full-colour, for bold brands and events."},
            "theme": {"type": "string", "enum": list(site_spec.THEME_NAMES),
                      "description": "Type pairing and surfaces. atelier (refined serif), industrial "
                                     "(grotesk, grid), botanical (elegant serif, soft), civic (sturdy, "
                                     "trustworthy), nocturne (dark, glowing), heritage (classic serif), "
                                     "studio (modern serif + sans), playground (chunky, friendly), "
                                     "brutal (heavy caps, mono, hard shadows), tender (warm, gentle)."},
            "motion": {"type": "string", "enum": list(site_spec.MOTIONS),
                       "description": "none; subtle (gentle entrances); lively (staggered reveals, "
                                      "ticker); cinematic (word-by-word headline, scroll depth, grain)."},
            "hero_image": {"type": "string",
                           "description": "URL of an image made with generate_image. Leave empty for "
                                          "generative artwork from the palette."},
            "palette": {
                "type": "object",
                "properties": {k: {"type": "string", "description": "#RRGGBB"}
                               for k in ("bg", "ink", "accent")},
            },
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(site_spec.SECTION_KINDS)},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "button": {"type": "string"},
                        "items": {"type": "array", "items": {"type": "object"},
                                  "description": "services: {name, detail, price}; faq: {q, a}; "
                                                 "testimonials: {quote, name}; stats: {value, label}; "
                                                 "steps: {title, detail}; gallery: {image, caption}"},
                    },
                    "required": ["kind"],
                },
            },
            "contact": {"type": "object",
                        "properties": {k: {"type": "string"} for k in ("email", "phone", "area")}},
        },
    },
}

TOOL_GENERATE_IMAGE = {
    "name": "generate_image",
    "description": "Create an image for the site (hero or gallery). Returns a URL to put in "
                   "hero_image or a gallery item. Describe subject, setting, light and style; no "
                   "text, logos or real people's faces. Costs a few credits.",
    "input_schema": {"type": "object", "properties": {
        "prompt": {"type": "string"},
        "shape": {"type": "string", "enum": ["square", "portrait", "landscape"]}},
        "required": ["prompt"]},
}

TOOL_REVISE_POST = {
    "name": "revise_post",
    "description": "Change a drafted post that hasn't gone out: its text, time, link or picture, "
                   "or discard it. The person approves it again afterwards.",
    "input_schema": {"type": "object", "properties": {
        "id": {"type": "integer"},
        "text": {"type": "string"},
        "scheduled_for": {"type": "string", "description": "ISO 8601 with offset"},
        "link": {"type": "string"},
        "image_prompt": {"type": "string", "description": "Describe a new picture to replace the current one"},
        "discard": {"type": "boolean"}},
        "required": ["id"]},
}

TOOL_SUGGEST_IMPROVEMENTS = {
    "name": "suggest_improvements",
    "description": "Offer up to three concrete, honest ideas that would clearly improve this project. "
                   "They appear in the person's launch checklist to accept or skip; nothing changes now.",
    "input_schema": {"type": "object", "properties": {"ideas": {"type": "array", "maxItems": 3, "items": {
        "type": "object", "properties": {
            "title": {"type": "string", "description": "Short, specific, 60 characters max"},
            "why": {"type": "string", "description": "The benefit for this business, one sentence"},
            "request": {"type": "string", "description": "The exact instruction to carry it out later"}},
        "required": ["title", "why", "request"]}}}, "required": ["ideas"]},
}

IMPROVE_EXTRA = """
After you change the site or app, you may call suggest_improvements with up to three ideas that
would clearly help THIS business (not generic advice, nothing already in the launch checklist such
as contact details, placeholders, hero image, FAQ, publishing or a domain). Skip it when nothing
stands out. Never suggest inventing facts. Ideas the person already declined: {skipped}
"""

TOOL_SAVE_ANSWER = {
    "name": "save_answer",
    "description": "Record a fact the person stated about their business.",
    "input_schema": {
        "type": "object",
        "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
        "required": ["key", "value"],
    },
}

TOOL_SUGGEST = {
    "name": "suggest_action",
    "description": "Offer the person a button for a step you cannot take yourself. "
                   + "; ".join(f"{k}: {v}" for k, v in ACTIONS.items()),
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(ACTIONS)},
            "label": {"type": "string", "description": "Button text naming the action"},
            "domain": {"type": "string", "description": "For connect_domain, if known"},
        },
        "required": ["kind", "label"],
    },
}

TOOL_READ_WEBSITE = {
    "name": "read_website",
    "description": "Read a public web page (the person's own site, usually) and get its title, "
                   "description, headings, main text and colours.",
    "input_schema": {"type": "object", "properties": {"url": {"type": "string"}},
                     "required": ["url"]},
}

TOOL_SAVE_BRAND = {
    "name": "save_brand_kit",
    "description": "Save what you learned about the business so every post stays on-brand.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "voice": {"type": "string", "description": "How they sound, in a sentence or two"},
            "audience": {"type": "string"},
            "services": {"type": "array", "items": {"type": "string"}},
            "colors": {"type": "array", "items": {"type": "string"}},
            "links": {"type": "array", "items": {"type": "object", "properties": {
                "label": {"type": "string"}, "url": {"type": "string"}}}},
            "website": {"type": "string"},
        },
    },
}

TOOL_DRAFT_POSTS = {
    "name": "draft_posts",
    "description": "Queue social post drafts for the person's approval. Nothing is published.",
    "input_schema": {
        "type": "object",
        "properties": {
            "posts": {
                "type": "array", "maxItems": 7,
                "items": {
                    "type": "object",
                    "properties": {
                        "network": {"type": "string",
                                    "enum": ["instagram", "facebook", "google_business", "linkedin",
                                             "tiktok", "x", "threads", "youtube", "pinterest"]},
                        "text": {"type": "string"},
                        "when": {"type": "string", "description": "Human-readable day and time"},
                        "scheduled_for": {"type": "string",
                                          "description": "ISO 8601 date-time with timezone offset"},
                        "link": {"type": "string", "description": "Page on their site this post points to"},
                        "image_prompt": {"type": "string",
                                         "description": "A picture for the post, described for an image "
                                                        "generator: subject, setting, light, style. Required "
                                                        "for Instagram. No text, logos or real people's faces."},
                        "image_shape": {"type": "string", "enum": ["square", "portrait", "landscape"]},
                    },
                    "required": ["network", "text"],
                },
            },
        },
        "required": ["posts"],
    },
}


def _zone(tz: str | None):
    from datetime import timezone
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return ZoneInfo(tz) if tz else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _when(value, tz: str | None = None) -> str | None:
    """A sane future time within a year, or None. Times without an offset are read
    in the person's own timezone."""
    from datetime import datetime, timedelta, timezone
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(tz))
    now = datetime.now(timezone.utc)
    return dt.isoformat() if now < dt < now + timedelta(days=366) else None


async def _app_tool(turn, name, args, project_id, org_id) -> dict:
    from . import appfs
    try:
        if name == "list_files":
            fs = await appfs.files(project_id, org_id)
            return {"files": [{"path": p, "chars": len(c)} for p, c in fs.items()]}
        if name == "read_file":
            fs = await appfs.files(project_id, org_id)
            path = str(args.get("path", ""))
            return {"path": path, "content": fs[path]} if path in fs else {"ok": False, "error": "no such file"}
        if name == "write_files":
            changes = {str(f.get("path", "")): str(f.get("content", ""))
                       for f in (args.get("files") or []) if isinstance(f, dict)}
            if not changes:
                return {"ok": False, "error": "no files given"}
            written = await appfs.write(project_id, org_id, changes)
            turn.log.append("wrote " + ", ".join(written))
            turn.app_changed = True
            return {"ok": True, "written": written}
        if name == "delete_file":
            gone = await appfs.delete(project_id, org_id, str(args.get("path", "")))
            if gone:
                turn.log.append("deleted " + str(args.get("path")))
                turn.app_changed = True
            return {"ok": gone}
    except appfs.AppError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": "unknown tool"}


class AgentUnavailable(RuntimeError):
    pass


@dataclass
class Turn:
    """Everything one turn changed. Routes persist it; the client renders it."""
    answers: dict
    site: dict
    thread: list
    reply: str = ""
    actions: list = field(default_factory=list)
    log: list = field(default_factory=list)
    posts: list = field(default_factory=list)
    calls: list = field(default_factory=list)     # (model, usage) per API call, for billing
    app_changed: bool = False
    tz: str | None = None
    drafts: object = None
    ideas: object = None


def trim(thread: list) -> list:
    """Keep the tail, starting at a plain user message so the API accepts it."""
    t = thread[-MAX_THREAD:]
    while t and not (t[0]["role"] == "user" and isinstance(t[0]["content"], str)):
        t = t[1:]
    return t


def visible(thread: list) -> list:
    """The conversation as the person saw it — text only, no tool plumbing."""
    out = []
    for m in thread:
        c = m["content"]
        if isinstance(c, str):
            out.append({"role": m["role"], "text": c})
        else:
            text = " ".join(b.get("text", "") for b in c if b.get("type") == "text").strip()
            if text and m["role"] == "assistant":
                out.append({"role": "assistant", "text": text})
    return out


async def _call(messages: list, tools: list, system: str, model: str, max_tokens: int = 2048) -> dict:
    from . import models
    try:
        return await models.complete(model, messages, tools, system, max_tokens)
    except models.ModelUnavailable as exc:
        log.warning("agent model call failed: %s", exc)
        if not any(models.configured(k) for k in models.REGISTRY):
            raise AgentUnavailable("the agent is not configured on this deployment")
        raise AgentUnavailable("The assistant is temporarily unavailable. Please try again in a moment.")
    except models.ModelRejected as exc:
        log.error("model rejected request: %s", exc)
        raise AgentUnavailable("The assistant hit a problem with this request. Please try rephrasing.")


APP_EXTRA = """
This project is a working WEB APP, not a marketing page. You write its code as files.
Rules for the code:
- Plain ES modules, no build step. Import only: 'preact', 'preact/hooks' and 'htm/preact'
  (use html`...` templates, not JSX). app.js is the entry and must render into
  document.getElementById('root'). Relative imports like './screens/list.js' work.
- Keep files focused and under ~300 lines: app.js for routing and layout, screens/*.js,
  components/*.js, lib/*.js.
- Save and load data only with window.creai.db.collection('name') which has list(), get(id),
  add(data), update(id, data), remove(id) (all async). Never use localStorage, cookies,
  eval or network calls to other sites. Show loading and empty states, and handle errors.
- Style with the built-in kit classes: shell, topbar (with nav buttons and aria-current),
  page, card, grid, stack, row, btn (ghost, danger), badge, stat, empty, toast; plus
  labelled inputs, selects, textareas and tables. Add a styles.css only for what the kit
  lacks. Colours and fonts come from the project theme (update_site palette/theme).
- Make it feel finished: real screens for the core flow, validation on forms, helpful
  empty states, and specific copy. No lorem ipsum, no fake data presented as real; sample
  data only if the person asks, and label it.
- Access when published: visitors can only do what app.json allows. Write it as
  {"collections": {"bookings": {"read": "owner", "write": "public"}}}. read = list/get,
  write = add new records, manage = edit/delete. Anything not listed is owner-only.
  Public forms (bookings, sign-ups, orders): write public, read owner. Public listings
  (menu, catalogue): read public. Never make personal data publicly readable. Visitor
  calls that aren't allowed fail with a clear error, so show a friendly message.
Workflow: list_files, then write_files with complete file contents (you may write several
files in one call), then reply briefly with what the app does and one question. If the
person reports a preview error, read the file named in it and fix the cause.
"""

TOOL_WRITE_FILES = {
    "name": "write_files",
    "description": "Create or replace app files. Each file's full content is required.",
    "input_schema": {"type": "object", "properties": {"files": {"type": "array", "items": {
        "type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"]}}}, "required": ["files"]},
}
TOOL_READ_FILE = {
    "name": "read_file",
    "description": "Read one app file.",
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
}
TOOL_LIST_FILES = {
    "name": "list_files",
    "description": "List the app's files with their sizes.",
    "input_schema": {"type": "object", "properties": {}},
}
TOOL_DELETE_FILE = {
    "name": "delete_file",
    "description": "Delete an app file (not app.js).",
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
}

EDIT_EXTRA = """
This project is the person's EXISTING website on another platform, not a CreAI-generated
page: ignore the update_site instructions above and never call update_site.
"""


async def run(text: str, answers: dict | None, *, project: bool = False,
              queue_posts=None, model: str | None = None, intent: str = "build",
              bridge=None, marketing: bool = False, marketing_only: bool = False,
              app: tuple | None = None, tz: str | None = None, drafts=None, ideas=None) -> Turn:
    """One conversational turn.

    `answers` is the draft's or project's stored JSON (site spec, facts, thread).
    `queue_posts` is supplied only for signed-in projects; it is already bound to
    that project and workspace, so the model never names either.
    """
    model = model or settings.agent_model
    answers = dict(answers or {})
    turn = Turn(answers=answers,
                site=site_spec.merge(answers.get("site"), {}),
                thread=trim(list(answers.get("_thread") or [])))

    intent = intent if intent in INTENTS else "build"
    turn.tz = tz
    system = SYSTEM
    if tz:
        from datetime import datetime
        local = datetime.now(_zone(tz))
        system += (f"\nThe person's timezone is {tz}; it is now {local:%A %d %B %Y, %H:%M} there "
                   f"(UTC{local:%z}). Plan and describe times in their local time, and give every "
                   f"scheduled_for with that offset.\n")
    if bridge is not None:
        system += EDIT_EXTRA + bridge.prompt()
    if marketing:
        system += MARKET_EXTRA if marketing_only or bridge is not None else MARKET_ALSO
    brand_tools = [TOOL_READ_WEBSITE, TOOL_SAVE_BRAND]
    if app is not None:
        system += APP_EXTRA
    if intent == "build" and app is not None:
        tools = [TOOL_LIST_FILES, TOOL_READ_FILE, TOOL_WRITE_FILES, TOOL_DELETE_FILE,
                 TOOL_UPDATE_SITE, TOOL_GENERATE_IMAGE, TOOL_SAVE_ANSWER, TOOL_SUGGEST]
        if ideas is not None:
            tools.append(TOOL_SUGGEST_IMPROVEMENTS)
    elif intent == "build" and bridge is not None:
        from .webflow import TOOL_DEFS
        tools = list(TOOL_DEFS) + [TOOL_SAVE_ANSWER, TOOL_SUGGEST] + brand_tools
        if queue_posts is not None:
            tools.append(TOOL_DRAFT_POSTS)
            if drafts is not None:
                tools.append(TOOL_REVISE_POST)
            system += PROJECT_EXTRA
    elif intent == "build" and marketing_only:
        tools = [TOOL_SAVE_ANSWER, TOOL_SUGGEST] + brand_tools
        if queue_posts is not None:
            tools.append(TOOL_DRAFT_POSTS)
            if drafts is not None:
                tools.append(TOOL_REVISE_POST)
            system += PROJECT_EXTRA
    elif intent == "build":
        tools = [TOOL_UPDATE_SITE, TOOL_GENERATE_IMAGE, TOOL_SAVE_ANSWER, TOOL_SUGGEST] + brand_tools
        if ideas is not None:
            tools.append(TOOL_SUGGEST_IMPROVEMENTS)
        if project and queue_posts is not None:
            tools.append(TOOL_DRAFT_POSTS)
            if drafts is not None:
                tools.append(TOOL_REVISE_POST)
            system += PROJECT_EXTRA
    else:
        # Chat and Plan never touch the site: they get no editing tools at all.
        tools = []
        system += CHAT_EXTRA if intent == "chat" else PLAN_EXTRA
    facts = {k: v for k, v in answers.items()
             if not k.startswith("_") and k not in ("site", "brand", "source", "marketing")}
    if answers.get("brand"):
        system += "\nBrand kit: " + json.dumps(answers["brand"])[:3000]
    from datetime import datetime, timezone
    system += "\nCurrent date and time (UTC): " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    system += ("\nCurrent site spec: " + json.dumps(turn.site)
               + "\nKnown facts: " + json.dumps(facts))
    turn.drafts = drafts
    turn.ideas = ideas
    if ideas is not None and TOOL_SUGGEST_IMPROVEMENTS in tools:
        system += IMPROVE_EXTRA.replace("{skipped}", json.dumps(await ideas.skipped())[:800])
    if drafts is not None and TOOL_REVISE_POST in tools:
        waiting = await drafts.list()
        if waiting:
            system += "\nDrafted posts not yet out (use revise_post with these ids): " + json.dumps(
                [{k: (v[:160] if k == "text" else v) for k, v in d.items()} for d in waiting])[:4000]

    turn.thread.append({"role": "user", "content": text[:MAX_USER_CHARS]})

    for _ in range(APP_MAX_STEPS if app is not None else MAX_STEPS):
        out = await _call(turn.thread, tools, system, model, 16000 if app is not None else 2048)
        turn.calls.append((out.get("model") or model, out.get("usage") or {}))
        content = out.get("content", [])
        turn.thread.append({"role": "assistant", "content": content})
        uses = [b for b in content if b.get("type") == "tool_use"]
        if not uses:
            turn.reply = " ".join(b.get("text", "") for b in content
                                  if b.get("type") == "text").strip()
            break
        results = []
        for u in uses:
            result = await _tool(turn, u["name"], u.get("input") or {}, queue_posts, bridge, app)
            results.append({"type": "tool_result", "tool_use_id": u["id"],
                            "content": json.dumps(result)})
        turn.thread.append({"role": "user", "content": results})
    else:
        turn.reply = turn.reply or "I've made those changes — have a look at the preview."

    if intent == "plan" and turn.reply:
        turn.actions.append({"kind": "build_plan", "label": "Build this plan"})
    turn.answers["site"] = turn.site
    turn.answers["_thread"] = trim(turn.thread)
    return turn


async def _tool(turn: Turn, name: str, args: dict, queue_posts, bridge=None, app=None) -> dict:
    try:
        if app is not None and name in ("list_files", "read_file", "write_files", "delete_file"):
            return await _app_tool(turn, name, args, *app)
        if name.startswith("webflow_") and bridge is not None:
            return await bridge.handle(name, args, turn)
        if bridge is not None and name == "update_site":
            return {"ok": False, "error": "this is an existing Webflow site; use the webflow_* tools"}

        if name == "update_site":
            turn.site = site_spec.merge(turn.site, args)
            changed = ", ".join(k for k in args) or "nothing"
            turn.log.append(f"updated site · {changed}")
            layout, theme = site_spec.design_of(turn.site)
            return {"ok": True, "site": turn.site, "design": {"layout": layout, "theme": theme},
                    "quality": site_spec.critique(turn.site)}

        if name == "suggest_improvements":
            if turn.ideas is None:
                return {"ok": False, "error": "not available here"}
            added = await turn.ideas.add([i for i in (args.get("ideas") or []) if isinstance(i, dict)])
            if added:
                turn.log.append(f"suggested {added} improvement{'s' if added != 1 else ''}")
            return {"ok": True, "added": added}

        if name == "revise_post":
            if turn.drafts is None:
                return {"ok": False, "error": "posts can't be changed here"}
            from datetime import datetime
            from . import posts as post_svc
            pid = int(args.get("id") or 0)
            if args.get("discard"):
                ok = await turn.drafts.discard(pid)
                if ok:
                    turn.log.append(f"discarded post {pid}")
                return {"ok": ok}
            image_url = None
            if args.get("image_prompt"):
                from . import images
                if images.configured():
                    try:
                        image_url = await images.generate(str(args["image_prompt"]), "square")
                        turn.calls.append((images.media_key(), {"images": 1}))
                    except images.ImageError as exc:
                        return {"ok": False, "error": f"couldn't make the new picture: {exc}"}
            when = None
            if args.get("scheduled_for"):
                iso = _when(args["scheduled_for"], turn.tz)
                if not iso:
                    return {"ok": False, "error": "scheduled_for must be in the next year"}
                when = datetime.fromisoformat(iso)
            try:
                out = await turn.drafts.update(pid, text=args.get("text"), scheduled_for=when,
                                               link=args.get("link"), image_url=image_url)
            except post_svc.PostError as exc:
                return {"ok": False, "error": str(exc)}
            turn.log.append(f"revised post {pid}")
            turn.actions.append({"kind": "review_posts", "label": "Review drafts"}) \
                if not any(a.get("kind") == "review_posts" for a in turn.actions) else None
            return {"ok": True, **out}

        if name == "generate_image":
            from . import images
            if not images.configured():
                return {"ok": False, "error": "image generation isn't switched on; use generative artwork"}
            try:
                url = await images.generate(str(args.get("prompt", "")), args.get("shape") or "landscape")
            except images.ImageError as exc:
                return {"ok": False, "error": str(exc)}
            turn.calls.append((images.media_key(), {"images": 1}))
            turn.log.append("created an image")
            return {"ok": True, "url": url}

        if name == "save_answer":
            key = str(args.get("key", ""))[:40].strip().lower().replace(" ", "_")
            if not key or key.startswith("_") or key == "site":
                return {"ok": False, "error": "invalid key"}
            turn.answers[key] = str(args.get("value", ""))[:500]
            return {"ok": True}

        if name == "suggest_action":
            kind = args.get("kind")
            if kind not in ACTIONS:
                return {"ok": False, "error": "unknown action"}
            action = {"kind": kind, "label": str(args.get("label", ""))[:48] or ACTIONS[kind]}
            if kind == "connect_domain" and args.get("domain"):
                action["domain"] = str(args["domain"])[:253]
            if action not in turn.actions:
                turn.actions.append(action)
            return {"ok": True, "shown_to_person": True}

        if name == "read_website":
            from . import brand
            try:
                page = await brand.read(str(args.get("url", "")))
            except brand.FetchError as exc:
                return {"ok": False, "error": str(exc)}
            turn.log.append("read " + page["url"].split("//", 1)[-1][:60])
            return {"ok": True, **page}

        if name == "save_brand_kit":
            kit = {
                "name": str(args.get("name", ""))[:80],
                "voice": str(args.get("voice", ""))[:400],
                "audience": str(args.get("audience", ""))[:300],
                "services": [str(x)[:80] for x in (args.get("services") or [])][:20],
                "colors": [c for c in (args.get("colors") or []) if isinstance(c, str)
                           and re.fullmatch(r"#[0-9a-fA-F]{6}", c)][:6],
                "links": [{"label": str(l.get("label", ""))[:60], "url": str(l.get("url", ""))[:300]}
                          for l in (args.get("links") or []) if isinstance(l, dict)
                          and str(l.get("url", "")).startswith(("http://", "https://"))][:12],
                "website": str(args.get("website", ""))[:300],
            }
            turn.answers["brand"] = kit
            turn.log.append("saved brand kit")
            return {"ok": True}

        if name == "draft_posts" and queue_posts is not None:
            posts = []
            for p in (args.get("posts") or [])[:14]:
                if isinstance(p, dict) and p.get("text"):
                    posts.append({"network": str(p.get("network", ""))[:20],
                                  "text": str(p["text"])[:2200],
                                  "when": str(p.get("when", ""))[:60],
                                  "link": str(p.get("link", ""))[:300],
                                  "scheduled_for": _when(p.get("scheduled_for"), turn.tz),
                                  "image_prompt": str(p.get("image_prompt", ""))[:1500],
                                  "image_shape": p.get("image_shape") if p.get("image_shape") in
                                  ("square", "portrait", "landscape") else "square"})
            from . import images
            wanted = [(i, x["image_prompt"], x["image_shape"]) for i, x in enumerate(posts) if x["image_prompt"]]
            if wanted and images.configured():
                made = await images.generate_many(wanted)
                for i, url in made.items():
                    posts[i]["media"] = [{"type": "image", "url": url}]
                if made:
                    turn.calls.append((images.media_key(), {"images": len(made)}))
                    turn.log.append(f"created {len(made)} image{'s' if len(made) != 1 else ''}")
            ids = await queue_posts(posts)
            turn.posts.extend(ids)
            turn.log.append(f"drafted {len(ids)} posts for approval")
            if not any(a["kind"] == "review_posts" for a in turn.actions):
                turn.actions.append({"kind": "review_posts", "label": "Review drafts"})
            return {"ok": True, "queued": len(ids), "published": 0,
                    "unscheduled": sum(1 for p in posts if not p["scheduled_for"])}

        return {"ok": False, "error": f"unknown tool {name}"}
    except Exception as exc:          # a bad tool call should not end the turn
        log.exception("agent tool %s failed", name)
        return {"ok": False, "error": str(exc)[:200]}
