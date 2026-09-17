"""Domain purchase (registrar mocked), CreAI hosting, and custom-domain serving."""

import os
import secrets

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("ENV", "development")

from app.core import db                                   # noqa: E402
from app.core.config import settings                      # noqa: E402
from app.main import app                                  # noqa: E402
from app.services import appfs, dns, hosting, registrar   # noqa: E402

from tests.test_isolation import auth, sign_in            # noqa: E402
from tests.test_social_publish import grant_plan         # noqa: E402

pytestmark = pytest.mark.asyncio

CONTACT = {"name": "Pat Rivera", "organization": "Shine Detailing", "email": "pat@shine.example",
           "phone": "+1.4145550100", "street": "12 Water St", "city": "Milwaukee", "state": "WI",
           "postal_code": "53202", "country_code": "US"}
SITE = {"business": "Shine Detailing", "headline": "Your car, spotless", "layout": "split",
        "sections": [{"kind": "about", "body": "Mobile detailing."}]}


@pytest_asyncio.fixture
async def api(monkeypatch):
    object.__setattr__(settings, "secret_key", "test-secret-key-" + "x" * 16)
    object.__setattr__(settings, "cloudflare_token", "cf-test")
    object.__setattr__(settings, "cloudflare_account_id", "acct")
    from app.api import sites
    sites._cache.clear()
    await db.connect()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await db.disconnect()
    object.__setattr__(settings, "cloudflare_token", "")


class Registry:
    def __init__(self, price=10.11, available=True, fail=False):
        self.price, self.available, self.fail, self.registered = price, available, fail, []

    async def check(self, domain):
        return registrar._shape({"name": domain, "registrable": self.available, "tier": "standard",
                                 "pricing": {"registration_cost": str(self.price), "renewal_cost": str(self.price)},
                                 "reason": None if self.available else "domain_unavailable"})

    async def search(self, q, limit=8):
        return [await self.check(q.replace(" ", "") + ".com")]

    async def register(self, domain, contact):
        if self.fail:
            raise registrar.RegistrarError("registration was refused: registry timeout")
        self.registered.append((domain, contact))
        return {"domain_name": domain, "status": "active", "expires_at": "2027-09-17T00:00:00Z"}


def use(monkeypatch, reg):
    for fn in ("check", "search", "register"):
        monkeypatch.setattr(registrar, fn, getattr(reg, fn))
    monkeypatch.setattr(hosting, "configured", lambda: False)


async def workspace(api):
    tok = await sign_in(api, f"d{secrets.token_hex(3)}@shine-{secrets.token_hex(2)}.io")
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    return tok, org


async def balance(api, tok):
    return (await api.get("/v1/billing", headers=auth(tok))).json()["balance"]


@pytest.mark.filterwarnings("ignore")
def test_pricing_and_host_record_translation():
    assert registrar.credits_for(10.11) == 2022            # retail: max(2x, +$10) -> $20.22
    assert registrar.credits_for(3.00) == 1300             # cheap endings still carry +$10
    assert registrar.credits_for(40.00) == 8000
    recs = dns.hosting_records("shine.example", [
        {"fqdn": "shine.example", "recordType": "DNS_RECORD_TYPE_CNAME", "requiredValue": "abc.up.railway.app."},
        {"fqdn": "www.shine.example", "recordType": "DNS_RECORD_TYPE_CNAME", "requiredValue": "def.up.railway.app"},
        {"fqdn": "_railway-verify.shine.example", "recordType": "DNS_RECORD_TYPE_TXT", "requiredValue": "railway-verify=1"},
        {"fqdn": "shine.example", "recordType": "DNS_RECORD_TYPE_MX", "requiredValue": "mail.evil"},
        {"fqdn": "x.other.example", "recordType": "DNS_RECORD_TYPE_CNAME", "requiredValue": "evil"},
        {"fqdn": "bad name.shine.example", "recordType": "DNS_RECORD_TYPE_TXT", "requiredValue": "x"}])
    assert [(r["type"], r["name"], r["content"], r["proxied"]) for r in recs] == [
        ("CNAME", "@", "abc.up.railway.app", False), ("CNAME", "www", "def.up.railway.app", False),
        ("TXT", "_railway-verify", "railway-verify=1", False)]


async def test_search_when_registrar_off(api, monkeypatch):
    object.__setattr__(settings, "cloudflare_token", "")
    tok, _ = await workspace(api)
    r = (await api.get("/v1/domains/search?q=shine", headers=auth(tok))).json()
    assert r["registrar_connected"] is False and r["results"] == []


