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
# Each picture is a queue round-trip of several seconds. Past a couple of them a
# turn runs long enough that the browser gives up mid-request, which reads to the
# person as a failure even though the work succeeded.
MAX_IMAGES_PER_TURN = 2
# Looking costs a render round-trip, so it is worth doing and worth bounding.
MAX_LOOKS_PER_TURN = 2
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
   A site button that says "Sign in", "Client portal" or "View your invoices" must reach a real
app: set cta_link (or a cta section's link) to "app", build the app and publish it. Until the app
is published that button only scrolls to contact, which is a dead end the visitor feels
immediately — so build the app first and the page second, never a page advertising a portal that
does not exist.
   When people need to sign in and see their own things — a client portal, bookings, orders, \
invoices, documents, memberships, a dashboard "for each customer" — that is an APP project with \
accounts, which Creai builds natively (window.creai.auth, and "own" access in app.json). Build it \
here. Never send them to a third party's portal (QuickBooks, Stripe, Xero, FreshBooks, Wave) for \
the sign-in itself; linking out is only right when the person explicitly asks to keep using a tool \
they already have. If the project is currently a SITE, you cannot build the app here — a site project holds no \
sign-in. Call start_app in that same turn to give them the button that creates it, then build the \
site part you can. Never repeat a plan you have already offered: if you have called start_app, \
say it is waiting on that button and move on.
3. Build. Make real edits with your tools; never describe changes you didn't make.
4. Verify. Read what your tools return, and LOOK at the result with the look tool before you call \
a build finished — spacing, hierarchy, contrast, crowding and whether the first screen earns the \
scroll are things you cannot judge from a spec. Fix what you see. Fix every item in update_site's quality list, and every \
problem check_app reports, before you reply. Don't stop at "probably fine".
5. Report. In a few plain sentences say what you changed and why it helps, name anything you \
couldn't do, and ask the ONE question that would most improve the result.

Quality bar:
- Draw the icons yourself, for this business. A services or steps item takes an "icon": SVG path
  data only, no elements or attributes — Creai supplies the <svg>, the 24x24 grid, the 1.75 stroke
  and the brand colour, so you supply geometry and nothing else.
  Draw on the 24 grid with about 2 units of padding, start with M, keep it to a handful of strokes,
  no fills, no text, and make it legible at 22px. A wrench for a plumber, a drill bit for a
  contractor, a comb for a barber — the object that business actually touches, not a generic gear.
  Simple and correct beats detailed and lumpy: think three or four confident strokes. If a shape
  won't come out cleanly, leave the icon out rather than shipping a smudge.
- Never use an emoji as an icon, in a button, a label, a list marker, a heading or a service
  name — not anywhere, on a site or in an app. They are a different typeface on every device,
  sit off the baseline, and cannot take the brand colour, so they make good work look amateur.
  In an app: import { createIcons, icons } from 'lucide' and use <i data-lucide="wrench"></i>.
  On a site: the built-in vector set. Typographic arrows (→) and dashes (—) are not emoji and
  are welcome.
- Never show a placeholder. No [Artist Name], {{business}} or <your name here> in anything a
  visitor would read, including the project title. If a detail is missing, pick a plausible real
  stand-in, use it everywhere consistently, and say in your reply what you chose and that they
  can correct it in a word.
- Copy is concrete and in the owner's voice: what they do, for whom, where, and what happens next. \
No stock phrasing (elevate, unlock, seamless, one-stop, welcome to, passionate about), and no
emoji anywhere in the copy.
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

TOOL_LOOK = {
    "name": "look",
    "description": "Look at what you just built, as a picture, on a phone and on a desktop. Use it "
                   "after a substantial build and before you claim it is done: you cannot judge "
                   "hierarchy, spacing, contrast, crowding or whether a hero actually lands "
                   "without seeing it. Then fix what you see and say what you changed.",
    "input_schema": {"type": "object", "properties": {
        "widths": {"type": "array", "items": {"type": "string", "enum": ["phone", "desktop"]},
                   "description": "Defaults to both."}}},
}

TOOL_START_APP = {
    "name": "start_app",
    "description": "Offer to start the app this business needs — a portal, bookings, orders, a "
                   "members area, anything where people sign in and see their own things. A site "
                   "project cannot hold sign-in, so this is the only way to build one. The person "
                   "gets a button that creates the app and opens it. Use it in the same turn you "
                   "explain what the app will do; never describe an app you have not offered.",
    "input_schema": {"type": "object", "properties": {
        "name": {"type": "string", "description": "What to call it, e.g. 'Client portal'"},
        "label": {"type": "string", "description": "The button, e.g. 'Build the client portal'"}},
        "required": ["name"]},
}

TOOL_GENRE = {
    "name": "game_rules",
    "description": "Get what somebody who has shipped this kind of game would tell you: the "
                   "shape of the loop, the numbers that matter, the mistake everyone makes "
                   "first, and what it takes to look right.\n"
                   "Call this BEFORE writing a game, once you know roughly what kind it is. A "
                   "runner needs a difficulty curve and coyote time; a puzzle needs undo and a "
                   "guarantee every board is solvable; a shooter needs telegraphed attacks and "
                   "a time-to-kill. Built the same way, each one is wrong in its own "
                   "particular manner.\n"
                   "Pass a visual theme too if the person suggested a look.",
    "input_schema": {"type": "object", "properties": {
        "genre": {"type": "string",
                  "enum": ["runner", "arcade", "puzzle", "platformer", "shooter",
                           "tower-defence", "idle", "rhythm", "local-versus"]},
        "theme": {"type": "string",
                  "enum": ["neon", "paper", "noir", "pastel", "terminal", "sunset"]}},
        "required": ["genre"]},
}

TOOL_BLUEPRINT = {
    "name": "use_blueprint",
    "description": "Build one of the things nearly every business needs, done properly: a CRM "
                   "for tracking leads and who to chase, or a content store for the prices, "
                   "posts and hours that change often.\n"
                   "Use this when someone says they need to track leads, customers, quotes or "
                   "follow-ups, or says they want to change prices or add posts themselves. "
                   "It gives you a brief carrying what makes these succeed or fail — a CRM with "
                   "more than one required field gets abandoned; a content store that becomes a "
                   "second editor defeats the point of Creai.\n"
                   "You still build it in their words, shaped to their trade. The blueprint is "
                   "judgement, not a template to paste.",
    "input_schema": {"type": "object", "properties": {
        "blueprint": {"type": "string", "enum": ["crm", "content"]},
        "business": {"type": "string", "description": "The business this is for"},
        "trade": {"type": "string", "description": "What they do, in their words"}},
        "required": ["blueprint", "business"]},
}

TOOL_PLAN_VIDEO = {
    "name": "plan_video",
    "description": "Write a video and offer to make it: an ad, a short, a clip for a channel. "
                   "You write the scenes; Creai buys the pictures, reads the lines aloud and cuts "
                   "it. The person gets a button that spends the credits.\n"
                   "Rules that decide whether anyone watches, in order of what they cost:\n"
                   "- The first scene shows the thing itself, mid-use. A title card first and most "
                   "people never reach the second scene.\n"
                   "- Never say the business name in the opening line; it reads as an advert. It "
                   "belongs in the last scene with what to do next.\n"
                   "- One idea per scene. Write for sound off: line and picture must carry it.\n"
                   "- Specific beats clever. Real jobs, real numbers, nothing invented — no prices, "
                   "awards or reviews the business has not given you.\n"
                   "- 15 to 30 seconds is right; 45 is the ceiling.\n"
                   "Sources: 'generated' needs a prompt and costs credits; 'footage' uses the "
                   "project's own site or app on screen, which is cheaper and more convincing; "
                   "'card' is words on black, for the ending.",
    "input_schema": {"type": "object", "properties": {
        "title": {"type": "string", "description": "What this video is, for the list"},
        "shape": {"type": "string", "enum": ["vertical", "square", "wide"],
                  "description": "vertical for Shorts, Reels and TikTok"},
        "scenes": {"type": "array", "description": "In order. Each is one idea.",
                   "items": {"type": "object", "properties": {
                       "line": {"type": "string", "description": "What is said over it"},
                       "source": {"type": "string", "enum": ["generated", "footage", "card"]},
                       "prompt": {"type": "string",
                                  "description": "For 'generated': subject, setting, light, style. "
                                                 "No text, logos or real faces."},
                       "footage": {"type": "string", "enum": ["site", "app", "game"],
                                   "description": "For 'footage': what to film"},
                       "title": {"type": "string", "description": "For 'card': the big words"},
                       "subtitle": {"type": "string", "description": "For 'card': the small line"},
                       "seconds": {"type": "number",
                                   "description": "A rough guess; the real length follows the line"}},
                       "required": ["source"]}}},
        "required": ["title", "scenes"]},
}

TOOL_GENERATE_IMAGE = {
    "name": "generate_image",
    "description": "Create an image. Returns a URL: put it in hero_image, a gallery item, or "
                   "straight into an app's markup or CSS (backgrounds, empty states, cards, "
                   "textures). Describe subject, setting, light and style; no text, logos or real "
                   "people's faces. Costs a few credits.",
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
            from . import quality as quality_svc
            await quality_svc.record(org_id, project_id, "game",
                                     (out.get("problems") or []) + (out.get("notes") or []))
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
            from . import quality as quality_svc
            await quality_svc.record(org_id, project_id, "app",
                                     (out.get("problems") or []) + (out.get("notes") or []))
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


class LiveLog(list):
    """The turn's log, which also reports each line as it is added.

    Made a list subclass rather than adding a say() method so that every existing
    `turn.log.append(...)` streams without being touched — there are dozens, and
    a migration that misses one leaves a silent gap in the middle of a build,
    which is the exact fault this fixes.
    """

    key: str | None = None

    def append(self, item) -> None:
        super().append(item)
        if self.key:
            from . import progress
            import asyncio
            try:
                asyncio.get_running_loop().create_task(progress.say(self.key, str(item)))
            except RuntimeError:
                pass          # outside a loop (tests); the log still works


@dataclass
class Turn:
    """Everything one turn changed. Routes persist it; the client renders it."""
    answers: dict
    site: dict
    thread: list
    reply: str = ""
    actions: list = field(default_factory=list)
    log: list = field(default_factory=LiveLog)
    posts: list = field(default_factory=list)
    calls: list = field(default_factory=list)     # (model, usage) per API call, for billing
    app_changed: bool = False
    app_checked: bool = False
    looks: int = 0                # renders of its own work, capped like pictures
    images_made: int = 0          # capped per turn: pictures are slow, and a turn
                                  # that outlives the browser looks like a crash
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


VIDEO_EXTRA = """
This project is a VIDEO. You are its director: you decide what is on screen, what is said, and
what order it lands in. Think like somebody who has watched their own ads get scrolled past and
worked out why.

Where the money is won or lost, in order:

- The first three seconds are a gate. Roughly a quarter of people reach second three at all; the
  rest never see your demonstration, your offer or your name however good they are. Everything
  downstream is capped here.
- The first two seconds must MOVE. A still frame with text on it does not stop a thumb. Something
  enters, changes, or a person speaks. If the opening shot could be a screenshot, it is wrong.
- Holding matters more than hooking. Two ads with the same opening can differ several times over
  in what they earn, because one keeps people to the end and the other empties at second six.
  Every scene must make the next one worth waiting for.
- The brand name never appears in the opening line. It is the fastest signal that this is an
  advertisement, and the defences go up.
- The last third states what to do and why now. A name and a domain is a signature, not a reason.

The angles. A set of ads that are all the same argument is one idea tested five ways. Across a
campaign, cover: the demonstration, the problem sat in before anything is offered, the outcome
shown first and explained after, the comparison with carrying on as they are, and real proof from
a real customer. Never write the last one — a testimonial nobody gave is the one mistake that
costs more than any variant wins.

Showing beats claiming. Do not say it saves time; show the thing happening in the time it takes
to watch. For anything Creai builds, the strongest shot is the transformation itself: words a
person said becoming software they can use. Name it, film it, let it land under the line that
describes it.

Making it:
- Scenes carry their own length. Write the line first; the scene is as long as the line takes to
  say plus a beat. Never squeeze speech into a slot — that is why AI ads sound rushed at the end
  of every sentence.
- Captions always, burned in. Most of a feed is watched on mute. Large, high contrast, and never
  over the thing they describe.
- Vertical by default. 1080x1920 for Shorts, Reels and TikTok.
- Real product footage wherever the product is the point. Generated footage is for establishing
  shots — a van at dusk, a counter at closing — where nobody speaks and nothing is claimed.
- A generated or stock person may appear and must never claim. They can be in the van. They
  cannot tell the viewer the product works.
- One idea per scene. A line carrying two ideas carries none.
- Nothing claimed that the product does not do. No invented prices, awards, ratings or customer
  numbers. If it has not happened, it does not go in.

Direct the read, do not just pick a voice. An ad has an emotional arc — weary at the open,
recognition as the problem is named, curiosity at the turn, quicker through the demonstration,
relief at the payoff, conviction at the ask. One instruction for every line is a voice setting,
and it sounds like a script being read. A line spoken by the customer is not performed at all:
somebody talking into their phone does not emphasise words.

Call plan_video when you have the scenes. The person gets a button showing what it costs, and
nothing is made until they press it.
"""


APP_EXTRA = """
This project is a working WEB APP. You are its engineer: you write the code as files, and it has to
run the first time someone opens it. Think like a senior front-end engineer who owns this product.

Before writing: work out the core job the app does, its screens, and the data it keeps (collection
names, fields, who can see them). For anything beyond a small change, sketch that plan in a sentence
or two in your head, then build it completely. Don't leave TODOs or half-built screens.

Platform rules (the preview enforces them):
- Plain ES modules, no build step. Reach for the known library that fits, not a hand-rolled one:
    preact, preact/hooks, htm/preact   the app itself
    lucide                             icons — use them; an interface without icons looks unfinished
    motion                             animate() for micro-interactions and enter/exit
    gsap, gsap/ScrollTrigger           timelines and scroll-linked reveals, when motion isn't enough
    @floating-ui/dom                   menus, tooltips and popovers that stay on screen
    zod                                validate a form before it saves, and show the message
    chart.js/auto                      any dashboard, total over time, breakdown
    d3                                 a chart Chart.js can't draw
    embla-carousel                     galleries and sliders
    date-fns                           dates and durations — bookings, invoices, "3 days ago"
    fuse.js                            search once a list is long enough to scroll
    sortablejs                         drag to reorder, kanban columns
    marked                             notes and descriptions written in markdown
    canvas-confetti                    a moment worth celebrating, used once
    three (+ three/addons/...)         3D
    phaser                             tile and physics games
  Don't write your own date maths, fuzzy search, drag-and-drop or chart renderer. Use
  html`...` tagged templates, never JSX; components render as html`<${Name} prop=${x} />`.
- app.js is the entry and renders into document.getElementById('root'). Relative imports must
  include the .js extension ('./screens/list.js'). Every named import must be exported by that file.
- Structure: app.js for layout and routing (a small state-based router is fine), screens/*.js,
  components/*.js, lib/*.js. Keep files focused, under about 300 lines.
- Data only through window.creai.db.collection('name'): list(), get(id), add(data),
  update(id, data), remove(id), all async. No localStorage, cookies, eval or calls to other sites.
- Styling: the kit classes shell, topbar (nav buttons with aria-current), page, card (flat to
  stop it lifting), grid, stack, row, btn (ghost, danger), badge, stat, empty, toast, skeleton,
  plus labelled inputs, selects, textareas and tables. The kit already carries layered surfaces,
  three shadow depths (--lift-1/2/3), hover lift, staggered card entrance, focus rings, a dark
  mode and reduced-motion. Use skeleton while data loads, never a bare "Loading…". Add styles.css
  only for what the kit lacks. Colours and fonts come from the project theme (update_site
  palette/theme).
- Build software somebody would want, not a form over a table. This is the difference
  between an app that gets opened twice and one that gets opened every morning, and it is
  almost entirely about what the FIRST screen carries:
    Lead with the number they actually care about. Money owed, jobs today, stock left,
    people waiting. Not a record count — a figure that changes a decision.
    Show state, not just rows. "On site", "Next", "Overdue", "Paid". A list where every
    row looks identical makes the person do the sorting the app should have done.
    Answer "what now". Surface the thing needing attention — the quote going cold, the
    invoice three weeks out — rather than waiting to be searched.
    Show shape over time where there is any. A week of takings as a small bar chart says
    more in one glance than thirty rows, and chart.js is already there.
    Fill it with plausible data when it is empty, drawn from their actual trade and town,
    so the first look shows what it becomes. Never lorem, never "Item 1", never a name in
    brackets.
  A screen that is a heading, a list and an add button is a database with a coat on. If the
  business would still reach for the spreadsheet after seeing it, build more.
- Pitch the visuals to the job, and say which register you chose and why:
    Calm       money, health, records, admin, anything someone checks quickly or under stress —
               a client portal, invoices, bookings, a dashboard. Restraint reads as trustworthy:
               the kit's defaults, real hierarchy, fast loads, no scroll effects. Motion only to
               explain a change (a row settling, a total updating).
    Crafted    marketing pages, portfolios, menus, launches, storefronts. In an APP, GSAP for
               entrances and scroll-linked reveals. On a SITE there is no JavaScript at all
               (published sites run script-src 'none', which keeps them fast and unhijackable),
               so motion comes from update_site's motion setting: "lively" or "cinematic" give
               scroll-driven reveals and a word-by-word headline, in CSS. Set it deliberately —
               the default is "subtle" and will look static. Generated imagery for hero and empty
               states, generous type.
    Bold       games, playful tools, anything meant to be shown off. three for 3D, GSAP timelines,
               full-bleed art. Go as far as the idea deserves.
  A bank statement dressed as a game loses the customer; a portfolio dressed as a spreadsheet
  loses them too. Never animate something a person needs to read quickly, keep every animation
  under 400ms unless it is decorative, and never block a first render on a library.
- Use generate_image for real pictures rather than grey placeholder blocks: hero art, empty
  states, card backgrounds, textures. A generated image beats a box with a letter in it. At most
  two per turn — each one takes seconds, and a slow turn feels broken. Spend them where they are
  seen first (the hero), then offer more next turn.
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
  Forgotten passwords, which every sign-in screen needs:
    await creai.auth.forgot(email)               emails a code; always succeeds, so it can't be
                                                 used to find out who has an account
    await creai.auth.reset(code, newPassword)    sets it and signs them in
  Build both screens, not just the link.
- Files people hand over — a receipt, a photo of the job, a signed form, a CV, an invoice PDF:
    await creai.files.upload('invoices', fileFromAnInput)  -> { id, name, mime, size, url }
    await creai.files.list('invoices')                     -> the ones this person may see
    await creai.files.link(file)                           -> a src/href for <img> or a download
    await creai.files.remove(file)
  The same app.json rules as records: "own" keeps each person's files to themselves, and a link
  copied out of one account is refused in another. Images and PDFs, up to 12 MB each. Call
  URL.revokeObjectURL on a link when the view closes.
- Taking money, on the business's own Stripe account:
    await creai.pay.charge('bookings', { amount: 7500, label: 'Deposit', reference: id })
        amount is in CENTS. This sends the buyer to Stripe's payment page.
    await creai.pay.settled(sessionId)   -> { paid, amount } when they come back
  The money goes to the business, not to Creai, and their name is on the statement. The owner
  connects their account once from the payments screen — until they have, charge() fails with a
  message saying so, which you should show rather than hide. Record what the payment was for in
  your own collection before charging, and mark it paid from settled(); never treat the return
  from Stripe as proof on its own.
  Still missing, so never promise it: an app cannot call another company's API.

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
- No emoji, in the HUD or anywhere else. Hearts, stars, coins and buttons get drawn: a canvas path,
  a Polygon2D, a Sprite2D or a TextureRect. Emoji can't be tinted when a life is lost, can't be
  atlased, can't be animated, and land as tofu boxes on the platforms that lack them.

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

Now the part that decides whether anyone plays it twice. Correct Godot and a dull game is the
common failure, and it is entirely avoidable:

- The first ten seconds teach without telling. Somebody should understand the game by playing it,
  not by reading a screen of instructions. If it needs a paragraph to explain, the design is
  wrong, not the copy.
- Feel comes before features. A single mechanic that is satisfying beats three that are limp.
  Before adding anything, make the thing that already exists respond: acceleration and friction
  rather than fixed speed, a few frames of squash on impact, a brief screen shake on a hit, a
  small pause on a big moment, particles that are three rectangles and a tween. This is most of
  the difference between a prototype and a game, and none of it needs art.
- Every input gets an immediate answer. Never let a tap produce nothing — a sound, a flash, a
  nudge, anything within one frame. A control that sometimes does nothing reads as broken, even
  when the logic is right.
- Difficulty rises, and the player can see why. Start easier than feels necessary. Add one
  variable at a time — speed, then frequency, then a second obstacle. A game that is hard in the
  first fifteen seconds is closed in the first fifteen seconds.
- Failure has to be fair and fast. The player must always understand what killed them, and be
  playing again within two seconds. No confirmation dialogs between death and the next attempt.
- Give the eye somewhere to rest. Two or three colours and a lot of empty space beats a busy
  screen. Contrast marks what matters: the player and the danger are the brightest things on it.
- Score is not the only feedback. A near miss, a streak, the speed creeping up — something should
  tell the player they are getting better before the number does.
- Know the genre before you write it. Call game_rules once you know roughly what kind of game
  this is — a runner, a puzzle, a shooter, two players on one device. Each has a loop, numbers
  that matter and a mistake everyone makes first, and the rules contradict each other: a runner
  wants rising speed, a puzzle wants none. Built the same way, each comes out wrong in its own
  particular manner.
- There is no game server here. A game is exported to WebAssembly and served as a static file,
  so networked player-versus-player cannot be built on this platform — no authoritative server,
  no lag compensation, no matchmaking. Two people play on ONE device: split keyboard, split
  screen, or passing the phone. Say that plainly if somebody asks for online multiplayer, and
  offer the local version rather than building something that cannot work.
- Play it before you claim it works. check_game catches broken code, not a boring game. Ask
  yourself what the thirty-second experience actually is, and say so honestly in your reply.

You can draw real artwork. This is the most underused thing available to you, and it changes what
is possible here:

- .svg is text, so you can WRITE artwork — you are not limited to rectangles and circles. A
  character, a tree, a spaceship, an enemy, a UI frame, a logo: author the SVG, save it in the
  project, and Godot imports it as a texture during the build. It scales without blurring at any
  resolution, which matters because this runs on phones and desktops alike.
- Write them small and deliberate. A good game sprite is twenty to sixty path commands, not a
  traced photograph. Flat fills, two or three colours per object, clean silhouettes. A shape that
  reads at 32 pixels reads at 512.
- Silhouette first. If the black shape alone does not say what it is, no amount of detail inside
  it will. Draw the outline, check it reads, then add the two or three interior shapes that
  carry character.
- Keep a consistent construction across every sprite in one game: the same stroke weight, the
  same corner radius, the same light direction. Inconsistency between assets is the single most
  common reason a set of drawings looks amateur when each one is fine alone.
- Use the same palette as the rest of the game. Sprites drawn in colours that are not in the
  theme are the fastest way to make a coherent game look assembled from parts.
- Set the import scale in the .tscn rather than drawing huge SVGs; a 64x64 viewBox scaled up is
  cleaner and smaller than a 1024 one.

So the honest limit is not "shapes only". It is that you are drawing, in a language made of text,
and drawing well is a skill — apply the same care to a sprite as to the code.

Making it look expensive, on top of that. Everything below is text source, so all of it
is available to you — and almost nobody uses it, which is why most browser games look like
prototypes:

- Write a .gdshader. This is the single biggest lever you have. A full-screen ColorRect with a
  shader gives you a gradient sky that shifts with the score, a vignette that tightens as danger
  rises, subtle chromatic aberration on impact, scanlines, a soft grain. Twenty lines of shader
  code is worth more than any sprite you cannot ship.
- Use a WorldEnvironment with glow enabled. Emissive colours on simple shapes — a bright circle
  against a dark field — look deliberate and modern the moment they bloom. This is how flat
  rectangles stop looking like flat rectangles.
- Pick three colours and one accent, and stay there. A dark desaturated background, one mid tone
  for everything neutral, and a single saturated colour reserved for the thing that matters. If
  the accent is on more than about a tenth of the screen it has stopped meaning anything.
- Ease everything. Tween with TRANS_CUBIC or TRANS_BACK and EASE_OUT, never linear. Linear motion
  is the clearest signal that nobody directed it. Entrances overshoot slightly and settle;
  exits accelerate away.
- Give the camera a job. Camera2D with position_smoothing_enabled, a small drift toward where the
  player is heading, a brief zoom-in on a big moment. A locked camera is a choice you should make
  on purpose, not by default.
- Type is design. Set font_size deliberately and vary it hard — a score at 64px and a label at
  14px reads as designed; everything at 24px reads as a placeholder. Add letter_spacing on small
  uppercase labels. Never centre everything by reflex.
- Respect the edges. Generous margins, and nothing important within about 8% of any edge. Crowded
  corners are the fastest way to look cheap on a phone.
- Sound, if any, is quiet and short. AudioStreamGenerator can produce a soft blip and a lower
  thud. Two sounds used well beat six. If you cannot make them pleasant, stay silent — bad audio
  is worse than none.
- Transitions, not cuts. A quarter-second fade between start screen, play and game over. An
  instant jump between states is the most common reason a finished game still feels unfinished.

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
              app: tuple | None = None, game: tuple | None = None, video: bool = False,
              tz: str | None = None, drafts=None, ideas=None,
              attachments: tuple | None = None, reviewer=None,
              progress_key: str | None = None) -> Turn:
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
    # Every line the agent logs from here reaches the waiting screen as it lands,
    # rather than all at once when the turn finishes.
    if progress_key:
        from . import progress as progress_svc
        turn.log.key = progress_key
        await progress_svc.start(progress_key)

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
    elif video:
        system += VIDEO_EXTRA
    if game is not None:
        system += GAME_EXTRA
    if intent == "build" and game is not None:
        tools = [TOOL_LIST_FILES, TOOL_READ_FILE, TOOL_WRITE_FILES, TOOL_CHECK_GAME,
                 TOOL_BUILD_GAME, TOOL_DELETE_FILE, TOOL_GENERATE_IMAGE,
                 TOOL_GENRE, TOOL_SAVE_ANSWER, TOOL_SUGGEST]
    elif intent == "build" and app is not None:
        tools = [TOOL_LIST_FILES, TOOL_READ_FILE, TOOL_WRITE_FILES, TOOL_CHECK_APP, TOOL_DELETE_FILE,
                 TOOL_UPDATE_SITE, TOOL_GENERATE_IMAGE, TOOL_LOOK, TOOL_SAVE_ANSWER, TOOL_SUGGEST]
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
        tools = [TOOL_UPDATE_SITE, TOOL_GENERATE_IMAGE, TOOL_START_APP, TOOL_LOOK,
                 TOOL_SAVE_ANSWER, TOOL_SUGGEST] + brand_tools
        # A video can be asked for from any build conversation: plenty of people
        # want a clip for a channel and never a website at all.
        tools.append(TOOL_PLAN_VIDEO)
        tools.append(TOOL_BLUEPRINT)
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
            shots = result.pop("_shots", None) if isinstance(result, dict) else None
            if shots:
                # The model looks at the page rather than imagining it.
                blocks = []
                for width, b64 in list(shots.items())[:2]:
                    blocks.append({"type": "text", "text": f"{width}:"})
                    blocks.append({"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": b64}})
                blocks.append({"type": "text", "text": json.dumps(result)})
                results.append({"type": "tool_result", "tool_use_id": u["id"], "content": blocks})
            else:
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
            # Anonymous drafts have no project; their faults still count, against
            # the org when there is one.
            if app or game:
                _pid, _oid = (app or game)[0], (app or game)[1]
                from . import quality as quality_svc
                await quality_svc.record(_oid, _pid, "site", quality)
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

        if name == "look":
            if turn.looks >= MAX_LOOKS_PER_TURN:
                return {"ok": False, "error": "you have already looked twice this turn; "
                                              "make your changes and look again next turn"}
            from . import review as render_svc
            widths = [w for w in (args.get("widths") or ["phone", "desktop"])
                      if w in ("phone", "desktop")] or ["phone", "desktop"]
            try:
                if app is not None:
                    files = await appfs.files(*app)
                    html = appfs.preview(files, turn.site, appfs.token(*app), settings.public_url)
                else:
                    html = site.render(turn.site)
                out = await render_svc.shots(html, widths)
            except Exception as exc:
                return {"ok": False, "error": f"couldn't render it just now ({exc})"}
            turn.looks += 1
            shots = out.get("shots") or {}
            if not shots:
                return {"ok": False, "error": "the renderer came back empty"}
            return {"ok": True, "_shots": shots, "measure": out.get("measure") or {},
                    "note": "This is the page as a visitor sees it. Judge it honestly and fix "
                            "what is weak before you reply."}

        if name == "start_app":
            label = str(args.get("label") or f"Build the {args.get('name') or 'app'}").strip()[:40]
            if not any(a.get("kind") == "new_app" for a in turn.actions):
                turn.actions.append({"kind": "new_app", "label": label,
                                     "name": str(args.get("name") or "App").strip()[:60]})
            turn.log.append(f"offered to build {args.get('name') or 'the app'}")
            return {"ok": True, "offered": label,
                    "note": "The person now has a button that creates the app and opens it. "
                            "Finish this turn by building the site part you can build."}

        if name == "game_rules":
            from . import genres
            brief = genres.brief_for(args.get("genre", ""), args.get("theme", ""))
            if not brief:
                return {"ok": False, "error": "no rules for that genre"}
            turn.log.append(f"read the rules for {args.get('genre')} games")
            return {"ok": True, "rules": brief,
                    "note": "Build to these. Do not read them back to the person — show them "
                            "the game."}

        if name == "use_blueprint":
            from . import blueprints
            try:
                brief = blueprints.brief_for(args.get("blueprint", ""),
                                             args.get("business", ""),
                                             args.get("trade", ""))
            except KeyError:
                return {"ok": False, "error": "no such blueprint"}
            bp = blueprints.get(args["blueprint"])
            turn.log.append(f"opened the {bp['name'].lower()} blueprint")
            return {"ok": True, "brief": brief, "collections": bp["collections"],
                    "fields": bp.get("fields"),
                    "note": "Build this now with write_files, in their words. Do not read the "
                            "brief back to them — show them the thing."}

        if name == "plan_video":
            from . import video as video_svc
            scenes = args.get("scenes") or []
            plan = {"brand_name": (answers.get("brand") or {}).get("name")
                                  or (answers.get("site") or {}).get("business") or "",
                    "scenes": [{k: v for k, v in sc.items() if v not in (None, "")}
                               for sc in scenes],
                    "voice": "ash"}
            problems = video_svc.check(plan)
            if problems:
                # Handed back rather than silently fixed: the agent wrote it, so the
                # agent rewrites it, and learns the rule for the next one.
                return {"ok": False, "error": "; ".join(problems[:3]),
                        "note": "Rewrite the scenes and call plan_video again."}
            cost = video_svc.estimate(plan)
            if not any(a.get("kind") == "new_video" for a in turn.actions):
                turn.actions.append({"kind": "new_video",
                                     "label": f"Make it ({cost} credits)",
                                     "title": str(args.get("title") or "Video")[:80],
                                     "shape": args.get("shape") or "vertical",
                                     "plan": plan})
            turn.log.append(f"planned a {len(scenes)}-scene video")
            return {"ok": True, "scenes": len(scenes), "credits": cost,
                    "note": "The person now has a button that makes the video. Tell them what "
                            "it opens on and what it says, in a sentence — do not list the "
                            "scenes back to them."}

        if name == "generate_image":
            from . import images
            if not images.configured():
                return {"ok": False, "error": "image generation isn't switched on; use generative artwork"}
            if turn.images_made >= MAX_IMAGES_PER_TURN:
                return {"ok": False, "error": f"that's {MAX_IMAGES_PER_TURN} pictures this turn, "
                                              "which is the limit so the build stays quick. Finish "
                                              "with what you have and offer more next turn."}
            try:
                url = await images.generate(str(args.get("prompt", "")), args.get("shape") or "landscape")
                turn.images_made += 1
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
