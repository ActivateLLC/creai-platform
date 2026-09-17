"""Launch readiness: honest scoring, decisions, agent ideas, apply plans, isolation."""

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
from app.services import agent, appfs, readiness          # noqa: E402

from tests.test_agent import FakeModel, text, tool        # noqa: E402
from tests.test_isolation import auth, sign_in            # noqa: E402

pytestmark = pytest.mark.asyncio

FULL = {"business": "Shine Detailing", "headline": "Your car, spotless, in your driveway",
        "subline": "Mobile car detailing across Milwaukee. We bring water and power to you.",
        "cta": "Book a detail", "layout": "split", "theme": "industrial",
        "palette": {"bg": "#0E2233", "ink": "#EAF2F8", "accent": "#4FD1C5"},
        "hero_image": "https://v3.fal.media/files/hero.jpg",
        "contact": {"phone": "414-555-0100"},
        "sections": [{"kind": "services", "title": "What we do", "items": [{"name": "Full detail", "detail": "Inside and out."}]},
                     {"kind": "testimonials", "items": [{"quote": "Spotless.", "name": "Pat"}]},
                     {"kind": "faq", "items": [{"q": "Do you need water?", "a": "No, we bring it."}]},
                     {"kind": "cta", "body": "Book before winter.", "button": "Book"}]}


@pytest_asyncio.fixture
async def api():
    object.__setattr__(settings, "anthropic_key", "test")
    from app.api import agent as agent_routes
    agent_routes._hits.clear()
    await db.connect()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await db.disconnect()


async def project(api, path="launch", site=None):
    tok = await sign_in(api, f"rd{secrets.token_hex(3)}@ready-{secrets.token_hex(2)}.io")
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    pid = (await api.post("/v1/projects", headers=auth(tok), json={"name": "X", "path": path})).json()["id"]
    if site is not None:
        async with db.conn() as c:
            await c.execute("UPDATE projects SET answers=$1 WHERE id=$2", {"site": site}, pid)
    return tok, org, pid


def keys(rep, done=None):
    return {i["key"] for i in rep["items"] if done is None or i["done"] == done}


async def test_empty_site_is_honestly_low_and_full_site_is_ready(api):
    tok, org, pid = await project(api, site={})
    rep = (await api.get(f"/v1/readiness/{pid}", headers=auth(tok))).json()
    assert rep["kind"] == "site" and rep["percent"] < 10 and rep["stage"]["id"] == "building"
    assert rep["essentials_left"] >= 4
    assert [s["level"] for s in rep["segments"]] == ["essential", "recommended", "golive"]
    assert abs(sum(s["weight"] for s in rep["segments"]) - 100) < 0.01
    todo = [i for i in rep["items"] if not i["done"] and i["level"] != "golive"]
    assert all(i["request"] for i in todo)                      # every open fix can be applied

    tok, org, pid = await project(api, site=FULL)
    rep = (await api.get(f"/v1/readiness/{pid}", headers=auth(tok))).json()
    assert rep["essentials_left"] == 0 and rep["stage"]["id"] == "ready"
    assert keys(rep, done=False) == {"published", "domain", "marketing"}
    assert rep["percent"] == 85


async def test_placeholders_and_quality_are_named(api):
    site = dict(FULL, headline="Welcome to Shine! Elevate your ride", contact={"phone": "[PHONE]"})
    tok, org, pid = await project(api, site=site)
    rep = (await api.get(f"/v1/readiness/{pid}", headers=auth(tok))).json()
    items = {i["key"]: i for i in rep["items"]}
    assert not items["placeholders"]["done"] and "[PHONE]" in items["placeholders"]["request"]
    assert not items["quality"]["done"] and "elevate" in items["quality"]["request"]


async def test_skipping_never_flatters_essentials(api):
    tok, org, pid = await project(api, site=dict(FULL, hero_image="", contact={}))
    before = (await api.get(f"/v1/readiness/{pid}", headers=auth(tok))).json()
    after_rec = (await api.post(f"/v1/readiness/{pid}/decide", headers=auth(tok),
                                json={"item": "hero", "decision": "skip"})).json()
    assert after_rec["percent"] > before["percent"]            # a skipped recommendation stops counting
    after_ess = (await api.post(f"/v1/readiness/{pid}/decide", headers=auth(tok),
                                json={"item": "contact", "decision": "skip"})).json()
    assert after_ess["percent"] == after_rec["percent"]        # a skipped essential still counts as not done
    contact = next(i for i in after_ess["items"] if i["key"] == "contact")
    assert contact["decision"] == "skip" and not contact["done"] and after_ess["essentials_left"] == 1
    later = (await api.post(f"/v1/readiness/{pid}/decide", headers=auth(tok),
                            json={"item": "faq", "decision": "later"})).json()
    assert next(i for i in later["items"] if i["key"] == "faq")["decision"] == "later"
    reset = (await api.post(f"/v1/readiness/{pid}/decide", headers=auth(tok),
                            json={"item": "hero", "decision": None})).json()
    assert reset["percent"] == before["percent"]
    assert (await api.post(f"/v1/readiness/{pid}/decide", headers=auth(tok),
                           json={"item": "x'; drop", "decision": "skip"})).status_code == 422


