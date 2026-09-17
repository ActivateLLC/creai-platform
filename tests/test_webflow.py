"""
Webflow connection tests. A fake Webflow (OAuth + MCP over Streamable HTTP)
stands in for the real one, so these check what CreAI does: PKCE, sealed
tokens, refresh, the approval policy, injection of Webflow's required
fields, and that no workspace can reach another's site.
"""

import base64
import hashlib
import json
import os
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("ENV", "development")

from app.core import db                               # noqa: E402
from app.core.config import settings                  # noqa: E402
from app.main import app                              # noqa: E402
from app.services import agent, webflow               # noqa: E402

from tests.test_agent import FakeModel, text, tool    # noqa: E402
from tests.test_isolation import auth, sign_in        # noqa: E402

pytestmark = pytest.mark.asyncio

SITE = {"id": "site_" + secrets.token_hex(6), "displayName": "Acme Studio", "shortName": "acme-studio"}
SCHEMA = {"type": "object", "properties": {
    "actions": {"type": "array"}, "agent_id": {"type": "string"},
    "session_id": {"type": "string"}, "context": {"type": "string"}},
    "required": ["actions", "agent_id", "session_id", "context"]}


class FakeWebflow:
    def __init__(self):
        self.calls, self.challenges, self.valid = [], {}, set()
        self.registered = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/oauth/register":
            self.registered += 1
            return httpx.Response(201, json={"client_id": "cid_test"})
        if path == "/oauth/token":
            form = parse_qs(req.content.decode())
            grant = form.get("grant_type", [""])[0]
            if grant == "authorization_code":
                code = form["code"][0]
                verifier = form["code_verifier"][0]
                digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if self.challenges.get(code) != digest or form["resource"][0] != webflow.MCP_URL:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                tok = "at_" + secrets.token_hex(4)
                self.valid.add(tok)
                return httpx.Response(200, json={"access_token": tok, "refresh_token": "rt_" + code,
                                                  "expires_in": 3600})
            if grant == "refresh_token":
                tok = "at_" + secrets.token_hex(4)
                self.valid.add(tok)
                return httpx.Response(200, json={"access_token": tok, "expires_in": 3600})
            return httpx.Response(200, json={})           # revocation
        if path == "/mcp":
            if req.method == "DELETE":
                return httpx.Response(200)
            token = req.headers.get("authorization", "").removeprefix("Bearer ")
            if token not in self.valid:
                return httpx.Response(401, headers={"www-authenticate": "Bearer"})
            msg = json.loads(req.content)
            if "id" not in msg:
                return httpx.Response(202)
            method = msg["method"]
            if method == "initialize":
                return httpx.Response(200, headers={"mcp-session-id": "sess1"},
                                      json={"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-06-18"}})
            if method == "tools/list":
                tools = [{"name": n, "description": d, "inputSchema": SCHEMA} for n, d in (
                    ("data_sites_tool", "Sites: list, get, publish"),
                    ("data_pages_tool", "Pages: list, update settings"),
                    ("data_cms_tool", "CMS collections and items"))]
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": tools}})
            if method == "tools/call":
                self.calls.append({"token": token, **msg["params"]})
                args = msg["params"]["arguments"]
                keys = [k for a in args.get("actions", []) for k in a if k != "label"]
                if "list_sites" in keys:
                    body = {"sites": [SITE]}
                else:
                    body = {"ok": True, "did": keys}
                payload = {"jsonrpc": "2.0", "id": msg["id"], "result": {
                    "content": [{"type": "text", "text": "[session_id issued: ses_" + "a" * 27 + "] "
                                 + json.dumps(body)}]}}
                # answer tool calls as an event stream, like the real server can
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=f"event: message\ndata: {json.dumps(payload)}\n\n".encode())
        return httpx.Response(404)


@pytest_asyncio.fixture
async def env(monkeypatch):
    object.__setattr__(settings, "anthropic_key", "test")
    object.__setattr__(settings, "secret_key", "test-secret-key-" + "x" * 16)
    await db.connect()
    webflow._tool_cache.clear()
    fake = FakeWebflow()
    real = httpx.AsyncClient

    def patched(**kw):
        return real(transport=httpx.MockTransport(fake), **kw)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        monkeypatch.setattr(httpx, "AsyncClient", patched)
        yield api, fake
    await db.disconnect()