async def test_buy_a_domain_end_to_end(api, monkeypatch):
    reg = Registry(); use(monkeypatch, reg)
    tok, org = await workspace(api)
    found = (await api.get("/v1/domains/search?q=shine detailing", headers=auth(tok))).json()
    assert found["results"][0]["credits"] == 2022
    q = (await api.post("/v1/domains/quote", headers=auth(tok), json={"domain": "https://www.ShineDetailing.com/"})).json()
    assert q["domain"] == "shinedetailing.com" and q["credits"] == 2022 and "refund" in q["terms"]
    assert q["included"] is False and q["hosting_included"] is False

    # needs consent, contact and enough credits
    r = await api.post("/v1/domains/purchase", headers=auth(tok), json={"quote_id": q["quote_id"], "accept_terms": False})
    assert r.status_code == 400
    r = await api.post("/v1/domains/purchase", headers=auth(tok), json={"quote_id": q["quote_id"], "accept_terms": True})
    assert r.status_code == 400 and "contact" in r.json()["detail"]
    r = await api.post("/v1/domains/purchase", headers=auth(tok),
                       json={"quote_id": q["quote_id"], "accept_terms": True, "contact": CONTACT})
    assert r.status_code == 402                                           # 150 signup credits < 2022
    assert not reg.registered

    async with db.conn() as c:
        await c.execute("INSERT INTO credit_ledger (org_id, delta, reason) VALUES ($1, 3000, 'adjustment')", org)
    before = await balance(api, tok)
    q = (await api.post("/v1/domains/quote", headers=auth(tok), json={"domain": "shinedetailing.com"})).json()
    pid = (await api.post("/v1/projects", headers=auth(tok), json={"name": "Site", "path": "launch"})).json()["id"]
    r = await api.post("/v1/domains/purchase", headers=auth(tok),
                       json={"quote_id": q["quote_id"], "accept_terms": True, "contact": CONTACT, "project_id": pid})
    assert r.status_code == 200, r.text
    assert r.json()["credits_spent"] == 2022 and r.json()["hosting"]["upgrade"] == "launch"
    assert await balance(api, tok) == before - 2022
    assert reg.registered == [("shinedetailing.com", CONTACT)]            # in the customer's name

    # the same quote can't buy twice
    again = await api.post("/v1/domains/purchase", headers=auth(tok),
                           json={"quote_id": q["quote_id"], "accept_terms": True})
    assert again.status_code == 409 and len(reg.registered) == 1

    # contact is saved encrypted and reused
    async with db.conn() as c:
        enc = await c.fetchval("SELECT registrant_enc FROM org_settings WHERE org_id=$1", org)
    assert enc and "pat@shine.example" not in enc
    assert (await api.get("/v1/domains/contact", headers=auth(tok))).json()["contact"]["city"] == "Milwaukee"
    listed = (await api.get("/v1/domains", headers=auth(tok))).json()
    assert listed[0]["name"] == "shinedetailing.com" and listed[0]["status"] == "registered"
    hist = (await api.get("/v1/billing", headers=auth(tok))).json()["history"]
    assert hist[0]["reason"] == "domain" and hist[0]["pack"] == "shinedetailing.com"

    # another workspace can't use this workspace's quote or contact
    other, _ = await workspace(api)
    q2 = (await api.post("/v1/domains/quote", headers=auth(tok), json={"domain": "shinewax.com"})).json()
    assert (await api.post("/v1/domains/purchase", headers=auth(other),
                           json={"quote_id": q2["quote_id"], "accept_terms": True, "contact": CONTACT})).status_code == 409
    assert (await api.get("/v1/domains/contact", headers=auth(other))).json()["contact"] is None


async def test_failed_registration_refunds_and_price_changes_stop(api, monkeypatch):
    reg = Registry(fail=True); use(monkeypatch, reg)
    tok, org = await workspace(api)
    async with db.conn() as c:
        await c.execute("INSERT INTO credit_ledger (org_id, delta, reason) VALUES ($1, 5000, 'adjustment')", org)
    before = await balance(api, tok)
    q = (await api.post("/v1/domains/quote", headers=auth(tok), json={"domain": "shinefail.com"})).json()
    r = await api.post("/v1/domains/purchase", headers=auth(tok),
                       json={"quote_id": q["quote_id"], "accept_terms": True, "contact": CONTACT})
    assert r.status_code == 502 and "returned" in r.json()["detail"]
    assert await balance(api, tok) == before
    assert (await api.get("/v1/domains", headers=auth(tok))).json() == []

    reg.fail = False
    q = (await api.post("/v1/domains/quote", headers=auth(tok), json={"domain": "shineprice.com"})).json()
    reg.price = 60.00                                                     # registry price jumped
    r = await api.post("/v1/domains/purchase", headers=auth(tok),
                       json={"quote_id": q["quote_id"], "accept_terms": True, "contact": CONTACT})
    assert r.status_code == 409 and await balance(api, tok) == before and not reg.registered

    reg.available = False
    taken = (await api.post("/v1/domains/quote", headers=auth(tok), json={"domain": "google.com"})).json()
    assert taken["available"] is False and "taken" in taken["message"]
    assert (await api.post("/v1/domains/quote", headers=auth(tok), json={"domain": "not a domain"})).status_code == 400


