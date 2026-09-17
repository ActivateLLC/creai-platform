"""Marketing: brand kit from a site, scheduled drafts, calendar, and a
website reader that refuses to be pointed at internal addresses."""

import asyncio
import os
import secrets
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("ENV", "development")

from app.core import db                              # noqa: E402
from app.core.config import settings                 # noqa: E402
from app.main import app                             # noqa: E402
from app.services import agent, brand                # noqa: E402

from tests.test_agent import FakeModel, text, tool   # noqa: E402
from tests.test_isolation import auth, sign_in       # noqa: E402

pytestmark = pytest.mark.asyncio

PAGE = """<html><head><title>Shine Detailing — Milwaukee</title>
<meta name="description" content="Mobile car detailing that comes to you.">
<meta property="og:image" content="/og.jpg"><meta name="theme-color" content="#0f2a2e">
<style>.a{color:#0f2a2e}.b{color:#f2c14e}.c{color:#0f2a2e}</style>
<script>var secret = "do not read";</script></head>
<body><h1>Your car, spotless</h1><h2>Full detail</h2><p>We come to your driveway.</p></body></html>"""


@pytest_asyncio.fixture
async def api(monkeypatch):
    object.__setattr__(settings, "anthropic_key", "test")
    from app.api import agent as agent_routes
    agent_routes._hits.clear()
    await db.connect()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await db.disconnect()


def fake_web(monkeypatch, pages):
    real = httpx.AsyncClient

    def handler(req):
        body = pages.get(str(req.url))
        if isinstance(body, tuple):
            return httpx.Response(body[0], headers={"location": body[1]})
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=body.encode())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(brand, "_public", lambda host: host.endswith(".example") or host.endswith(".test-shop.com"))


def test_extract_reads_what_matters():
    page = brand.extract("https://shine.example/", PAGE)
    assert page["title"].startswith("Shine Detailing")
    assert page["description"] == "Mobile car detailing that comes to you."
    assert page["headings"][:2] == ["Your car, spotless", "Full detail"]
    assert page["colors"][0] == "#0f2a2e" and page["image"] == "https://shine.example/og.jpg"
    assert "do not read" not in page["text"] and "driveway" in page["text"]


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost/admin", "http://169.254.169.254/latest",
                                 "http://10.0.0.5/", "ftp://shine.example", "https://shine.example:8443/",
                                 "javascript:alert(1)"])
async def test_reader_refuses_internal_or_odd_addresses(url):
    with pytest.raises(brand.FetchError):
        await brand.read(url)


async def test_redirects_are_rechecked(monkeypatch):
    fake_web(monkeypatch, {"https://shine.example/": (302, "http://internal.corp/secret")})
    with pytest.raises(brand.FetchError):
        await brand.read("shine.example")


async def test_marketing_project_builds_brand_kit_and_schedules_posts(api, monkeypatch):
    fake_web(monkeypatch, {"https://shine.example/": PAGE})
    tok = await sign_in(api, f"m{secrets.token_hex(3)}@test-shop.com")
    r = await api.post("/v1/marketing/start", headers=auth(tok), json={"website": "shine.example"})
    assert r.status_code == 200, r.text
    pid = r.json()["project_id"]
    assert (await api.post("/v1/marketing/start", headers=auth(tok), json={"website": "shine.example"})).json()["project_id"] == pid

    soon = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()
    later = (datetime.now(timezone.utc) + timedelta(days=5)).replace(microsecond=0).isoformat()
    model = FakeModel(
        [tool("read_website", {"url": "https://shine.example/"})],
        [tool("save_brand_kit", {"name": "Shine Detailing", "voice": "Friendly, practical",
                                 "services": ["Full detail"], "colors": ["#0f2a2e", "red"],
                                 "links": [{"label": "Book", "url": "https://shine.example/book"},
                                           {"label": "bad", "url": "javascript:x"}]})],
        [tool("draft_posts", {"posts": [
            {"network": "instagram", "text": "Salt season prep", "scheduled_for": soon, "link": "https://shine.example/"},
            {"network": "facebook", "text": "Driveway detailing", "scheduled_for": later},
            {"network": "instagram", "text": "Time traveller", "scheduled_for": "1999-01-01T10:00:00Z"}]})],
        [text("Brand kit saved and three posts drafted.")])
    monkeypatch.setattr(agent, "_call", model)
    out = (await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok),
                          json={"message": "Plan my next two weeks"})).json()

    first = model.calls[0]
    assert "MARKETING" in first["system"] and "update_site" not in first["tools"]
    assert {"read_website", "save_brand_kit", "draft_posts"} <= set(first["tools"])
    page = model.calls[1]["messages"][-1]["content"][0]["content"]
    assert "Shine Detailing" in page
    assert len(out["posts"]) == 3
    nxt = FakeModel([text("Noted.")])
    monkeypatch.setattr(agent, "_call", nxt)
    await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "thanks"})
    assert "Brand kit" in nxt.calls[0]["system"] and "Friendly, practical" in nxt.calls[0]["system"]

    thread = (await api.get(f"/v1/agent/projects/{pid}", headers=auth(tok))).json()["project"]
    assert thread["brand"]["colors"] == ["#0f2a2e"]
    assert [l["label"] for l in thread["brand"]["links"]] == ["Book"]
    assert thread["path"] == "market" and thread["website"] == "https://shine.example"

    cal = (await api.get(f"/v1/marketing/calendar?project_id={pid}", headers=auth(tok))).json()["posts"]
    assert [p["text"] for p in cal] == ["Salt season prep", "Driveway detailing", "Time traveller"]
    assert cal[0]["scheduled_for"] and cal[1]["scheduled_for"] and cal[2]["scheduled_for"] is None


