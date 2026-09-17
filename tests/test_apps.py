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
    assert appfs.verify(t) == (12, 34, "owner")
    assert appfs.verify(appfs.token(12, 34, "public")) == (12, 34, "public")
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


APP_JS = TODO.replace("./components/row.js", "./row.js")
ROW_JS = "import { html } from 'htm/preact';\nexport const Row = ({ item }) => html`<div>${item.title}</div>`;"


async def build(api, tok, pid):
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    await appfs.write(pid, org, {"app.js": APP_JS, "row.js": ROW_JS, "app.json": json.dumps(
        {"collections": {"bookings": {"read": "owner", "write": "public"},
                         "menu": {"read": "public"}}})})
    return org


async def test_publish_serves_isolated_page_with_visitor_rules(api):
    tok, pid = await new_app(api)
    r = await api.post(f"/v1/apps/{pid}/publish", headers=auth(tok))
    assert r.status_code == 409 and "starter" in r.json()["detail"]
    org = await build(api, tok, pid)
    r = await api.post(f"/v1/apps/{pid}/publish", headers=auth(tok))
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    slug = url.rsplit("/", 1)[1]
    assert r.json()["rules"]["bookings"] == {"read": "owner", "write": "public", "manage": "owner"}

    page = await api.get(f"/a/{slug}")
    assert page.status_code == 200
    assert page.headers["content-security-policy"].startswith("sandbox allow-scripts")
    assert "allow-same-origin" not in page.headers["content-security-policy"]
    import re
    public_tok = re.search(r'const API = [^,]+, TOKEN = "([^"]+)"', page.text).group(1)
    assert appfs.verify(public_tok) == (pid, org, "public")
    v = {"X-App-Token": public_tok}
    owner = {"X-App-Token": appfs.token(pid, org)}

    booked = await api.post("/v1/appdata/bookings", headers=v, json={"data": {"name": "Pat", "time": "3pm"}})
    assert booked.status_code == 200
    assert (await api.get("/v1/appdata/bookings", headers=v)).status_code == 403        # can't read others
    bid = booked.json()["id"]
    assert (await api.patch(f"/v1/appdata/bookings/{bid}", headers=v, json={"data": {"time": "4pm"}})).status_code == 403
    assert (await api.delete(f"/v1/appdata/bookings/{bid}", headers=v)).status_code == 403
    assert (await api.get("/v1/appdata/bookings", headers=owner)).json()["items"][0]["name"] == "Pat"
    await api.post("/v1/appdata/menu", headers=owner, json={"data": {"dish": "Tacos"}})
    assert (await api.get("/v1/appdata/menu", headers=v)).json()["items"][0]["dish"] == "Tacos"
    assert (await api.post("/v1/appdata/menu", headers=v, json={"data": {"dish": "spam"}})).status_code == 403
    assert (await api.get("/v1/appdata/secrets", headers=v)).status_code == 403          # undeclared = owner-only

    # editing after publishing doesn't change the live app until republished
    await appfs.write(pid, org, {"app.json": json.dumps({"collections": {"bookings": {"read": "public"}}})})
    assert (await api.get("/v1/appdata/bookings", headers=v)).status_code == 403
    again = await api.post(f"/v1/apps/{pid}/publish", headers=auth(tok))
    assert again.json()["url"] == url                                                   # same address
    assert (await api.get("/v1/appdata/bookings", headers=v)).status_code == 200

    other, _ = await new_app(api)
    assert (await api.post(f"/v1/apps/{pid}/publish", headers=auth(other))).status_code == 404
    assert (await api.post(f"/v1/apps/{pid}/unpublish", headers=auth(tok))).status_code == 200
    assert (await api.get(f"/a/{slug}")).status_code == 404
    assert (await api.get("/a/../etc")).status_code == 404


async def test_publish_blocked_while_app_reports_errors(api):
    tok, pid = await new_app(api)
    org = await build(api, tok, pid)
    await api.post("/v1/appdata/_report", headers={"X-App-Token": appfs.token(pid, org)},
                   json={"message": "TypeError: x is undefined"})
    r = await api.post(f"/v1/apps/{pid}/publish", headers=auth(tok))
    assert r.status_code == 409 and "error" in r.json()["detail"]
    # public visitors can't file error reports that affect the owner
    await api.post("/v1/appdata/_report", headers={"X-App-Token": appfs.token(pid, org, "public")},
                   json={"message": "fake"})
    async with db.conn() as c:
        assert await c.fetchval("SELECT count(*) FROM app_errors WHERE project_id=$1", pid) == 1


