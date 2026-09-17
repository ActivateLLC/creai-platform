"""
Agent tests. The model is replaced by a scripted fake, so these check what the
platform does with tool calls — scoping, validation, escaping, approvals — not
what a model happens to say.
"""

import os
import secrets

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("ENV", "development")

from app.core import db                      # noqa: E402
from app.core.config import settings         # noqa: E402
from app.main import app                     # noqa: E402
from app.services import agent               # noqa: E402

from tests.test_isolation import auth, sign_in    # noqa: E402

pytestmark = pytest.mark.asyncio


def tool(name, args):
    return {"type": "tool_use", "id": "tu_" + secrets.token_hex(4), "name": name, "input": args}


def text(t):
    return {"type": "text", "text": t}


class FakeModel:
    """Plays back scripted responses, one per model call."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    async def __call__(self, messages, tools, system, model):
        self.calls.append({"tools": [t["name"] for t in tools], "system": system,
                           "messages": messages, "model": model})
        return {"content": self.steps.pop(0), "model": model,
                "usage": {"input_tokens": 3000, "output_tokens": 500}}


@pytest_asyncio.fixture
async def api(monkeypatch):
    object.__setattr__(settings, "anthropic_key", "test-key")
    from app.api import agent as agent_routes
    agent_routes._hits.clear()
    await db.connect()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await db.disconnect()


async def test_front_page_and_logo_are_served(api):
    r = await api.get("/")
    assert r.status_code == 200 and "CreAI" in r.text
    r = await api.get("/logo.svg")
    assert r.status_code == 200 and r.text.lstrip().startswith("<svg")


async def test_anonymous_turn_builds_the_site(api, monkeypatch):
    fake = FakeModel(
        [tool("update_site", {"business": "Milwaukee Shine",
                              "headline": "Your car, spotless.",
                              "sections": [{"kind": "services", "title": "Services",
                                            "items": [{"name": "Full detail", "price": "[PRICE]"}]}]}),
         tool("save_answer", {"key": "City", "value": "Milwaukee"})],
        [text("Here is a first version. How should it sound?")],
    )
    monkeypatch.setattr(agent, "_call", fake)

    r = await api.post("/v1/agent/draft", json={"message": "Mobile car detailing in Milwaukee"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["site"]["headline"] == "Your car, spotless."
    assert out["messages"][-1]["text"].startswith("Here is a first version")
    assert any("updated site" in line for line in out["log"])
    # anonymous turns never get the post-drafting tool
    assert "draft_posts" not in fake.calls[0]["tools"]

    page = await api.get("/v1/agent/draft/preview")
    assert "Your car, spotless." in page.text and "Full detail" in page.text
    assert "script-src" not in page.headers["content-security-policy"]

    again = await api.get("/v1/agent/draft")
    assert [m["role"] for m in again.json()["messages"]] == ["user", "assistant"]


async def test_model_output_cannot_inject_markup(api, monkeypatch):
    monkeypatch.setattr(agent, "_call", FakeModel(
        [tool("update_site", {"headline": "<script>alert(1)</script>",
                              "palette": {"bg": "red;}</style><script>x()</script>"},
                              "sections": [{"kind": "raw_html", "body": "<iframe>"}]})],
        [text("Done.")]))
    await api.post("/v1/agent/draft", json={"message": "hi"})
    page = (await api.get("/v1/agent/draft/preview")).text
    assert "<script" not in page and "<iframe" not in page
    assert "&lt;script&gt;" in page


async def test_agent_cannot_invent_actions(api, monkeypatch):
    monkeypatch.setattr(agent, "_call", FakeModel(
        [tool("suggest_action", {"kind": "delete_everything", "label": "Go"}),
         tool("suggest_action", {"kind": "sign_in", "label": "Save your work"}),
         tool("publish_site", {})],
        [text("Ok.")]))
    out = (await api.post("/v1/agent/draft", json={"message": "hi"})).json()
    assert out["actions"] == [{"kind": "sign_in", "label": "Save your work"}]


async def test_unconfigured_agent_says_so(api, monkeypatch):
    object.__setattr__(settings, "anthropic_key", "")
    r = await api.post("/v1/agent/draft", json={"message": "hi"})
    assert r.status_code == 503


async def test_posts_are_queued_not_published_and_scoped(api, monkeypatch):
    a = await sign_in(api, f"ana{secrets.token_hex(3)}@alpha-studio.io")
    b = await sign_in(api, f"ben{secrets.token_hex(3)}@beta-studio.io")
    pid = (await api.post("/v1/projects", headers=auth(a),
                          json={"name": "Alpha launch"})).json()["id"]

    fake = FakeModel(
        [tool("draft_posts", {"posts": [
            {"network": "instagram", "text": "Salt season is coming.", "when": "Mon 9:00"},
            {"network": "instagram", "text": "Book a full detail."}]})],
        [text("Two drafts are waiting for you.")])
    monkeypatch.setattr(agent, "_call", fake)

    out = await api.post(f"/v1/agent/projects/{pid}", headers=auth(a),
                         json={"message": "Draft two posts"})
    assert out.status_code == 200, out.text
    body = out.json()
    assert len(body["posts"]) == 2
    assert {"kind": "review_posts", "label": "Review drafts"} in body["actions"]
    assert "draft_posts" in fake.calls[0]["tools"]

    queue = (await api.get("/v1/approvals", headers=auth(a))).json()
    assert len(queue) == 2 and all(q["state"] == "pending" for q in queue)

    # Bob can neither talk to, read, nor preview Alice's project...
    for method, path in (("post", f"/v1/agent/projects/{pid}"),
                         ("get", f"/v1/agent/projects/{pid}"),
                         ("get", f"/v1/agent/projects/{pid}/preview")):
        kw = {"json": {"message": "hi"}} if method == "post" else {}
        r = await getattr(api, method)(path, headers=auth(b), **kw)
        assert r.status_code == 404, (path, r.status_code)
    # ...and sees none of her drafts.
    assert (await api.get("/v1/approvals", headers=auth(b))).json() == []


async def test_draft_work_survives_sign_up(api, monkeypatch):
    monkeypatch.setattr(agent, "_call", FakeModel(
        [tool("update_site", {"business": "Crumb & Co", "headline": "Cakes for every day"})],
        [text("First version is up.")]))
    await api.post("/v1/agent/draft", json={"message": "A home bakery"})

    token = await sign_in(api, f"cara{secrets.token_hex(3)}@crumb-co.io")
    claimed = (await api.post("/v1/drafts/claim", headers=auth(token))).json()
    assert claimed["claimed"]
    pid = claimed["project_id"]

    thread = (await api.get(f"/v1/agent/projects/{pid}", headers=auth(token))).json()
    assert thread["site"]["headline"] == "Cakes for every day"
    assert thread["messages"][0]["text"] == "A home bakery"
    page = await api.get(f"/v1/agent/projects/{pid}/preview", headers=auth(token))
    assert "Cakes for every day" in page.text


async def test_anonymous_rate_limit(api, monkeypatch):
    from app.api import agent as agent_routes
    monkeypatch.setattr(agent_routes, "IP_CAP", 2)

    async def quick(*_a):
        return {"content": [text("ok")], "usage": {}}
    monkeypatch.setattr(agent, "_call", quick)
    codes = [(await api.post("/v1/agent/draft", json={"message": "hi"})).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]


def test_thread_trim_starts_on_a_user_message():
    thread = [{"role": "user", "content": "first"},
              {"role": "assistant", "content": [tool("update_site", {})]},
              {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x",
                                            "content": "{}"}]},
              {"role": "assistant", "content": [text("done")]},
              {"role": "user", "content": "second"}]
    old = agent.MAX_THREAD
    try:
        agent.MAX_THREAD = 3
        t = agent.trim(thread)
    finally:
        agent.MAX_THREAD = old
    assert t == [{"role": "user", "content": "second"}]