async def connect(api, fake, email=None):
    tok = await sign_in(api, email or f"w{secrets.token_hex(3)}@acme-studio.io")
    r = await api.post("/v1/connections/webflow/start", headers=auth(tok))
    assert r.status_code == 200, r.text
    q = parse_qs(urlparse(r.json()["url"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["resource"] == [webflow.MCP_URL]
    assert q["redirect_uri"][0].endswith("/v1/connections/webflow/callback")
    code = "code_" + secrets.token_hex(4)
    fake.challenges[code] = q["code_challenge"][0]
    cb = await api.get("/v1/connections/webflow/callback",
                       params={"code": code, "state": q["state"][0]})
    assert cb.status_code == 303 and "status=ok" in cb.headers["location"]
    return tok, q["state"][0], code


async def pick_site(api, tok):
    r = await api.post("/v1/connections/webflow/select", headers=auth(tok), json={"site_id": SITE["id"]})
    assert r.status_code == 200, r.text
    return r.json()["project_id"]


# ---------------------------------------------------------------- connecting

async def test_connect_seals_the_token_and_lists_sites(env):
    api, fake = env
    tok, state, _ = await connect(api, fake)
    status = (await api.get("/v1/connections", headers=auth(tok))).json()
    assert status[0]["provider"] == "webflow" and status[0]["connected"]
    assert any(not c["available"] for c in status)            # others marked coming soon

    async with db.conn() as c:
        blob = await c.fetchval("SELECT secret_enc FROM connections ORDER BY id DESC LIMIT 1")
    assert b"at_" not in bytes(blob) and b"rt_" not in bytes(blob)

    sites = (await api.get("/v1/connections/webflow/sites", headers=auth(tok))).json()["sites"]
    assert sites[0]["id"] == SITE["id"] and sites[0]["name"] == "Acme Studio"
    assert fake.registered <= 1                                # registered once, reused

    pid = await pick_site(api, tok)
    assert await pick_site(api, tok) == pid                    # same site, same project
    thread = (await api.get(f"/v1/agent/projects/{pid}", headers=auth(tok))).json()
    assert thread["project"]["source"] == "webflow"
    assert thread["project"]["site"]["designer_url"] == "https://webflow.com/design/acme-studio"


async def test_state_is_single_use(env):
    api, fake = env
    tok, state, code = await connect(api, fake)
    again = await api.get("/v1/connections/webflow/callback", params={"code": code, "state": state})
    assert "status=failed" in again.headers["location"]
    cancelled = await api.get("/v1/connections/webflow/callback", params={"error": "access_denied"})
    assert "status=cancelled" in cancelled.headers["location"]


async def test_expired_token_is_refreshed_transparently(env):
    api, fake = env
    tok, _, _ = await connect(api, fake)
    fake.valid.clear()                                         # Webflow revokes the old token
    r = await api.get("/v1/connections/webflow/sites", headers=auth(tok))
    assert r.status_code == 200 and r.json()["sites"]
    assert fake.calls[-1]["token"] in fake.valid


# ---------------------------------------------------------------- editing

async def test_agent_edits_freely_and_live_publish_waits(env, monkeypatch):
    api, fake = env
    tok, _, _ = await connect(api, fake)
    pid = await pick_site(api, tok)
    before = len(fake.calls)

    live = {"actions": [{"label": "go live", "publish_site": {"site_id": SITE["id"], "customDomains": ["acme.com"]}}]}
    model = FakeModel(
        [tool("webflow_list_tools", {})],
        [tool("webflow_describe_tool", {"name": "data_pages_tool"})],
        [tool("webflow_call", {"name": "data_pages_tool", "arguments": {"actions": [
            {"label": "seo", "update_page_settings": {"page_id": "p1", "seo": {"title": "Acme"}}}]}}),
         tool("webflow_call", {"name": "data_sites_tool", "arguments": {"actions": [
             {"label": "stage", "publish_site": {"site_id": SITE["id"], "publishToWebflowSubdomain": True}}]}}),
         tool("webflow_call", {"name": "data_sites_tool", "arguments": live}),
         tool("webflow_call", {"name": "data_cms_tool", "arguments": {"actions": [
             {"label": "rm", "delete_collection_item": {"collection_id": "c", "item_id": "i"}}]}}),
         tool("update_site", {"headline": "should not exist here"})],
        [text("Updated the SEO title and published a staging preview.")])
    monkeypatch.setattr(agent, "_call", model)

    r = await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "fix my SEO and go live"})
    assert r.status_code == 200, r.text
    out = r.json()

    names = model.calls[0]["tools"]
    assert "webflow_call" in names and "update_site" not in names
    assert "Acme Studio" in model.calls[0]["system"]

    described = json.loads(model.calls[2]["messages"][-1]["content"][0]["content"])
    assert "agent_id" not in described["input_schema"]["properties"]
    assert "session_id" not in described["input_schema"]["required"]

    ran = fake.calls[before:]
    ran_keys = [k for c in ran for a in c["arguments"]["actions"] for k in a if k != "label"]
    assert "update_page_settings" in ran_keys and ran_keys.count("publish_site") == 1
    assert "delete_collection_item" not in ran_keys
    for c in ran:
        a = c["arguments"]
        assert a["agent_id"].endswith("|creai|w" + str(await org_of(api, tok)))
        assert a["context"] and a["session_id"].startswith(("start", "ses_"))
    assert ran[-1]["arguments"]["session_id"] == "ses_" + "a" * 27     # continuity carried

    assert len(out["posts"]) == 2
    assert {"kind": "review_posts", "label": "Review & approve"} in out["actions"]

    queue = (await api.get("/v1/approvals", headers=auth(tok))).json()
    held = {q["id"]: q for q in queue if q["kind"] == "webflow_action"}
    assert len(held) == 2
    live_id = next(i for i, q in held.items() if "acme.com" in q["payload"]["summary"])

    done = await api.post(f"/v1/approvals/{live_id}/approve", headers=auth(tok))
    assert done.status_code == 200 and done.json()["state"] == "done"
    assert fake.calls[-1]["arguments"]["actions"][0]["publish_site"]["customDomains"] == ["acme.com"]
    again = await api.post(f"/v1/approvals/{live_id}/approve", headers=auth(tok))
    assert again.status_code == 404                                    # runs once


async def test_auto_publish_setting_lets_live_publish_run(env, monkeypatch):
    api, fake = env
    tok, _, _ = await connect(api, fake)
    pid = await pick_site(api, tok)
    assert (await api.post("/v1/connections/webflow/settings", headers=auth(tok),
                           json={"auto_publish": True})).json()["auto_publish"] is True
    monkeypatch.setattr(agent, "_call", FakeModel(
        [tool("webflow_call", {"name": "data_sites_tool", "arguments": {"actions": [
            {"label": "live", "publish_site": {"site_id": SITE["id"], "customDomains": ["acme.com"]}}]}})],
        [text("Published.")]))
    out = (await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "publish"})).json()
    assert out["posts"] == []
    assert fake.calls[-1]["arguments"]["actions"][0]["publish_site"]["customDomains"] == ["acme.com"]


