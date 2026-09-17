"""
Credits and payments. The model and the payment processor are both faked, so
these check the money rules: metering, the balance gate, webhook security,
idempotency and tenant isolation.
"""

import hashlib
import hmac
import json
import os
import secrets
import time

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("ENV", "development")

from app.core import db                      # noqa: E402
from app.core.config import settings         # noqa: E402
from app.main import app                     # noqa: E402
from app.services import agent, billing      # noqa: E402

from tests.test_agent import FakeModel, text, tool   # noqa: E402
from tests.test_isolation import auth, sign_in       # noqa: E402

pytestmark = pytest.mark.asyncio
WHSEC = "whsec_test_" + secrets.token_hex(8)


def setting(name, value):
    object.__setattr__(settings, name, value)


@pytest_asyncio.fixture
async def api():
    for k, v in (("anthropic_key", "test"), ("stripe_key", "sk_test_x"),
                 ("stripe_publishable_key", "pk_test_x"), ("stripe_webhook_secret", WHSEC)):
        setting(k, v)
    from app.api import agent as agent_routes
    agent_routes._hits.clear()
    await db.connect()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await db.disconnect()


async def new_user(api, tag="u"):
    tok = await sign_in(api, f"{tag}{secrets.token_hex(3)}@example-shop.io")
    pid = (await api.post("/v1/projects", headers=auth(tok), json={"name": "Shop"})).json()["id"]
    return tok, pid


def signed(event: dict, secret=WHSEC, ts=None):
    body = json.dumps(event).encode()
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"}


def paid_intent(org_id, pack="builder", amount=2500, status="succeeded", pid=None):
    return {"type": "payment_intent.succeeded", "data": {"object": {
        "id": pid or "pi_" + secrets.token_hex(6), "object": "payment_intent",
        "status": status, "amount_received": amount, "currency": "usd",
        "payment_method_types": ["card"],
        "metadata": {"app": "creai", "org_id": str(org_id), "pack": pack}}}}


async def org_of(api, tok):
    return (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]


# ---------------------------------------------------------------- metering

def test_cost_maths_matches_published_prices():
    # 1M input + 1M output on Fable 5.1 = $10 + $50
    assert billing.usage_cost("claude-fable-5-1",
                              {"input_tokens": 1_000_000, "output_tokens": 1_000_000}) == 60
    # cache reads on Fable are $0.25 / MTok
    assert billing.usage_cost("claude-fable-5-1",
                              {"cache_read_input_tokens": 1_000_000}) == 0.25
    # an unknown model is never free
    assert billing.usage_cost("mystery-model", {"output_tokens": 1_000_000}) >= 50
    credits, cost = billing.credits_for([("claude-sonnet-5",
                                          {"input_tokens": 3000, "output_tokens": 500})])
    assert cost == pytest.approx(0.011) and credits == 4      # ceil(1.1¢ × 3)


def test_every_pack_keeps_a_healthy_margin():
    for p in billing.PACKS.values():
        price = p["price_cents"] / 100
        api_cost = p["credits"] / 100 / billing.MARKUP
        stripe_fee = price * 0.029 + 0.30
        assert (price - api_cost - stripe_fee) / price > 0.5, p["name"]


async def test_signup_grant_once_and_turns_are_charged(api, monkeypatch):
    tok, pid = await new_user(api)
    first = (await api.get("/v1/billing", headers=auth(tok))).json()
    again = (await api.get("/v1/billing", headers=auth(tok))).json()
    assert first["balance"] == again["balance"] == billing.SIGNUP_CREDITS

    fake = FakeModel([tool("update_site", {"headline": "Hi"})], [text("Done.")])
    monkeypatch.setattr(agent, "_call", fake)
    r = await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok),
                       json={"message": "hello", "mode": "best"})
    assert r.status_code == 200, r.text
    used = r.json()["credits"]
    # two Fable calls × (3000 in, 500 out) = $0.11 → 33 credits at 3×
    assert fake.calls[0]["model"] == settings.agent_model == "claude-fable-5-1"
    assert used["spent"] == 33 and used["balance"] == billing.SIGNUP_CREDITS - 33


async def test_fast_mode_uses_the_cheaper_model(api, monkeypatch):
    tok, pid = await new_user(api)
    fake = FakeModel([text("Quick answer.")])
    monkeypatch.setattr(agent, "_call", fake)
    r = await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok),
                       json={"message": "hi", "mode": "fast"})
    assert fake.calls[0]["model"] == "claude-sonnet-5"
    assert r.json()["credits"]["spent"] == 4


async def test_no_credits_no_turn(api, monkeypatch):
    tok, pid = await new_user(api)
    org = await org_of(api, tok)
    await billing.ensure_signup_grant(org)
    async with db.conn() as c:
        await c.execute("INSERT INTO credit_ledger (org_id, delta, reason) VALUES ($1,$2,'adjustment')",
                        org, -billing.SIGNUP_CREDITS)

    async def must_not_run(*_a):
        raise AssertionError("model called with no credits")
    monkeypatch.setattr(agent, "_call", must_not_run)
    r = await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={"message": "hi"})
    assert r.status_code == 402 and "Top up" in r.json()["detail"]


async def test_anonymous_free_messages_are_capped(api, monkeypatch):
    async def quick(*_a):
        return {"content": [text("ok")], "usage": {}}
    monkeypatch.setattr(agent, "_call", quick)
    codes = [(await api.post("/v1/agent/draft", json={"message": "hi"})).status_code
             for _ in range(billing.SIGNUP_CREDITS and 6)]
    assert codes == [200] * 5 + [402]


