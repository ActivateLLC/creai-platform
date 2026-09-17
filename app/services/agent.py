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
from dataclasses import dataclass, field

import httpx

from ..core.config import settings
from . import site as site_spec

log = logging.getLogger("creai.agent")

API = "https://api.anthropic.com/v1/messages"
MAX_STEPS = 6
MAX_THREAD = 40          # messages kept per draft or project
MAX_USER_CHARS = 4000

ACTIONS = {
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
- Style: plain, specific and calm. Short replies — two or three sentences. No \
exclamation marks. No markdown headings or bullet lists.
"""

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
                                  "description": "services: {name, detail, price}; "
                                                 "faq: {q, a}; testimonials: {quote, name}"},
                    },
                    "required": ["kind"],
                },
            },
            "contact": {"type": "object",
                        "properties": {k: {"type": "string"} for k in ("email", "phone", "area")}},
        },
    },
}

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
                                    "enum": ["instagram", "facebook", "linkedin", "x", "tiktok"]},
                        "text": {"type": "string"},
                        "when": {"type": "string", "description": "Suggested day and time"},
                    },
                    "required": ["network", "text"],
                },
            },
        },
        "required": ["posts"],
    },
}


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


async def _call(messages: list, tools: list, system: str) -> dict:
    if not settings.anthropic_key:
        raise AgentUnavailable("the agent is not configured on this deployment")
    async with httpx.AsyncClient(timeout=90) as x:
        r = await x.post(API, headers={
            "x-api-key": settings.anthropic_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }, json={"model": settings.agent_model, "max_tokens": 2048,
                 "system": system, "tools": tools, "messages": messages})
    if r.status_code != 200:
        log.warning("agent model call failed: %s %s", r.status_code, r.text[:300])
        raise AgentUnavailable("the agent could not respond just now — try again")
    return r.json()


async def run(text: str, answers: dict | None, *, project: bool = False,
              queue_posts=None) -> Turn:
    """One conversational turn.

    `answers` is the draft's or project's stored JSON (site spec, facts, thread).
    `queue_posts` is supplied only for signed-in projects; it is already bound to
    that project and workspace, so the model never names either.
    """
    answers = dict(answers or {})
    turn = Turn(answers=answers,
                site=site_spec.merge(answers.get("site"), {}),
                thread=trim(list(answers.get("_thread") or [])))

    tools = [TOOL_UPDATE_SITE, TOOL_SAVE_ANSWER, TOOL_SUGGEST]
    system = SYSTEM
    if project and queue_posts is not None:
        tools.append(TOOL_DRAFT_POSTS)
        system += PROJECT_EXTRA
    facts = {k: v for k, v in answers.items() if not k.startswith("_") and k != "site"}
    system += ("\nCurrent site spec: " + json.dumps(turn.site)
               + "\nKnown facts: " + json.dumps(facts))

    turn.thread.append({"role": "user", "content": text[:MAX_USER_CHARS]})

    for _ in range(MAX_STEPS):
        out = await _call(turn.thread, tools, system)
        content = out.get("content", [])
        turn.thread.append({"role": "assistant", "content": content})
        uses = [b for b in content if b.get("type") == "tool_use"]
        if not uses:
            turn.reply = " ".join(b.get("text", "") for b in content
                                  if b.get("type") == "text").strip()
            break
        results = []
        for u in uses:
            result = await _tool(turn, u["name"], u.get("input") or {}, queue_posts)
            results.append({"type": "tool_result", "tool_use_id": u["id"],
                            "content": json.dumps(result)})
        turn.thread.append({"role": "user", "content": results})
    else:
        turn.reply = turn.reply or "I've made those changes — have a look at the preview."

    turn.answers["site"] = turn.site
    turn.answers["_thread"] = trim(turn.thread)
    return turn


async def _tool(turn: Turn, name: str, args: dict, queue_posts) -> dict:
    try:
        if name == "update_site":
            turn.site = site_spec.merge(turn.site, args)
            changed = ", ".join(k for k in args) or "nothing"
            turn.log.append(f"updated site · {changed}")
            return {"ok": True, "site": turn.site}

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

        if name == "draft_posts" and queue_posts is not None:
            posts = []
            for p in (args.get("posts") or [])[:7]:
                if isinstance(p, dict) and p.get("text"):
                    posts.append({"network": str(p.get("network", ""))[:20],
                                  "text": str(p["text"])[:2200],
                                  "when": str(p.get("when", ""))[:60]})
            ids = await queue_posts(posts)
            turn.posts.extend(ids)
            turn.log.append(f"drafted {len(ids)} posts for approval")
            if not any(a["kind"] == "review_posts" for a in turn.actions):
                turn.actions.append({"kind": "review_posts", "label": "Review drafts"})
            return {"ok": True, "queued": len(ids), "published": 0}

        return {"ok": False, "error": f"unknown tool {name}"}
    except Exception as exc:          # a bad tool call should not end the turn
        log.exception("agent tool %s failed", name)
        return {"ok": False, "error": str(exc)[:200]}
