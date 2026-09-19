"""
One way to call any model.

The agent speaks a single message format (Anthropic's content blocks: text,
tool_use, tool_result). Each provider adapter translates to and from its own API,
so the agent's tools, approvals and safeguards behave the same whichever model
answers. Every response reports the model that actually served it, and billing
prices that model from the registry below.

A provider outage falls through to the next model in the chain. A request the
provider rejects as invalid does not: that is a bug to fix, not to paper over.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field

import httpx

from ..core.config import settings

log = logging.getLogger("creai.models")


@dataclass(frozen=True)
class Model:
    key: str                 # what Creai calls it (and bills it as)
    provider: str            # anthropic | meta
    api_model: str           # what the provider calls it
    input: float             # USD per million tokens
    output: float
    cache_write: float = 0.0
    cache_read: float = 0.0
    tools: bool = True
    vision: bool = False
    fallback: tuple = field(default_factory=tuple)


REGISTRY: dict[str, Model] = {m.key: m for m in (
    Model("claude-fable-5-1", "anthropic", "claude-fable-5-1", 10.0, 50.0, 12.50, 0.25,
          vision=True, fallback=("claude-opus-5",)),
    Model("claude-opus-5", "anthropic", "claude-opus-5", 5.0, 25.0, 6.25, 0.50,
          vision=True, fallback=("claude-sonnet-5",)),
    Model("claude-sonnet-5", "anthropic", "claude-sonnet-5", 2.0, 10.0, 2.50, 0.20,
          vision=True, fallback=("claude-haiku-4-5-20251001",)),
    Model("claude-haiku-4-5-20251001", "anthropic", "claude-haiku-4-5-20251001", 1.0, 5.0, 1.25, 0.10,
          vision=True),
    # Meta Model API (public preview, US). Cached-input pricing not published: billed as input.
    Model("muse-spark-1.1", "meta", "muse-spark-1.1", 1.25, 4.25, vision=True,
          fallback=("claude-sonnet-5",)),
)}

# Media generation, priced per output. Hugging Face Inference Providers.
MEDIA: dict[str, dict] = {
    "image:Tongyi-MAI/Z-Image-Turbo": {"kind": "image", "usd": 0.01, "license": "apache-2.0"},
    "image:black-forest-labs/FLUX.1-schnell": {"kind": "image", "usd": 0.01, "license": "apache-2.0"},
}


class ModelUnavailable(RuntimeError):
    """The provider could not serve the request (down, overloaded, unconfigured)."""


class ModelAccountProblem(RuntimeError):
    """The provider refused because of the account, not the request.

    Separate from ModelRejected on purpose: one is something a person can fix by
    saying it differently, and the other is not. Collapsing them tells somebody
    to rephrase a message that was never the problem.
    """


class ModelRejected(RuntimeError):
    """The provider refused the request as invalid. Not retried on another model."""


def get(key: str) -> Model:
    if key not in REGISTRY:
        raise ModelRejected(f"unknown model {key}")
    return REGISTRY[key]


def configured(key: str) -> bool:
    m = REGISTRY.get(key)
    if not m:
        return False
    return bool(settings.anthropic_key if m.provider == "anthropic" else settings.meta_api_key)


def price(model_key: str) -> tuple[float, float, float, float] | None:
    m = REGISTRY.get(model_key)
    return (m.input, m.output, m.cache_write, m.cache_read) if m else None


# ---------------------------------------------------------------- Anthropic

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


async def _anthropic(m: Model, messages, tools, system, max_tokens) -> dict:
    body = {"model": m.api_model, "max_tokens": max_tokens,
            "cache_control": {"type": "ephemeral"},      # repeated prefixes bill at cache rate
            "system": system, "messages": messages}
    if tools:
        body["tools"] = tools
    async with httpx.AsyncClient(timeout=90) as x:
        r = await x.post(ANTHROPIC_URL, headers={
            "x-api-key": settings.anthropic_key, "anthropic-version": "2023-06-01",
            "content-type": "application/json"}, json=body)
    _raise_for(r, m)
    out = r.json()
    out["model"] = m.key
    return out


# ---------------------------------------------------------------- OpenAI-compatible (Meta)

def to_openai(messages: list, tools: list, system: str) -> tuple[list, list]:
    """Anthropic-shaped thread and tools → Chat Completions messages and tools."""
    msgs = [{"role": "system", "content": system}] if system else []
    for msg in messages:
        content = msg["content"]
        if isinstance(content, str):
            msgs.append({"role": msg["role"], "content": content})
            continue
        if msg["role"] == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                     for b in content if b.get("type") == "tool_use"]
            out = {"role": "assistant", "content": text or None}
            if calls:
                out["tool_calls"] = calls
            msgs.append(out)
        else:
            parts = []
            for b in content:
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    msgs.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                 "content": c if isinstance(c, str) else json.dumps(c)})
                elif b.get("type") == "text":
                    parts.append({"type": "text", "text": b.get("text", "")})
                elif b.get("type") == "image" and (b.get("source") or {}).get("type") == "base64":
                    src = b["source"]
                    parts.append({"type": "image_url", "image_url": {
                        "url": f"data:{src['media_type']};base64,{src['data']}"}})
                elif b.get("type") == "document":
                    parts.append({"type": "text", "text": f"[A PDF named {b.get('title', 'document')} was attached "
                                                          "but this model can't read PDFs.]"})
            if parts:
                if all(p["type"] == "text" for p in parts):
                    msgs.append({"role": "user", "content": "\n".join(p["text"] for p in parts)})
                else:
                    msgs.append({"role": "user", "content": parts})
    fns = [{"type": "function", "function": {
        "name": t["name"], "description": t.get("description", ""),
        "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
        for t in tools or []]
    return msgs, fns


def from_openai(data: dict, m: Model) -> dict:
    """Chat Completions response → Anthropic-shaped response."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            log.warning("%s sent unparseable tool arguments for %s", m.key, fn.get("name"))
            args = {}
        content.append({"type": "tool_use", "id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                        "name": fn.get("name", ""), "input": args if isinstance(args, dict) else {}})
    u = data.get("usage") or {}
    cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    return {
        "model": m.key,
        "content": content,
        "stop_reason": "tool_use" if msg.get("tool_calls") else "end_turn",
        "usage": {
            # cached tokens are billed at the input rate until Meta publishes a cache price
            "input_tokens": int(u.get("prompt_tokens") or 0),
            "output_tokens": int(u.get("completion_tokens") or 0),
            "cached_tokens": cached,
        },
    }


async def _meta(m: Model, messages, tools, system, max_tokens) -> dict:
    msgs, fns = to_openai(messages, tools, system)
    body = {"model": m.api_model, "messages": msgs, "max_tokens": max_tokens}
    if fns:
        body["tools"] = fns
        body["parallel_tool_calls"] = True
    async with httpx.AsyncClient(timeout=120) as x:
        r = await x.post(settings.meta_api_base.rstrip("/") + "/chat/completions",
                         headers={"Authorization": f"Bearer {settings.meta_api_key}",
                                  "Content-Type": "application/json"}, json=body)
    _raise_for(r, m)
    return from_openai(r.json(), m)


PROVIDERS = {"anthropic": _anthropic, "meta": _meta}


# Refusals that are about the account rather than the message. Telling somebody
# to rephrase when the bill is unpaid sends them to fix the one thing that cannot
# be the problem — and they will try, twice, before asking.
ACCOUNT_TROUBLE = ("credit balance is too low", "billing", "quota", "insufficient",
                   "payment", "subscription", "spending limit", "rate limit")


def _raise_for(r: httpx.Response, m: Model) -> None:
    if r.status_code == 200:
        return
    detail = r.text[:300]
    log.warning("%s (%s) returned %s: %s", m.key, m.provider, r.status_code, detail)
    if any(w in detail.lower() for w in ACCOUNT_TROUBLE):
        raise ModelAccountProblem(detail)
    if r.status_code in (400, 404, 422):
        raise ModelRejected(f"{m.key} rejected the request")
    raise ModelUnavailable(f"{m.key} unavailable ({r.status_code})")


# ---------------------------------------------------------------- entry point

async def complete(model_key: str, messages: list, tools: list, system: str,
                   max_tokens: int = 2048) -> dict:
    """Call the model, falling back along its chain if a provider is down."""
    tried, chain = [], [model_key]
    while chain:
        key = chain.pop(0)
        if key in tried:
            continue
        tried.append(key)
        m = get(key)
        if not configured(key):
            chain.extend(m.fallback)
            continue
        try:
            return await PROVIDERS[m.provider](m, messages, tools, system, max_tokens)
        except (ModelUnavailable, httpx.TimeoutException, httpx.TransportError) as exc:
            log.warning("falling back from %s: %s", key, exc)
            chain.extend(m.fallback)
    raise ModelUnavailable("no configured model could serve this request (tried " + ", ".join(tried) + ")")
