"""
Webflow: connect a customer's existing site and let the agent edit it.

Three parts:

  OAuth    Creai registers itself with Webflow's MCP server (dynamic client
           registration), then each workspace authorises with PKCE. Tokens are
           sealed in the vault and refreshed automatically.

  Policy   Full access by default. Every Webflow tool and action is available
           except a short list that is public, irreversible, or sends data out
           of Webflow — those become approval cards. A workspace can switch
           live publishing to automatic.

  Bridge   Webflow's tool definitions are large. Rather than send ~30 schemas
           on every message, the agent gets three small tools: list, describe
           and call. It loads a schema only when it needs one, so capability is
           complete and each message stays cheap.
"""

import base64
import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from ..core.config import settings
from ..core.db import conn, log_event
from . import mcp, vault

log = logging.getLogger("creai.webflow")

PROVIDER = "webflow"
MCP_URL = "https://mcp.webflow.com/mcp"
AUTH_BASE = "https://mcp.webflow.com"
STATE_TTL = timedelta(minutes=15)
# Filled in by Webflow's client on every call; the model never supplies them.
INJECTED = ("agent_id", "session_id", "context")

_tool_cache: dict[int, tuple[float, list[dict]]] = {}
TOOL_CACHE_SECONDS = 600


class WebflowError(RuntimeError):
    pass


def redirect_uri() -> str:
    return settings.public_url.rstrip("/") + "/v1/connections/webflow/callback"


# ---------------------------------------------------------------- OAuth

async def _client_id() -> str:
    """Our registration with Webflow's authorization server, created once."""
    uri = redirect_uri()
    async with conn() as c:
        cid = await c.fetchval(
            "SELECT client_id FROM oauth_clients WHERE provider=$1 AND redirect_uri=$2",
            PROVIDER, uri)
    if cid:
        return cid
    async with httpx.AsyncClient(timeout=30) as x:
        r = await x.post(f"{AUTH_BASE}/oauth/register", json={
            "client_name": "Creai",
            "client_uri": "https://creai.dev",
            "redirect_uris": [uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        })
    if r.status_code >= 400:
        raise WebflowError(f"could not register with Webflow: {r.text[:200]}")
    cid = r.json()["client_id"]
    async with conn() as c:
        await c.execute(
            """INSERT INTO oauth_clients (provider, redirect_uri, client_id) VALUES ($1,$2,$3)
               ON CONFLICT (provider, redirect_uri) DO NOTHING""", PROVIDER, uri, cid)
        cid = await c.fetchval(
            "SELECT client_id FROM oauth_clients WHERE provider=$1 AND redirect_uri=$2",
            PROVIDER, uri)
    return cid


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def start_auth(org_id: int, user_id: int) -> str:
    cid = await _client_id()
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(32)
    async with conn() as c:
        await c.execute("DELETE FROM oauth_states WHERE expires_at < now()")
        await c.execute(
            """INSERT INTO oauth_states (state, org_id, user_id, provider, verifier, expires_at)
               VALUES ($1,$2,$3,$4,$5,$6)""",
            state, org_id, user_id, PROVIDER, verifier, datetime.now(timezone.utc) + STATE_TTL)
    return f"{AUTH_BASE}/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": cid, "redirect_uri": redirect_uri(),
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": MCP_URL,
    })


def _tokens(payload: dict) -> dict:
    return {"access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token"),
            "expires_at": time.time() + int(payload.get("expires_in") or 3600) - 60}


