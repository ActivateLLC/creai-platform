"""Plans: webhooks drive state, credits granted once, posting gated, checkout wiring."""

import json
import os
import secrets
import time

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("ENV", "development")

from app.core import db                                   # noqa: E402
from app.core.config import settings                      # noqa: E402
from app.main import app                                  # noqa: E402
from app.services import billing, plans                   # noqa: E402

from tests.test_isolation import auth, sign_in            # noqa: E402
from tests.test_billing import signed                     # noqa: E402
from tests.test_billing import WHSEC as SECRET            # noqa: E402

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def api():
    object.__setattr__(settings, "stripe_webhook_secret", SECRET)
    await db.connect()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await db.disconnect()


async def workspace(api):
    tok = await sign_in(api, f"p{secrets.token_hex(3)}@plan-{secrets.token_hex(2)}.io")
    return tok, (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]


async def hook(api, event):
    body, headers = signed(event, SECRET)
    return await api.post("/v1/billing/webhook", content=body, headers=headers)


def sub_event(kind, org, plan="launch", interval="yearly", status="active", end=None, sid="sub_1"):
    end = end or int(time.time()) + 365 * 86400
    return {"type": kind, "data": {"object": {
        "id": sid, "object": "subscription", "customer": "cus_1", "status": status,
        "cancel_at_period_end": False, "current_period_end": end,
        "metadata": {"app": "creai", "org_id": str(org), "plan": plan, "interval": interval},
        "items": {"data": [{"price": {"metadata": {"app": "creai", "plan": plan, "interval": interval}},
                            "current_period_end": end}]}}}}


async def test_subscription_lifecycle_from_webhooks(api):
    tok, org = await workspace(api)
    cat = (await api.get("/v1/plans", headers=auth(tok))).json()
    assert cat["current"]["plan"] == "free" and [p["id"] for p in cat["plans"]] == ["free", "launch", "growth"]

    # an unsigned event changes nothing
    bad = await api.post("/v1/billing/webhook", content=json.dumps(sub_event("customer.subscription.created", org)))
    assert bad.status_code == 400
    assert (await plans.current(org))["plan"] == "free"

    assert (await hook(api, sub_event("customer.subscription.created", org))).status_code == 200
    cur = (await api.get("/v1/plans", headers=auth(tok))).json()["current"]
    assert cur["plan"] == "launch" and cur["interval"] == "yearly" and cur["domain_included"] is True
    assert await plans.has(org, "custom_domain") and not await plans.has(org, "social_publish")

    # someone else's product or a forged plan name is ignored
    other, org2 = await workspace(api)
    foreign = sub_event("customer.subscription.created", org2, plan="enterprise")
    await hook(api, foreign)
    assert (await plans.current(org2))["plan"] == "free"
    notours = sub_event("customer.subscription.created", org2)
    notours["data"]["object"]["metadata"]["app"] = "someone-else"
    await hook(api, notours)
    assert (await plans.current(org2))["plan"] == "free"

    # a renewal into a new year restores the included domain
    assert await plans.claim_included_domain(org)
    assert (await plans.current(org))["domain_included"] is False
    await hook(api, sub_event("customer.subscription.updated", org, end=int(time.time()) + 2 * 365 * 86400))
    assert (await plans.current(org))["domain_included"] is True

    # past_due keeps access while Stripe retries; cancellation ends it
    await hook(api, sub_event("customer.subscription.updated", org, status="past_due"))
    assert (await plans.current(org))["plan"] == "launch"
    await hook(api, sub_event("customer.subscription.deleted", org, status="canceled"))
    assert (await plans.current(org))["plan"] == "free"


async def test_monthly_credits_granted_once(api):
    tok, org = await workspace(api)
    await hook(api, sub_event("customer.subscription.created", org, plan="growth", interval="monthly"))
    invoice = {"type": "invoice.paid", "data": {"object": {
        "id": "in_1", "object": "invoice", "status": "paid", "created": int(time.time()),
        "subscription_details": {"metadata": {"app": "creai", "org_id": str(org), "plan": "growth"}},
        "lines": {"data": [{"period": {"start": int(time.time())}, "metadata": {}}]}}}}
    await api.get("/v1/billing", headers=auth(tok))           # welcome credits land first
    before = await billing.balance(org)
    await hook(api, invoice)
    await hook(api, invoice)                                  # Stripe retries
    assert await billing.balance(org) == before + 3000
    hist = (await api.get("/v1/billing", headers=auth(tok))).json()["history"]
    assert any(h["reason"] == "plan" and h["delta"] == 3000 for h in hist)

    unpaid = json.loads(json.dumps(invoice)); unpaid["data"]["object"]["status"] = "open"
    unpaid["data"]["object"]["id"] = "in_2"
    unpaid["data"]["object"]["lines"]["data"][0]["period"]["start"] = int(time.time()) + 40 * 86400
    await hook(api, unpaid)
    assert await billing.balance(org) == before + 3000

    # yearly subscribers get the month's credits from the sweep, once
    tok2, org2 = await workspace(api)
    await hook(api, sub_event("customer.subscription.created", org2, plan="launch", interval="yearly", sid="sub_2"))
    b2 = await billing.balance(org2)
    await plans.monthly_sweep()
    await plans.monthly_sweep()
    assert await billing.balance(org2) == b2 + 1000


