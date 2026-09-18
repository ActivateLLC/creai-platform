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
MAX_STEPS = 10
APP_MAX_STEPS = 24
APP_MAX_TOKENS = 32000
MAX_THREAD = 40          # messages kept per draft or project
MAX_USER_CHARS = 4000

ACTIONS = {
    "build_plan": "Build the plan just proposed",
    "sign_in": "Create an account (needed before anything is registered or published)",
    "connect_domain": "Connect a domain the person already owns",
    "review_posts": "Open the post drafts waiting for approval",
}

SYSTEM = """You are Creai, the agent inside the Creai platform. You build websites, web apps and \
marketing for small businesses, and you take real pride in the craft. Your work is the business's \
public face, so it should look like it was made by a senior designer and engineer who cared: \
specific, polished, honest, and working.

How you work, every time:
1. Understand. Read the request, the current site spec, known facts, attachments and the brand kit. \
Work out who this business serves and what a visitor needs to do.
2. Decide. Pick the smallest set of changes that fully does the job. On a first message, build a \
complete first version straight away rather than asking questions first. Build on every turn: a \
reply that only plans, only asks, or only offers a button is a turn the person paid for and got \
nothing they can look at. If a detail is missing, choose a sensible placeholder, build, and say \
what you assumed.
   When people need to sign in and see their own things — a client portal, bookings, orders, \
invoices, documents, memberships, a dashboard "for each customer" — that is an APP project with \
accounts, which Creai builds natively (window.creai.auth, and "own" access in app.json). Build it \
here. Never send them to a third party's portal (QuickBooks, Stripe, Xero, FreshBooks, Wave) for \
the sign-in itself; linking out is only right when the person explicitly asks to keep using a tool \
they already have. If the project is currently a site and the ask needs accounts, say so plainly \
and create the app rather than writing a page that advertises a portal that does not exist.
3. Build. Make real edits with your tools; never describe changes you didn't make.
4. Verify. Read what your tools return. Fix every item in update_site's quality list, and every \
problem check_app reports, before you reply. Don't stop at "probably fine".
5. Report. In a few plain sentences say what you changed and why it helps, name anything you \
couldn't do, and ask the ONE question that would most improve the result.

Quality bar:
- Copy is concrete and in the owner's voice: what they do, for whom, where, and what happens next. \
No stock phrasing (elevate, unlock, seamless, one-stop, welcome to, passionate about).
- Design fits the business: choose layout, theme, motion and a palette with strong contrast on \
purpose. Two different businesses should never look alike. Use the section kinds that tell this \
business's story; stats only with real numbers the person gave.
- Honesty over polish: never invent prices, addresses, phone numbers, reviews, licences or years in \
business. Use a placeholder like [PRICE] and say so. Testimonials only if the person supplies them.
- Their own material wins: attached photos beat generated ones (hero_image, gallery), a video can be \
the hero (hero_video), and menus, price lists or brochures are the source of truth. Describe only \
what you can actually see, and ask before featuring people's faces prominently.

What you can and can't do:
- The person sees a live preview beside this chat; update_site re-renders it.
- Use save_answer for facts they tell you (audience, location, services, tone).
- You never spend money, publish, register domains or connect accounts yourself. The person does \
that with a tap: publishing and domains live under Your site (Domain) and the launch checklist. \
When one is the natural next step, call suggest_action and say what it does and what it costs if known.

Style: plain, warm and calm. No exclamation marks, no markdown headings. Keep replies short: what \
changed, why, and one question.
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
                           "description": "URL of an image made with generate_image or a photo the person "
                                          "attached. Leave empty for generative artwork from the palette."},
            "hero_video": {"type": "string",
                           "description": "URL of a video the person attached, shown muted and looping in "
                                          "the hero. Set to empty to remove."},
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


async def _game_tool(turn, name, args, project_id, org_id) -> dict:
    """Godot file tools, plus the build. Same shapes as the app tools, different rules."""
    from . import godot
    try:
        if name == "list_files":
            fs = await godot.files(project_id, org_id)
            return {"files": [{"path": p, "chars": len(c)} for p, c in fs.items()]}
        if name == "read_file":
            fs = await godot.files(project_id, org_id)
            path = str(args.get("path", ""))
            return {"path": path, "content": fs[path]} if path in fs else {"ok": False, "error": "no such file"}
        if name == "write_files":
            changes = {str(f.get("path", "")): str(f.get("content", ""))
                       for f in (args.get("files") or []) if isinstance(f, dict)}
            if not changes:
                return {"ok": False, "error": "no files given"}
            written = await godot.write(project_id, org_id, changes)
            turn.log.append("wrote " + ", ".join(written))
            turn.app_changed = True
            turn.app_checked = False
            return {"ok": True, "written": written}
        if name == "check_game":
            out = godot.review(await godot.files(project_id, org_id))
            turn.app_checked = out["ok"]
            turn.log.append("checked the game · " + ("all clear" if out["ok"]
                            else f"{len(out['problems'])} to fix"))
            return out
        if name == "build_game":
            return await _build_game(turn, project_id, org_id)
        if name == "delete_file":
            gone = await godot.delete(project_id, org_id, str(args.get("path", "")))
            if gone:
                turn.log.append("deleted " + str(args.get("path")))
                turn.app_changed = True
            return {"ok": gone}
    except godot.GameError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": "unknown tool"}


BUILD_WAIT = 150          # a turn will wait this long for an export before handing off to the UI


async def _build_game(turn, project_id: int, org_id: int) -> dict:
    """Run an export inside the turn so the agent can fix what the log says.

    Exports usually land inside a minute. If one runs long the turn stops waiting and
    says so — the job keeps going and the client watches its progress.
    """
    from . import billing, godot
    want = await godot.estimate(project_id, org_id)
    if await billing.balance(org_id) < want:
        return {"ok": False, "error": f"a build costs about {want} credits and the balance is short; "
                                      "tell the person to top up — nothing is lost"}
    job = await godot.start(project_id, org_id, None)
    turn.log.append("building the game")
    task = asyncio.create_task(godot.run(job["id"], project_id, org_id))
    for _ in range(BUILD_WAIT // 3):
        await asyncio.sleep(3)
        st = await godot.status(job["id"], project_id, org_id)
        if st and st["state"] in ("done", "failed"):
            turn.game_built = st["state"] == "done"
            turn.log.append("built the game · " + ("ready to play" if turn.game_built
                            else "the export failed"))
            if st["state"] == "failed":
                return {"ok": False, "build": st, "error": st["error"]}
            return {"ok": True, "build": st,
                    "note": "exported and ready; publishing is the person's call"}
    task.add_done_callback(lambda t: t.exception())
    return {"ok": True, "build": job, "note": "still exporting; it will finish on its own"}


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
            turn.app_checked = False
            return {"ok": True, "written": written}
        if name == "check_app":
            fs = await appfs.files(project_id, org_id)
            out = appfs.review(fs)
            from ..core.db import conn
            async with conn() as c:
                errs = await c.fetch(
                    """SELECT message FROM app_errors WHERE project_id=$1 AND created_at >
                         (SELECT COALESCE(MAX(updated_at),'epoch') FROM project_files WHERE project_id=$1)
                       ORDER BY id DESC LIMIT 5""", project_id)
            out["preview_errors"] = [e["message"] for e in errs]
            out["ok"] = out["ok"] and not errs
            turn.app_checked = out["ok"]
            turn.log.append("checked the app · " + ("all clear" if out["ok"] else
                            f"{len(out['problems']) + len(errs)} to fix"))
            return out
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
    app_checked: bool = False
    game_built: bool = False
    reviewed: bool = False
    review: dict | None = None
    site_issues: list = field(default_factory=list)
    site_changed: bool = False
    nudges: int = 0
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
        if m.get("internal"):
            continue
        if isinstance(c, str):
            item = {"role": m["role"], "text": c}
            if m.get("assets"):
                item["assets"] = m["assets"]
            out.append(item)
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
This project is a working WEB APP. You are its engineer: you write the code as files, and it has to
run the first time someone opens it. Think like a senior front-end engineer who owns this product.

Before writing: work out the core job the app does, its screens, and the data it keeps (collection
names, fields, who can see them). For anything beyond a small change, sketch that plan in a sentence
or two in your head, then build it completely. Don't leave TODOs or half-built screens.

Platform rules (the preview enforces them):
- Plain ES modules, no build step. Import only 'preact', 'preact/hooks' and 'htm/preact'. Use
  html`...` tagged templates, never JSX; components render as html`<${Name} prop=${x} />`.
- app.js is the entry and renders into document.getElementById('root'). Relative imports must
  include the .js extension ('./screens/list.js'). Every named import must be exported by that file.
- Structure: app.js for layout and routing (a small state-based router is fine), screens/*.js,
  components/*.js, lib/*.js. Keep files focused, under about 300 lines.
- Data only through window.creai.db.collection('name'): list(), get(id), add(data),
  update(id, data), remove(id), all async. No localStorage, cookies, eval or calls to other sites.
- Styling: the kit classes shell, topbar (nav buttons with aria-current), page, card, grid, stack,
  row, btn (ghost, danger), badge, stat, empty, toast, plus labelled inputs, selects, textareas and
  tables. Add styles.css only for what the kit lacks. Colours and fonts come from the project theme
  (update_site palette/theme).
- Access once published lives in app.json:
  {"collections": {"bookings": {"read": "own", "write": "user", "manage": "own"}}}
  read = list/get, write = add records, manage = edit/delete. Four levels, least to most trusted:
    public  anyone, signed in or not
    user    any signed-in person (a shared list members can all see)
    own     signed-in, and only the rows that person created
    owner   the business owner only (the default for anything unlisted)
  Public forms: write public, read owner. Public listings: read public. Never make personal data
  publicly readable, and show a friendly message when a visitor call isn't allowed.
- Accounts, when the app needs people to sign in — bookings, orders, memberships, portals,
  anything where someone returns to their own things. window.creai.auth:
    await creai.auth.signUp(email, password, name)  -> user, and signs them in
    await creai.auth.signIn(email, password)        -> user
    await creai.auth.me()                           -> user or null, never throws
    creai.auth.signOut()
  Accounts belong to this app alone; they are not Creai accounts. Build the sign-in screen with
  the kit, call me() once on load to decide what to render, keep a signed-out view that explains
  what the app is, and put the person's name and a sign-out control in the topbar. Use "own" for
  anything personal so one member never sees another's rows; the server enforces it, but choose
  it deliberately. Show the error text from a failed sign-in exactly as it comes back.

Games:
- For a game, import { canvas, loop, keys, tapped, pointer, sprite, loadAll, beep, save, leaderboard,
  rand, clamp, hits } from 'creai/game'. It gives you a fixed-step loop (same speed on every machine,
  paused with the tab), keyboard, pointer and touch input, image loading, simple sound, saves and a
  leaderboard through the app's own data.
- Phaser ('phaser') suits tile and physics games; three ('three', plus 'three/addons/...') suits 3D.
  Plain canvas through the kit is the lightest and usually the right choice.
- A game still needs the things people forget: a start screen that says how to play, a pause, a
  game-over with the score and a way to play again, touch controls that work with one thumb, and a
  score saved so it survives a refresh. Keep the first playable loop small and make it feel good.

Craft:
- Every data call has a loading state, an empty state that says what to do next, and error handling
  that tells the person what went wrong in plain words.
- Forms validate before saving, disable the submit button while saving, and confirm success.
- Accessible by default: real buttons and labels, visible focus, sensible headings, keyboard use.
- Specific copy for this business. No lorem ipsum; no fake records presented as real (sample data
  only if asked, and labelled).
- Small, readable functions with clear names. No dead code.

Workflow: list_files and read_file what you'll change, write_files with complete contents (several
files per call is fine), then ALWAYS call check_app. Fix every problem it lists and check again
until it passes; consider its notes. Then reply: what the app does now, what you checked, and one
question. If the person reports a preview error, read the file it names and fix the root cause.
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
TOOL_CHECK_APP = {
    "name": "check_app",
    "description": "Review the app like a careful engineer before you reply: missing files, wrong imports "
                   "or exports, unavailable libraries, JSX, unclosed templates, invalid app.json, plus any "
                   "errors the live preview reported since your last change. Call after every write.",
    "input_schema": {"type": "object", "properties": {}},
}

TOOL_DELETE_FILE = {
    "name": "delete_file",
    "description": "Delete an app file (not app.js).",
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
}

GAME_EXTRA = """
This project is a real GODOT GAME, exported to WebAssembly and played in a browser. You are its
engineer: you write Godot source files, and an export has to succeed and be fun the first time.

