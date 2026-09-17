"""App projects: code files, sandboxed preview, app data isolation."""

import json
import os
import secrets

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("ENV", "development")

from app.core import db                              # noqa: E402
from app.core.config import settings                 # noqa: E402
from app.main import app                             # noqa: E402
from app.services import agent, appfs                # noqa: E402

from tests.test_agent import FakeModel, text, tool   # noqa: E402
from tests.test_isolation import auth, sign_in       # noqa: E402

pytestmark = pytest.mark.asyncio

TODO = """import { html, render } from 'htm/preact';
import { useEffect, useState } from 'preact/hooks';
import { Row } from './components/row.js';
const tasks = window.creai.db.collection('tasks');
function App() {
  const [items, setItems] = useState([]);
  useEffect(() => { tasks.list().then(setItems); }, []);
  return html`<main class="shell"><section class="page">${items.map(i => html`<${Row} item=${i} />`)}</section></main>`;
}
render(html`<${App} />`, document.getElementById('root'));
"""


@pytest_asyncio.fixture
async def api():
    object.__setattr__(settings, "anthropic_key", "test")
    object.__setattr__(settings, "secret_key", "test-secret-key-" + "x" * 16)
    from app.api import agent as agent_routes
    agent_routes._hits.clear()
    from app.api import appdata
    appdata._hits.clear()
    await db.connect()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await db.disconnect()


async def new_app(api):
    tok = await sign_in(api, f"a{secrets.token_hex(3)}@app-shop.io")
    r = await api.post("/v1/projects", headers=auth(tok), json={"name": "Tasks", "path": "app"})
    assert r.status_code == 200, r.text
    return tok, r.json()["id"]


def test_file_rules():
    appfs.check("screens/list.js", "export const x = 1;")
    for bad in ("../x.js", "App.js", "x.html", "a/b/c/d/e.js", "/etc/passwd.js", "x.py"):
        with pytest.raises(appfs.AppError):
            appfs.check(bad, "")
    for bad in ("eval('1')", "new Function('x')", "document.cookie", "localStorage.setItem('a',1)"):
        with pytest.raises(appfs.AppError):
            appfs.check("app.js", bad)
    with pytest.raises(appfs.AppError):
        appfs.check("app.js", "x" * (appfs.MAX_FILE + 1))


def test_app_token_is_scoped_and_signed():
    object.__setattr__(settings, "secret_key", "test-secret-key-" + "x" * 16)
    t = appfs.token(12, 34)
    assert appfs.verify(t) == (12, 34)
    for forged in (t[:-2] + "AA", appfs.token(12, 34).replace("E", "F"), "garbage"):
        if forged != t:
            with pytest.raises(appfs.AppError):
                appfs.verify(forged)


async def test_agent_builds_an_app_and_preview_is_sandboxed(api, monkeypatch):
    tok, pid = await new_app(api)
    model = FakeModel(
        [tool("list_files", {})],
        [tool("write_files", {"files": [
            {"path": "app.js", "content": TODO},
            {"path": "components/row.js", "content": "import { html } from 'htm/preact';\n"
                                                     "export const Row = ({ item }) => html`<div class=\"card\">${item.title}</div>`;"},
            {"path": "../escape.js", "content": "x"}]})],
        [tool("write_files", {"files": [
            {"path": "app.js", "content": TODO},
            {"path": "components/row.js", "content": "import { html } from 'htm/preact';\n"
                                                     "export const Row = ({ item }) => html`<div class=\"card\">${item.title}</div>`;"}]}),
         tool("read_file", {"path": "components/row.js"})],
        [text("A task list that saves to the app's data.")])
    monkeypatch.setattr(agent, "_call", model)
    r = await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "Build a task tracker"})
    assert r.status_code == 200, r.text
    first = model.calls[0]
    assert "WEB APP" in first["system"] and "write_files" in first["tools"]
    assert "draft_posts" not in first["tools"]
    listed = json.loads(model.calls[1]["messages"][-1]["content"][0]["content"])
    assert [f["path"] for f in listed["files"]] == ["app.js"]           # starter was seeded
    refused = json.loads(model.calls[2]["messages"][-1]["content"][0]["content"])
    assert refused["ok"] is False and "allowed file name" in refused["error"]
    assert "wrote app.js, components/row.js" in " ".join(r.json()["log"])

    page = await api.get(f"/v1/agent/projects/{pid}/preview", headers=auth(tok))
    csp = page.headers["content-security-policy"]
    assert csp.startswith("sandbox allow-scripts") and "allow-same-origin" not in csp
    body = page.text
    assert '"preact": "https://esm.sh/preact@10.29.8"' in body
    assert "components/row.js" in body and "X-App-Token" in body
    assert "connect-src http://localhost:8080 https://esm.sh" in body or "connect-src" in body
    assert "</script><script>" not in body.split('id="files"')[1].split("</script>")[0]


async def test_app_data_is_isolated_per_app(api):
    tok_a, pid_a = await new_app(api)
    tok_b, pid_b = await new_app(api)
    org_a = (await api.get("/v1/auth/me", headers=auth(tok_a))).json()["active_org"]
    org_b = (await api.get("/v1/auth/me", headers=auth(tok_b))).json()["active_org"]
    ta, tb = appfs.token(pid_a, org_a), appfs.token(pid_b, org_b)
    ha, hb = {"X-App-Token": ta, "Origin": "null"}, {"X-App-Token": tb}

    pre = await api.options("/v1/appdata/tasks", headers={"Origin": "null", "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in pre.headers

    made = await api.post("/v1/appdata/tasks", headers=ha, json={"data": {"title": "Buy milk"}})
    assert made.status_code == 200 and made.headers["access-control-allow-origin"] == "*"
    rid = made.json()["id"]
    assert (await api.get("/v1/appdata/tasks", headers=ha)).json()["items"][0]["title"] == "Buy milk"
    assert (await api.get("/v1/appdata/tasks", headers=hb)).json()["items"] == []
    assert (await api.get(f"/v1/appdata/tasks/{rid}", headers=hb)).status_code == 404
    assert (await api.patch(f"/v1/appdata/tasks/{rid}", headers=hb, json={"data": {"title": "x"}})).status_code == 404
    assert (await api.delete(f"/v1/appdata/tasks/{rid}", headers=hb)).status_code == 404
    upd = await api.patch(f"/v1/appdata/tasks/{rid}", headers=ha, json={"data": {"done": True}})
    assert upd.json()["title"] == "Buy milk" and upd.json()["done"] is True

    assert (await api.get("/v1/appdata/tasks")).status_code == 401
    assert (await api.get("/v1/appdata/tasks", headers={"X-App-Token": "forged"})).status_code == 401
    assert (await api.get("/v1/appdata/Bad-Name", headers=ha)).status_code == 400
    big = {"data": {"x": "y" * 30_000}}
    assert (await api.post("/v1/appdata/tasks", headers=ha, json=big)).status_code == 413
    # the owner's session token is not an app token
    assert (await api.get("/v1/appdata/tasks", headers={"X-App-Token": tok_a})).status_code == 401
    # strict CORS still applies everywhere else
    other = await api.options("/v1/auth/me", headers={"Origin": "null", "Access-Control-Request-Method": "GET"})
    assert other.headers.get("access-control-allow-origin") is None


async def test_app_data_rate_limit(api, monkeypatch):
    from app.api import appdata
    monkeypatch.setattr(appdata, "RATE", (3, 60))
    tok, pid = await new_app(api)
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    h = {"X-App-Token": appfs.token(pid, org)}
    codes = [(await api.get("/v1/appdata/tasks", headers=h)).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429
