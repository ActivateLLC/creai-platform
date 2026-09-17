"""The reviewer: evidence beats opinion, blockers send the builder back, failures are harmless."""

import json
import os
import secrets

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("ENV", "development")

from app.core import db                                   # noqa: E402
from app.core.config import settings                      # noqa: E402
from app.main import app                                  # noqa: E402
from app.services import agent, appfs, review             # noqa: E402

from tests.test_agent import FakeModel, text, tool        # noqa: E402
from tests.test_isolation import auth, sign_in            # noqa: E402

pytestmark = pytest.mark.asyncio

SITE = {"business": "Shine", "headline": "Your car, spotless, in your driveway",
        "sections": [{"kind": "services", "items": [{"name": "Full detail", "detail": "Inside and out."}]},
                     {"kind": "cta", "body": "Book now.", "button": "Book"}]}


@pytest_asyncio.fixture
async def api():
    object.__setattr__(settings, "anthropic_key", "test")
    object.__setattr__(settings, "render_url", "http://renderer.test:8080")
    object.__setattr__(settings, "render_token", "t")
    from app.api import agent as agent_routes
    agent_routes._hits.clear()
    await db.connect()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await db.disconnect()
    object.__setattr__(settings, "render_url", "")


async def project(api, path="launch", site=None):
    tok = await sign_in(api, f"rv{secrets.token_hex(3)}@review-{secrets.token_hex(2)}.io")
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    pid = (await api.post("/v1/projects", headers=auth(tok), json={"name": "X", "path": path})).json()["id"]
    if site:
        async with db.conn() as c:
            await c.execute("UPDATE projects SET answers=$1 WHERE id=$2", {"site": site}, pid)
    return tok, org, pid


def test_measured_problems_become_blockers_the_model_cannot_soften():
    facts = review._site_facts({"phone": {"overflow": [{"tag": "div", "cls": "grid", "over": 42}],
                                          "tiny_text": [{"text": "Call us", "px": 9.5}],
                                          "low_contrast": [{"text": "Book now", "ratio": 2.1, "px": 16}],
                                          "images_without_alt": 2}})
    assert any("42px past" in f for f in facts) and any("9.5px" in f for f in facts)
    assert any("2.1:1" in f for f in facts) and any("2 image" in f for f in facts)
    merged = review._merge(facts, {"findings": [], "verdict": "ship", "summary": "Looks great!"})
    assert merged["verdict"] == "fix_first" and merged["blockers"] >= 4
    assert all(f["measured"] for f in merged["findings"][:4])

    run = {"rendered": False, "errors": ["ReferenceError: rows is not defined"],
           "interactions": [{"clicked": "Save", "ok": False, "error": "not a function"}],
           "form": {"submitted": True, "page_changed": False}}
    app_facts = review._app_facts(run)
    assert any("rows is not defined" in f for f in app_facts)
    assert any("rendered nothing" in f for f in app_facts)
    assert any("clicking “Save” failed" in f for f in app_facts)
    assert any("nothing on the page changed" in f for f in app_facts)


def test_clean_build_passes_and_findings_are_capped():
    clean = review._merge([], {"findings": [], "verdict": "ship", "summary": "Clear and specific."})
    assert clean["verdict"] == "ship" and clean["blockers"] == 0 and clean["findings"] == []
    many = review._merge([], {"findings": [{"severity": "nice_to_have", "what": f"point {i}", "fix": "x"}
                                           for i in range(20)], "verdict": "ship"})
    assert len(many["findings"]) <= review.MAX_FINDINGS + 4
    junk = review._merge([], {"findings": [{"nope": 1}, {"what": "real", "severity": "invented"}]})
    assert [f["severity"] for f in junk["findings"]] == ["should_fix"]
    assert "MUST FIX" in review.as_instruction(review._merge(["overflow"], {}), "site")