def test_policy_holds_only_what_it_should():
    def held(key, params=None, auto=False):
        return webflow.needs_approval("t", {"actions": [{"label": "x", key: params or {}}]}, auto)
    assert held("publish_site", {"site_id": "s", "publishToWebflowSubdomain": True}) == []
    assert held("publish_site", {"site_id": "s", "customDomains": ["a.com"]})
    assert held("publish_site", {"site_id": "s", "customDomains": ["a.com"]}, auto=True) == []
    assert held("publish_branch") == []
    for k in ("delete_collection_item", "delete_asset", "remove_style", "merge_branch",
              "create_webhook", "unpublish_collection_items", "publish_collection_items"):
        assert held(k), k
    assert held("delete_page", auto=True)                       # auto-publish never auto-deletes
    for k in ("update_page_settings", "create_collection_item", "list_pages", "set_text",
              "create_page", "update_collection_item", "bulk_update_pages", "create_style",
              "publish_branch", "create_branch", "upload_asset"):
        assert held(k) == [], k


# ---------------------------------------------------------------- isolation

async def org_of(api, tok):
    return (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]


async def test_workspaces_cannot_reach_each_others_webflow(env, monkeypatch):
    api, fake = env
    a, _, _ = await connect(api, fake)
    pid = await pick_site(api, a)
    b = await sign_in(api, f"b{secrets.token_hex(3)}@other-co.io")

    assert (await api.get("/v1/connections", headers=auth(b))).json()[0]["connected"] is False
    assert (await api.get("/v1/connections/webflow/sites", headers=auth(b))).status_code == 409
    assert (await api.post("/v1/connections/webflow/select", headers=auth(b),
                           json={"site_id": SITE["id"]})).status_code == 409
    assert (await api.post(f"/v1/agent/projects/{pid}", headers=auth(b),
                           json={"message": "hi"})).status_code == 404
    assert (await api.get("/v1/connections", headers=auth(b, await org_of(api, a)))).status_code == 404

    monkeypatch.setattr(agent, "_call", FakeModel(
        [tool("webflow_call", {"name": "data_cms_tool", "arguments": {"actions": [
            {"label": "rm", "delete_collection_item": {"collection_id": "c", "item_id": "i"}}]}})],
        [text("Queued.")]))
    held = (await api.post(f"/v1/agent/projects/{pid}", headers=auth(a), json={"message": "delete"})).json()["posts"][0]
    assert (await api.post(f"/v1/approvals/{held}/approve", headers=auth(b))).status_code == 404


async def test_disconnect_removes_access(env):
    api, fake = env
    tok, _, _ = await connect(api, fake)
    pid = await pick_site(api, tok)
    assert (await api.delete("/v1/connections/webflow", headers=auth(tok))).status_code == 200
    assert (await api.get("/v1/connections", headers=auth(tok))).json()[0]["connected"] is False
    r = await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "edit"})
    assert r.status_code == 409 and "reconnect" in r.json()["detail"]