async def test_hosting_writes_the_records_the_host_asks_for(api, monkeypatch):
    reg = Registry(); use(monkeypatch, reg)
    monkeypatch.setattr(hosting, "configured", lambda: True)

    async def attach(host):
        label = "" if host.count(".") == 1 else "www."
        return {"id": "rd-" + host, "certificate": "CERTIFICATE_STATUS_TYPE_VALID", "verified": True,
                "records": [{"fqdn": host, "recordType": "DNS_RECORD_TYPE_CNAME", "requiredValue": "x.up.railway.app"},
                            {"fqdn": f"_railway-verify.{label}shinehost.com", "recordType": "DNS_RECORD_TYPE_TXT",
                             "requiredValue": "railway-verify=" + host}]}
    written = {}

    async def zone(name):
        return "zone-1"

    async def write(zone_id, domain, token, hosting_recs=None):
        written["records"] = hosting_recs
        return [{"type": r["type"], "name": r["name"], "value": r["content"], "ok": True, "provider_id": "p"} for r in hosting_recs]
    monkeypatch.setattr(hosting, "attach", attach)
    monkeypatch.setattr(dns, "ensure_zone", zone)
    monkeypatch.setattr(dns, "write_records", write)
    tok, org = await workspace(api)
    await grant_plan(org, "launch")
    async with db.conn() as c:
        await c.execute("INSERT INTO credit_ledger (org_id, delta, reason) VALUES ($1, 5000, 'adjustment')", org)
    q = (await api.post("/v1/domains/quote", headers=auth(tok), json={"domain": "shinehost.com"})).json()
    r = (await api.post("/v1/domains/purchase", headers=auth(tok),
                        json={"quote_id": q["quote_id"], "accept_terms": True, "contact": CONTACT})).json()
    assert r["hosting"]["ok"] is True
    names = sorted((x["type"], x["name"]) for x in written["records"])
    assert names == [("CNAME", "@"), ("CNAME", "www"), ("TXT", "_railway-verify"), ("TXT", "_railway-verify.www")]
    assert all(x["proxied"] is False for x in written["records"])

    # a connected, unverified domain can't be hosted; nor can one another workspace uses
    c1 = (await api.post("/v1/domains/connect", headers=auth(tok), json={"name": "mine-unverified.com"})).json()
    ids = {d["name"]: d["id"] for d in (await api.get("/v1/domains", headers=auth(tok))).json()}
    assert (await api.post(f"/v1/domains/{ids['mine-unverified.com']}/host", headers=auth(tok))).status_code == 409
    other, other_org = await workspace(api)
    await grant_plan(other_org, "launch")
    await api.post("/v1/domains/connect", headers=auth(other), json={"name": "shinehost.com"})
    oid = (await api.get("/v1/domains", headers=auth(other))).json()[0]["id"]
    async with db.conn() as c:
        await c.execute("UPDATE domains SET status='verifying' WHERE id=$1", oid)
    assert (await api.post(f"/v1/domains/{oid}/host", headers=auth(other))).status_code == 409