async def test_renewal_reminder_email(api, monkeypatch):
    from app.services import mailer
    sent = []
    monkeypatch.setattr(mailer, "send_notice", lambda to, subject, text: sent.append((to, subject, text)) or True)
    await hook(api, {"type": "invoice.upcoming", "data": {"object": {
        "object": "invoice", "customer_email": "owner@shop.example", "amount_due": 12000,
        "next_payment_attempt": int(time.time()) + 7 * 86400}}})
    assert sent and sent[0][0] == "owner@shop.example" and "$120.00" in sent[0][2] and "cancel" in sent[0][2]


async def test_checkout_and_portal(api, monkeypatch):
    tok, org = await workspace(api)
    object.__setattr__(settings, "stripe_key", "")
    r = await api.post("/v1/plans/checkout", headers=auth(tok), json={"plan": "launch", "interval": "yearly"})
    assert r.status_code == 503
    object.__setattr__(settings, "stripe_key", "sk_test_x")
    calls = []

    async def fake(method, path, data=None):
        calls.append((method, path, data))
        if path.startswith("/prices?"):
            return {"data": []}
        if path == "/products":
            return {"id": "prod_1"}
        if path == "/prices":
            return {"id": "price_1"}
        if path == "/customers":
            return {"id": "cus_9"}
        if path == "/checkout/sessions":
            return {"url": "https://checkout.stripe.com/c/pay/cs_1"}
        if path == "/billing_portal/sessions":
            return {"url": "https://billing.stripe.com/p/session/1"}
        if path.startswith("/billing_portal/configurations?"):
            return {"data": [{"id": "bpc_other", "metadata": {"app": "orcacredit"}}]}
        if path == "/billing_portal/configurations":
            return {"id": "bpc_creai"}
        if path.startswith("/prices/"):
            return {"id": "price_1", "product": "prod_1"}
        raise AssertionError(path)
    monkeypatch.setattr(billing, "_stripe", fake)
    monkeypatch.setattr(type(settings), "missing_for", lambda self, cap: False)
    assert (await api.post("/v1/plans/checkout", headers=auth(tok), json={"plan": "platinum"})).status_code == 400
    r = await api.post("/v1/plans/checkout", headers=auth(tok), json={"plan": "launch", "interval": "yearly"})
    assert r.json()["url"].startswith("https://checkout.stripe.com/")
    session = next(d for m, p, d in calls if p == "/checkout/sessions")
    assert session["mode"] == "subscription" and session["line_items"][0]["price"] == "price_1"
    assert session["subscription_data"]["metadata"]["org_id"] == org
    price = next(d for m, p, d in calls if p == "/prices")
    assert price["unit_amount"] == 12000 and price["recurring"]["interval"] == "year"
    plans._portal_config = None
    assert (await api.post("/v1/plans/portal", headers=auth(tok))).json()["url"].startswith("https://billing.stripe.com/")
    portal = next(d for m, p, d in calls if p == "/billing_portal/sessions")
    assert portal["configuration"] == "bpc_creai"              # never the account default shared with other products
    cfg = next(d for m, p, d in calls if p == "/billing_portal/configurations")
    assert cfg["features"]["subscription_cancel"]["mode"] == "at_period_end" and cfg["metadata"]["app"] == "creai"
    # already subscribed: send them to manage instead of a second subscription
    await hook(api, sub_event("customer.subscription.created", org))
    again = await api.post("/v1/plans/checkout", headers=auth(tok), json={"plan": "growth", "interval": "monthly"})
    assert again.status_code == 400 and "Manage plan" in again.json()["detail"]
    object.__setattr__(settings, "stripe_key", "")


async def test_invoice_paid_in_newer_api_layout(api):
    tok, org = await workspace(api)
    await api.get("/v1/billing", headers=auth(tok))
    before = await billing.balance(org)
    await hook(api, {"type": "invoice.paid", "data": {"object": {
        "id": "in_new", "object": "invoice", "status": "paid", "created": int(time.time()),
        "parent": {"type": "subscription_details",
                   "subscription_details": {"metadata": {"app": "creai", "org_id": str(org), "plan": "launch"}}},
        "lines": {"data": [{"period": {"start": int(time.time())}, "metadata": {}}]}}}})
    assert await billing.balance(org) == before + 1000