async def test_fixing_reported_errors_is_free_a_few_times(api, monkeypatch):
    from app.api.agent import FIX_PREFIX
    tok, pid = await new_app(api)
    org = await build(api, tok, pid)
    err = "Error: taskRow is not ready"
    await api.post("/v1/appdata/_report", headers={"X-App-Token": appfs.token(pid, org)}, json={"message": err})
    monkeypatch.setattr(agent, "_call", FakeModel(*[[text("fixed")]] * 6))
    before = (await api.get("/v1/billing", headers=auth(tok))).json()["balance"]
    for _ in range(3):
        r = (await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": FIX_PREFIX + err})).json()
        assert r["credits"]["spent"] == 0 and r["credits"]["waived"] is True
    assert (await api.get("/v1/billing", headers=auth(tok))).json()["balance"] == before
    fourth = (await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": FIX_PREFIX + err})).json()
    assert fourth["credits"]["spent"] > 0                          # capped
    # extra work tacked onto a fix, or an error that was never reported, is charged
    padded = (await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok),
                             json={"message": FIX_PREFIX + err + ". Also build a CRM"})).json()
    assert padded["credits"]["spent"] > 0
    made_up = (await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok),
                              json={"message": FIX_PREFIX + "Error: invented"})).json()
    assert made_up["credits"]["spent"] > 0
    async with db.conn() as c:
        waived = await c.fetchval("SELECT count(*) FROM credit_ledger WHERE org_id=$1 AND reason='waived'", org)
    assert waived == 3


async def test_estimates_and_monthly_cap(api, monkeypatch):
    tok, pid = await new_app(api)
    est = (await api.get("/v1/billing/estimate?mode=best&kind=app", headers=auth(tok))).json()
    assert est["low"] >= 1 and est["high"] >= est["low"] and est["based_on"] == "typical use"
    assert (await api.get("/v1/billing/estimate?mode=turbo&kind=app", headers=auth(tok))).status_code == 400
    assert (await api.post("/v1/billing/cap", headers=auth(tok), json={"monthly_cap": 5})).status_code == 400
    cap = (await api.post("/v1/billing/cap", headers=auth(tok), json={"monthly_cap": 10})).json()
    assert cap["monthly_cap"] == 10 and cap["used_this_month"] == 0
    monkeypatch.setattr(agent, "_call", FakeModel(*[[text("ok")]] * 10))
    r = await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "build it"})
    assert r.status_code == 402 and "monthly limit" in r.json()["detail"]      # estimate exceeds what's left
    await api.post("/v1/billing/cap", headers=auth(tok), json={"monthly_cap": None})
    r = await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "build it"})
    assert r.status_code == 200
    for _ in range(5):
        await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "again", "mode": "best"})
    learned = (await api.get("/v1/billing/estimate?mode=best&kind=app", headers=auth(tok))).json()
    assert learned["based_on"] == "your recent messages"



async def test_app_turn_must_pass_check_before_replying(api, monkeypatch):
    tok, pid = await new_app(api)
    broken = "import { html, render } from 'htm/preact';\nimport { Row } from './row';\nrender(html`<p>x</p>`, document.getElementById('root'));"
    fixed = broken.replace("'./row'", "'./row.js'")
    row = "import { html } from 'htm/preact';\nexport const Row = () => html`<div>Loading</div>`;"
    model = FakeModel(
        [tool("write_files", {"files": [{"path": "app.js", "content": broken}, {"path": "row.js", "content": row}]})],
        [text("All done.")],                                  # tries to finish without checking
        [tool("check_app", {})],
        [tool("write_files", {"files": [{"path": "app.js", "content": fixed}]})],
        [tool("check_app", {})],
        [text("Built and checked.")])
    monkeypatch.setattr(agent, "_call", model)
    out = (await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "build it"})).json()
    assert "check_app" in model.calls[0]["tools"]
    assert "call check_app" in model.calls[2]["messages"][-1]["content"]
    first = json.loads(model.calls[3]["messages"][-1]["content"][0]["content"])
    assert first["ok"] is False and any(".js extension" in p for p in first["problems"])
    second = json.loads(model.calls[5]["messages"][-1]["content"][0]["content"])
    assert second["ok"] is True
    assert out["messages"][-1]["text"] == "Built and checked."
    assert "checked the app · all clear" in out["log"]


def test_reviewer_catches_common_breakages():
    files = {
        "app.js": "import { html, render } from 'htm/preact';\nimport List from './screens/list.js';\n"
                  "import { Card, Nope } from './components/card.js';\nimport x from 'lodash';\n"
                  "const App = () => <div />;\nrender(html`<p>x</p>`, document.getElementById('root'));",
        "screens/list.js": "export const List = 1;",
        "components/card.js": "import { html } from 'htm/preact';\nexport function Card() { return html`<div>`; }\nconst s = `oops;",
        "app.json": "{bad json",
    }
    rep = appfs.review(files)
    text_ = " ".join(rep["problems"])
    for expect in ("no default export", "doesn't export it", "'lodash' isn't available", "looks like JSX",
                   "odd number of backticks", "app.json isn't valid JSON"):
        assert expect in text_, expect
    assert appfs.review({"app.js": appfs.STARTER["app.js"]})["ok"] is False