Platform rules (the builder enforces them):
- Godot 4.5. Text source only: project.godot, .tscn scenes, .gd scripts, .tres, .gdshader, .svg.
  No binary art or audio yet — draw with code (draw_circle, draw_rect, draw_line, Polygon2D,
  ColorRect) and make sounds with AudioStreamGenerator, or keep it silent.
- project.godot must set run/main_scene, config/features PackedStringArray("4.5", "GL Compatibility")
  and renderer/rendering_method="gl_compatibility" — the web export is far more reliable that way.
- Every res:// path you reference in a scene or preload must be a file you actually wrote.
- Scenes are text .tscn files with a [gd_scene] header and correctly numbered load_steps.
- GDScript indents with TABS. Never mix tabs and spaces. Every func declaration ends with ':'.
- The game must work on a PHONE: touch input as a first-class control scheme, not a keyboard
  afterthought. Handle InputEventScreenTouch/Drag, size the viewport for portrait or landscape
  deliberately, and set window/stretch so it scales.

A game needs the things people forget: a start screen that says how to play, a pause, a game-over
with the score and a way to play again, and a score that survives a refresh. Keep the first playable
loop small and make it feel good before adding a second system.

Workflow: list_files and read_file what you'll change, write_files with complete contents, then
check_game and fix everything it lists. When it passes and the change is worth playing, call
build_game — it exports the real engine build and takes about a minute. If the export fails, read
the log it returns, fix the root cause, and build again. Then reply: what the game does now, what
you checked, and one question.