async def test_publish_site_and_serve_on_custom_domain(api, monkeypatch):
    tok, org = await workspace(api)
    served, empty = f"served-{secrets.token_hex(3)}.com", f"empty-{secrets.token_hex(3)}.com"
    pid = (await api.post("/v1/projects", headers=auth(tok), json={"name": "Site", "path": "launch"})).json()["id"]
    assert (await api.post(f"/v1/sites/{pid}/publish", headers=auth(tok))).status_code == 409
    async with db.conn() as c:
        await c.execute("UPDATE projects SET answers=$1 WHERE id=$2", {"site": SITE}, pid)
    pub = (await api.post(f"/v1/sites/{pid}/publish", headers=auth(tok))).json()
    slug = pub["url"].rsplit("/", 1)[1]
    page = await api.get(f"/s/{slug}")
    assert page.status_code == 200 and "Your car, spotless" in page.text
    assert "Made with CreAI" in page.text                               # free plan shows the badge
    assert "script-src 'none'" in page.headers["content-security-policy"]
    assert (await api.get("/s/nope-000000")).status_code == 404

    # someone else can't publish or unpublish it
    other, _ = await workspace(api)
    assert (await api.post(f"/v1/sites/{pid}/publish", headers=auth(other))).status_code == 404

    # attach a domain and request it by Host
    await grant_plan(org, "launch")
    assert "Made with CreAI" not in (await api.get(f"/s/{slug}")).text
    async with db.conn() as c:
        await c.execute("""INSERT INTO domains (org_id, project_id, name, source, status)
                           VALUES ($1,$2,$3,'registered','registered')""", org, pid, served)
    from app.api import sites
    sites._cache.clear()
    for host in (served, "www." + served):
        r = await api.get("/", headers={"Host": host})
        assert r.status_code == 200 and "Your car, spotless" in r.text and "CreAI · split" in r.text
        assert "script-src 'none'" in r.headers["content-security-policy"]
    # the platform is never reachable through a customer's domain
    for path in ("/v1/auth/me", "/v1/projects", "/app.js", "/s/" + slug):
        assert (await api.get(path, headers={"Host": served})).status_code == 404
    # unknown hosts fall through to the platform as before
    assert (await api.get("/health", headers={"Host": "healthcheck.railway.app"})).status_code == 200

    # domain without a release shows a placeholder, not the CreAI app
    async with db.conn() as c:
        await c.execute("""INSERT INTO domains (org_id, name, source, status)
                           VALUES ($1,$2,'registered','registered')""", org, empty)
    r = await api.get("/", headers={"Host": empty})
    assert "on its way" in r.text and "CreAI" in r.text and "<script" not in r.text

    await api.post(f"/v1/sites/{pid}/unpublish", headers=auth(tok))
    assert (await api.get(f"/s/{slug}")).status_code == 404
    sites._cache.clear()
    r = await api.get("/", headers={"Host": served})
    assert "on its way" in r.text


async def test_published_app_on_custom_domain_is_sandboxed(api):
    tok, org = await workspace(api)
    await grant_plan(org, "growth")
    pid = (await api.post("/v1/projects", headers=auth(tok), json={"name": "App", "path": "app"})).json()["id"]
    await appfs.write(pid, org, {"app.js": "import { html, render } from 'htm/preact';\nrender(html`<p>Hi</p>`, document.getElementById('root'));"})
    assert (await api.post(f"/v1/apps/{pid}/publish", headers=auth(tok))).status_code == 200
    host = f"app-{secrets.token_hex(3)}.com"
    async with db.conn() as c:
        await c.execute("""INSERT INTO domains (org_id, project_id, name, source, status)
                           VALUES ($1,$2,$3,'registered','live')""", org, pid, host)
    r = await api.get("/", headers={"Host": host})
    assert r.headers["content-security-policy"].startswith("sandbox allow-scripts")
    assert "allow-same-origin" not in r.headers["content-security-policy"] and "render(html" in r.text


async def test_renewals_charge_once_per_term(api):
    tok, org = await workspace(api)
    async with db.conn() as c:
        await c.execute("INSERT INTO credit_ledger (org_id, delta, reason) VALUES ($1, 1150, 'adjustment')", org)
        did = await c.fetchval(
            """INSERT INTO domains (org_id, name, source, status, expires_at, renewal_credits)
               VALUES ($1,$2,'registered','live', now() + interval '2 hours', 1263) RETURNING id""",
            org, f"renew-{secrets.token_hex(3)}.com")
    before = await balance(api, tok)
    await registrar.renewal_sweep()
    await registrar.renewal_sweep()                                     # second run is a no-op
    assert await balance(api, tok) == before - 1263
    async with db.conn() as c:
        left = await c.fetchval("SELECT expires_at - now() FROM domains WHERE id=$1", did)
        await c.execute("UPDATE domains SET expires_at = now() + interval '1 hour' WHERE id=$1", did)
    assert left.days >= 364
    await registrar.renewal_sweep()                                     # next term: not enough credits
    assert await balance(api, tok) == before - 1263
    async with db.conn() as c:
        unpaid = await c.fetchval("SELECT count(*) FROM events WHERE org_id=$1 AND kind='domain.renewal_unpaid'", org)
    assert unpaid == 1



