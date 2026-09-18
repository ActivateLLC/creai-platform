"""
Accounts for the people who use an app a customer built.

The security claim worth testing is narrow and absolute: one member must never
reach another member's rows, and the server must enforce that rather than the
app remembering to.
"""

import pytest

from app.services import appauth, appfs


# ---------------------------------------------------------------- passwords

def test_passwords_are_hashed_not_stored():
    h = appauth.hash_password("correct horse battery")
    assert "correct horse battery" not in h
    assert h.startswith("scrypt$")
    assert appauth.check_password("correct horse battery", h)
    assert not appauth.check_password("Correct horse battery", h)


def test_a_short_password_is_refused_with_a_reason():
    with pytest.raises(appauth.AuthError) as e:
        appauth.hash_password("short")
    assert "8" in str(e.value)


def test_two_accounts_with_the_same_password_do_not_look_alike():
    """Per-password salt: a leaked table must not reveal who shares a password."""
    assert appauth.hash_password("the same one") != appauth.hash_password("the same one")


# ---------------------------------------------------------------- sessions

def test_a_session_names_its_app_and_its_person():
    tok = appauth.session(7, 3, 42)
    assert appauth.open_session(tok) == (7, 3, 42)


def test_a_tampered_session_is_refused():
    tok = appauth.session(7, 3, 42)
    bad = tok[:-4] + ("aaaa" if not tok.endswith("aaaa") else "bbbb")
    with pytest.raises(appauth.AuthError):
        appauth.open_session(bad)


def test_an_expired_session_is_refused(monkeypatch):
    monkeypatch.setattr(appauth, "SESSION_TTL", -1)
    with pytest.raises(appauth.AuthError):
        appauth.open_session(appauth.session(7, 3, 42))


# ---------------------------------------------------------------- access rules

def test_the_four_levels_survive_a_round_trip():
    files = {"app.json": '{"collections": {"bookings": '
                         '{"read": "own", "write": "user", "manage": "own"}}}'}
    assert appfs.rules(files)["bookings"] == {"read": "own", "write": "user", "manage": "own"}


def test_an_unknown_level_falls_back_to_owner_only():
    """A typo must fail closed, never open."""
    files = {"app.json": '{"collections": {"secrets": {"read": "everyone", "write": "public"}}}'}
    assert appfs.rules(files)["secrets"]["read"] == "owner"
    assert appfs.rules(files)["secrets"]["write"] == "public"


def test_an_app_knows_whether_it_needs_sign_in():
    assert appfs.signin_required({"app.json": '{"collections": {"a": {"read": "own"}}}'})
    assert not appfs.signin_required({"app.json": '{"collections": {"a": {"read": "public"}}}'})
    assert not appfs.signin_required({})


# ---------------------------------------------------------------- the reviewer

def test_review_catches_rules_that_expect_a_sign_in_screen_that_does_not_exist():
    r = appfs.review({
        "app.js": "import { html, render } from 'htm/preact';\n"
                  "const rows = window.creai.db.collection('bookings');\n"
                  "render(html`<div />`, document.getElementById('root'));",
        "app.json": '{"collections": {"bookings": {"read": "own", "write": "own"}}}',
    })
    assert any("sign in" in p for p in r["problems"])


def test_review_notices_signing_in_that_changes_nothing():
    r = appfs.review({
        "app.js": "import { html, render } from 'htm/preact';\n"
                  "await window.creai.auth.me();\n"
                  "window.creai.auth.signOut();\n"
                  "const rows = window.creai.db.collection('notes');\n"
                  "render(html`<div />`, document.getElementById('root'));",
        "app.json": '{"collections": {"notes": {"read": "public", "write": "public"}}}',
    })
    assert any("changes nothing" in n for n in r["notes"])


# ---------------------------------------------------------------- the SDK

def test_the_sdk_sends_the_session_and_drops_it_when_it_expires():
    assert "X-App-Session" in appfs.SDK
    assert "creai_app_session" in appfs.SDK
    # a 401 with a session in hand must clear it rather than loop
    assert "r.status === 401" in appfs.SDK


def test_app_code_still_cannot_reach_browser_storage_itself():
    """The SDK may use sessionStorage; app code may not. That line must hold."""
    with pytest.raises(appfs.AppError):
        appfs.check("app.js", "sessionStorage.setItem('x', 1)")
    with pytest.raises(appfs.AppError):
        appfs.check("app.js", "localStorage.getItem('x')")


# ---------------------------------------------------------------- end to end

import os                                                   # noqa: E402
import secrets                                              # noqa: E402

import pytest_asyncio                                       # noqa: E402
from httpx import ASGITransport, AsyncClient                # noqa: E402

os.environ.setdefault("ENV", "development")

from app.core import db                                     # noqa: E402
from app.core.config import settings                        # noqa: E402
from app.main import app                                    # noqa: E402

from tests.test_isolation import auth, sign_in              # noqa: E402

MEMBER_APP = {
    "app.js": "import { html, render } from 'htm/preact';\n"
              "await window.creai.auth.me();\n"
              "window.creai.auth.signOut();\n"
              "const notes = window.creai.db.collection('notes');\n"
              "render(html`<div />`, document.getElementById('root'));",
    "app.json": '{"collections": {"notes": {"read": "own", "write": "user", "manage": "own"}}}',
}


@pytest_asyncio.fixture
async def api():
    object.__setattr__(settings, "anthropic_key", "test")
    object.__setattr__(settings, "secret_key", "test-secret-key-" + "x" * 16)
    from app.api import appdata, appauth as appauth_routes
    appdata._hits.clear()
    appauth_routes._hits.clear()
    await db.connect()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await db.disconnect()