# ---------------------------------------------------------------- paying

async def test_payment_intent_is_created_for_the_right_workspace(api, monkeypatch):
    tok, _ = await new_user(api)
    org = await org_of(api, tok)
    seen = {}

    def stripe(req):
        seen["path"] = req.url.path
        seen["form"] = dict(httpx.QueryParams(req.content.decode()))
        return httpx.Response(200, json={"id": "pi_1", "client_secret": "pi_1_secret_x"})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(stripe), **kw))

    r = await api.post("/v1/billing/pay", headers=auth(tok), json={"pack": "pro"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["client_secret"] == "pi_1_secret_x" and out["publishable_key"] == "pk_test_x"
    f = seen["form"]
    assert seen["path"] == "/v1/payment_intents"
    assert f["amount"] == "5000" and f["currency"] == "usd"
    assert f["metadata[org_id]"] == str(org) and f["metadata[pack]"] == "pro"
    assert {f["payment_method_types[0]"], f["payment_method_types[1]"]} == {"card", "klarna"}
    assert "link" not in seen["form"].values()

    bad = await api.post("/v1/billing/pay", headers=auth(tok), json={"pack": "free-money"})
    assert bad.status_code == 400


async def test_webhook_credits_once(api):
    tok, _ = await new_user(api)
    org = await org_of(api, tok)
    before = (await api.get("/v1/billing", headers=auth(tok))).json()["balance"]
    body, headers = signed(paid_intent(org, "builder", 2500, pid="pi_once_" + secrets.token_hex(4)))
    for _ in range(3):                        # Stripe retries; we must not triple-credit
        assert (await api.post("/v1/billing/webhook", content=body, headers=headers)).status_code == 200
    after = (await api.get("/v1/billing", headers=auth(tok))).json()
    assert after["balance"] == before + 2750
    assert after["history"][0]["reason"] == "purchase" and after["history"][0]["pack"] == "builder"


@pytest.mark.parametrize("case", ["forged", "stale", "unsigned"])
async def test_webhook_rejects_bad_signatures(api, case):
    tok, _ = await new_user(api)
    org = await org_of(api, tok)
    event = paid_intent(org)
    if case == "forged":
        body, headers = signed(event, secret="whsec_attacker")
    elif case == "stale":
        body, headers = signed(event, ts=int(time.time()) - 3600)
    else:
        body, headers = json.dumps(event).encode(), {"Content-Type": "application/json"}
    r = await api.post("/v1/billing/webhook", content=body, headers=headers)
    assert r.status_code == 400
    assert (await api.get("/v1/billing", headers=auth(tok))).json()["balance"] == billing.SIGNUP_CREDITS


async def test_webhook_ignores_underpaid_failed_or_foreign_payments(api):
    tok, _ = await new_user(api)
    org = await org_of(api, tok)
    start = (await api.get("/v1/billing", headers=auth(tok))).json()["balance"]
    bad = [paid_intent(org, "studio", amount=100),           # paid $1 for a $100 pack
           paid_intent(org, status="requires_payment_method"),
           paid_intent(org, pack="unlimited")]
    foreign = paid_intent(org); foreign["data"]["object"]["metadata"]["app"] = "other"
    bad.append(foreign)
    for e in bad:
        body, headers = signed(e)
        assert (await api.post("/v1/billing/webhook", content=body, headers=headers)).status_code == 200
    assert (await api.get("/v1/billing", headers=auth(tok))).json()["balance"] == start


async def test_balances_are_per_workspace(api):
    a, _ = await new_user(api, "a")
    b, _ = await new_user(api, "b")
    body, headers = signed(paid_intent(await org_of(api, a), "studio", 10000))
    await api.post("/v1/billing/webhook", content=body, headers=headers)
    ha = (await api.get("/v1/billing", headers=auth(a))).json()
    hb = (await api.get("/v1/billing", headers=auth(b))).json()
    assert ha["balance"] == billing.SIGNUP_CREDITS + 13000
    assert hb["balance"] == billing.SIGNUP_CREDITS
    assert all(h["reason"] != "purchase" for h in hb["history"])
    # and B cannot pick A's workspace to spend from
    r = await api.get("/v1/billing", headers=auth(b, await org_of(api, a)))
    assert r.status_code == 404


async def test_apple_pay_domain_file_is_served(api):
    r = await api.get("/.well-known/apple-developer-merchantid-domain-association")
    assert r.status_code == 200 and len(r.content) > 1000


async def test_payments_still_work_if_klarna_is_off(api, monkeypatch):
    tok, _ = await new_user(api)
    tried = []

    def stripe(req):
        form = dict(httpx.QueryParams(req.content.decode()))
        methods = [v for k, v in form.items() if k.startswith("payment_method_types")]
        tried.append(methods)
        if "klarna" in methods:
            return httpx.Response(400, json={"error": {"message":
                "The payment method type \"klarna\" is invalid. Please ensure it is activated."}})
        return httpx.Response(200, json={"id": "pi_2", "client_secret": "pi_2_secret"})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(stripe), **kw))
    r = await api.post("/v1/billing/pay", headers=auth(tok), json={"pack": "starter"})
    assert r.status_code == 200 and r.json()["client_secret"] == "pi_2_secret"
    assert tried == [["card", "klarna"], ["card"]]
