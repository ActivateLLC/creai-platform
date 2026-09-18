"""
Agent tests. The model is replaced by a scripted fake, so these check what the
platform does with tool calls — scoping, validation, escaping, approvals — not
what a model happens to say.
"""

import json
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
        self.last = None

    async def __call__(self, messages, tools, system, model, max_tokens=2048):
        import json as _json
        self.calls.append({"tools": [t["name"] for t in tools], "system": system,
                           "messages": _json.loads(_json.dumps(messages)), "model": model})
        # When the script runs out (the agent was asked to keep going), repeat the final reply.
        step = self.steps.pop(0) if self.steps else self.last
        if not any(b.get("type") == "tool_use" for b in step):
            self.last = step
        return {"content": step, "model": model,
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
    assert r.status_code == 200 and "Creai" in r.text
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
    assert "script-src 'none'" in page.headers["content-security-policy"]

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


async def test_chat_and_plan_never_edit_the_site(api, monkeypatch):
    fake = FakeModel([text("A tagline is a short promise.")],
                     [text("1. Tighten the headline\n2. Add pricing\nShall I build it?")])
    monkeypatch.setattr(agent, "_call", fake)
    chat = (await api.post("/v1/agent/draft", json={"message": "what is a tagline", "intent": "chat"})).json()
    assert fake.calls[0]["tools"] == [] and fake.calls[0]["model"] == settings.agent_fast_model
    assert "Mode: CHAT" in fake.calls[0]["system"] and chat["actions"] == []

    plan = (await api.post("/v1/agent/draft", json={"message": "improve it", "intent": "plan"})).json()
    assert fake.calls[1]["tools"] == [] and "Mode: PLAN" in fake.calls[1]["system"]
    assert plan["actions"] == [{"kind": "build_plan", "label": "Build this plan"}]
    assert plan["log"] == []                       # nothing was changed


async def test_unknown_intent_is_rejected(api):
    r = await api.post("/v1/agent/draft", json={"message": "hi", "intent": "deploy"})
    assert r.status_code == 422


async def test_production_says_when_email_cannot_be_sent(api, monkeypatch):
    object.__setattr__(settings, "env", "production")
    object.__setattr__(settings, "resend_key", "")
    try:
        r = await api.post("/v1/auth/start", json={"email": "someone@example-shop.io"})
        assert r.status_code == 503 and "couldn't send" in r.json()["detail"]
        assert "code" not in r.text
    finally:
        object.__setattr__(settings, "env", "development")


async def test_mobile_app_keeps_its_draft_by_header(api, monkeypatch):
    monkeypatch.setattr(agent, "_call", FakeModel([text("Hi.")], [text("Still here.")]))
    first = await api.post("/v1/agent/draft", json={"message": "a bakery"})
    tok = first.headers["x-draft-token"]
    assert tok.startswith("d_")
    api.cookies.clear()
    again = await api.post("/v1/agent/draft", json={"message": "more"}, headers={"X-Draft-Token": tok})
    assert len(again.json()["messages"]) == 4
    assert (await api.get("/v1/agent/draft", headers={"X-Draft-Token": "../../etc"})).json()["messages"] == []
    pre = await api.options("/v1/agent/draft", headers={
        "Origin": "capacitor://localhost", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-draft-token"})
    assert pre.headers.get("access-control-allow-origin") == "capacitor://localhost"
    evil = await api.options("/v1/agent/draft", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert evil.headers.get("access-control-allow-origin") is None


def test_renderer_design_system_is_safe_and_varied():
    from app.services import site as s
    evil = {"business": "<script>x</script>", "headline": '"><img src=x onerror=alert(1)>',
            "layout": "bogus", "theme": "../../etc", "motion": "explode",
            "hero_image": "javascript:alert(1)",
            "sections": [{"kind": "gallery", "items": [
                {"image": "https://evil.example/track.gif", "caption": "x"},
                {"image": "https://v3.fal.media/files/ok.jpeg", "caption": '<b>"hi"</b>'}]},
                {"kind": "stats", "items": [{"value": "<i>9</i>", "label": "x"}]}]}
    spec = s.merge({}, evil)
    assert spec["layout"] == "" and spec["theme"] == "" and spec["motion"] == "subtle"
    assert spec["hero_image"] == ""
    assert [i["image"] for i in spec["sections"][0]["items"]] == ["https://v3.fal.media/files/ok.jpeg"]
    page = s.render(spec)
    assert "<script>" not in page and "onerror=alert" not in page.replace("onerror=alert(1)&gt;", "")
    assert "<img src=x" not in page and "evil.example" not in page and "<b>" not in page
    looks = {s.design_of(s.merge({}, {"business": b})) for b in
             ("Rise & Crumb", "Shine Detailing", "Maple Dental", "Northside Barbers", "Kiln & Clay",
              "Ledger Bookkeeping", "Tidewater Yoga", "Pixel Repair")}
    assert len(looks) >= 5                                   # different businesses, different defaults
    for layout in s.LAYOUTS:
        for motion in s.MOTIONS:
            html = s.render(s.merge({}, {"business": "Shine", "layout": layout, "theme": "studio", "motion": motion}))
            assert f"Creai · {layout} · studio · {motion}" in html
            if motion != "none":
                assert "prefers-reduced-motion:no-preference" in html   # motion always opt-out-able


def test_quality_gate_flags_slop_and_contrast():
    from app.services import site as s
    bad = s.merge({}, {"business": "Acme", "headline": "Welcome to Acme! Elevate your seamless journey today with us now",
                       "palette": {"bg": "#777777", "ink": "#888888", "accent": "#787878"},
                       "sections": [{"kind": "about", "body": "We are passionate about quality."}]})
    issues = " ".join(s.critique(bad))
    for expect in ("elevate", "seamless", "welcome to", "passionate about", "10 or fewer",
                   "greeting", "exclamation", "two sections", "contrast", "accent colour", "automatic"):
        assert expect in issues, expect
    good = s.merge({}, {"business": "Shine", "headline": "Your car, spotless, in your driveway",
                        "layout": "split", "theme": "industrial", "motion": "lively",
                        "palette": {"bg": "#0E2233", "ink": "#EAF2F8", "accent": "#4FD1C5"},
                        "sections": [{"kind": "services", "items": [{"name": "Full detail", "detail": "Inside and out."}]},
                                     {"kind": "cta", "body": "Book before winter.", "button": "Book"}]})
    assert s.critique(good) == []


async def test_update_site_returns_quality_to_the_agent(api, monkeypatch):
    model = FakeModel([tool("update_site", {"business": "Acme", "headline": "Unlock seamless savings"})],
                      [text("Fixed.")])
    monkeypatch.setattr(agent, "_call", model)
    await api.post("/v1/agent/draft", json={"message": "a shop"})
    result = json.loads(model.calls[1]["messages"][-1]["content"][0]["content"])
    assert any("unlock" in q for q in result["quality"]) and result["design"]["layout"] in s_layouts()
    assert "generate_image" in model.calls[0]["tools"]


def s_layouts():
    from app.services import site
    return site.LAYOUTS


async def test_timezone_reaches_agent_and_schedules(api, monkeypatch):
    model = FakeModel([text("ok")])
    monkeypatch.setattr(agent, "_call", model)
    r = await api.post("/v1/agent/draft", json={"message": "hi", "timezone": "America/Chicago"})
    assert r.status_code == 200
    assert "America/Chicago" in model.calls[0]["system"]
    bad = await api.post("/v1/agent/draft", json={"message": "hi", "timezone": "x; DROP TABLE"})
    assert bad.status_code == 422
    from datetime import datetime, timedelta
    naive = (datetime.now() + timedelta(days=3)).replace(hour=9, minute=0, second=0, microsecond=0)
    when = agent._when(naive.isoformat(), "America/Chicago")
    assert when.endswith(("-05:00", "-06:00"))
    assert agent._when(naive.isoformat(), "Not/AZone").endswith("+00:00")


def test_site_preview_and_app_do_not_navigate_to_the_platform():
    from app.services import appfs
    page = appfs.preview({"app.js": "x"}, {}, "tok", "https://app.creai.dev")
    assert "a[href]" in page and "preventDefault" in page


async def test_platform_page_refuses_frames(api):
    r = await api.get("/")
    assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in r.headers["content-security-policy"]


async def test_balance_never_goes_negative(api, monkeypatch):
    from app.services import billing
    from tests.test_isolation import auth, sign_in
    tok = await sign_in(api, f"neg{secrets.token_hex(3)}@example-shop.io")
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    await billing.ensure_signup_grant(org)
    have = await billing.balance(org)
    big = [("claude-fable-5-1", {"input_tokens": 400_000, "output_tokens": 60_000})]   # far more than 150 credits
    spent, left = await billing.charge_usage(org, None, None, big, kind="best:site")
    assert spent == have and left == 0
    spent, left = await billing.charge_usage(org, None, None, big, kind="best:site")
    assert spent == 0 and left == 0



async def test_agent_is_sent_back_to_fix_quality_issues(api, monkeypatch):
    model = FakeModel([tool("update_site", {"business": "Acme", "headline": "Unlock seamless savings"})],
                      [text("Done.")],
                      [tool("update_site", {"headline": "Plumbing fixed the same day in Madison",
                                            "sections": [{"kind": "services", "items": [{"name": "Leaks", "detail": "Found fast."}]},
                                                         {"kind": "cta", "body": "Call now.", "button": "Call"}]})],
                      [text("Fixed the headline.")])
    monkeypatch.setattr(agent, "_call", model)
    out = (await api.post("/v1/agent/draft", json={"message": "a plumber"})).json()
    assert len(model.calls) == 4
    nudge = model.calls[2]["messages"][-1]
    assert nudge["role"] == "user" and "quality check still lists issues" in nudge["content"]
    assert out["messages"][-1]["text"] == "Fixed the headline."
    assert not any("quality check" in m["text"] for m in out["messages"])     # never shown as the person's words


def test_system_prompt_is_current():
    assert "not switched on yet" not in agent.SYSTEM
    assert "Verify" in agent.SYSTEM and "check_app" in agent.APP_EXTRA