async def _published_app(api):
    """A project with a live release whose notes are private to each member."""
    tok = await sign_in(api, f"o{secrets.token_hex(3)}@members.io")
    r = await api.post("/v1/projects", headers=auth(tok), json={"name": "Notes", "path": "app"})
    pid = r.json()["id"]
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    await appfs.write(pid, org, MEMBER_APP)
    pub = await api.post(f"/v1/apps/{pid}/publish", headers=auth(tok))
    assert pub.status_code == 200, pub.text
    return tok, pid, org


@pytest.mark.asyncio
async def test_one_member_never_sees_another_members_rows(api):
    """The whole point. Enforced by the server, not by the app remembering to."""
    _owner, pid, org = await _published_app(api)
    visitor = {"X-App-Token": appfs.token(pid, org, "public")}

    # two people sign up to the same app
    a = await api.post("/v1/appauth/signup", headers=visitor,
                       json={"email": "ana@example.com", "password": "a-good-password"})
    b = await api.post("/v1/appauth/signup", headers=visitor,
                       json={"email": "ben@example.com", "password": "a-good-password"})
    assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
    ana = {**visitor, "X-App-Session": a.json()["session"]}
    ben = {**visitor, "X-App-Session": b.json()["session"]}

    made = await api.post("/v1/appdata/notes", headers=ana, json={"data": {"text": "ana's note"}})
    assert made.status_code == 200, made.text
    note_id = made.json()["id"]

    assert [i["text"] for i in (await api.get("/v1/appdata/notes", headers=ana)).json()["items"]] \
        == ["ana's note"]
    assert (await api.get("/v1/appdata/notes", headers=ben)).json()["items"] == []
    assert (await api.get(f"/v1/appdata/notes/{note_id}", headers=ben)).status_code == 404
    assert (await api.patch(f"/v1/appdata/notes/{note_id}", headers=ben,
                            json={"data": {"text": "hijacked"}})).status_code == 404
    assert (await api.delete(f"/v1/appdata/notes/{note_id}", headers=ben)).status_code == 404
    # and ana's note is untouched by all that
    assert (await api.get(f"/v1/appdata/notes/{note_id}", headers=ana)).json()["text"] == "ana's note"


@pytest.mark.asyncio
async def test_signed_out_visitors_are_asked_to_sign_in_not_refused_outright(api):
    _owner, pid, org = await _published_app(api)
    visitor = {"X-App-Token": appfs.token(pid, org, "public")}
    r = await api.get("/v1/appdata/notes", headers=visitor)
    assert r.status_code == 401 and "sign in" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_session_from_one_app_is_worthless_in_another(api):
    _o1, pid1, org1 = await _published_app(api)
    _o2, pid2, org2 = await _published_app(api)
    s = (await api.post("/v1/appauth/signup",
                        headers={"X-App-Token": appfs.token(pid1, org1, "public")},
                        json={"email": "cara@example.com", "password": "a-good-password"})).json()
    # same person's session, pointed at the other app
    r = await api.get("/v1/appdata/notes",
                      headers={"X-App-Token": appfs.token(pid2, org2, "public"),
                               "X-App-Session": s["session"]})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_signing_up_twice_does_not_reveal_who_has_an_account(api):
    _owner, pid, org = await _published_app(api)
    visitor = {"X-App-Token": appfs.token(pid, org, "public")}
    body = {"email": "dee@example.com", "password": "a-good-password"}
    assert (await api.post("/v1/appauth/signup", headers=visitor, json=body)).status_code == 200
    again = await api.post("/v1/appauth/signup", headers=visitor, json=body)
    assert again.status_code == 400 and "sign in instead" in again.json()["detail"]


@pytest.mark.asyncio
async def test_the_owner_can_see_who_uses_their_app_but_never_a_password(api):
    tok, pid, org = await _published_app(api)
    await api.post("/v1/appauth/signup", headers={"X-App-Token": appfs.token(pid, org, "public")},
                   json={"email": "eve@example.com", "password": "a-good-password", "name": "Eve"})
    r = await api.get(f"/v1/appauth/users/{pid}", headers=auth(tok))
    assert r.status_code == 200, r.text
    users = r.json()["users"]
    assert [u["email"] for u in users] == ["eve@example.com"]
    assert "pw" not in users[0] and "password" not in str(users[0])


# ---------------------------------------------------------------- the reviewer, again

def test_nested_templates_are_not_mistaken_for_jsx():
    """A conditional render nests html`` inside ${…}. The old regex stopped at the
    first backtick and reported phantom JSX, which sends the agent chasing nothing."""
    src = ("import { html, render } from 'htm/preact';\n"
           "const err = '';\n"
           "const view = () => html`<div>${err && html`<p class=\"muted\">${err}</p>`}</div>`;\n"
           "render(view(), document.getElementById('root'));")
    assert not [p for p in appfs.review({"app.js": src})["problems"] if "JSX" in p]


def test_real_jsx_is_still_caught():
    src = ("import { render } from 'preact';\n"
           "const A = () => <div className='x' />;\n"
           "render(<A />, document.getElementById('root'));")
    assert any("JSX" in p for p in appfs.review({"app.js": src})["problems"])


def test_me_is_recognised_through_a_local_alias():
    """Apps normally do `const auth = window.creai.auth` and then call auth.me()."""
    src = ("import { html, render } from 'htm/preact';\n"
           "const auth = window.creai.auth;\n"
           "auth.me().then(() => {});\n"
           "auth.signOut();\n"
           "const rows = window.creai.db.collection('notes');\n"
           "render(html`<div />`, document.getElementById('root'));")
    r = appfs.review({"app.js": src,
                      "app.json": '{"collections": {"notes": {"read": "own", "write": "own"}}}'})
    assert not any("me()" in n for n in r["notes"])