Builds cost credits by the minute, so build when there is something new to play, not after every
edit.
"""

TOOL_CHECK_GAME = {
    "name": "check_game",
    "description": "Review the game project before spending a build on it: missing project.godot "
                   "keys, a main scene that doesn't exist, res:// references to files that were "
                   "never written, malformed scenes, GDScript indentation and syntax slips. "
                   "Call after every write and before build_game.",
    "input_schema": {"type": "object", "properties": {}},
}
TOOL_BUILD_GAME = {
    "name": "build_game",
    "description": "Export the game to WebAssembly with the real Godot engine and wait for it. "
                   "Takes about a minute and costs build credits. Returns the export log so you "
                   "can fix what failed. Build when there is something new worth playing.",
    "input_schema": {"type": "object", "properties": {}},
}


EDIT_EXTRA = """
This project is the person's EXISTING website on another platform, not a Creai-generated
page: ignore the update_site instructions above and never call update_site.
"""


async def run(text: str, answers: dict | None, *, project: bool = False,
              queue_posts=None, model: str | None = None, intent: str = "build",
              bridge=None, marketing: bool = False, marketing_only: bool = False,
              app: tuple | None = None, game: tuple | None = None,
              tz: str | None = None, drafts=None, ideas=None,
              attachments: tuple | None = None, reviewer=None) -> Turn:
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
    if game is not None:
        system += GAME_EXTRA
    if intent == "build" and game is not None:
        tools = [TOOL_LIST_FILES, TOOL_READ_FILE, TOOL_WRITE_FILES, TOOL_CHECK_GAME,
                 TOOL_BUILD_GAME, TOOL_DELETE_FILE, TOOL_SAVE_ANSWER, TOOL_SUGGEST]
    elif intent == "build" and app is not None:
        tools = [TOOL_LIST_FILES, TOOL_READ_FILE, TOOL_WRITE_FILES, TOOL_CHECK_APP, TOOL_DELETE_FILE,
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

    blocks, note, record = attachments or ([], "", [])
    typed = text[:MAX_USER_CHARS]
    if blocks or note:
        turn.thread.append({"role": "user", "content": blocks + [{"type": "text", "text": (note + "\n\n" + typed).strip()}]})
    else:
        turn.thread.append({"role": "user", "content": typed})
    user_at = len(turn.thread) - 1

    for _ in range(APP_MAX_STEPS if (app is not None or game is not None) else MAX_STEPS):
        out = await _call([{"role": m["role"], "content": m["content"]} for m in turn.thread],
                          tools, system, model,
                          APP_MAX_TOKENS if (app is not None or game is not None) else 4096)
        turn.calls.append((out.get("model") or model, out.get("usage") or {}))
        content = out.get("content", [])
        turn.thread.append({"role": "assistant", "content": content})
        uses = [b for b in content if b.get("type") == "tool_use"]
        if not uses:
            unfinished = None
            if game is not None and turn.app_changed and not turn.app_checked:
                unfinished = ("Before you reply: call check_game, fix every problem it lists, "
                              "and check again until it passes.")
            elif app is not None and turn.app_changed and not turn.app_checked:
                unfinished = ("Before you reply: call check_app, fix every problem it lists, "
                              "and check again until it passes.")
            elif app is None and turn.site_issues:
                unfinished = ("Before you reply: the quality check still lists issues: "
                              + " ".join(turn.site_issues[:4]) + " Fix them with update_site.")
            # A second pair of eyes on the finished build, once per turn.
            if reviewer is not None and not unfinished and not turn.reviewed and intent == "build" \
                    and (turn.app_changed or turn.site_changed):
                turn.reviewed = True
                found = await reviewer(turn)
                if found and found.get("findings"):
                    turn.review = found
                    turn.log.append("reviewed on phone and desktop · "
                                    + (f"{found['blockers']} to fix" if found["blockers"] else "looks good"))
                    if found["verdict"] == "fix_first":
                        turn.thread[-1]["internal"] = True
                        turn.thread.append({"role": "user", "internal": True,
                                            "content": reviewer.instruction(found)})
                        continue
                elif found:
                    turn.log.append("reviewed on phone and desktop · looks good")
            if unfinished and turn.nudges < 2:
                turn.nudges += 1
                turn.thread[-1]["internal"] = True       # the premature reply isn't shown
                turn.thread.append({"role": "user", "content": unfinished, "internal": True})
                continue
            turn.reply = " ".join(b.get("text", "") for b in content
                                  if b.get("type") == "text").strip()
            break
        results = []
        for u in uses:
            result = await _tool(turn, u["name"], u.get("input") or {}, queue_posts, bridge, app, game)
            results.append({"type": "tool_result", "tool_use_id": u["id"],
                            "content": json.dumps(result)})
        turn.thread.append({"role": "user", "content": results})
    else:
        turn.reply = turn.reply or "I've made those changes — have a look at the preview."

    if intent == "plan" and turn.reply:
        turn.actions.append({"kind": "build_plan", "label": "Build this plan"})
    turn.answers["site"] = turn.site
    # The saved conversation keeps what was typed and which files were attached — never file bytes.
    saved = {"role": "user", "content": typed}
    if record:
        saved["assets"] = record
    turn.thread[user_at] = saved
    turn.answers["_thread"] = trim(turn.thread)
    return turn


async def _tool(turn: Turn, name: str, args: dict, queue_posts, bridge=None, app=None, game=None) -> dict:
    try:
        if game is not None and name in ("list_files", "read_file", "write_files", "delete_file",
                                         "check_game", "build_game"):
            return await _game_tool(turn, name, args, *game)
        if app is not None and name in ("list_files", "read_file", "write_files", "delete_file", "check_app"):
            return await _app_tool(turn, name, args, *app)
        if name.startswith("webflow_") and bridge is not None:
            return await bridge.handle(name, args, turn)
        if bridge is not None and name == "update_site":
            return {"ok": False, "error": "this is an existing Webflow site; use the webflow_* tools"}

        if name == "update_site":
            turn.site = site_spec.merge(turn.site, args)
            turn.site_changed = True
            changed = ", ".join(k for k in args) or "nothing"
            turn.log.append(f"updated site · {changed}")
            layout, theme = site_spec.design_of(turn.site)
            quality = site_spec.critique(turn.site)
            turn.site_issues = [q for q in quality if "automatic" not in q and "default colours" not in q]
            return {"ok": True, "site": turn.site, "design": {"layout": layout, "theme": theme},
                    "quality": quality}

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