async def test_blocking_review_sends_the_builder_back(api, monkeypatch):
    tok, org, pid = await project(api, site=SITE)
    seen = {}

    async def fake_review(kind, html, context):
        seen["kind"], seen["html"], seen["context"] = kind, html, context
        return review._merge(["phone: <div class=\"grid\"> runs 42px past the screen edge"],
                             {"findings": [{"severity": "should_fix", "what": "Headline is generic",
                                            "fix": "Name the city."}], "verdict": "fix_first", "summary": "Needs work"})
    monkeypatch.setattr(review, "safe_review", fake_review)
    model = FakeModel([tool("update_site", {"business": "Shine", "headline": "Mobile detailing in Milwaukee",
                                            "sections": SITE["sections"]})],
                      [text("Built it.")],
                      [tool("update_site", {"subline": "We come to you across Milwaukee."})],
                      [text("Fixed the overflow and named the city.")])
    monkeypatch.setattr(agent, "_call", model)
    out = (await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "build my site"})).json()
    assert seen["kind"] == "site" and "Your car" not in seen["html"]
    nudge = model.calls[2]["messages"][-1]
    assert "MUST FIX" in nudge["content"] and "42px" in nudge["content"] and "Name the city" in nudge["content"]
    assert out["messages"][-1]["text"] == "Fixed the overflow and named the city."
    assert not any("MUST FIX" in m["text"] for m in out["messages"])       # never shown as the person's words
    assert out["review"]["blockers"] == 1 and out["review"]["verdict"] == "fix_first"
    assert any("reviewed on phone and desktop" in line for line in out["log"])


async def test_clean_review_does_not_add_a_round(api, monkeypatch):
    tok, org, pid = await project(api, site=SITE)

    async def clean(kind, html, context):
        return review._merge([], {"findings": [], "verdict": "ship", "summary": "Good"})
    monkeypatch.setattr(review, "safe_review", clean)
    model = FakeModel([tool("update_site", {"business": "Shine", "headline": "Mobile detailing in Milwaukee",
                                            "sections": SITE["sections"]})], [text("Done.")])
    monkeypatch.setattr(agent, "_call", model)
    out = (await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "build"})).json()
    assert len(model.calls) == 2 and out["messages"][-1]["text"] == "Done."
    assert out["review"] is None and any("looks good" in line for line in out["log"])


async def test_a_broken_reviewer_never_breaks_a_build(api, monkeypatch):
    tok, org, pid = await project(api, site=SITE)

    async def boom(*a, **kw):
        raise RuntimeError("renderer down")
    monkeypatch.setattr(review, "review_site", boom)
    monkeypatch.setattr(review, "review_app", boom)
    model = FakeModel([tool("update_site", {"business": "Shine", "headline": "Mobile detailing in Milwaukee",
                                            "sections": SITE["sections"]})], [text("Done.")])
    monkeypatch.setattr(agent, "_call", model)
    out = await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "build"})
    assert out.status_code == 200 and out.json()["messages"][-1]["text"] == "Done."
    assert await review.safe_review("site", "<html></html>", "{}") is None


async def test_chat_and_marketing_turns_are_not_reviewed(api, monkeypatch):
    tok, org, pid = await project(api, site=SITE)
    calls = []

    async def counted(kind, html, context):
        calls.append(kind)
        return None
    monkeypatch.setattr(review, "safe_review", counted)
    monkeypatch.setattr(agent, "_call", FakeModel([text("Just answering.")]))
    await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "what is a brand kit?", "intent": "chat"})
    assert calls == []                                     # nothing was built, nothing to review


async def test_app_review_runs_the_app(api, monkeypatch):
    tok, org, pid = await project(api, path="app")
    seen = {}

    async def fake_review(kind, html, context):
        seen["kind"], seen["html"] = kind, html
        return review._merge(["error while running: ReferenceError: rows is not defined"], {"verdict": "fix_first"})
    monkeypatch.setattr(review, "safe_review", fake_review)
    good = ("import { html, render } from 'htm/preact';\n"
            "render(html`<p>Hi</p>`, document.getElementById('root'));")
    model = FakeModel([tool("write_files", {"files": [{"path": "app.js", "content": good}]})],
                      [tool("check_app", {})],
                      [text("Built.")],
                      [tool("write_files", {"files": [{"path": "app.js", "content": good + "\n// fixed"}]})],
                      [tool("check_app", {})],
                      [text("Fixed the error.")])
    monkeypatch.setattr(agent, "_call", model)
    out = (await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "build an app"})).json()
    assert seen["kind"] == "app" and "importmap" in seen["html"]            # the real preview page was run
    assert out["messages"][-1]["text"] == "Fixed the error."
    assert out["review"]["blockers"] == 1
