"""Low / empty credit warnings: in-app status and once-per-refill emails."""

import os
import secrets

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("ENV", "development")

from app.core import db                                   # noqa: E402
from app.core.config import settings                      # noqa: E402
from app.main import app                                  # noqa: E402
from app.services import agent, billing, credit_alerts, mailer   # noqa: E402

from tests.test_agent import FakeModel, text              # noqa: E402
from tests.test_isolation import auth, sign_in            # noqa: E402
from tests.test_social_publish import grant_plan          # noqa: E402

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def api(monkeypatch):
    object.__setattr__(settings, "anthropic_key", "test")
    from app.api import agent as agent_routes
    agent_routes._hits.clear()
    sent = []
    monkeypatch.setattr(mailer, "send_notice", lambda to, subject, body: sent.append((to, subject, body)) or True)
    await db.connect()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.sent = sent
        yield c
    await db.disconnect()


async def workspace(api):
    email = f"cr{secrets.token_hex(3)}@alerts-{secrets.token_hex(2)}.io"
    tok = await sign_in(api, email)
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    pid = (await api.post("/v1/projects", headers=auth(tok), json={"name": "Shop"})).json()["id"]
    await billing.ensure_signup_grant(org)
    return tok, org, pid, email


async def set_balance(org, target):
    have = await billing.balance(org)
    async with db.conn() as c:
        await c.execute("INSERT INTO credit_ledger (org_id, delta, reason) VALUES ($1,$2,'usage')", org, target - have)


async def test_status_levels(api):
    tok, org, pid, _ = await workspace(api)
    st = (await api.get("/v1/billing/status?mode=best&kind=site", headers=auth(tok))).json()
    assert st["state"] == "ok" and st["balance"] == 150 and st["messages_left"] == 4 and st["can_buy"]
    await set_balance(org, 60)
    st = (await api.get("/v1/billing/status?mode=best&kind=site", headers=auth(tok))).json()
    assert st["state"] == "low" and st["messages_left"] == 1
    await set_balance(org, 10)
    assert (await credit_alerts.status(org))["state"] == "out"
    assert (await credit_alerts.status(org, "fast", "site"))["state"] == "low"     # Fast still works
    await grant_plan(org, "launch")
    st = await credit_alerts.status(org)
    assert st["plan"] == "launch" and st["refill_at"] and st["refill_credits"] == 1000
    assert (await api.get("/v1/billing/status?mode=turbo", headers=auth(tok))).status_code == 400


async def test_turn_reports_status_and_refusal_mentions_refill(api, monkeypatch):
    tok, org, pid, _ = await workspace(api)
    monkeypatch.setattr(agent, "_call", FakeModel(*[[text("ok")]] * 4))
    r = (await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "hi"})).json()
    assert r["credits"]["status"]["state"] in ("ok", "low") and "messages_left" in r["credits"]["status"]
    await set_balance(org, 0)
    await grant_plan(org, "growth")
    r = await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "hi"})
    assert r.status_code == 402 and "refill on" in r.json()["detail"]


async def test_emails_once_per_refill(api, monkeypatch):
    tok, org, pid, email = await workspace(api)
    await set_balance(org, 100)
    assert await credit_alerts.check(org) is None                 # 100 > 20% of 150 = 30
    await set_balance(org, 25)
    assert await credit_alerts.check(org) == "low"
    assert await credit_alerts.check(org) is None                 # not repeated
    assert api.sent[-1][0] == email and "25 CreAI credits left" in api.sent[-1][1]
    await set_balance(org, 0)
    assert await credit_alerts.check(org) == "empty"
    assert await credit_alerts.check(org) is None
    assert "out of CreAI credits" in api.sent[-1][1] and "published sites stay online" in api.sent[-1][2]
    n = len(api.sent)

    # a top-up re-arms both alerts
    async with db.conn() as c:
        await c.execute("INSERT INTO credit_ledger (org_id, delta, reason) VALUES ($1, 1000, 'purchase')", org)
    await set_balance(org, 150)
    assert await credit_alerts.check(org) == "low"                # threshold is now 20% of 1,000
    assert await credit_alerts.check(org) is None
    assert len(api.sent) == n + 1 and "150 CreAI credits left" in api.sent[-1][1]

    # members who aren't owners don't get billing email; unconfigured mail isn't marked as sent
    monkeypatch.setattr(mailer, "send_notice", lambda *a: False)
    _, org2, _, _ = await workspace(api)
    await set_balance(org2, 0)
    assert await credit_alerts.check(org2) is None
    async with db.conn() as c:
        assert await c.fetchval("SELECT empty_alert_at FROM org_settings WHERE org_id=$1", org2) is None


async def test_a_turn_triggers_the_email(api, monkeypatch):
    tok, org, pid, email = await workspace(api)
    await set_balance(org, 30)
    monkeypatch.setattr(agent, "_call", FakeModel([text("ok")]))
    await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "hi", "mode": "fast"})
    assert any(to == email and "credits left" in subject for to, subject, _ in api.sent)


async def test_old_negative_balances_are_floored_on_startup(api):
    tok, org, pid, _ = await workspace(api)
    await set_balance(org, -2)
    await db.disconnect()
    await db.connect()
    assert await billing.balance(org) == 0
    await db.disconnect()
    await db.connect()                                    # runs once per workspace
    assert await billing.balance(org) == 0