async def test_marketing_can_be_added_to_any_project_and_is_private(api, monkeypatch):
    a = await sign_in(api, f"a{secrets.token_hex(3)}@test-shop.com")
    b = await sign_in(api, f"b{secrets.token_hex(3)}@test-shop.com")
    pid = (await api.post("/v1/projects", headers=auth(a), json={"name": "Site"})).json()["id"]
    assert (await api.post("/v1/marketing/enable", headers=auth(b), json={"project_id": pid})).status_code == 404
    assert (await api.post("/v1/marketing/enable", headers=auth(a), json={"project_id": pid})).status_code == 200
    model = FakeModel([text("Ready to market.")])
    monkeypatch.setattr(agent, "_call", model)
    await api.post(f"/v1/agent/projects/{pid}", headers=auth(a), json={"message": "market it"})
    assert "MARKETING" in model.calls[0]["system"] and "update_site" in model.calls[0]["tools"]
    assert (await api.get(f"/v1/marketing/calendar?project_id={pid}", headers=auth(b))).json()["posts"] == []
    assert (await api.post("/v1/marketing/start", headers=auth(a), json={"website": "not a url"})).status_code == 400
    assert (await api.post("/v1/marketing/start", headers=auth(a), json={"website": "javascript:alert(1)"})).status_code == 400


async def test_drafted_posts_get_images_and_are_billed(api, monkeypatch):
    from app.services import images
    object.__setattr__(settings, "hf_token", "hf_test")
    made = []

    async def fake_generate(prompt, shape="square"):
        made.append((prompt, shape))
        if "fail" in prompt:
            raise images.ImageError("nope")
        return f"https://v3.fal.media/files/{len(made)}.jpeg"
    monkeypatch.setattr(images, "generate", fake_generate)
    try:
        tok = await sign_in(api, f"i{secrets.token_hex(3)}@test-shop.com")
        pid = (await api.post("/v1/projects", headers=auth(tok), json={"name": "Shop"})).json()["id"]
        await api.post("/v1/marketing/enable", headers=auth(tok), json={"project_id": pid})
        before = (await api.get("/v1/billing", headers=auth(tok))).json()["balance"]
        monkeypatch.setattr(agent, "_call", FakeModel(
            [tool("draft_posts", {"posts": [
                {"network": "instagram", "text": "A", "image_prompt": "Shiny SUV at dusk", "image_shape": "portrait"},
                {"network": "facebook", "text": "B"},
                {"network": "instagram", "text": "C", "image_prompt": "this will fail"}]})],
            [text("Drafted.")]))
        out = (await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "posts"})).json()
        assert "created 1 image" in " ".join(out["log"])
        queue = sorted((await api.get("/v1/approvals", headers=auth(tok))).json(), key=lambda q: q["id"])
        media = [q["payload"].get("media") for q in queue]
        assert media[0] == [{"type": "image", "url": "https://v3.fal.media/files/1.jpeg"}]
        assert media[1] is None and media[2] is None
        assert ("Shiny SUV at dusk", "portrait") in made
        after = (await api.get("/v1/billing", headers=auth(tok))).json()["balance"]
        assert before - after >= 3                                 # one image at $0.01 × 3 markup
    finally:
        object.__setattr__(settings, "hf_token", "")
