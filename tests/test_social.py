"""Google sign-in against a fake Google: state, PKCE, nonce, audience,
verified-email linking and the one-time handoff."""

import base64
import hashlib
import json
import os
import secrets
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("ENV", "development")

from app.core import db                         # noqa: E402
from app.core.config import settings            # noqa: E402
from app.main import app                        # noqa: E402

from tests.test_isolation import auth, sign_in  # noqa: E402

pytestmark = pytest.mark.asyncio
CID = "cid-test.apps.googleusercontent.com"


def jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


class FakeGoogle:
    def __init__(self):
        self.pending = {}          # code -> (challenge, claims)

    def __call__(self, req):
        form = parse_qs(req.content.decode())
        code = form["code"][0]
        challenge, claims = self.pending.pop(code)
        v = form["code_verifier"][0]
        if base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode() != challenge:
            return httpx.Response(400, json={"error": "invalid_grant"})
        assert form["client_secret"] == ["secret-test"]
        return httpx.Response(200, json={"access_token": "x", "id_token": jwt(claims)})


@pytest_asyncio.fixture
async def env(monkeypatch):
    for k, v in (("google_client_id", CID), ("google_client_secret", "secret-test"),
                 ("secret_key", "test-secret-key-" + "x" * 16)):
        object.__setattr__(settings, k, v)
    await db.connect()
    g = FakeGoogle()
    real = httpx.AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(g), **kw))
        yield api, g
    await db.disconnect()


async def google_login(api, g, email, sub=None, verified=True, **override):
    r = await api.get("/v1/auth/google/start")
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["client_id"] == [CID] and q["code_challenge_method"] == ["S256"]
    assert q["scope"] == ["openid email profile"]
    claims = {"iss": "https://accounts.google.com", "aud": CID, "exp": int(time.time()) + 300,
              "nonce": q["nonce"][0], "sub": sub or secrets.token_hex(8), "email": email,
              "email_verified": verified, "name": "Pat Doe"} | override
    code = "c" + secrets.token_hex(4)
    g.pending[code] = (q["code_challenge"][0], claims)
    cb = await api.get("/v1/auth/google/callback", params={"code": code, "state": q["state"][0]})
    return cb.headers["location"], q["state"][0], code


async def token_from(api, location):
    handoff = parse_qs(urlparse(location).query)["login_code"][0]
    r = await api.post("/v1/auth/handoff", json={"code": handoff})
    return r, handoff


async def test_google_sign_in_creates_account_and_workspace(env):
    api, g = env
    assert (await api.get("/v1/auth/providers")).json()["providers"] == [{"provider": "google", "name": "Google"}]
    email = f"pat{secrets.token_hex(3)}@gmail-shop.io"
    loc, _, _ = await google_login(api, g, email)
    assert "login_code=" in loc and "creai_s_" not in loc          # no session token in the URL
    r, handoff = await token_from(api, loc)
    me = (await api.get("/v1/auth/me", headers=auth(r.json()["token"]))).json()
    assert me["email"] == email and me["name"] == "Pat Doe" and len(me["workspaces"]) == 1
    again, _ = await token_from(api, loc)                           # handoff is single-use
    assert again.status_code == 401


async def test_same_person_lands_in_same_account_either_way(env):
    api, g = env
    email = f"sam{secrets.token_hex(3)}@example-shop.io"
    code_token = await sign_in(api, email)
    org = (await api.get("/v1/auth/me", headers=auth(code_token))).json()["active_org"]
    sub = secrets.token_hex(8)
    loc, _, _ = await google_login(api, g, email, sub=sub)
    tok = (await token_from(api, loc))[0].json()["token"]
    assert (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"] == org
    # later, Google reports a changed email: the linked identity still wins
    loc2, _, _ = await google_login(api, g, "renamed@example-shop.io", sub=sub)
    tok2 = (await token_from(api, loc2))[0].json()["token"]
    assert (await api.get("/v1/auth/me", headers=auth(tok2))).json()["email"] == email


async def test_unverified_email_never_takes_over_an_account(env):
    api, g = env
    email = f"vic{secrets.token_hex(3)}@example-shop.io"
    await sign_in(api, email)
    loc, _, _ = await google_login(api, g, email, verified=False)
    assert "login=failed" in loc and "login_code" not in loc


@pytest.mark.parametrize("bad", [{"aud": "someone-else"}, {"iss": "https://evil.example"},
                                 {"exp": 1}, {"nonce": "replayed"}])
async def test_tampered_tokens_are_refused(env, bad):
    api, g = env
    loc, _, _ = await google_login(api, g, f"t{secrets.token_hex(3)}@example-shop.io", **bad)
    assert "login=failed" in loc


async def test_state_is_single_use_and_cancel_is_handled(env):
    api, g = env
    loc, state, code = await google_login(api, g, f"s{secrets.token_hex(3)}@example-shop.io")
    g.pending[code] = ("x", {})
    replay = await api.get("/v1/auth/google/callback", params={"code": code, "state": state})
    assert "login=failed" in replay.headers["location"]
    cancel = await api.get("/v1/auth/google/callback", params={"error": "access_denied"})
    assert "login=cancelled" in cancel.headers["location"]


async def test_unconfigured_provider_is_hidden(env):
    api, _ = env
    object.__setattr__(settings, "google_client_id", "")
    try:
        assert (await api.get("/v1/auth/providers")).json()["providers"] == []
        assert (await api.get("/v1/auth/google/start")).status_code == 404
        assert (await api.get("/v1/auth/facebook/start")).status_code == 404
    finally:
        object.__setattr__(settings, "google_client_id", CID)