async def _token_request(form: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as x:
        r = await x.post(f"{AUTH_BASE}/oauth/token", data=form,
                         headers={"Accept": "application/json"})
    if r.status_code >= 400:
        raise WebflowError(f"Webflow rejected the authorization: {r.text[:200]}")
    return r.json()


async def finish_auth(code: str, state: str) -> int:
    """Exchange the code; store the sealed token on the workspace. Returns org_id."""
    async with conn() as c:
        row = await c.fetchrow(
            """DELETE FROM oauth_states WHERE state=$1 AND provider=$2 AND expires_at > now()
               RETURNING org_id, user_id, verifier""", state, PROVIDER)
    if not row:
        raise WebflowError("this connection link has expired — please try again")
    cid = await _client_id()
    tok = _tokens(await _token_request({
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
        "client_id": cid, "code_verifier": row["verifier"], "resource": MCP_URL}))
    async with conn() as c:
        await c.execute(
            """INSERT INTO connections (org_id, provider, secret_enc, meta, status, created_by)
               VALUES ($1,$2,$3,$4,'connected',$5)
               ON CONFLICT (org_id, provider) DO UPDATE
                 SET secret_enc=EXCLUDED.secret_enc, status='connected', updated_at=now()""",
            row["org_id"], PROVIDER, vault.seal(tok), {"auto_publish": False}, row["user_id"])
    _tool_cache.pop(row["org_id"], None)
    await log_event(row["org_id"], "connection.webflow", "connected", None, row["user_id"])
    return row["org_id"]


async def _connection(org_id: int):
    async with conn() as c:
        return await c.fetchrow(
            "SELECT * FROM connections WHERE org_id=$1 AND provider=$2", org_id, PROVIDER)


async def access_token(org_id: int, force_refresh: bool = False) -> str:
    row = await _connection(org_id)
    if not row or row["status"] != "connected":
        raise WebflowError("Webflow is not connected for this workspace")
    tok = vault.open_(row["secret_enc"])
    if force_refresh or time.time() >= tok.get("expires_at", 0):
        if not tok.get("refresh_token"):
            await _mark(org_id, "expired")
            raise WebflowError("the Webflow connection expired — reconnect to continue")
        try:
            new = _tokens(await _token_request({
                "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                "client_id": await _client_id(), "resource": MCP_URL}))
        except WebflowError:
            await _mark(org_id, "expired")
            raise WebflowError("the Webflow connection expired — reconnect to continue")
        new["refresh_token"] = new["refresh_token"] or tok["refresh_token"]
        tok = new
        async with conn() as c:
            await c.execute(
                "UPDATE connections SET secret_enc=$1, updated_at=now() WHERE id=$2",
                vault.seal(tok), row["id"])
    return tok["access_token"]


async def _mark(org_id: int, status: str) -> None:
    async with conn() as c:
        await c.execute("UPDATE connections SET status=$1 WHERE org_id=$2 AND provider=$3",
                        status, org_id, PROVIDER)


async def disconnect(org_id: int) -> None:
    row = await _connection(org_id)
    if not row:
        return
    try:
        tok = vault.open_(row["secret_enc"])
        async with httpx.AsyncClient(timeout=15) as x:
            await x.post(f"{AUTH_BASE}/oauth/token", data={
                "token": tok.get("refresh_token") or tok["access_token"],
                "client_id": await _client_id()})
    except Exception:                      # revocation is best effort
        log.info("webflow revocation failed for org %s", org_id)
    async with conn() as c:
        await c.execute("DELETE FROM connections WHERE id=$1 AND org_id=$2", row["id"], org_id)
    _tool_cache.pop(org_id, None)


async def status(org_id: int) -> dict:
    row = await _connection(org_id)
    if not row:
        return {"provider": PROVIDER, "connected": False}
    meta = row["meta"] or {}
    return {"provider": PROVIDER, "connected": row["status"] == "connected",
            "status": row["status"], "site": meta.get("site"),
            "auto_publish": bool(meta.get("auto_publish"))}


async def update_meta(org_id: int, changes: dict) -> dict:
    async with conn() as c:
        row = await c.fetchrow(
            """UPDATE connections SET meta = meta || $1::jsonb, updated_at=now()
               WHERE org_id=$2 AND provider=$3 RETURNING meta""", changes, org_id, PROVIDER)
    if not row:
        raise WebflowError("Webflow is not connected for this workspace")
    return row["meta"]


# ---------------------------------------------------------------- MCP calls

async def _with_client(org_id: int, fn):
    """Run fn(client) with a fresh session; refresh the token once on 401."""
    for attempt in (0, 1):
        token = await access_token(org_id, force_refresh=attempt == 1)
        try:
            async with mcp.MCPClient(MCP_URL, token) as client:
                return await fn(client)
        except mcp.Unauthorized:
            if attempt == 1:
                await _mark(org_id, "expired")
                raise WebflowError("Webflow no longer accepts this connection — reconnect to continue")


async def tools(org_id: int) -> list[dict]:
    hit = _tool_cache.get(org_id)
    if hit and time.time() - hit[0] < TOOL_CACHE_SECONDS:
        return hit[1]
    listed = await _with_client(org_id, lambda c: c.list_tools())
    _tool_cache[org_id] = (time.time(), listed)
    return listed


def _args(org_id: int, arguments: dict, session: dict) -> dict:
    a = {k: v for k, v in (arguments or {}).items() if k not in INJECTED}
    a["agent_id"] = f"{settings.agent_model}|creai|w{org_id}"
    a["session_id"] = session.get("id") or "start"
    a["context"] = "Creai assistant editing the customer's Webflow site at their request."
    return a


async def call(org_id: int, name: str, arguments: dict, session: dict) -> str:
    async def run(client):
        return await client.call_tool(name, _args(org_id, arguments, session))
    result = await _with_client(org_id, run)
    text = mcp.result_text(result)
    sid = ((result.get("structuredContent") or {}).get("mcp_session") or {}).get("session_id")
    if not sid and "[session_id issued" in text:
        frag = text.split("[session_id issued", 1)[1]
        sid = next((w.strip(":].,") for w in frag.split() if w.startswith("ses_")), None)
    if sid and sid.startswith("ses_"):
        session["id"] = sid
    return text


# ---------------------------------------------------------------- policy

def _action_keys(arguments: dict) -> list[tuple[str, dict]]:
    out = []
    for item in (arguments or {}).get("actions") or []:
        if isinstance(item, dict):
            for k, v in item.items():
                if k not in ("label", "build_label") and isinstance(v, (dict, list, bool, str, int)):
                    out.append((k, v if isinstance(v, dict) else {}))
    return out


def needs_approval(tool: str, arguments: dict, auto_publish: bool = False) -> list[str]:
    """Reasons this call must wait for a person. Empty list = run it now.

    Default is to allow. Only actions that are public, irreversible or send data
    out of Webflow are held.
    """
    reasons = []
    for key, params in _action_keys(arguments):
        k = key.lower()
        if k == "publish_site":
            live = bool(params.get("customDomains"))
            staging_only = not live and params.get("publishToWebflowSubdomain", True)
            if not staging_only and not auto_publish:
                domains = ", ".join(params.get("customDomains") or []) or "the live site"
                reasons.append(f"Publish to {domains}")
        elif k == "publish_branch":
            continue                                   # staging preview only
        elif "unpublish" in k:
            reasons.append("Take content offline")
        elif ("publish" in k) and not auto_publish:
            reasons.append("Publish content live")
        elif k.startswith("delete") or "_delete" in k or k.startswith("remove"):
            reasons.append("Delete " + k.replace("delete_", "").replace("_", " "))
        elif k == "merge_branch":
            reasons.append("Merge a branch into the main site")
        elif k == "create_webhook":
            reasons.append("Send site events to " + str(params.get("url", "an outside address")))
    return reasons


# ---------------------------------------------------------------- the agent bridge

TOOL_DEFS = [
    {
        "name": "webflow_list_tools",
        "description": "List every Webflow tool available on the person's connected site, "
                       "with a one-line description each. Start here.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "webflow_describe_tool",
        "description": "Get the full input schema for one Webflow tool before calling it.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}},
                         "required": ["name"]},
    },
    {
        "name": "webflow_call",
        "description": "Call a Webflow tool with arguments matching its schema. Most tools "
                       "take an `actions` array. Do not pass agent_id, session_id or "
                       "context; they are filled in for you. Publishing to the live domain, "
                       "deleting, merging branches and creating webhooks are queued for the "
                       "person's approval instead of running; the result will say so.",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string"}, "arguments": {"type": "object"}},
            "required": ["name", "arguments"]},
    },
]

PROMPT = """
You are editing the person's existing Webflow site: {site_name} (site id {site_id}).
Use the webflow_* tools. Call webflow_list_tools first, webflow_describe_tool before using a
tool for the first time, then webflow_call. Changes are staged until published. To let the
person preview, publish to the Webflow staging subdomain only (publishToWebflowSubdomain true,
no customDomains). Publishing to their live domain needs their approval — ask for it by
calling publish normally; it will be queued. Some visual work (selection, breakpoints,
snapshots) needs the Webflow Designer open with the "Webflow MCP Bridge App" running; if a
tool reports it cannot reach the Designer, tell the person how to open it.
Describe what you changed in plain words, not tool names.
"""


class Bridge:
    """Bound to one workspace and one site before the model runs."""

    def __init__(self, org_id: int, project_id: int, site: dict, auto_publish: bool,
                 queue_approval):
        self.org_id, self.project_id, self.site = org_id, project_id, site
        self.auto_publish, self.queue_approval = auto_publish, queue_approval
        self.session: dict = {}

    def prompt(self) -> str:
        return PROMPT.format(site_name=self.site.get("name", "their site"),
                             site_id=self.site.get("id"))

    async def handle(self, name: str, args: dict, turn) -> dict:
        if name == "webflow_list_tools":
            listed = await tools(self.org_id)
            return {"tools": [{"name": t["name"],
                               "description": (t.get("description") or "")[:240]} for t in listed]}

        if name == "webflow_describe_tool":
            for t in await tools(self.org_id):
                if t["name"] == args.get("name"):
                    schema = json.loads(json.dumps(t.get("inputSchema") or {}))
                    for k in INJECTED:
                        (schema.get("properties") or {}).pop(k, None)
                    if "required" in schema:
                        schema["required"] = [r for r in schema["required"] if r not in INJECTED]
                    return {"name": t["name"], "description": t.get("description"),
                            "input_schema": schema}
            return {"ok": False, "error": "no such tool — call webflow_list_tools"}

        if name == "webflow_call":
            tool, arguments = str(args.get("name", "")), args.get("arguments") or {}
            if not any(t["name"] == tool for t in await tools(self.org_id)):
                return {"ok": False, "error": "no such tool — call webflow_list_tools"}
            reasons = needs_approval(tool, arguments, self.auto_publish)
            if reasons:
                aid = await self.queue_approval({
                    "provider": PROVIDER, "tool": tool, "arguments": arguments,
                    "site": self.site, "summary": "; ".join(reasons)})
                turn.log.append("waiting for your approval · " + "; ".join(reasons))
                turn.posts.append(aid)
                if not any(a["kind"] == "review_posts" for a in turn.actions):
                    turn.actions.append({"kind": "review_posts", "label": "Review & approve"})
                return {"ok": True, "queued_for_approval": True, "approval_id": aid,
                        "note": "Not run yet. The person will approve or discard it."}
            text = await call(self.org_id, tool, arguments, self.session)
            turn.log.append(f"webflow · {tool.replace('_', ' ')}")
            return {"ok": not text.startswith("ERROR"), "result": text}

        return {"ok": False, "error": f"unknown tool {name}"}


async def run_approved(org_id: int, payload: dict) -> str:
    """Execute a call a person approved. Re-checks it belongs to this workspace."""
    if payload.get("provider") != PROVIDER:
        raise WebflowError("not a Webflow action")
    return await call(org_id, payload["tool"], payload.get("arguments") or {}, {})
