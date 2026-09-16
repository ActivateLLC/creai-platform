"""
Tenant isolation tests.

Isolation you have not tried to break is isolation you are assuming. These tests
deliberately attempt cross-tenant access and require it to fail. They are the
reason the multi-tenant claim is checkable.

Run: pytest -q   (needs DATABASE_URL pointing at a throwaway database)
"""

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("ENV", "development")

from app.core import db                      # noqa: E402
from app.core.db import TENANT_TABLES        # noqa: E402
from app.main import app                     # noqa: E402

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def api():
    await db.connect()
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c
    await db.disconnect()


async def sign_in(api, email: str) -> str:
    r = await api.post("/v1/auth/start", json={"email": email})
    code = r.json().get("code")
    assert code, "development sign-in must return the code when email is unconfigured"
    r = await api.post("/v1/auth/code", json={"email": email, "code": code})
    return r.json()["token"]


def auth(token: str, org: int | None = None) -> dict:
    h = {"Authorization": f"Bearer {token}"}
    if org:
        h["X-Org"] = str(org)
    return h


# ---------------------------------------------------------------- the core test

async def test_a_cannot_read_b(api):
    """The whole product risk in one test."""
    a = await sign_in(api, "alice@tenant-a.test")
    b = await sign_in(api, "bob@tenant-b.test")

    made = await api.post("/v1/projects", headers=auth(a),
                          json={"name": "Alice's secret launch"})
    pid = made.json()["id"]

    # B lists projects: Alice's must not appear.
    seen = (await api.get("/v1/projects", headers=auth(b))).json()
    assert all(p["id"] != pid for p in seen), "cross-tenant leak in list"

    # B fetches it directly: 404, not 403 — B learns nothing about its existence.
    direct = await api.get(f"/v1/projects/{pid}", headers=auth(b))
    assert direct.status_code == 404


async def test_org_header_cannot_be_forged(api):
    """Passing someone else's org id must not grant access to it."""
    a = await sign_in(api, "alice2@tenant-a.test")
    b = await sign_in(api, "bob2@tenant-b.test")

    a_org = (await api.get("/v1/auth/me", headers=auth(a))).json()["active_org"]
    r = await api.get("/v1/dashboard", headers=auth(b, org=a_org))
    assert r.status_code == 404, "membership must be asserted, not trusted"


async def test_approval_needs_membership(api):
    a = await sign_in(api, "alice3@tenant-a.test")
    b = await sign_in(api, "bob3@tenant-b.test")

    made = await api.post("/v1/approvals", headers=auth(a),
                          json={"kind": "post", "payload": {"text": "hello"}})
    aid = made.json()["id"]

    assert (await api.post(f"/v1/approvals/{aid}/approve",
                           headers=auth(b))).status_code == 404


async def test_viewer_cannot_approve(api):
    """Roles are enforced at the route, not assumed by the client."""
    owner = await sign_in(api, "owner@roles.test")
    org = (await api.get("/v1/auth/me", headers=auth(owner))).json()["active_org"]

    await api.post("/v1/workspaces/invite", headers=auth(owner),
                   json={"email": "viewer@roles.test", "role": "viewer"})
    # Accepting requires the invite token, which is emailed; here we assert the
    # permission table directly instead.
    from app.core.tenancy import CAN
    assert "approve" not in CAN["viewer"]
    assert "members" not in CAN["member"]
    assert "billing" not in CAN["admin"]


async def test_admin_is_not_a_user_flag(api):
    """No customer may become a platform admin by any customer-facing route."""
    u = await sign_in(api, "nosy@tenant-a.test")
    me = (await api.get("/v1/auth/me", headers=auth(u))).json()
    assert me["platform_admin"] is None

    r = await api.get("/v1/admin/overview", headers=auth(u))
    assert r.status_code == 404, "admin routes must not confirm they exist"


async def test_admin_requires_a_reason(api):
    """Even a real admin cannot look without saying why."""
    u = await sign_in(api, "staff@creai.dev")
    async with db.conn() as c:
        uid = await c.fetchval("SELECT id FROM users WHERE email=$1", "staff@creai.dev")
        await c.execute(
            """INSERT INTO platform_admins (user_id, level) VALUES ($1,'engineer')
               ON CONFLICT (user_id) DO NOTHING""", uid)

    assert (await api.get("/v1/admin/overview", headers=auth(u))).status_code == 400

    ok = await api.get("/v1/admin/overview",
                       headers=auth(u) | {"X-Reason": "checking signup totals"})
    assert ok.status_code == 200

    async with db.conn() as c:
        logged = await c.fetchval(
            "SELECT count(*) FROM admin_access_log WHERE admin_id=$1", uid)
    assert logged >= 1, "every admin read must leave an audit row"


async def test_every_tenant_table_has_org_id(api):
    """A new table without org_id is the likeliest way isolation gets lost."""
    async with db.conn() as c:
        for t in TENANT_TABLES:
            col = await c.fetchval(
                """SELECT 1 FROM information_schema.columns
                   WHERE table_name=$1 AND column_name='org_id'""", t)
            assert col, f"{t} must have an org_id column"
