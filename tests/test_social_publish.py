"""Publishing approved posts through Postiz: only to the approving workspace's
channels, held with a reason when it can't go, never sent twice, cancellable."""

import json
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
from app.services import social_publish as sp        # noqa: E402

from tests.test_isolation import auth, sign_in       # noqa: E402

pytestmark = pytest.mark.asyncio


class FakePostiz:
    def __init__(self):
        self.integrations = []
        self.created, self.deleted, self.uploads = [], [], []
        self.fail = False

    def __call__(self, req):
        assert req.headers["authorization"] == "pz-test-key"
        path = req.url.path.split("/api/public/v1", 1)[1]
        if path == "/integrations":
            return httpx.Response(200, json=self.integrations)
        if path == "/posts" and req.method == "POST":
            if self.fail:
                return httpx.Response(400, json={"message": "invalid"})
            body = json.loads(req.content)
            self.created.append(body)
            return httpx.Response(200, json=[{"postId": f"p{len(self.created)}",
                                              "integration": body["posts"][0]["integration"]["id"]}])
        if path == "/upload-from-url":
            url = json.loads(req.content)["url"]
            self.uploads.append(url)
            return httpx.Response(200, json={"id": f"up{len(self.uploads)}", "path": "https://postiz.example/u/1.jpeg"})
        if path.startswith("/posts/") and req.method == "DELETE":
            self.deleted.append(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={})
        if path == "/is-connected":
            return httpx.Response(200, json={"connected": True})
        return httpx.Response(404)


@pytest_asyncio.fixture
async def env(monkeypatch):
    object.__setattr__(settings, "postiz_api_key", "pz-test-key")
    await db.connect()
    fake = FakePostiz()
    real = httpx.AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(fake), **kw))
        yield api, fake
    object.__setattr__(settings, "postiz_api_key", "")
    await db.disconnect()


async def workspace(api):
    tok = await sign_in(api, f"s{secrets.token_hex(3)}@example-shop.io")
    me = (await api.get("/v1/auth/me", headers=auth(tok))).json()
    pid = (await api.post("/v1/projects", headers=auth(tok), json={"name": "Shop"})).json()["id"]
    await grant_plan(me["active_org"], "growth")
    return tok, me["active_org"], pid


async def grant_plan(org_id, plan, interval="monthly"):
    async with db.conn() as c:
        await c.execute(
            """INSERT INTO subscriptions (org_id, plan, interval, status, current_period_end)
               VALUES ($1,$2,$3,'active', now() + interval '30 days')
               ON CONFLICT (org_id) DO UPDATE SET plan=$2, interval=$3, status='active',
                 current_period_end = now() + interval '30 days', domain_claimed=false""",
            org_id, plan, interval)


async def draft_post(org_id, pid, network, text="Hello", when=None, link="", media=None):
    payload = {"network": network, "text": text, "link": link}
    if media:
        payload["media"] = media
    async with db.conn() as c:
        return await c.fetchval(
            """INSERT INTO approvals (org_id, project_id, kind, payload, scheduled_for)
               VALUES ($1,$2,'post',$3,$4) RETURNING id""", org_id, pid, payload, when)


async def make_admin(api):
    email = f"admin{secrets.token_hex(3)}@creai-staff.io"
    tok = await sign_in(api, email)
    async with db.conn() as c:
        uid = await c.fetchval("SELECT id FROM users WHERE email=$1", email)
        await c.execute("INSERT INTO platform_admins (user_id, level) VALUES ($1,'owner')", uid)
    return auth(tok) | {"X-Reason": "onboarding a customer's social accounts"}


async def state_of(aid):
    async with db.conn() as c:
        r = await c.fetchrow("SELECT state, payload FROM approvals WHERE id=$1", aid)
    return r["state"], (r["payload"] or {}).get("delivery", {})


async def test_posts_wait_with_a_reason_until_a_channel_exists(env):
    api, fake = env
    tok, org, pid = await workspace(api)
    fb = await draft_post(org, pid, "facebook")
    ig = await draft_post(org, pid, "instagram")
    assert (await api.post(f"/v1/approvals/{fb}/approve", headers=auth(tok))).json()["state"] == "held"
    assert (await api.post(f"/v1/approvals/{ig}/approve", headers=auth(tok))).json()["state"] == "held"
    assert "Connect Facebook" in (await state_of(fb))[1]["reason"]
    assert "need an image" in (await state_of(ig))[1]["reason"]
    assert fake.created == []
    cal = (await api.get(f"/v1/marketing/calendar?project_id={pid}", headers=auth(tok))).json()["posts"]
    assert {p["delivery"]["reason"] for p in cal} == {"Connect Facebook to publish this.",
                                                      "Instagram posts need an image."}