async def test_yearly_plan_includes_one_domain_and_renewals(api, monkeypatch):
    reg = Registry(); use(monkeypatch, reg)
    tok, org = await workspace(api)
    await grant_plan(org, "launch", "yearly")
    q = (await api.post("/v1/domains/quote", headers=auth(tok), json={"domain": "yearly-one.com"})).json()
    assert q["included"] is True and q["credits"] == 0 and q["list_credits"] == 2022 and q["includable"]
    assert "included while your yearly plan" in q["terms"]
    before = await balance(api, tok)
    r = await api.post("/v1/domains/purchase", headers=auth(tok),
                       json={"quote_id": q["quote_id"], "accept_terms": True, "contact": CONTACT})
    assert r.status_code == 200 and r.json()["credits_spent"] == 0
    assert await balance(api, tok) == before
    hist = (await api.get("/v1/billing", headers=auth(tok))).json()["history"]
    assert hist[0]["reason"] == "waived" and "yearly plan" in hist[0]["note"]

    # the second domain that year is charged
    q2 = (await api.post("/v1/domains/quote", headers=auth(tok), json={"domain": "yearly-two.com"})).json()
    assert q2["included"] is False and q2["credits"] == 2022
    # two quotes taken while the domain was still unclaimed can't both be free
    await grant_plan(org, "launch", "yearly")
    a = (await api.post("/v1/domains/quote", headers=auth(tok), json={"domain": "race-a.com"})).json()
    b = (await api.post("/v1/domains/quote", headers=auth(tok), json={"domain": "race-b.com"})).json()
    assert a["included"] and b["included"]
    ok = await api.post("/v1/domains/purchase", headers=auth(tok), json={"quote_id": a["quote_id"], "accept_terms": True})
    no = await api.post("/v1/domains/purchase", headers=auth(tok), json={"quote_id": b["quote_id"], "accept_terms": True})
    assert ok.status_code == 200 and no.status_code == 409

    # an expensive ending isn't covered
    reg.price = 30.0
    pricey = (await api.post("/v1/domains/quote", headers=auth(tok), json={"domain": "pricey.io"})).json()
    assert pricey["included"] is False and pricey["includable"] is False

    # a failed registration gives the included domain back
    reg.price, reg.fail = 10.11, True
    await grant_plan(org, "launch", "yearly")
    f = (await api.post("/v1/domains/quote", headers=auth(tok), json={"domain": "fails.com"})).json()
    assert (await api.post("/v1/domains/purchase", headers=auth(tok),
                           json={"quote_id": f["quote_id"], "accept_terms": True})).status_code == 502
    assert (await api.get("/v1/plans", headers=auth(tok))).json()["current"]["domain_included"] is True

    # renewals are waived while the yearly plan is active
    async with db.conn() as c:
        did = await c.fetchval(
            """INSERT INTO domains (org_id, name, source, status, expires_at, renewal_credits)
               VALUES ($1,$2,'registered','live', now() + interval '1 hour', 2022) RETURNING id""",
            org, f"renew-inc-{secrets.token_hex(3)}.com")
    before = await balance(api, tok)
    await registrar.renewal_sweep()
    assert await balance(api, tok) == before
    async with db.conn() as c:
        assert (await c.fetchval("SELECT expires_at - now() FROM domains WHERE id=$1", did)).days >= 364


async def test_lapsed_plan_falls_back_to_free_address(api):
    tok, org = await workspace(api)
    await grant_plan(org, "launch")
    pid = (await api.post("/v1/projects", headers=auth(tok), json={"name": "Site", "path": "launch"})).json()["id"]
    async with db.conn() as c:
        await c.execute("UPDATE projects SET answers=$1 WHERE id=$2", {"site": SITE}, pid)
    url = (await api.post(f"/v1/sites/{pid}/publish", headers=auth(tok))).json()["url"]
    host = f"lapse-{secrets.token_hex(3)}.com"
    async with db.conn() as c:
        await c.execute("""INSERT INTO domains (org_id, project_id, name, source, status)
                           VALUES ($1,$2,$3,'registered','live')""", org, pid, host)
    from app.api import sites
    sites._cache.clear()
    assert "Your car, spotless" in (await api.get("/", headers={"Host": host})).text
    async with db.conn() as c:
        await c.execute("UPDATE subscriptions SET status='canceled' WHERE org_id=$1", org)
    sites._cache.clear()
    r = await api.get("/", headers={"Host": host}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].endswith(url.split("/s/")[1])
    d = (await api.get("/v1/domains", headers=auth(tok))).json()[0]
    assert (await api.post(f"/v1/domains/{d['id']}/host", headers=auth(tok))).status_code == 402