async def test_publishing_and_domain_move_to_live(api):
    tok, org, pid = await project(api, site=FULL)
    await api.post(f"/v1/sites/{pid}/publish", headers=auth(tok))
    rep = (await api.get(f"/v1/readiness/{pid}", headers=auth(tok))).json()
    assert rep["stage"]["id"] == "live" and rep["published"]
    async with db.conn() as c:
        await c.execute("""INSERT INTO domains (org_id, project_id, name, source, status)
                           VALUES ($1,$2,$3,'registered','live')""", org, pid, f"rd-{secrets.token_hex(3)}.com")
    rep = (await api.get(f"/v1/readiness/{pid}", headers=auth(tok))).json()
    assert rep["stage"]["id"] == "live_domain" and rep["percent"] == 95


async def test_app_checklist(api):
    tok, org, pid = await project(api, path="app")
    rep = (await api.get(f"/v1/readiness/{pid}", headers=auth(tok))).json()
    assert rep["kind"] == "app" and "built" in keys(rep, done=False)
    await appfs.write(pid, org, {"app.js": "import { html, render } from 'htm/preact';\n"
                                           "const t = window.creai.db.collection('bookings');\n"
                                           "render(html`<p>x</p>`, document.getElementById('root'));"})
    rep = (await api.get(f"/v1/readiness/{pid}", headers=auth(tok))).json()
    rules = next(i for i in rep["items"] if i["key"] == "rules")
    assert not rules["done"] and "bookings" in rules["request"]
    await appfs.write(pid, org, {"app.json": json.dumps({"collections": {"bookings": {"write": "public"}}})})
    await api.post("/v1/appdata/_report", headers={"X-App-Token": appfs.token(pid, org)}, json={"message": "boom"})
    rep = (await api.get(f"/v1/readiness/{pid}", headers=auth(tok))).json()
    items = {i["key"]: i for i in rep["items"]}
    assert items["rules"]["done"] and not items["errors"]["done"]


async def test_agent_ideas_apply_plan_and_isolation(api, monkeypatch):
    tok, org, pid = await project(api, site=dict(FULL, contact={}))
    ideas = [{"title": "Add a winter salt-protection package", "why": "Salt season drives demand in Milwaukee.",
              "request": "Add a winter salt-protection package to my services."},
             {"title": "Add a winter salt-protection package", "why": "dup", "request": "dup"},
             {"title": "", "why": "x", "request": "x"}]
    model = FakeModel([tool("suggest_improvements", {"ideas": ideas})], [text("Done.")])
    monkeypatch.setattr(agent, "_call", model)
    out = (await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "polish it"})).json()
    assert "suggest_improvements" in model.calls[0]["tools"] and "suggested 1 improvement" in out["log"]
    rep = (await api.get(f"/v1/readiness/{pid}", headers=auth(tok))).json()
    assert [s["title"] for s in rep["suggestions"]] == ["Add a winter salt-protection package"]
    sid = rep["suggestions"][0]["id"]
    before = rep["percent"]

    plan = (await api.post(f"/v1/readiness/{pid}/plan", headers=auth(tok),
                           json={"items": ["contact", f"s:{sid}", "published", "nonsense"]})).json()
    assert plan["message"].startswith("Please make these improvements to my site:")
    assert "1. Add my contact details" in plan["message"] and "2. Add a winter salt" in plan["message"]
    assert "published" not in plan["message"] and plan["suggestion_ids"] == [sid]
    assert plan["estimate"]["high"] >= plan["estimate"]["low"] > 0
    await api.post(f"/v1/readiness/{pid}/applied", headers=auth(tok), json={"items": [f"s:{sid}"]})
    rep = (await api.get(f"/v1/readiness/{pid}", headers=auth(tok))).json()
    assert rep["suggestions"] == [] and rep["percent"] == before      # ideas never move the score

    # a declined idea is passed to the agent so it isn't offered again
    await readiness.add_suggestions(org, pid, [{"title": "Add a loyalty card", "why": "Repeat visits.", "request": "Add it."}])
    rep = (await api.get(f"/v1/readiness/{pid}", headers=auth(tok))).json()
    await api.post(f"/v1/readiness/{pid}/decide", headers=auth(tok), json={"item": f"s:{rep['suggestions'][0]['id']}", "decision": "skip"})
    model2 = FakeModel([text("ok")])
    monkeypatch.setattr(agent, "_call", model2)
    await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "more"})
    assert "Add a loyalty card" in model2.calls[0]["system"]
    assert await readiness.add_suggestions(org, pid, [{"title": "add a loyalty card", "why": "x", "request": "x"}]) == 0

    other, _, _ = await project(api)
    assert (await api.get(f"/v1/readiness/{pid}", headers=auth(other))).status_code == 404
    assert (await api.post(f"/v1/readiness/{pid}/decide", headers=auth(other),
                           json={"item": "hero", "decision": "skip"})).status_code == 404
    assert (await api.post(f"/v1/readiness/{pid}/plan", headers=auth(other),
                           json={"items": ["contact"]})).status_code == 404
    assert (await api.post(f"/v1/readiness/{pid}/plan", headers=auth(tok), json={"items": ["published"]})).status_code == 400


async def test_suggestion_list_stays_short(api):
    tok, org, pid = await project(api, site=FULL)
    for n in range(4):
        await readiness.add_suggestions(org, pid, [{"title": f"Idea {n}-{k}", "why": "w", "request": "r"} for k in range(3)])
    rep = (await api.get(f"/v1/readiness/{pid}", headers=auth(tok))).json()
    assert len(rep["suggestions"]) == readiness.MAX_SUGGESTIONS and rep["suggestions"][0]["title"] == "Idea 3-2"