async def test_assigned_channel_publishes_only_for_its_workspace(env):
    api, fake = env
    tok_a, org_a, pid_a = await workspace(api)
    tok_b, org_b, pid_b = await workspace(api)
    admin = await make_admin(api)
    fake.integrations = [
        {"id": "int-a", "identifier": "facebook", "name": "Shine Detailing", "picture": "", "disabled": False},
        {"id": "int-b", "identifier": "gmb", "name": "Other Co", "picture": "", "disabled": False},
    ]
    assert (await api.post("/v1/admin/social/assign", headers=admin,
                           json={"integration_id": "int-a", "org_id": org_a})).status_code == 200
    assert (await api.post("/v1/admin/social/assign", headers=admin,
                           json={"integration_id": "int-b", "org_id": org_b})).status_code == 200
    assert (await api.post("/v1/admin/social/assign", headers=auth(tok_a) | {"X-Reason": "x"},
                           json={"integration_id": "int-b", "org_id": org_a})).status_code == 404

    chans = (await api.get("/v1/channels", headers=auth(tok_a))).json()
    assert chans["publishing"] and [c["network"] for c in chans["channels"]] == ["facebook"]

    later = datetime.now(timezone.utc) + timedelta(days=2)
    fb = await draft_post(org_a, pid_a, "facebook", "Salt season prep", later, "https://shine.example/book")
    gb = await draft_post(org_a, pid_a, "google_business", "Weekend slots")
    assert (await api.post(f"/v1/approvals/{fb}/approve", headers=auth(tok_a))).json()["state"] == "scheduled"
    assert (await api.post(f"/v1/approvals/{gb}/approve", headers=auth(tok_a))).json()["state"] == "held"

    assert len(fake.created) == 1
    sent = fake.created[0]
    assert sent["type"] == "schedule" and sent["date"].startswith(later.date().isoformat())
    post = sent["posts"][0]
    assert post["integration"]["id"] == "int-a"                       # never int-b
    assert post["settings"] == {"__type": "facebook", "url": "https://shine.example/book"}
    assert post["value"][0]["content"] == "Salt season prep"
    state, delivery = await state_of(fb)
    assert state == "scheduled" and delivery["channel"] == "Shine Detailing"

    assert await sp.deliver(fb) == "skipped"                          # never sent twice
    assert len(fake.created) == 1

    b_post = await draft_post(org_b, pid_b, "google_business", "Now open", link="https://other.example")
    assert (await api.post(f"/v1/approvals/{b_post}/approve", headers=auth(tok_b))).json()["state"] == "scheduled"
    gmb = fake.created[-1]["posts"][0]
    assert gmb["integration"]["id"] == "int-b" and gmb["settings"]["callToActionUrl"] == "https://other.example"
    assert fake.created[-1]["type"] == "now"

    assert (await api.post(f"/v1/approvals/{fb}/discard", headers=auth(tok_b))).status_code == 404
    assert (await api.post(f"/v1/approvals/{fb}/discard", headers=auth(tok_a))).status_code == 200
    assert fake.deleted == ["p1"]


async def test_sweep_picks_up_held_posts_once_connected(env):
    api, fake = env
    tok, org, pid = await workspace(api)
    x = await draft_post(org, pid, "x", "Launch day", link="https://shine.example")
    await api.post(f"/v1/approvals/{x}/approve", headers=auth(tok))
    assert (await state_of(x))[0] == "held"
    fake.integrations = [{"id": "int-x", "identifier": "x", "name": "@shine", "picture": "", "disabled": False}]
    await sp.assign("int-x", org)
    await sp.sweep_once()
    assert (await state_of(x))[0] == "scheduled"
    body = fake.created[-1]["posts"][0]
    assert body["value"][0]["content"] == "Launch day\n\nhttps://shine.example"
    assert body["settings"]["__type"] == "x"


async def test_platform_errors_are_recorded(env):
    api, fake = env
    tok, org, pid = await workspace(api)
    fake.integrations = [{"id": "int-f", "identifier": "facebook", "name": "Page", "picture": "", "disabled": False}]
    await sp.assign("int-f", org)
    fake.fail = True
    p = await draft_post(org, pid, "facebook", "Hi")
    assert (await api.post(f"/v1/approvals/{p}/approve", headers=auth(tok))).json()["state"] == "failed"
    assert (await state_of(p))[1]["reason"] == "The platform didn't accept this post."
    with pytest.raises(sp.PostizError):
        await sp.assign("int-missing", org)


async def test_instagram_goes_out_with_its_generated_image(env):
    api, fake = env
    tok, org, pid = await workspace(api)
    fake.integrations = [{"id": "int-ig", "identifier": "instagram", "name": "@shine", "picture": "", "disabled": False}]
    await sp.assign("int-ig", org)
    ig = await draft_post(org, pid, "instagram", "Fresh detail", media=[{"type": "image", "url": "https://v3.fal.media/files/abc.jpeg"}])
    assert (await api.post(f"/v1/approvals/{ig}/approve", headers=auth(tok))).json()["state"] == "scheduled"
    assert fake.uploads == ["https://v3.fal.media/files/abc.jpeg"]
    post = fake.created[-1]["posts"][0]
    assert post["value"][0]["image"] == [{"id": "up1", "path": "https://postiz.example/u/1.jpeg"}]
    assert post["settings"]["__type"] == "instagram"
    cal = (await api.get(f"/v1/marketing/calendar?project_id={pid}", headers=auth(tok))).json()["posts"]
    assert cal[0]["image"] == "https://v3.fal.media/files/abc.jpeg"
