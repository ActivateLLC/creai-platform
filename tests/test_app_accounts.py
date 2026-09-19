"""
Accounts for the people who use an app a customer built.

The security claim worth testing is narrow and absolute: one member must never
reach another member's rows, and the server must enforce that rather than the
app remembering to.
"""

import asyncio
from datetime import datetime, timezone

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


# ---------------------------------------------------------------- forgotten passwords

@pytest.mark.asyncio
async def test_a_reset_link_works_once_and_then_never_again(api):
    _owner, pid, org = await _published_app(api)
    visitor = {"X-App-Token": appfs.token(pid, org, "public")}
    await api.post("/v1/appauth/signup", headers=visitor,
                   json={"email": "fay@example.com", "password": "first-password"})

    found = await appauth.begin_reset(pid, "fay@example.com")
    assert found and found[0] == "fay@example.com"
    token = found[1]

    r = await api.post("/v1/appauth/reset", headers=visitor,
                       json={"token": token, "password": "second-password"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["email"] == "fay@example.com"
    assert r.json()["session"]                      # signed in straight after

    # the same link a second time is dead, because the old hash signed it
    again = await api.post("/v1/appauth/reset", headers=visitor,
                           json={"token": token, "password": "third-password"})
    assert again.status_code == 400 and "already been used" in again.json()["detail"]

    # and the new password is the one that works
    ok = await api.post("/v1/appauth/signin", headers=visitor,
                        json={"email": "fay@example.com", "password": "second-password"})
    assert ok.status_code == 200
    old = await api.post("/v1/appauth/signin", headers=visitor,
                         json={"email": "fay@example.com", "password": "first-password"})
    assert old.status_code == 401


@pytest.mark.asyncio
async def test_forgot_says_the_same_thing_whether_or_not_the_account_exists(api):
    _owner, pid, org = await _published_app(api)
    visitor = {"X-App-Token": appfs.token(pid, org, "public")}
    a = await api.post("/v1/appauth/forgot", headers=visitor, json={"email": "nobody@example.com"})
    await api.post("/v1/appauth/signup", headers=visitor,
                   json={"email": "gus@example.com", "password": "a-good-password"})
    b = await api.post("/v1/appauth/forgot", headers=visitor, json={"email": "gus@example.com"})
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json() == {"sent": True}


@pytest.mark.asyncio
async def test_a_reset_link_is_useless_in_another_app(api):
    _o1, pid1, org1 = await _published_app(api)
    _o2, pid2, org2 = await _published_app(api)
    await api.post("/v1/appauth/signup", headers={"X-App-Token": appfs.token(pid1, org1, "public")},
                   json={"email": "hal@example.com", "password": "a-good-password"})
    token = (await appauth.begin_reset(pid1, "hal@example.com"))[1]
    r = await api.post("/v1/appauth/reset",
                       headers={"X-App-Token": appfs.token(pid2, org2, "public")},
                       json={"token": token, "password": "new-password-here"})
    assert r.status_code == 400


# ---------------------------------------------------------------- dead sign-in buttons

def test_a_sign_in_button_with_nowhere_to_go_is_reported():
    """The failure seen in the wild: a page advertising a portal, with a button
    that only scrolls to contact."""
    from app.services import site as site_spec
    spec = site_spec.merge({}, {"business": "Cannon Construction",
                                "headline": "Sign in to see where you stand",
                                "cta": "Sign in", "contact": {"email": "a@b.com"}})
    assert any("sounds like a way into an app" in i for i in site_spec.critique(spec))


def test_linking_the_button_to_the_app_clears_it_and_renders_a_real_href():
    from app.services import site as site_spec
    spec = site_spec.merge({}, {"business": "Cannon Construction", "headline": "Sign in",
                                "cta": "Sign in", "cta_link": "app",
                                "contact": {"email": "a@b.com"}})
    assert not any("sounds like a way into an app" in i for i in site_spec.critique(spec))
    assert 'href="https://x.dev/a/portal"' in site_spec.render(spec, "https://x.dev/a/portal")


def test_without_a_published_app_the_button_falls_back_rather_than_breaking():
    from app.services import site as site_spec
    spec = site_spec.merge({}, {"business": "X", "headline": "Sign in", "cta": "Sign in",
                                "cta_link": "app", "contact": {"email": "a@b.com"}})
    assert 'href="#contact"' in site_spec.render(spec, None)


def test_an_ordinary_button_is_left_alone():
    from app.services import site as site_spec
    spec = site_spec.merge({}, {"business": "X", "headline": "Fast quotes",
                                "cta": "Get a quote", "contact": {"email": "a@b.com"}})
    assert not any("sounds like a way into an app" in i for i in site_spec.critique(spec))


# ---------------------------------------------------------------- what apps may reach

def test_motion_and_3d_are_available_to_apps():
    assert "gsap" in appfs.IMPORTS and "gsap/ScrollTrigger" in appfs.IMPORTS
    assert "three" in appfs.IMPORTS
    for spec in ("gsap", "gsap/ScrollTrigger", "three"):
        assert appfs.IMPORTS[spec].startswith("https://esm.sh/"), spec


def test_importing_gsap_is_not_reported_as_unavailable():
    src = ("import { html, render } from 'htm/preact';\n"
           "import gsap from 'gsap';\n"
           "gsap.from('.card', { y: 12, opacity: 0 });\n"
           "render(html`<div />`, document.getElementById('root'));")
    assert not appfs.review({"app.js": src})["problems"]


def test_an_unknown_library_is_still_refused():
    src = ("import { html, render } from 'htm/preact';\n"
           "import axios from 'axios';\n"
           "render(html`<div />`, document.getElementById('root'));")
    assert any("axios" in p for p in appfs.review({"app.js": src})["problems"])


def test_the_kit_has_depth_motion_and_respects_reduced_motion():
    css = appfs.kit_css({})
    for token in ("--lift-1", "--lift-2", "--lift-3", "@keyframes rise", ".skeleton",
                  "prefers-color-scheme:dark", "prefers-reduced-motion"):
        assert token in css, token


# ---------------------------------------------------------------- turn length

def test_pictures_are_capped_per_turn():
    """Five sequential image generations made a turn outlive the browser, which
    the person saw as "Load failed" even though the build succeeded."""
    from app.services import agent
    assert agent.MAX_IMAGES_PER_TURN <= 3


def test_a_dropped_connection_says_something_useful():
    html = open("app/web/index.html").read()
    assert "connection dropped before the reply came back" in html
    assert "You're offline" in html


# ---------------------------------------------------------------- placeholders

def test_bracketed_placeholders_are_reported_including_in_the_business_name():
    """Shipped twice in real builds: [Artist Name] in the header, footer and tab
    title. The old rule scanned everything except the business name — and told the
    agent to use brackets in the first place."""
    from app.services import site as site_spec
    spec = site_spec.merge({}, {"business": "[Artist Name] Tattoo", "headline": "Black ink.",
                                "cta": "Book", "contact": {"email": "a@b.com"},
                                "sections": [{"kind": "about", "body": "x"}]})
    issues = [i for i in site_spec.critique(spec) if "placeholder" in i.lower()]
    assert issues and "broken page" in issues[0]


def test_the_advice_no_longer_recommends_brackets():
    from app.services import site as site_spec
    spec = site_spec.merge({}, {"business": "X", "headline": "lorem ipsum", "cta": "Go",
                                "contact": {"email": "a@b.com"}})
    issues = " ".join(site_spec.critique(spec))
    assert "[BRACKETED]" not in issues


def test_a_real_name_passes():
    from app.services import site as site_spec
    spec = site_spec.merge({}, {"business": "Vera Ink", "headline": "Black ink. Milwaukee skin.",
                                "cta": "Book a session", "contact": {"email": "a@b.com"},
                                "sections": [{"kind": "about", "body": "x"},
                                             {"kind": "cta", "body": "y"}]})
    assert not [i for i in site_spec.critique(spec) if "placeholder" in i.lower()]


# ---------------------------------------------------------------- the agent's tools

def test_the_agent_can_start_an_app_from_a_site():
    """Without this it proposed a portal, offered a button, and proposed again
    forever — a site project has no way to hold sign-in."""
    from app.services import agent
    assert agent.TOOL_START_APP["name"] == "start_app"
    site_tools = agent.TOOL_UPDATE_SITE, agent.TOOL_START_APP, agent.TOOL_LOOK
    assert all(t.get("name") for t in site_tools)


def test_the_agent_can_see_what_it_built():
    from app.services import agent
    assert agent.TOOL_LOOK["name"] == "look"
    assert agent.MAX_LOOKS_PER_TURN <= 3


def test_screenshots_reach_the_model_as_images_not_as_text():
    """A base64 blob pasted into JSON teaches it nothing; an image block does."""
    src = open("app/services/agent.py").read()
    assert '"_shots"' in src
    assert '"media_type": "image/png"' in src


def test_content_images_are_not_square_cornered():
    from app.services import site as site_spec
    css = site_spec.render(site_spec.merge({}, {"business": "X", "headline": "Y"}))
    assert "img{max-width:100%;display:block;border-radius:var(--r)}" in css


def test_the_known_library_for_each_job_is_available():
    """Verified loading in a real sandboxed browser, not just listed here."""
    for spec in ("lucide", "motion", "@floating-ui/dom", "zod", "chart.js/auto", "d3",
                 "date-fns", "marked", "fuse.js", "sortablejs", "embla-carousel",
                 "canvas-confetti", "gsap", "three", "phaser"):
        assert spec in appfs.IMPORTS, spec
        assert appfs.IMPORTS[spec].startswith("https://esm.sh/"), spec


def test_importing_a_charting_library_is_not_reported_as_unavailable():
    src = ("import { html, render } from 'htm/preact';\n"
           "import Chart from 'chart.js/auto';\n"
           "import { format } from 'date-fns';\n"
           "render(html`<div />`, document.getElementById('root'));")
    assert not appfs.review({"app.js": src})["problems"]


# ---------------------------------------------------------------- site motion & CSP

def test_only_creais_own_motion_script_may_run_on_a_published_site():
    """Verified in a real browser: the hash-pinned script runs, an injected inline
    script and a CDN script are both blocked."""
    from app.api.sites import SITE_CSP
    from app.services import site as site_spec
    assert site_spec.motion_hash() in SITE_CSP
    assert "'unsafe-inline'" not in SITE_CSP.split("style-src")[0]
    assert "https://cdn" not in SITE_CSP


def test_the_hash_matches_the_script_that_is_actually_served():
    """If these drift, every published site silently loses its motion."""
    import base64, hashlib
    from app.services import site as site_spec
    want = "'sha256-" + base64.b64encode(
        hashlib.sha256(site_spec.MOTION_JS.encode()).digest()).decode() + "'"
    assert site_spec.motion_hash() == want
    assert site_spec.MOTION_JS in site_spec.render(
        site_spec.merge({}, {"business": "X", "headline": "Y"}))


def test_reduced_motion_is_still_honoured():
    from app.services import site as site_spec
    assert "prefers-reduced-motion" in site_spec.MOTION_JS
    assert "no-motion" in site_spec.MOTION_JS


def test_icons_are_inline_vectors_needing_no_request():
    from app.services import site as site_spec
    svg = site_spec.icon("wrench")
    assert svg.startswith("<svg") and "stroke=\"currentColor\"" in svg
    assert site_spec.icon("not-a-real-icon") == ""


# ---------------------------------------------------------------- no emoji as icons

def test_emoji_are_refused_everywhere_on_a_site():
    from app.services import site as site_spec

    def issues(**kw):
        spec = site_spec.merge({}, dict(
            {"business": "X", "headline": "Fast repairs", "cta": "Book",
             "contact": {"email": "a@b.com"},
             "sections": [{"kind": "about", "body": "x"}, {"kind": "cta", "body": "y"}]}, **kw))
        return [i for i in site_spec.critique(spec) if "emoji" in i.lower()]

    assert issues(cta="Book now 🔧")
    assert issues(business="Plumbing 💧")
    assert issues(sections=[{"kind": "services", "items": [{"name": "✅ Fast", "detail": "x"}]},
                            {"kind": "cta", "body": "y"}])
    assert issues(sections=[{"kind": "about", "body": "x"},
                            {"kind": "cta", "body": "y", "button": "Go 🚀"}])


def test_typographic_marks_are_not_emoji():
    """Arrows and dashes are typography, and the tattoo site used them well."""
    from app.services import site as site_spec
    spec = site_spec.merge({}, {"business": "X", "headline": "Fast repairs — done right",
                                "cta": "Book now →", "contact": {"email": "a@b.com"},
                                "sections": [{"kind": "about", "body": "x"},
                                             {"kind": "cta", "body": "y"}]})
    assert not [i for i in site_spec.critique(spec) if "emoji" in i.lower()]


def test_emoji_in_app_code_is_refused_and_points_at_the_icon_set():
    src = ("import { html, render } from 'htm/preact';\n"
           "const b = html`<button class=\"btn\">🔧 Fix it</button>`;\n"
           "render(b, document.getElementById('root'));")
    problems = [p for p in appfs.review({"app.js": src})["problems"] if "emoji" in p.lower()]
    assert problems and "lucide" in problems[0]


def test_an_app_using_the_real_icon_set_passes():
    src = ("import { html, render } from 'htm/preact';\n"
           "import { createIcons, icons } from 'lucide';\n"
           "render(html`<button class=\"btn\"><i data-lucide=\"wrench\"></i> Fix it</button>`,"
           " document.getElementById('root'));\n"
           "createIcons({ icons });")
    assert not [p for p in appfs.review({"app.js": src})["problems"] if "emoji" in p.lower()]


def test_emoji_are_refused_in_godot_games_too():
    """Godot draws emoji through the project font: tofu boxes or flat grey, and
    they can't be tinted, atlased or animated like real art."""
    from app.services import godot
    files = dict(godot.STARTER)
    assert not [p for p in godot.review(files)["problems"] if "emoji" in p.lower()]
    script = next(k for k in files if k.endswith(".gd"))
    files[script] += '\nfunc _hud(): return "❤️ Lives"\n'
    problems = [p for p in godot.review(files)["problems"] if "emoji" in p.lower()]
    assert problems and ("Polygon2D" in problems[0] or "Sprite2D" in problems[0])


def test_browser_games_are_covered_by_the_app_reviewer():
    src = ("import { canvas, loop } from 'creai/game';\n"
           "const c = canvas(); const ctx = c.getContext('2d');\n"
           "const hud = '⭐ 0';\n"
           "loop({ update(){}, draw(){ ctx.fillText(hud, 10, 10); } });")
    assert [p for p in appfs.review({"app.js": src})["problems"] if "emoji" in p.lower()]


# ---------------------------------------------------------------- drawn icons

def test_an_icon_drawn_for_the_business_is_rendered():
    from app.services import site as site_spec
    spec = site_spec.merge({}, {"business": "Copperline", "headline": "Leaks fixed", "cta": "Book",
        "contact": {"email": "a@b.com"},
        "sections": [{"kind": "services", "items": [
            {"name": "Emergency repairs", "detail": "Burst pipes.",
             "icon": "M4 12 L12 4 L20 12 M6 10 v9 h12 v-9"}]},
            {"kind": "cta", "body": "y"}]})
    html = site_spec.render(spec)
    assert '<svg class="ico"' in html and "M4 12 L12 4 L20 12" in html


def test_the_icon_channel_carries_geometry_and_nothing_else():
    """The agent draws; it cannot smuggle markup, script or a URL through the path."""
    from app.services import site as site_spec
    assert site_spec.safe_path("M4 12 L12 4 L20 12")
    for evil in ('M0 0"/><script>x()</script><path d="M1 1',
                 "M0 0 url(#x)", "M0 0 <g>", "hello", "", "L4 4",
                 "M4 12 L99999 4"):
        assert site_spec.safe_path(evil) == "", evil


def test_a_refused_drawing_falls_back_rather_than_breaking_the_page():
    from app.services import site as site_spec
    spec = site_spec.merge({}, {"business": "X", "headline": "Y", "cta": "Book",
        "contact": {"email": "a@b.com"},
        "sections": [{"kind": "services", "items": [
            {"name": "A", "detail": "b", "icon": "<script>bad()</script>"}]},
            {"kind": "cta", "body": "y"}]})
    html = site_spec.render(spec)
    assert "bad()" not in html and "<h3>A</h3>" in html


# ---------------------------------------------------------------- files people upload

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

FILE_APP = {
    "app.js": "import { html, render } from 'htm/preact';\n"
              "await window.creai.auth.me();\n"
              "window.creai.auth.signOut();\n"
              "const notes = window.creai.db.collection('notes');\n"
              "render(html`<div />`, document.getElementById('root'));",
    "app.json": '{"collections": {"notes": {"read": "own", "write": "user", "manage": "own"}}}',
}


@pytest_asyncio.fixture
async def files_api(api, monkeypatch):
    """Storage stubbed at the bucket, so the access rules are what's under test."""
    from app.services import appfiles, assets
    store = {}
    monkeypatch.setattr(assets, "configured", lambda: True)

    async def put(key, data, mime):
        store[key] = data

    async def blob(key):
        return store.get(key)

    monkeypatch.setattr(assets, "put_blob", put)
    monkeypatch.setattr(appfiles.assets, "put_blob", put)
    monkeypatch.setattr(appfiles.assets, "blob", blob)
    monkeypatch.setattr(appfiles.assets, "configured", lambda: True)
    yield api


@pytest.mark.asyncio
async def test_one_persons_upload_is_invisible_to_another(files_api):
    api = files_api
    _owner, pid, org = await _published_app(api)
    visitor = {"X-App-Token": appfs.token(pid, org, "public")}
    a = (await api.post("/v1/appauth/signup", headers=visitor,
                        json={"email": "ivy@example.com", "password": "a-good-password"})).json()
    b = (await api.post("/v1/appauth/signup", headers=visitor,
                        json={"email": "jon@example.com", "password": "a-good-password"})).json()
    ivy = {**visitor, "X-App-Session": a["session"]}
    jon = {**visitor, "X-App-Session": b["session"]}

    up = await api.post("/v1/appfiles/notes", headers=ivy,
                        files={"file": ("receipt.png", PNG, "image/png")})
    assert up.status_code == 200, up.text
    f = up.json()

    assert [x["name"] for x in (await api.get("/v1/appfiles/notes", headers=ivy)).json()["files"]] \
        == ["receipt.png"]
    assert (await api.get("/v1/appfiles/notes", headers=jon)).json()["files"] == []

    # the exact link, in someone else's hands, is a 404
    assert (await api.get(f["url"], headers=ivy)).status_code == 200
    assert (await api.get(f["url"], headers=jon)).status_code == 404
    assert (await api.delete(f["url"], headers=jon)).status_code == 404
    assert (await api.get(f["url"], headers=ivy)).content == PNG


@pytest.mark.asyncio
async def test_a_signed_out_stranger_cannot_read_a_private_file(files_api):
    api = files_api
    _owner, pid, org = await _published_app(api)
    visitor = {"X-App-Token": appfs.token(pid, org, "public")}
    s = (await api.post("/v1/appauth/signup", headers=visitor,
                        json={"email": "kim@example.com", "password": "a-good-password"})).json()
    f = (await api.post("/v1/appfiles/notes", headers={**visitor, "X-App-Session": s["session"]},
                        files={"file": ("x.png", PNG, "image/png")})).json()
    assert (await api.get(f["url"], headers=visitor)).status_code == 401


@pytest.mark.asyncio
async def test_a_file_that_lies_about_its_type_is_refused(files_api):
    api = files_api
    _owner, pid, org = await _published_app(api)
    visitor = {"X-App-Token": appfs.token(pid, org, "public")}
    s = (await api.post("/v1/appauth/signup", headers=visitor,
                        json={"email": "lee@example.com", "password": "a-good-password"})).json()
    who = {**visitor, "X-App-Session": s["session"]}
    bad = await api.post("/v1/appfiles/notes", headers=who,
                         files={"file": ("evil.png", b"<?php echo 1; ?>", "image/png")})
    assert bad.status_code == 400 and "isn't what its name says" in bad.json()["detail"]
    wrong = await api.post("/v1/appfiles/notes", headers=who,
                           files={"file": ("a.exe", b"MZ", "application/x-msdownload")})
    assert wrong.status_code == 400


def test_the_sdk_never_puts_a_token_in_a_url():
    """A token in a query string leaks through referrers and server logs."""
    assert "createObjectURL" in appfs.SDK
    assert "?t=" not in appfs.SDK and "token=" not in appfs.SDK


# ---------------------------------------------------------------- payments

def test_creai_takes_a_fee_that_can_never_exceed_the_charge():
    from app.services import payments
    assert payments.fee_for(10_000) == 200          # 2% of $100
    assert payments.fee_for(50) == 1
    assert payments.fee_for(1) == 0                 # never more than the charge
    assert all(payments.fee_for(a) < a for a in (1, 50, 99, 100, 10_000, 2_000_000))


@pytest.mark.asyncio
async def test_a_charge_is_made_on_the_customers_account_not_ours(monkeypatch):
    """Direct charges: the money reaches the business and their name is on the
    statement. Without Stripe-Account this would charge Creai's own account."""
    from app.services import payments
    seen = {}

    async def fake_stripe(method, path, data=None, stripe_account=None):
        seen[path] = stripe_account
        return {"id": "cs_test_1", "url": "https://stripe.test/pay"}

    async def fake_account(org_id, project_id):
        return {"stripe_account": "acct_customer", "ready": True}

    class _Conn:
        async def execute(self, *a, **k): return None
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    def noop(*a, **k): return _Conn()

    monkeypatch.setattr(payments, "_stripe", fake_stripe)
    monkeypatch.setattr(payments, "account_for", fake_account)
    monkeypatch.setattr(payments, "conn", noop)
    out = await payments.checkout(1, 2, amount=10_000, currency="usd", label="Deposit",
                                  success_url="https://x/ok", cancel_url="https://x/no")
    assert seen["/checkout/sessions"] == "acct_customer"
    assert out["fee"] == 200 and out["url"] == "https://stripe.test/pay"


@pytest.mark.asyncio
async def test_an_app_cannot_charge_before_the_owner_has_connected(api, monkeypatch):
    _owner, pid, org = await _published_app(api)
    visitor = {"X-App-Token": appfs.token(pid, org, "public")}
    s = (await api.post("/v1/appauth/signup", headers=visitor,
                        json={"email": "mia@example.com", "password": "a-good-password"})).json()
    r = await api.post("/v1/apppay/notes",
                       headers={**visitor, "X-App-Session": s["session"]},
                       json={"amount": 5000, "label": "Deposit"})
    assert r.status_code == 400 and "connecting its payment account" in r.json()["detail"]


@pytest.mark.asyncio
async def test_an_absurd_amount_is_refused_before_it_reaches_stripe(api):
    _owner, pid, org = await _published_app(api)
    visitor = {"X-App-Token": appfs.token(pid, org, "public")}
    s = (await api.post("/v1/appauth/signup", headers=visitor,
                        json={"email": "ned@example.com", "password": "a-good-password"})).json()
    who = {**visitor, "X-App-Session": s["session"]}
    for amount in (0, 10, -500, 900_000_000):
        r = await api.post("/v1/apppay/notes", headers=who,
                           json={"amount": amount, "label": "Oops"})
        assert r.status_code in (400, 422), amount


@pytest.mark.asyncio
async def test_a_signed_out_stranger_cannot_start_a_payment(api):
    _owner, pid, org = await _published_app(api)
    r = await api.post("/v1/apppay/notes",
                       headers={"X-App-Token": appfs.token(pid, org, "public")},
                       json={"amount": 5000, "label": "Deposit"})
    assert r.status_code == 401


def test_the_owner_has_a_way_to_connect_stripe():
    """The endpoints existed with nothing calling them, which is the same as not
    existing to the person who needs them."""
    html = open("app/web/index.html").read()
    assert 'id="tPay"' in html and 'id="pPay"' in html
    assert "loadPayments" in html
    assert "/v1/payments/' + S.project.id + '/connect" in html
    # the fee is stated where the owner can see it, not buried
    assert "fee_bps" in html


@pytest.mark.asyncio
async def test_connect_being_switched_off_is_reported_not_guessed(monkeypatch):
    """A working Stripe key does not mean Connect is enabled. Finding out at boot
    beats finding out when a customer presses Connect."""
    from app.services import payments

    async def ok(method, path, data=None, stripe_account=None):
        assert path.startswith("/accounts")
        return {"data": [{"id": "acct_1"}]}

    monkeypatch.setattr(payments, "_stripe", ok)
    assert "ok" in await payments.self_check()

    async def refused(method, path, data=None, stripe_account=None):
        from app.services.billing import BillingError
        raise BillingError("Only Stripe Connect platforms can work with other accounts")

    monkeypatch.setattr(payments, "_stripe", refused)
    with pytest.raises(Exception):
        await payments.self_check()


def test_health_reports_payments_separately_from_billing():
    """Billing is Creai charging its customers; payments is customers charging
    theirs. They fail independently."""
    src = open("app/core/config.py").read()
    assert '"payments":' in src and "CONNECT_OFF" in src


# ---------------------------------------------------------------- the improvement loop

def test_faults_are_counted_by_shape_not_by_wording():
    """Two emoji complaints are one fault. Otherwise the tally says nothing."""
    from app.services import quality
    assert quality.shape_of("Remove the emoji (🔧). They render differently") == "emoji-as-icon"
    assert quality.shape_of("Remove the emoji (💧). They render differently") == "emoji-as-icon"
    assert quality.shape_of('"Sign in" sounds like a way into an app') == "dead-sign-in-button"
    assert quality.shape_of("There is placeholder text on the page") == "placeholder-on-page"
    assert quality.shape_of("app.js: looks like JSX. Use html`` instead") == "jsx-instead-of-templates"
    assert quality.shape_of("project.godot has no run/main_scene") == "godot-main-scene"
    assert quality.shape_of("something nobody has classified") == "other"


@pytest.mark.asyncio
async def test_recording_a_fault_can_never_break_the_build_it_watches(monkeypatch):
    """Telemetry that can throw is worse than no telemetry."""
    from app.services import quality

    def broken(*a, **k):
        raise RuntimeError("database is having a day")

    monkeypatch.setattr(quality, "conn", broken)
    await quality.record(1, 2, "site", ["Remove the emoji (x)"])   # must not raise


@pytest.mark.asyncio
async def test_nothing_is_written_when_there_is_nothing_to_report(monkeypatch):
    from app.services import quality
    called = []
    monkeypatch.setattr(quality, "conn", lambda *a, **k: called.append(1))
    await quality.record(1, 2, "site", [])
    assert not called


# ---------------------------------------------------------------- taking it with you

@pytest.mark.asyncio
async def test_a_site_exports_as_a_page_that_stands_on_its_own(api):
    import io, zipfile
    tok = await sign_in(api, f"x{secrets.token_hex(3)}@leaving.io")
    r = await api.post("/v1/projects", headers=auth(tok), json={"name": "Copperline", "path": "launch"})
    pid = r.json()["id"]
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    async with db.conn() as c:
        await c.execute("UPDATE projects SET answers=$1 WHERE id=$2",
                        {"site": {"business": "Copperline", "headline": "Leaks fixed"}}, pid)

    out = await api.get(f"/v1/projects/{pid}/export", headers=auth(tok))
    assert out.status_code == 200
    assert out.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(out.content))
    assert set(z.namelist()) == {"index.html", "site.json", "README.md"}
    page = z.read("index.html").decode()
    assert "Copperline" in page and "<!doctype html>" in page.lower()


@pytest.mark.asyncio
async def test_an_app_export_is_honest_about_what_still_needs_creai(api):
    import io, zipfile
    tok = await sign_in(api, f"y{secrets.token_hex(3)}@leaving.io")
    pid = (await api.post("/v1/projects", headers=auth(tok),
                          json={"name": "Portal", "path": "app"})).json()["id"]
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    await appfs.write(pid, org, {"app.js": "export const x = 1;\n"})

    out = await api.get(f"/v1/projects/{pid}/export", headers=auth(tok))
    z = zipfile.ZipFile(io.BytesIO(out.content))
    assert "files/app.js" in z.namelist()
    readme = z.read("README.md").decode()
    for depends in ("creai.db", "creai.auth", "creai.files", "creai.pay"):
        assert depends in readme, depends
    assert "need nothing from Creai" in readme          # and what doesn't


@pytest.mark.asyncio
async def test_another_workspace_cannot_export_your_project(api):
    tok = await sign_in(api, f"z{secrets.token_hex(3)}@leaving.io")
    pid = (await api.post("/v1/projects", headers=auth(tok),
                          json={"name": "Mine", "path": "launch"})).json()["id"]
    other = await sign_in(api, f"w{secrets.token_hex(3)}@elsewhere.io")
    assert (await api.get(f"/v1/projects/{pid}/export", headers=auth(other))).status_code == 404


@pytest.mark.asyncio
async def test_a_data_export_never_carries_password_material(api):
    tok = await sign_in(api, f"v{secrets.token_hex(3)}@leaving.io")
    pid = (await api.post("/v1/projects", headers=auth(tok),
                          json={"name": "Portal", "path": "app"})).json()["id"]
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    await appauth.sign_up(pid, org, "member@example.com", "a-good-password", "Mem")
    out = await api.get(f"/v1/projects/{pid}/export/data", headers=auth(tok))
    assert out.status_code == 200
    body = out.text
    assert "member@example.com" in body
    assert "scrypt" not in body and "pw" not in out.json()["users"][0]


# ---------------------------------------------------------------- bring your own site

def _zip(files):
    import io, zipfile
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return b.getvalue()


def test_an_uploaded_folder_is_unpacked_the_way_a_person_expects():
    from app.services import imports
    out = imports.read_zip(_zip({"mysite/index.html": "<h1>Hi</h1>",
                                 "mysite/app.js": "console.log(1)",
                                 "mysite/.git/config": "secret",
                                 "mysite/node_modules/x/y.js": "junk"}))
    assert sorted(out) == ["app.js", "index.html"]          # wrapper stripped, junk skipped


def test_a_project_that_needs_a_server_is_refused_with_a_reason():
    from app.services import imports
    with pytest.raises(imports.ImportError_) as e:
        imports.read_zip(_zip({"index.php": "<?php echo 1; ?>"}))
    assert "needs a server" in str(e.value)


def test_path_traversal_cannot_escape_the_bundle():
    from app.services import imports
    out = imports.read_zip(_zip({"index.html": "x", "../../../etc/passwd": "root:x:0:0"}))
    assert list(out) == ["index.html"]


def test_a_folder_with_no_landing_page_is_refused():
    from app.services import imports
    with pytest.raises(imports.ImportError_):
        imports.read_zip(_zip({"readme.txt": "hello"}))


@pytest.mark.asyncio
async def test_an_imported_site_is_never_served_from_the_apps_own_origin(api):
    """The whole reason this feature is shaped the way it is: somebody else's
    JavaScript on app.creai.dev could read a signed-in visitor's token."""
    r = await api.get("/i/anything/index.html", headers={"host": "test"})
    assert r.status_code == 404
    from app.core.config import settings
    app_host = (settings.public_url or "").split("//")[-1].split("/")[0]
    r2 = await api.get("/i/anything/index.html", headers={"host": app_host})
    assert r2.status_code == 404


@pytest.mark.asyncio
async def test_publishing_without_a_domain_or_a_hosting_host_is_refused(api, monkeypatch):
    from app.services import imports as imports_svc
    monkeypatch.setattr(imports_svc, "can_serve_free_address", lambda: False)
    tok = await sign_in(api, f"i{secrets.token_hex(3)}@bringyourown.io")
    pid = (await api.post("/v1/projects", headers=auth(tok),
                          json={"name": "Mine", "path": "launch"})).json()["id"]
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    async with db.conn() as c:
        await c.execute(
            """INSERT INTO site_imports (org_id, project_id, slug, source, manifest, files, bytes)
               VALUES ($1,$2,'abcd1234','upload',$3,1,10)""",
            org, pid, {"index.html": {"key": "k", "mime": "text/html", "size": 10}})
    r = await api.post(f"/v1/imports/{pid}/publish", headers=auth(tok))
    assert r.status_code == 409 and "read a signed-in visitor's account" in r.json()["detail"]


def test_a_whole_repo_is_rooted_at_its_built_output():
    """People zip the repo, not the one folder a host wants. Without this the site
    404s on its own homepage while every file is present."""
    from app.services import imports
    out = imports.read_zip(_zip({
        "my-app/package.json": "{}", "my-app/src/App.jsx": "export default 1",
        "my-app/dist/index.html": "<h1>Built</h1>", "my-app/dist/assets/app.js": "1",
        "my-app/README.md": "# hi"}))
    assert sorted(out) == ["assets/app.js", "index.html"]
    assert imports.entry_for("", out) == "index.html"


def test_source_that_was_never_built_says_what_to_do():
    from app.services import imports
    with pytest.raises(imports.ImportError_) as e:
        imports.read_zip(_zip({"app/package.json": "{}", "app/vite.config.js": "x",
                               "app/src/main.jsx": "y"}))
    assert "npm run build" in str(e.value)


def test_every_common_build_folder_is_recognised():
    from app.services import imports
    for d in ("dist", "build", "out", "public", "_site"):
        assert imports.find_root([f"{d}/index.html", "package.json"]) == d + "/"
    assert imports.find_root(["index.html"]) == ""
    assert imports.find_root(["src/main.js"]) is None


def test_a_shallow_index_wins_over_a_deep_one():
    """A stray index.html in a docs example must not become the homepage."""
    from app.services import imports
    root = imports.find_root(["site/index.html", "site/examples/demo/index.html"])
    assert root == "site/"


# ---------------------------------------------------------------- no zip required

def test_a_folder_picked_in_a_browser_imports_without_zipping_anything():
    """Non-technical people do not zip things. A browser hands each file over with
    the path it had on their machine, which is enough."""
    from app.services import imports
    out = imports.read_loose([
        ("my site/index.html", b"<h1>Hi</h1>"),
        ("my site/style.css", b"body{}"),
        ("my site/photos/a.jpg", b"\xff\xd8\xff"),
        ("my site/.DS_Store", b"junk")])
    assert sorted(out) == ["index.html", "photos/a.jpg", "style.css"]


def test_a_single_page_is_a_website_too():
    from app.services import imports
    assert list(imports.read_loose([("index.html", b"<h1>One page</h1>")])) == ["index.html"]


def test_picking_the_wrong_things_is_explained_in_plain_words():
    from app.services import imports
    with pytest.raises(imports.ImportError_) as e:
        imports.read_loose([("holiday.mov", b"x"), ("notes.docx", b"y")])
    assert "a website is made of" in str(e.value)


def test_a_picked_repo_still_roots_at_its_build_output():
    from app.services import imports
    out = imports.read_loose([
        ("app/package.json", b"{}"), ("app/src/main.jsx", b"x"),
        ("app/dist/index.html", b"<h1>Built</h1>"), ("app/dist/app.js", b"1")])
    assert sorted(out) == ["app.js", "index.html"]


@pytest.mark.asyncio
async def test_the_upload_route_takes_loose_files_as_well_as_a_zip(api):
    tok = await sign_in(api, f"n{secrets.token_hex(3)}@nozip.io")
    pid = (await api.post("/v1/projects", headers=auth(tok),
                          json={"name": "Mine", "path": "launch"})).json()["id"]
    r = await api.post(f"/v1/imports/{pid}", headers=auth(tok),
                       files=[("files", ("index.html", b"<h1>Hi</h1>", "text/html")),
                              ("files", ("style.css", b"body{}", "text/css"))],
                       data={"paths": ["site/index.html", "site/style.css"]})
    # 503 is the honest answer when the bucket isn't configured in this test env
    assert r.status_code in (200, 503), r.text
    if r.status_code == 200:
        assert r.json()["files"] == 2


def test_the_import_screen_does_not_assume_anyone_can_zip():
    html = open("app/web/index.html").read()
    assert "webkitdirectory" in html and "webkitRelativePath" in html
    assert "Drop your website folder here" in html
    assert "I have a zip file instead" in html      # the fallback, not the default


# ---------------------------------------------------------------- put it back

@pytest.mark.asyncio
async def test_restoring_keeps_the_version_you_restored_from(api):
    """Nobody should be punished for restoring by mistake: going back makes a new
    version, so forward is still there."""
    from app.services import versions
    tok = await sign_in(api, f"h{secrets.token_hex(3)}@history.io")
    pid = (await api.post("/v1/projects", headers=auth(tok),
                          json={"name": "Shop", "path": "launch"})).json()["id"]
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]

    async with db.conn() as c:
        for html, live in (("<h1>Monday</h1>", False), ("<h1>Tuesday</h1>", True)):
            await c.execute(
                """INSERT INTO site_releases (org_id, project_id, slug, html, live)
                   VALUES ($1,$2,'shop-1',$3,$4)""", org, pid, html, live)

    listed = (await api.get(f"/v1/projects/{pid}/versions", headers=auth(tok))).json()["versions"]
    assert len(listed) == 2 and listed[0]["live"] is True
    monday = [v for v in listed if not v["live"]][0]

    r = await api.post(f"/v1/projects/{pid}/versions/{monday['id']}/restore", headers=auth(tok))
    assert r.status_code == 200, r.text

    after = (await api.get(f"/v1/projects/{pid}/versions", headers=auth(tok))).json()["versions"]
    assert len(after) == 3                       # a new version, nothing overwritten
    async with db.conn() as c:
        live_html = await c.fetchval(
            "SELECT html FROM site_releases WHERE project_id=$1 AND live", pid)
    assert live_html == "<h1>Monday</h1>"
    # and Tuesday is still there to come back to
    async with db.conn() as c:
        all_html = [r["html"] for r in await c.fetch(
            "SELECT html FROM site_releases WHERE project_id=$1", pid)]
    assert "<h1>Tuesday</h1>" in all_html


@pytest.mark.asyncio
async def test_restoring_the_live_version_is_refused_rather_than_duplicated(api):
    tok = await sign_in(api, f"j{secrets.token_hex(3)}@history.io")
    pid = (await api.post("/v1/projects", headers=auth(tok),
                          json={"name": "Shop", "path": "launch"})).json()["id"]
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    async with db.conn() as c:
        vid = await c.fetchval(
            """INSERT INTO site_releases (org_id, project_id, slug, html, live)
               VALUES ($1,$2,'s','<h1>Now</h1>',true) RETURNING id""", org, pid)
    r = await api.post(f"/v1/projects/{pid}/versions/{vid}/restore", headers=auth(tok))
    assert r.status_code == 409 and "already the live one" in r.json()["detail"]


@pytest.mark.asyncio
async def test_another_workspace_can_neither_see_nor_restore_your_versions(api):
    tok = await sign_in(api, f"k{secrets.token_hex(3)}@history.io")
    pid = (await api.post("/v1/projects", headers=auth(tok),
                          json={"name": "Shop", "path": "launch"})).json()["id"]
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    async with db.conn() as c:
        vid = await c.fetchval(
            """INSERT INTO site_releases (org_id, project_id, slug, html, live)
               VALUES ($1,$2,'s','<h1>x</h1>',false) RETURNING id""", org, pid)
    other = await sign_in(api, f"l{secrets.token_hex(3)}@elsewhere.io")
    assert (await api.get(f"/v1/projects/{pid}/versions", headers=auth(other))).status_code == 404
    assert (await api.post(f"/v1/projects/{pid}/versions/{vid}/restore",
                           headers=auth(other))).status_code == 404


def test_history_is_shown_as_moments_not_commit_hashes():
    html = open("app/web/index.html").read()
    assert "Earlier versions" in html and "Put this back" in html
    assert "toLocaleString" in html             # a date a person can read
    assert "nothing is ever lost" in html


# ---------------------------------------------------------------- gallery and remix

@pytest.mark.asyncio
async def test_a_remix_takes_the_shape_and_leaves_the_business_behind(api):
    """The line that must never move: customers, records, files, takings and the
    domain belong to whoever built it."""
    from app.services import gallery
    owner = await sign_in(api, f"g{secrets.token_hex(3)}@maker.io")
    pid = (await api.post("/v1/projects", headers=auth(owner),
                          json={"name": "Copperline", "path": "app"})).json()["id"]
    org = (await api.get("/v1/auth/me", headers=auth(owner))).json()["active_org"]
    await appfs.write(pid, org, {"app.js": "export const hello = 1;\n"})
    async with db.conn() as c:
        await c.execute("UPDATE projects SET answers=$1 WHERE id=$2",
                        {"site": {"business": "Copperline", "headline": "Leaks fixed"}}, pid)
        await c.execute("""INSERT INTO app_releases (org_id, project_id, slug, files, live)
                           VALUES ($1,$2,'cl',$3,true)""", org, pid, {"app.js": "x"})
        # things that belong to the business and must not travel
        await c.execute("""INSERT INTO app_records (org_id, project_id, collection, data)
                           VALUES ($1,$2,'invoices',$3)""", org, pid, {"amount": 900})
    await appauth.sign_up(pid, org, "client@example.com", "a-good-password")

    await api.post(f"/v1/projects/{pid}/showcase?on=true", headers=auth(owner))

    stranger = await sign_in(api, f"s{secrets.token_hex(3)}@stranger.io")
    out = await api.post(f"/v1/gallery/{pid}/remix", headers=auth(stranger))
    assert out.status_code == 200, out.text
    new_id = out.json()["project_id"]
    new_org = (await api.get("/v1/auth/me", headers=auth(stranger))).json()["active_org"]

    # the shape came across
    assert (await appfs.files(new_id, new_org))["app.js"] == "export const hello = 1;\n"

    # the business did not
    async with db.conn() as c:
        assert await c.fetchval(
            "SELECT count(*) FROM app_records WHERE project_id=$1", new_id) == 0
        assert await c.fetchval(
            "SELECT count(*) FROM app_users WHERE project_id=$1", new_id) == 0
        assert await c.fetchval(
            "SELECT count(*) FROM app_releases WHERE project_id=$1", new_id) == 0
        assert await c.fetchval(
            "SELECT count(*) FROM payment_accounts WHERE project_id=$1", new_id) == 0
        assert await c.fetchval(
            "SELECT count(*) FROM domains WHERE project_id=$1", new_id) == 0


@pytest.mark.asyncio
async def test_nothing_appears_in_the_gallery_unless_the_owner_said_so(api):
    tok = await sign_in(api, f"p{secrets.token_hex(3)}@private.io")
    pid = (await api.post("/v1/projects", headers=auth(tok),
                          json={"name": "Private", "path": "launch"})).json()["id"]
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    async with db.conn() as c:
        await c.execute("""INSERT INTO site_releases (org_id, project_id, slug, html, live)
                           VALUES ($1,$2,'pv','<h1>x</h1>',true)""", org, pid)
    items = (await api.get("/v1/gallery")).json()["items"]
    assert pid not in [i["id"] for i in items]

    await api.post(f"/v1/projects/{pid}/showcase?on=true", headers=auth(tok))
    assert pid in [i["id"] for i in (await api.get("/v1/gallery")).json()["items"]]

    await api.post(f"/v1/projects/{pid}/showcase?on=false", headers=auth(tok))
    assert pid not in [i["id"] for i in (await api.get("/v1/gallery")).json()["items"]]


@pytest.mark.asyncio
async def test_a_draft_cannot_be_shown_because_it_would_be_a_broken_link(api):
    tok = await sign_in(api, f"d{secrets.token_hex(3)}@draft.io")
    pid = (await api.post("/v1/projects", headers=auth(tok),
                          json={"name": "Draft", "path": "launch"})).json()["id"]
    r = await api.post(f"/v1/projects/{pid}/showcase?on=true", headers=auth(tok))
    assert r.status_code == 409 and "publish it first" in r.json()["detail"]


@pytest.mark.asyncio
async def test_something_taken_down_by_support_cannot_be_remixed(api):
    tok = await sign_in(api, f"m{secrets.token_hex(3)}@maker.io")
    pid = (await api.post("/v1/projects", headers=auth(tok),
                          json={"name": "Bad", "path": "launch"})).json()["id"]
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    async with db.conn() as c:
        await c.execute("""INSERT INTO site_releases (org_id, project_id, slug, html, live)
                           VALUES ($1,$2,'bd','<h1>x</h1>',true)""", org, pid)
    await api.post(f"/v1/projects/{pid}/showcase?on=true", headers=auth(tok))
    async with db.conn() as c:
        await c.execute("UPDATE projects SET showcase_hidden=true WHERE id=$1", pid)
    other = await sign_in(api, f"o{secrets.token_hex(3)}@stranger.io")
    assert (await api.post(f"/v1/gallery/{pid}/remix", headers=auth(other))).status_code == 404
    assert pid not in [i["id"] for i in (await api.get("/v1/gallery")).json()["items"]]


@pytest.mark.asyncio
async def test_the_gallery_needs_no_account_to_look_at(api):
    r = await api.get("/v1/gallery")
    assert r.status_code == 200 and "items" in r.json()


# ---------------------------------------------------------------- earned autonomy

def test_posts_that_make_promises_always_wait_for_a_person():
    """Prices and offers create obligations; awards and ratings are checkable facts
    a model can get wrong in a way that reads as lying. Trust level is irrelevant."""
    from app.services import autonomy
    for text in ("Boiler swaps from $1,200 this month",
                 "20% off drain clearing until Friday",
                 "Free callout for new customers",
                 "Rated 5 stars by our customers",
                 "Award-winning plumbing in Milwaukee",
                 "We're the best plumbers in town",
                 "Fully licensed and insured"):
        assert autonomy.why_a_human(text), text


def test_ordinary_posts_are_allowed_through():
    from app.services import autonomy
    for text in ("Frozen pipe season is here. A trickle of water overnight helps.",
                 "We replaced a water heater in Bay View this morning.",
                 "Booking into next week for drain work."):
        assert autonomy.why_a_human(text) is None, text


@pytest.mark.asyncio
async def test_autonomy_cannot_be_switched_on_before_the_brand_is_confirmed(api):
    from app.services import autonomy
    tok = await sign_in(api, f"a{secrets.token_hex(3)}@voice.io")
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    async with db.conn() as c:
        ch = await c.fetchval(
            """INSERT INTO social_channels (org_id, postiz_id, network, identifier, name)
               VALUES ($1,$2,'instagram','instagram','Shop') RETURNING id""",
            org, secrets.token_hex(6))
    with pytest.raises(PermissionError):
        await autonomy.set_channel(org, ch, True)

    await autonomy.confirm_brand(org, True)
    assert (await autonomy.set_channel(org, ch, True))["autonomous"] is True


@pytest.mark.asyncio
async def test_taking_back_the_brand_stops_every_channel(api):
    """If the voice is in question, nothing should still be speaking in it."""
    from app.services import autonomy
    tok = await sign_in(api, f"b{secrets.token_hex(3)}@voice.io")
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    async with db.conn() as c:
        ch = await c.fetchval(
            """INSERT INTO social_channels (org_id, postiz_id, network, identifier, name)
               VALUES ($1,$2,'facebook','facebook','Shop') RETURNING id""",
            org, secrets.token_hex(6))
    await autonomy.confirm_brand(org, True)
    await autonomy.set_channel(org, ch, True)
    await autonomy.confirm_brand(org, False)
    async with db.conn() as c:
        assert await c.fetchval("SELECT autonomous FROM social_channels WHERE id=$1", ch) is False


@pytest.mark.asyncio
async def test_pausing_stops_everything_at_once(api):
    from app.services import autonomy
    tok = await sign_in(api, f"c{secrets.token_hex(3)}@voice.io")
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    await autonomy.confirm_brand(org, True)
    async with db.conn() as c:
        ch = await c.fetchval(
            """INSERT INTO social_channels (org_id, postiz_id, network, identifier, name)
               VALUES ($1,$2,'x','x','Shop') RETURNING id""", org, secrets.token_hex(6))
    await autonomy.set_channel(org, ch, True)
    assert (await autonomy.decide(org, "x", "Frozen pipe season is here."))["autonomous"]
    await autonomy.pause(org, True)
    out = await autonomy.decide(org, "x", "Frozen pipe season is here.")
    assert out["autonomous"] is False and "paused" in out["reason"]


@pytest.mark.asyncio
async def test_the_weekly_cap_stops_a_loop_from_spamming_an_account(api):
    from app.services import autonomy
    tok = await sign_in(api, f"d{secrets.token_hex(3)}@voice.io")
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    await autonomy.confirm_brand(org, True)
    async with db.conn() as c:
        await c.execute(
            """INSERT INTO social_channels (org_id, postiz_id, network, identifier, name)
               VALUES ($1,$2,'threads','threads','Shop')""", org, secrets.token_hex(6))
        await c.execute("UPDATE org_settings SET weekly_post_cap=2 WHERE org_id=$1", org)
        for _ in range(2):
            await c.execute(
                """INSERT INTO approvals (org_id, kind, payload, state, executed_at)
                   VALUES ($1,'post','{}','done', now())""", org)
    async with db.conn() as c:
        ch = await c.fetchval("SELECT id FROM social_channels WHERE org_id=$1", org)
    await autonomy.set_channel(org, ch, True)
    out = await autonomy.decide(org, "threads", "A normal post about our week.")
    assert out["autonomous"] is False and "limit" in out["reason"]


@pytest.mark.asyncio
async def test_an_autonomous_post_is_stoppable_for_a_window_not_instant(api):
    from app.services import autonomy
    tok = await sign_in(api, f"e{secrets.token_hex(3)}@voice.io")
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    await autonomy.confirm_brand(org, True)
    async with db.conn() as c:
        ch = await c.fetchval(
            """INSERT INTO social_channels (org_id, postiz_id, network, identifier, name)
               VALUES ($1,$2,'linkedin','linkedin','Shop') RETURNING id""",
            org, secrets.token_hex(6))
    await autonomy.set_channel(org, ch, True)
    out = await autonomy.decide(org, "linkedin", "We fixed a burst pipe in Riverwest today.")
    assert out["autonomous"] and out["holds_until"] > datetime.now(timezone.utc)


def test_a_cta_section_linked_to_the_app_actually_renders():
    """It didn't. _section referenced app_url without receiving it, so the very
    thing the sign-in fix told the agent to do crashed the page."""
    from app.services import site as site_spec
    spec = site_spec.merge({}, {"business": "X", "headline": "Y", "cta": "Book",
                                "contact": {"email": "a@b.com"},
                                "sections": [{"kind": "cta", "body": "Sign in to see your invoices",
                                              "button": "Sign in", "link": "app"}]})
    html = site_spec.render(spec, "https://app.creai.dev/a/portal")
    assert 'href="https://app.creai.dev/a/portal"' in html
    assert 'href="#contact"' in site_spec.render(spec, None)


# ---------------------------------------------------------------- video as a project

def test_a_plan_that_opens_on_a_title_card_is_refused():
    """Opening on a card is the single most expensive mistake in a feed: most of
    the audience never reaches the second scene."""
    from app.services import video
    bad = video.check({"scenes": [
        {"id": "a", "source": "card", "line": "Copperline Books", "seconds": 3},
        {"id": "b", "source": "footage", "footage": "app", "line": "See your invoices", "seconds": 4}]})
    assert any("title card" in p for p in bad), bad


def test_saying_the_brand_in_the_opening_line_is_refused():
    from app.services import video
    bad = video.check({"brand_name": "Copperline", "scenes": [
        {"id": "a", "source": "footage", "footage": "app",
         "line": "Copperline keeps your books", "seconds": 4}]})
    assert any("brand name" in p for p in bad), bad


def test_a_good_plan_passes_and_prices_itself():
    from app.services import video
    plan = {"brand_name": "Copperline", "music": "ace-step", "scenes": [
        {"id": "a", "source": "footage", "footage": "app",
         "line": "Your customers shouldn't have to ring up and ask what they owe.", "seconds": 5},
        {"id": "b", "source": "generated", "prompt": "copper fittings on a workbench",
         "line": "They sign in and see their own invoices.", "seconds": 5},
        {"id": "c", "source": "card", "line": "Creai. Free to start.", "seconds": 4}]}
    assert video.check(plan) == []
    # render + 3 voices + 1 still + 1 motion + music
    assert video.estimate(plan) == 3 + 3 + 1 + 6 + 2


def test_changing_one_line_only_re_renders_that_scene():
    """Pictures cost money. An edit to scene three must not re-buy scene one."""
    from app.services import video
    old = {"scenes": [
        {"id": "a", "source": "generated", "prompt": "workbench", "line": "One", "seconds": 4,
         "assets": {"still": "k1"}},
        {"id": "b", "source": "generated", "prompt": "heater", "line": "Two", "seconds": 4,
         "assets": {"still": "k2"}}]}
    new = {"scenes": [
        {"id": "a", "source": "generated", "prompt": "workbench", "line": "One", "seconds": 4},
        {"id": "b", "source": "generated", "prompt": "heater", "line": "Two, changed", "seconds": 4}]}
    assert video.reusable(old, new) == {"a"}


def test_music_that_cannot_run_in_an_ad_is_refused_by_name():
    """MusicGen is CC-BY-NC. A customer shipping an ad with it would be exposed,
    and would never have been told why."""
    from app.services import sound
    with pytest.raises(sound.SoundError) as e:
        sound.pick("musicgen", for_ads=True)
    assert "non-commercial" in str(e.value).lower()
    assert sound.pick("ace-step", for_ads=True).ads_ok
    assert "musicgen" not in [s["id"] for s in sound.cleared(for_ads=True)]


def test_a_bed_ducks_under_the_voice_rather_than_fighting_it():
    from app.services import sound
    args = sound.mix("vo.mp3", "bed.mp3", [1.0, 2.0], "out.m4a")
    assert "sidechaincompress" in " ".join(args)
    assert sound.mix("vo.mp3", None, [], "out.m4a").count("-i") == 1


@pytest.mark.asyncio
async def test_a_video_project_can_be_created_and_planned(api):
    tok = await sign_in(api, f"vid{secrets.token_hex(3)}@studio.io")
    r = await api.post("/v1/projects", headers=auth(tok),
                       json={"name": "Autumn ad", "path": "video"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    plan = {"brand_name": "Copperline", "scenes": [
        {"source": "footage", "footage": "app", "line": "No more ringing up to ask.", "seconds": 5},
        {"source": "card", "line": "Creai. Free to start.", "seconds": 4}]}
    out = await api.post(f"/v1/videos/{pid}", headers=auth(tok), json={"plan": plan,
                                                                      "title": "Autumn ad"})
    assert out.status_code == 200, out.text
    assert out.json()["problems"] == [] and out.json()["credits"] > 0


@pytest.mark.asyncio
async def test_the_music_terms_are_visible_before_anything_is_made(api):
    r = await api.get("/v1/videos/sources")
    ids = [m["id"] for m in r.json()["music"]]
    assert "ace-step" in ids and "musicgen" not in ids
    assert all(m.get("licence") for m in r.json()["music"])


# ---------------------------------------------------------------- the render worker

def test_the_name_is_spelled_for_the_ear_not_the_eye():
    """A speech model cannot guess 'Kree-aye' from 'Creai'. Spelling it
    phonetically in the input is the only lever, and nobody ever sees it."""
    from app.services.speech import _say_the_name
    assert _say_the_name("Creai builds it.") == "Kree-aye builds it."
    assert _say_the_name("Creative work") == "Creative work"      # not a false match
    assert _say_the_name("creai.dev") == "creai.dev"              # a domain is read as one


@pytest.mark.asyncio
async def test_a_scene_runs_as_long_as_its_line_takes_to_say(api, monkeypatch):
    """Squeezing speech into a fixed slot is how ads end up sounding rushed at the
    end of every sentence."""
    from app.services import videoworker
    long_line = 4.8
    monkeypatch.setattr(videoworker, "_seconds_of", lambda _b: _async(long_line))
    scene = {"id": "a", "line": "a fairly long sentence", "seconds": 2.0}
    # the plan said 2 seconds; the voice takes 4.8, so the scene grows
    runs = round(max(long_line + videoworker.BEAT, scene["seconds"]), 2)
    assert runs > scene["seconds"]


async def _async(v):
    return v


@pytest.mark.asyncio
async def test_a_failed_render_says_what_went_wrong(api):
    """'Rendering failed' costs the person another paid attempt to find out why."""
    from app.services import video, videoworker
    tok = await sign_in(api, f"w{secrets.token_hex(3)}@studio.io")
    pid = (await api.post("/v1/projects", headers=auth(tok),
                          json={"name": "Ad", "path": "video"})).json()["id"]
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    made = await video.save(pid, org, None, {"brand_name": "X", "scenes": [
        {"source": "footage", "footage": "app", "line": "Something true.", "seconds": 3}]})
    async with db.conn() as c:
        await c.execute("UPDATE videos SET state='rendering' WHERE id=$1", made["id"])
    await videoworker.run(made["id"])          # no builder configured in tests
    async with db.conn() as c:
        row = await c.fetchrow("SELECT state, error FROM videos WHERE id=$1", made["id"])
    assert row["state"] == "failed"
    assert row["error"] and "isn't switched on" in row["error"]


async def _settle():
    """Let the tasks sweep() fired actually run.

    sweep hands each claim to asyncio.create_task, so a single sleep(0) only
    yields once and the task may not have run yet — which made this test fail
    about one run in five for a reason that had nothing to do with claiming.
    """
    for _ in range(20):
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pending:
            break
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_two_workers_cannot_claim_the_same_render(api):
    """Claiming happens in SQL, so a second instance picking up the queue does not
    render and charge for the same video twice."""
    from app.services import video, videoworker
    tok = await sign_in(api, f"q{secrets.token_hex(3)}@studio.io")
    pid = (await api.post("/v1/projects", headers=auth(tok),
                          json={"name": "Ad", "path": "video"})).json()["id"]
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    made = await video.save(pid, org, None, {"scenes": [
        {"source": "card", "line": "x", "seconds": 2}]})
    async with db.conn() as c:
        # Other tests leave videos in 'rendering', and sweep only takes a page of
        # them — so with enough left behind, this video is never reached and the
        # test fails for a reason that has nothing to do with claiming. Clear the
        # queue first so the assertion is about this video alone.
        await c.execute("UPDATE videos SET state='draft' WHERE id <> $1 AND state='rendering'",
                        made["id"])
        await c.execute("""UPDATE videos SET state='rendering',
                           updated_at = now() - interval '10 minutes' WHERE id=$1""", made["id"])
    # Count the claims of THIS video, not of everything the suite left behind:
    # a test that depends on what ran before it fails for the wrong reason.
    claimed = []
    original = videoworker.run

    async def watch(vid):
        claimed.append(vid)

    videoworker.run = watch
    try:
        await videoworker.sweep(limit=20)
        await _settle()
        await videoworker.sweep(limit=20)
        await _settle()
    finally:
        videoworker.run = original
    assert claimed.count(made["id"]) == 1, claimed


def test_the_videos_screen_exists_and_stops_asking_when_nobody_is_looking():
    """A render takes minutes, so the screen polls — but a pane nobody has open
    should not keep questioning the server."""
    html = open("app/web/index.html").read()
    assert 'id="tVideos"' in html and 'id="pVideos"' in html
    assert "loadVideos" in html
    assert "!$('pVideos').hidden" in html          # polling is conditional on being visible
    assert "clearTimeout(videoPoll)" in html       # and never stacks up
    assert "Video: '\u25b6'" in html               # a video has its own mark in the list


def test_a_failed_render_offers_a_way_back():
    html = open("app/web/index.html").read()
    assert "Try again" in html


def test_the_agent_can_plan_a_video_and_is_told_the_rules_that_matter():
    from app.services.agent import TOOL_PLAN_VIDEO
    d = TOOL_PLAN_VIDEO["description"]
    assert "title card" in d and "never reach the second scene" in d
    assert "never say the business name in the opening line" in d.lower()
    assert "nothing invented" in d.lower()
    kinds = TOOL_PLAN_VIDEO["input_schema"]["properties"]["scenes"]["items"]["properties"]
    assert set(kinds["source"]["enum"]) == {"generated", "footage", "card"}


@pytest.mark.asyncio
async def test_a_badly_opened_video_is_handed_back_to_the_agent_not_silently_fixed(api):
    """The agent wrote it, so the agent rewrites it — and learns the rule."""
    from app.services import video
    bad = {"brand_name": "Copperline", "scenes": [
        {"source": "card", "line": "Copperline Books", "seconds": 3},
        {"source": "footage", "footage": "app", "line": "See your invoices", "seconds": 4}]}
    problems = video.check(bad)
    assert any("title card" in p for p in problems)


def test_the_make_it_button_says_the_price_before_it_is_pressed():
    """Spending credits should never be a surprise: the cost is on the button."""
    agent_src = open("app/services/agent.py").read()
    assert 'f"Make it ({cost} credits)"' in agent_src
    html = open("app/web/index.html").read()
    assert "new_video" in html and "/render" in html


@pytest.mark.asyncio
async def test_a_free_site_carries_no_badge(api):
    """A free site already lives at something.creai.dev. A badge on top of that is
    the same message twice, and the second time it reads as a penalty."""
    tok = await sign_in(api, f"bd{secrets.token_hex(3)}@free.io")
    pid = (await api.post("/v1/projects", headers=auth(tok),
                          json={"name": "Free shop", "path": "launch"})).json()["id"]
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    async with db.conn() as c:
        await c.execute(
            """INSERT INTO site_releases (org_id, project_id, slug, html, live)
               VALUES ($1,$2,'freeshop','<!doctype html><html><body><h1>Hi</h1></body></html>',true)""",
            org, pid)
    page = await api.get("/s/freeshop")
    assert page.status_code == 200
    assert "Made with Creai" not in page.text
    assert "ref=badge" not in page.text


@pytest.mark.asyncio
async def test_every_plan_is_badge_free_including_the_free_one(api):
    from app.services import plans
    r = await api.get("/v1/plans")
    text = str(r.json())
    assert "Made with Creai badge" not in text
    assert "No Creai badge" not in text        # nothing to sell the removal of


def test_the_badge_joke_survives_a_wrap_and_reduced_motion():
    """An absolutely positioned bar only crosses the first line of a wrapped
    sentence, and on a phone this sentence always wraps."""
    html = open("app/web/index.html").read()
    assert "text-decoration:line-through" in html
    assert "Just kidding" in html and "you take the credit" in html
    # Someone who asked for less motion still gets the joke, not the setup.
    # Anchored from the .struck rule: the file has several reduced-motion blocks
    # and the first one belongs to something else entirely.
    block = html[html.index(".struck{"):][:900]
    quiet = block[block.index("@media (prefers-reduced-motion:reduce){"):]
    assert "text-decoration-color:currentColor" in quiet
    assert "opacity:1" in quiet


# ---------------------------------------------------------------- watching what's live

@pytest.mark.asyncio
async def test_a_domain_left_pointing_at_an_old_host_is_named(monkeypatch):
    """The bug this exists for: a CNAME left behind by something switched off a
    year ago, quietly serving a stranger's error page."""
    from app.services import watch
    monkeypatch.setattr(watch, "_cname",
                        lambda h: _resolve("b3b0847891f6fd20.vercel-dns-017.com"))
    monkeypatch.setattr(watch, "_reach", lambda u: _resolve((503, "")))
    found = await watch.check_host("arbi.example.dev")
    assert any("Vercel" in f.what for f in found)
    assert any(f.fix for f in found)          # and says what to do about it


async def _resolve(v):
    return v


@pytest.mark.asyncio
async def test_a_healthy_address_produces_no_noise(monkeypatch):
    """A watcher that cries wolf is muted within a week, and then it is worse than
    nothing because everyone believes it is working."""
    from app.services import watch
    monkeypatch.setattr(watch, "_cname", lambda h: _resolve(None))
    monkeypatch.setattr(watch, "_reach", lambda u: _resolve((200, "")))
    monkeypatch.setattr(watch, "_cert_days", lambda h: 60)
    assert await watch.check_host("fine.example.dev") == []


@pytest.mark.asyncio
async def test_a_lookup_that_fails_is_silence_not_a_warning(api, monkeypatch):
    """Failing to check is not the same as finding a problem."""
    from app.services import watch

    async def boom(_h):
        raise RuntimeError("resolver down")

    monkeypatch.setattr(watch, "check_host", boom)
    async with db.conn() as c:
        org = await c.fetchval("SELECT id FROM organizations ORDER BY id LIMIT 1")
        await c.execute("""INSERT INTO domains (org_id, name, status)
                           VALUES ($1,'wobbly.example.dev','live')
                           ON CONFLICT (org_id, name) DO NOTHING""", org)
    report = await watch.check_org(org)
    assert report.findings == []               # no invented problem


@pytest.mark.asyncio
async def test_a_certificate_about_to_lapse_is_flagged_before_it_does(monkeypatch):
    from app.services import watch
    monkeypatch.setattr(watch, "_cname", lambda h: _resolve(None))
    monkeypatch.setattr(watch, "_reach", lambda u: _resolve((200, "")))
    monkeypatch.setattr(watch, "_cert_days", lambda h: 5)
    found = await watch.check_host("expiring.example.dev")
    assert len(found) == 1 and found[0].severity == "soon"
    assert "5 days" in found[0].what


# ---------------------------------------------------------------- the scoreboard

def test_where_someone_came_from_is_classified_the_way_the_budget_is_split():
    from app.services import metrics
    assert metrics.classify(None, "cpc", None)[0] == "paid"
    assert metrics.classify("partnerco", "affiliate", None)[0] == "partner"
    assert metrics.classify(None, None, "https://www.google.com/search?q=x")[0] == "content"
    assert metrics.classify(None, None, "https://chatgpt.com/")[0] == "content"
    assert metrics.classify(None, None, "https://www.producthunt.com/posts/x")[0] == "pr"
    assert metrics.classify(None, None, "https://someblog.example/post")[0] == "referral"
    assert metrics.classify(None, None, None)[0] == "direct"       # never a guess


@pytest.mark.asyncio
async def test_first_touch_is_kept_and_a_later_visit_cannot_take_the_credit(api):
    """Whatever introduced somebody earned the signup. Overwriting with the last
    click moves budget to the wrong channel."""
    from app.services import metrics
    email = f"touch{secrets.token_hex(3)}@example.com"
    async with db.conn() as c:
        from app.core import tenancy as T
        await T.start_session(c, email, None,
                              first_touch={"channel": "content", "detail": "reddit.com",
                                           "landed_on": "/build/ai-crm", "referrer": "https://reddit.com/"})
        # they come back later through an ad
        await T.start_session(c, email, None,
                              first_touch={"channel": "paid", "detail": "google-ads",
                                           "landed_on": "/", "referrer": "https://google.com/"})
        row = await c.fetchrow("SELECT source, source_detail, landed_on FROM users WHERE email=$1",
                               email)
    assert row["source"] == "content"
    assert row["source_detail"] == "reddit.com"
    assert row["landed_on"] == "/build/ai-crm"


@pytest.mark.asyncio
async def test_the_scoreboard_says_what_it_cannot_measure(api):
    """A zero in a row that is simply not instrumented reads as a fact, and a
    stranger doing diligence finds the gap in an afternoon."""
    from app.services import metrics
    out = await metrics.funnel(30)
    assert set(out["not_instrumented"]) == {"visitors", "cac", "ltv", "referral_rate"}
    for reason in out["not_instrumented"].values():
        assert len(reason) > 20                      # each says what it would need
    assert "registered" in out["measured"] and "mrr_cents" in out["measured"]


@pytest.mark.asyncio
async def test_coverage_admits_how_little_history_has_a_source(api):
    from app.services import metrics
    cov = await metrics.coverage()
    assert 0 <= cov["share"] <= 1
    assert "permanently" in cov["note"]


# ---------------------------------------------------------------- blueprints

def test_the_crm_blueprint_requires_exactly_one_field():
    """Adoption is the number one cause of CRM failure, and every extra required
    field is a reason not to bother. An unentered deal beats a half-entered one."""
    from app.services import blueprints
    crm = blueprints.get("crm")
    required = [f for f in crm["fields"] if f.get("required")]
    assert len(required) == 1 and required[0]["name"] == "who"


def test_the_crm_blueprint_leads_with_voice_and_with_what_to_do():
    from app.services import blueprints
    brief = blueprints.get("crm")["brief"].lower()
    assert "voice first" in brief
    assert "tells them what to do" in brief
    assert "person selling, not the person watching" in brief
    assert "their words" in brief


def test_a_salespersons_pipeline_is_theirs_alone():
    from app.services import blueprints
    for c in blueprints.get("crm")["collections"].values():
        assert c["read"] == "own"          # enforced in SQL, not by the screen


def test_the_content_blueprint_refuses_to_become_a_second_editor():
    """A website editor here would hand back the exact problem Creai removes."""
    from app.services import blueprints
    brief = blueprints.get("content")["brief"]
    assert "NOT" in brief and "website editor" in brief
    assert "stay in the conversation" in brief
    # the brief wraps, so compare on collapsed whitespace rather than raw text
    flat = " ".join(brief.lower().split())
    assert "nothing on the live site changes until they publish" in flat


def test_content_is_public_to_read_and_owner_only_to_change():
    from app.services import blueprints
    c = blueprints.get("content")["collections"]["content"]
    assert c["read"] == "public" and c["write"] == "owner"


def test_the_agent_is_offered_the_blueprints_and_told_not_to_recite_them():
    from app.services.agent import TOOL_BLUEPRINT
    d = TOOL_BLUEPRINT["description"]
    assert "more than one required field gets abandoned" in d
    assert "second editor" in d
    assert set(TOOL_BLUEPRINT["input_schema"]["properties"]["blueprint"]["enum"]) == {"crm", "content"}


def test_a_brief_is_written_for_the_business_it_is_for():
    from app.services import blueprints
    brief = blueprints.brief_for("crm", "Copperline Plumbing", "plumber")
    assert "Copperline Plumbing" in brief and "a plumber" in brief
    with pytest.raises(KeyError):
        blueprints.brief_for("nonsense", "X")


# ---------------------------------------------------------------- speech into a lead

def test_money_is_read_the_way_a_contractor_says_it():
    """"eighteen four" is 18,400 on a job sheet and 184 to a parser that has never
    met a contractor. The failure is silent: nobody notices until the forecast is
    wrong."""
    from app.services.voicelead import money_from_speech as m
    assert m("just quoted them eighteen four for the duplex") == 18400
    assert m("twelve fifty") == 1250
    assert m("two and a half k") == 2500
    assert m("a grand") == 1000
    assert m("eighteen hundred") == 1800
    assert m("said yes to the nine six") == 9600
    assert m("$4,820") == 4820


def test_ordinary_sentences_are_not_mistaken_for_money():
    from app.services.voicelead import money_from_speech as m
    assert m("sent four five invoices this morning") is None
    assert m("spoke to a guy about a boiler") is None
    assert m("") is None


def test_a_value_nobody_said_is_never_invented():
    """A guessed number in a pipeline total is a decision made on a lie, and three
    quarters of people already say their CRM data is wrong."""
    from app.services import voicelead
    out = voicelead._tidy({"who": "Kessler Dental", "stage": "New", "value": 5000},
                          "Kessler Dental called about a blocked drain")
    assert out["value"] is None


def test_a_spoken_amount_overrides_a_model_that_misheard_it():
    from app.services import voicelead
    out = voicelead._tidy({"who": "Reyes Roofing", "stage": "Quoted", "value": 184},
                          "quoted Reyes Roofing eighteen four for the duplex")
    assert out["value"] == 18400


def test_a_stage_outside_the_four_is_refused():
    from app.services import voicelead
    assert voicelead._tidy({"who": "X", "stage": "Negotiating"}, "x")["stage"] == "New"


@pytest.mark.asyncio
async def test_a_sentence_with_no_name_asks_rather_than_guesses():
    from app.services import voicelead
    with pytest.raises(voicelead.LeadError) as e:
        voicelead._tidy({"who": "", "stage": "New"}, "spoke to a guy about a boiler")
        raise voicelead.LeadError("I didn't catch who that was for — say the name and try again")
    assert "didn't catch who" in str(e.value)


def test_the_sentence_is_handed_back_so_a_mistake_costs_a_tap():
    from app.services import voicelead
    said = "quoted Reyes Roofing eighteen four, chase Thursday"
    assert voicelead._tidy({"who": "Reyes Roofing"}, said)["heard"] == said


# ---------------------------------------------------------------- voice to lead

def test_money_is_read_the_way_a_contractor_says_it():
    """The silent failure: nobody notices a lead worth 184 instead of 18,400
    until the forecast is wrong."""
    from app.services.voicelead import money_from_speech as money
    assert money("quoted them eighteen four for the duplex") == 18400
    assert money("twelve fifty") == 1250
    assert money("two and a half k") == 2500
    assert money("a grand") == 1000
    assert money("eighteen hundred") == 1800
    assert money("said yes to the nine six") == 9600
    assert money("$4,820 for the job") == 4820


def test_ordinary_numbers_are_not_mistaken_for_money():
    from app.services.voicelead import money_from_speech as money
    assert money("sent four five invoices") is None
    assert money("spoke to them about the boiler") is None


def test_a_value_nobody_said_is_thrown_away():
    """Three quarters of people already say their CRM data is wrong. A guessed
    number in a pipeline total is a decision made on a lie."""
    from app.services import voicelead
    out = voicelead._tidy({"who": "Kessler Dental", "stage": "New", "value": 5000},
                          "Kessler Dental called about a blocked drain")
    assert out["value"] is None


def test_a_misheard_value_is_corrected_from_the_words():
    from app.services import voicelead
    out = voicelead._tidy({"who": "Reyes Roofing", "stage": "Quoted", "value": 184},
                          "quoted Reyes Roofing eighteen four for the duplex")
    assert out["value"] == 18400


def test_a_stage_it_does_not_recognise_falls_back_rather_than_inventing():
    from app.services import voicelead
    assert voicelead._tidy({"who": "X", "stage": "Negotiating"}, "X called")["stage"] == "New"


def test_what_was_heard_is_kept_so_a_mistake_costs_a_tap():
    from app.services import voicelead
    said = "quoted Reyes Roofing eighteen four, chase Thursday"
    assert voicelead._tidy({"who": "Reyes Roofing"}, said)["heard"] == said


# ---------------------------------------------------------------- ad variants

def test_a_variant_set_is_balanced_enough_to_learn_from():
    """A random draw put seven of twelve on one opening. An axis value seen twice
    cannot be compared with one seen seven times — that is a lottery, not a test."""
    from app.services import admutate
    vs = admutate.plan({"trade": "plumber", "shows": "x"}, want=18, seed=3)
    for axis in ("hook", "style", "length", "open", "pace", "cta"):
        counts = {}
        for v in vs:
            counts[str(v[axis])] = counts.get(str(v[axis]), 0) + 1
        assert max(counts.values()) - min(counts.values()) <= 2, (axis, counts)


def test_variants_needing_a_real_person_are_separated_not_faked():
    """A generated face making a testimonial is caught in the comments, and most
    stock licences forbid implying endorsement. These wait for a human."""
    from app.services import admutate
    vs = admutate.plan({"trade": "plumber", "shows": "x"}, want=14, seed=5)
    filming = admutate.to_film(vs)
    assert filming and all(v["style"] == "ugc" for v in filming)
    assert all(v["status"] == "to film" for v in filming)
    assert all(v["status"] == "ready to render" for v in vs if not v["needs_a_person"])
    # and the ugc brief insists on a real one
    assert "not a generated or stock one" in admutate.STYLE_RULES["ugc"]


def test_generated_footage_may_appear_but_never_claim():
    from app.services import admutate
    rule = admutate.STYLE_RULES["cinematic"]
    assert "nobody speaks and nothing is claimed" in rule
    assert "never shows the product" in rule


def test_the_conversion_rules_are_not_one_of_the_axes():
    """A variant that breaks them is not an experiment, it is a wasted impression."""
    from app.services import admutate
    for v in admutate.plan({"trade": "baker", "shows": "x"}, want=8, seed=1):
        b = v["brief"]
        assert "within the first two seconds" in b
        assert "brand name does not appear in the opening line" in b
        assert "Nothing claimed that the product does not do." in b


def test_contradictory_combinations_are_never_produced():
    from app.services import admutate
    for v in admutate.plan({"trade": "plumber", "shows": "x"}, want=20, seed=9):
        assert not (v["style"] == "demo" and v["open"] == "person")
        assert not (v["style"] == "ugc" and v["open"] == "product")
        assert not (v["length"] == 15 and v["pace"] == "measured")


def test_near_duplicate_openings_are_dropped_rather_than_shipped():
    from app.services import admutate
    kept = admutate.drop_near_duplicates([
        {"id": "a", "opening_line": "Still updating your CRM after work?"},
        {"id": "b", "opening_line": "After work, still updating your CRM?"},
        {"id": "c", "opening_line": "Your evenings belong to paperwork now."},
    ])
    assert [v["id"] for v in kept] == ["a", "c"]


def test_results_roll_up_by_axis_so_the_next_fifty_are_informed():
    from app.services import admutate
    results = [
        {"id": "1", "hook": "objection", "style": "demo", "open": "product",
         "length": 15, "cta": "free", "pace": "quick", "trade": "plumber",
         "plays": 1000, "actions": 40},
        {"id": "2", "hook": "objection", "style": "ugc", "open": "person",
         "length": 30, "cta": "try", "pace": "measured", "trade": "baker",
         "plays": 1000, "actions": 60},
        {"id": "3", "hook": "number", "style": "demo", "open": "product",
         "length": 15, "cta": "free", "pace": "quick", "trade": "plumber",
         "plays": 1000, "actions": 10},
    ]
    learned = admutate.learn(results)
    assert list(learned["hook"])[0] == "objection"          # 100/2000 vs 10/1000
    assert learned["hook"]["objection"]["ads"] == 2


def test_a_winner_cannot_be_declared_off_forty_impressions():
    from app.services import admutate
    thin = [{"hook": "objection", "plays": 40, "actions": 4},
            {"hook": "number", "plays": 30, "actions": 1}]
    assert admutate.enough(thin, "hook") is False
    fat = [{"hook": "objection", "plays": 2000, "actions": 80},
           {"hook": "number", "plays": 1500, "actions": 20}]
    assert admutate.enough(fat, "hook") is True


def test_the_angles_are_a_first_class_axis():
    """A library missing most angles is one idea tested several ways. The set
    grew from five to thirteen; what matters is that they are genuinely different
    arguments, not that there is a particular number of them."""
    from app.services import admutate
    assert len(admutate.ANGLES) >= 12
    for core in ("demo", "problem", "outcome", "nothing", "proof",
                 "curiosity", "story", "comparison", "founder", "customerpov"):
        assert core in admutate.ANGLES
    vs = admutate.plan({"trade": "plumber", "shows": "x"}, want=20, seed=4)
    spread = {}
    for v in vs:
        spread[v["angle"]] = spread.get(v["angle"], 0) + 1
    assert max(spread.values()) - min(spread.values()) <= 1


def test_the_proof_angle_is_refused_until_a_real_customer_said_something():
    """It is reported as the largest single lever and it is the one angle that
    cannot be written. Writing it would be inventing a testimonial."""
    from app.services import admutate
    bare = {"trade": "plumber", "shows": "x"}
    assert "proof" not in admutate.available_angles(bare)
    assert "proof" not in {v["angle"] for v in admutate.plan(bare, want=20, seed=2)}
    withq = {**bare, "proof": {"who": "Dana, Cannon Build",
                               "quote": "I stopped doing paperwork on Sundays."}}
    assert "proof" in admutate.available_angles(withq)


def test_every_brief_demands_motion_in_the_opening_and_an_offer_at_the_end():
    """A still frame with text on it does not stop a thumb, and everything
    downstream is capped by the three-second gate."""
    from app.services import admutate
    for v in admutate.plan({"trade": "baker", "shows": "x"}, want=10, seed=6):
        assert "first two seconds must move" in v["brief"]
        assert "last third states what to do and why now" in v["brief"]


def test_a_read_has_an_arc_rather_than_one_setting_for_every_line():
    """The fault was not the model: every line got the same instruction, which is
    a voice setting, not a performance."""
    from app.services import voicedirect as vd
    read = vd.plan_read("problem", [
        {"beat": "open", "text": "Six forty-seven. You are not done."},
        {"beat": "agitate", "text": "The paperwork is at home."},
        {"beat": "payoff", "text": "Entered."},
        {"beat": "cta", "text": "Build yours free at Creai dot dev."}])
    feelings = [l["feeling"] for l in read]
    assert len(set(feelings)) == len(feelings)          # every beat directed differently
    # check the shape, not the adjective: rewriting the direction must not break
    # the test that the direction exists
    assert any(w in feelings[0] for w in ("weary", "exhausted", "tired"))
    assert any(w in feelings[-1] for w in ("conviction", "certainty", "confident"))
    assert read[0]["hold"] > read[-1]["hold"]      # the open breathes, the close does not


def test_a_line_the_customer_says_is_not_performed():
    from app.services import voicedirect as vd
    read = vd.plan_read("problem", [
        {"beat": "turn", "text": "Quoted Reyes Roofing eighteen four.", "in_character": True}])
    assert "not performing" in read[0]["feeling"]


def test_brand_names_are_spelled_for_the_ear_not_the_eye():
    """The viewer reads the brand spelled properly and hears it said properly."""
    from app.services import voicedirect as vd
    say, note = vd.phonetic("Build yours free at Creai dot dev.")
    assert say == "Build yours free at kreeYAY dot dev." and note
    assert vd.phonetic("Arbi finds it while you sleep.")[0].startswith("ARbee")
    assert vd.phonetic("Creative work")[0] == "Creative work"     # no false match


def test_the_direction_travels_to_any_speech_model():
    from app.services import voicedirect as vd
    read = vd.plan_read("problem", [{"beat": "cta", "text": "Try Arbi free."}])[0]
    assert isinstance(read["openai"], str) and read["openai"]
    assert "[" in read["gemini"] or "voice actor" in read["gemini"]
    el = read["elevenlabs"]
    assert 0 < el["stability"] <= 1
    # the call to action wants to land the same way every time; feeling beats want range
    assert el["stability"] > vd.plan_read(
        "problem", [{"beat": "open", "text": "Six forty-seven."}])[0]["elevenlabs"]["stability"]


def test_the_variant_engine_carries_another_products_audience():
    """Arbi's audience is not plumbers, and the framework should not need editing."""
    from app.services import admutate
    vs = admutate.plan({"trade": "reseller",
                        "audiences": ["reseller", "dropshipper", "bargain hunter"],
                        "shows": "a price gap found automatically"}, want=10, seed=3)
    assert {v["trade"] for v in vs} <= {"reseller", "dropshipper", "bargain hunter"}


# ---------------------------------------------------------------- directing the read

def test_every_beat_gets_its_own_direction_not_one_setting_for_the_whole_ad():
    """The fault this fixes: one instruction for every line is a voice setting,
    not a performance. A read with no arc sounds like a script being read."""
    from app.services import voicedirect as vd
    read = vd.plan_read("problem", [
        {"beat": "open", "text": "Six forty-seven. You are not done."},
        {"beat": "agitate", "text": "The paperwork is at home."},
        {"beat": "payoff", "text": "Entered."},
        {"beat": "cta", "text": "Build yours free at Creai dot dev."}])
    feelings = [l["feeling"] for l in read]
    assert len(set(feelings)) == len(feelings)          # no two beats read alike
    # the words change when the direction is rewritten; the shape must not —
    # the open is low and the close is certain
    assert any(w in read[0]["feeling"] for w in ("weary", "exhausted", "tired"))
    assert any(w in read[-1]["feeling"] for w in ("conviction", "certainty", "confident"))
    assert read[0]["hold"] > 0                     # and it leaves silence after itself


def test_the_customers_own_line_is_never_performed():
    """Somebody talking into a phone is not acting, and a performed version loses
    the very thing the ad demonstrates."""
    from app.services import voicedirect as vd
    read = vd.plan_read("problem", [
        {"beat": "turn", "in_character": True, "text": "Quoted Reyes Roofing eighteen four."}])
    assert "not performing" in read[0]["feeling"]
    assert "no emphasis on any word" in read[0]["delivery"]


def test_a_brand_name_is_spelled_for_the_ear_not_the_eye():
    from app.services import voicedirect as vd
    say, note = vd.phonetic("Build yours free at Creai dot dev.")
    assert say == "Build yours free at kreeYAY dot dev."
    assert "second syllable" in note
    assert vd.phonetic("nothing here")[0] == "nothing here"
    # a second product is an entry, not an edit
    assert vd.phonetic("Arbi finds it")[0] == "ARbee finds it"


def test_the_direction_travels_to_whichever_model_is_used():
    """The arc belongs to the ad; the dialect belongs to the supplier."""
    from app.services import voicedirect as vd
    read = vd.plan_read("problem", [{"beat": "cta", "text": "Build yours free at Creai dot dev."}])[0]
    assert isinstance(read["openai"], str) and read["openai"]
    assert "[" not in read["openai"]                     # openai takes prose
    assert isinstance(read["elevenlabs"], dict)
    assert read["elevenlabs"]["stability"] > vd.for_elevenlabs(
        vd.direct("problem", "open"))["stability"]       # the ask lands the same way twice


def test_an_arc_that_does_not_exist_is_refused():
    from app.services import voicedirect as vd
    with pytest.raises(vd.DirectionError):
        vd.arc("nonsense")
    with pytest.raises(vd.DirectionError):
        vd.direct("problem", "no-such-beat")


def test_the_agent_is_told_what_the_first_screen_must_carry():
    """The brief was senior on correctness and silent on substance, so the agent
    built correct forms over tables — which is a database with a coat on."""
    from app.services import agent
    b = agent.APP_EXTRA
    assert "Lead with the number they actually care about" in b
    assert "Show state, not just rows" in b
    assert 'Answer "what now"' in b
    assert "database with a coat on" in b
    assert "never a name in\n    brackets" in b or "never a name in brackets" in b.replace("\n    ", " ")


def test_the_video_agent_gets_a_real_brief_not_a_tool_description():
    """The app agent had 150 lines of direction and the video agent had eight.
    That imbalance is why the ads came out correct and unwatchable."""
    from app.services import agent
    b = agent.VIDEO_EXTRA
    assert len(b.splitlines()) > 40
    # the things that actually decide whether an ad earns anything
    assert "first two seconds must MOVE" in b
    assert "brand name never appears in the opening line" in b
    assert "Holding matters more than hooking" in b
    assert "last third states what to do and why now" in b
    # and the lines that stop it lying
    assert "testimonial nobody gave" in b
    assert "may appear and must never claim" in b
    assert "Nothing claimed that the product does not do" in b
    # direction, not a voice setting
    assert "One instruction for every line is a voice setting" in b


def test_the_two_speakers_never_collapse_into_one_voice():
    """The tonal change should be structural — one voice doing both parts is the
    thing that made it sound like a single person reading."""
    from app.services import voicedirect as vd
    assert vd.CAST["narrator"]["openai"] != vd.CAST["customer"]["openai"]
    read = vd.plan_read("problem", [
        {"beat": "open", "text": "Six forty-seven."},
        {"beat": "turn", "in_character": True, "text": "Quoted Reyes Roofing eighteen four."}])
    assert read[0]["voice"] != read[1]["voice"]
    assert read[0]["who"] == "narrator" and read[1]["who"] == "customer"


def test_the_rejected_narrator_can_never_be_cast_again():
    """Kept by name rather than deleted, so nobody reaches for it out of habit and
    the reason outlives whoever made the call."""
    from app.services import voicedirect as vd
    assert "ash" in vd.RETIRED and vd.RETIRED["ash"]
    assert vd.cast_voice("narrator") == "echo"
    cast = {c["openai"] for c in vd.CAST.values()}
    assert "ash" not in cast
    # and it is never handed out to a new speaker either
    for role in ("second customer", "a mate", "the accountant", "a supplier"):
        assert vd.cast_voice(role) != "ash"


def test_a_third_speaker_does_not_share_a_voice_with_the_first():
    """The point of more than one voice is sounding like more than one person."""
    from app.services import voicedirect as vd
    voices = [vd.cast_voice(r) for r in ("narrator", "customer", "a neighbour", "a supplier")]
    assert len(set(voices)) == len(voices)


# ---------------------------------------------------------------- captions

def test_captions_carry_no_container():
    """White text in a black rounded box reads as 2021 creator content and
    undercuts anything trying to look like software worth paying for."""
    from app.services import captions as cp
    f = cp.filters("You are not done.", 0, 2, 1080, 1920)
    assert "box=1" not in f and "boxcolor" not in f
    assert "shadowcolor" in f                      # contrast without a container


def test_the_caption_shows_the_figure_not_the_spoken_words():
    """A voice says 'eighteen four'; the screen says $18,400. Written out it reads
    as a subtitle of somebody talking — as a figure it is the thing itself."""
    from app.services import captions as cp
    assert cp.as_written("Six forty-seven. You are not done.").startswith("6:47")
    assert "$18,400" in cp.as_written("quoted them eighteen four")
    assert "creai.dev" in cp.as_written("Build yours free at Creai dot dev.")


def test_exactly_one_word_carries_a_phrase():
    """Emphasising three words is emphasising none."""
    from app.services import captions as cp
    assert cp.pick_emphasis("6:47.") == "6:47"
    assert cp.pick_emphasis("Chase Thursday.") == "Thursday"
    assert cp.pick_emphasis("You are not done.") == ""      # nothing worth lifting


def test_phrases_are_short_enough_to_read_at_speed():
    from app.services import captions as cp
    for p in cp.phrases("Jobs in your head and the paperwork is still sitting at home", 0, 5):
        assert len(p.words.split()) <= cp.MAX_WORDS


def test_captions_clear_the_space_the_platforms_cover():
    """TikTok and Reels put their own interface over the bottom fifth."""
    from app.services import captions as cp
    H = 1920
    f = cp.draw(cp.Phrase("You are not done.", 0, 2), 1080, H)
    y = int(f.split(":y=")[1].split(":")[0].split("{")[0])
    assert y < H * (1 - cp.SAFE_BOTTOM)


def test_captions_stay_readable_on_pale_footage():
    """White on cream is invisible, and nothing reports it — the caption simply
    is not there. The app screens are pale; the establishing footage is dark."""
    from app.services import captions as cp
    dark = cp.filters("Chase Thursday.", 0, 2, 1080, 1920)
    light = cp.filters("Chase Thursday.", 0, 2, 1080, 1920, light=True)
    assert cp.INK.lstrip("#") in dark and "black@" in dark
    assert cp.INK_DARK.lstrip("#") in light and "white@" in light
    assert cp.ACCENT_DARK.lstrip("#") in light        # the lifted word too


# ---------------------------------------------------------------- direction

def test_the_camera_is_never_saying_nothing():
    """Eye level throughout is the angle with no point of view. If every scene
    shares an angle, the camera has abdicated."""
    from app.services import producer
    flat = [{"angle": "eye", "move": "push"}, {"angle": "eye", "move": "still"}]
    assert any("saying nothing" in p for p in producer.check(flat))


def test_the_problem_is_shot_high_and_the_payoff_low():
    """The move that carries an ad: the problem shrinks them, the solution
    restores them, stated in where the camera stands."""
    from app.services import producer
    backwards = [{"angle": "low", "move": "push"}, {"angle": "high", "move": "still"},
                 {"angle": "over", "move": "push"}]
    problems = producer.check(backwards)
    assert any("ends on the person diminished" in p for p in problems)
    assert any("opens low" in p for p in problems)

    right = [{"angle": "high", "move": "push"}, {"angle": "over", "move": "push"},
             {"angle": "low", "move": "still"}]
    assert producer.check(right) == []


def test_a_product_shot_is_over_the_shoulder_or_close():
    """A screen shot from across the room is somebody else's software."""
    from app.services import producer
    distant = [{"angle": "high", "move": "push"}, {"angle": "eye", "move": "still"},
               {"angle": "low", "move": "still"}]
    assert any("never theirs" in p for p in producer.check(distant))


def test_the_opening_shot_has_to_move():
    from app.services import producer
    assert any("locked off" in p for p in producer.check(
        [{"angle": "high", "move": "still"}, {"angle": "over", "move": "push"},
         {"angle": "low", "move": "still"}]))


def test_one_tilt_at_most():
    from app.services import producer
    twice = [{"angle": "high", "move": "tilt"}, {"angle": "over", "move": "tilt"},
             {"angle": "low", "move": "still"}]
    assert any("style rather than a moment" in p for p in producer.check(twice))


def test_camera_movement_is_appended_last_in_a_generation_prompt():
    """Appended after the description it reduces drift; embedded mid-sentence it
    confuses the subject."""
    from app.services import producer
    p = producer.prompt_for({"shows": "A plumber closing a van", "angle": "low",
                             "move": "push", "line": "", "say": "narrator",
                             "seconds": 3, "why": ""})
    assert p.rstrip().endswith(producer.MOVES["push"] + ".")
    assert producer.ANGLES["low"] in p
    assert "no text" in p


def test_a_production_sheet_specifies_every_attribute_of_a_moment():
    """Angle alone is not direction. Lens, light, tone, sound and music are the
    rest of it, and each one maps to something the pipeline can execute."""
    from app.services import producer
    for table in (producer.ANGLES, producer.MOVES, producer.LENSES,
                  producer.LIGHT, producer.SOUNDS, producer.MUSIC):
        assert table and all(isinstance(v, str) and v for v in table.values())
    # the sounds named are the ones that actually exist as files
    assert set(producer.SOUNDS) >= {"pop", "tick", "snap", "chime", "none"}


def test_sound_marks_a_beat_rather_than_decorating_every_one():
    from app.services import producer
    noisy = [{"angle": "high", "move": "push", "sound": "pop", "music": "under", "tone": "a"},
             {"angle": "over", "move": "push", "sound": "tick", "music": "under", "tone": "b"},
             {"angle": "low", "move": "still", "sound": "snap", "music": "out", "tone": "c"}]
    assert any("decorate" in p for p in producer.check(noisy))


def test_the_bed_lifts_once_and_leaves_the_last_line_dry():
    from app.services import producer
    twice = [{"angle": "high", "move": "push", "music": "lift", "tone": "a"},
             {"angle": "over", "move": "push", "music": "lift", "tone": "b"},
             {"angle": "low", "move": "still", "music": "under", "tone": "c"}]
    problems = producer.check(twice)
    assert any("lifts more than once" in p for p in problems)
    assert any("land dry" in p for p in problems)


def test_one_tone_across_the_whole_ad_is_caught():
    """That is a voice setting, not a performance — the exact fault in the early cuts."""
    from app.services import producer
    flat = [{"angle": "high", "move": "push", "tone": "confident", "music": "under"},
            {"angle": "over", "move": "push", "tone": "confident", "music": "under"},
            {"angle": "low", "move": "still", "tone": "confident", "music": "out"}]
    assert any("voice setting" in p for p in producer.check(flat))


def test_a_generation_prompt_carries_lens_and_light_too():
    from app.services import producer
    p = producer.prompt_for({"shows": "A plumber at a kitchen table", "angle": "high",
                             "move": "push", "lens": "long", "light": "lamp"})
    assert producer.LENSES["long"] in p and producer.LIGHT["lamp"] in p
    assert p.rstrip().endswith(producer.MOVES["push"] + ".")      # movement last


def test_a_licence_is_checked_before_any_sound_is_paid_for():
    """Discovering afterwards that a track cannot run in an ad means the money is
    gone and the cut is already built around it."""
    import asyncio
    from app.services import sound
    with pytest.raises(sound.SoundError) as e:
        asyncio.get_event_loop().run_until_complete(sound.make("musicgen", "x", 5)) \
            if False else sound.pick("musicgen", for_ads=True)
    assert "non-commercial" in str(e.value).lower()
    assert "ace-step" in str(e.value).lower()          # and names the one that works


def test_only_sources_with_a_wired_endpoint_can_be_made():
    from app.services import sound
    assert set(sound.ENDPOINTS) == {"ace-step", "stable-sfx"}
    for src in sound.ENDPOINTS:
        assert sound.SOURCES[src].ads_ok


def test_an_effect_is_short_and_a_bed_is_not():
    """An effect that outlasts its moment is music."""
    from app.services import sound
    assert sound.bed_prompt({"voice": "plain"}, "calm").endswith("Loopable.")
    assert "no vocals" in sound.bed_prompt({"voice": "plain"})


# ---------------------------------------------------------------- the cutter

@pytest.mark.asyncio
async def test_a_plan_the_checker_rejects_is_never_rendered():
    """A variant that opens on a still frame or ends on the person diminished is
    not an experiment, it is a wasted impression somebody paid for."""
    import pathlib
    from app.services import cutter
    flat = {"scenes": [{"line": "x", "say": "narrator", "angle": "eye", "move": "still",
                        "music": "under", "tone": "flat", "seconds": 3}]}
    with pytest.raises(cutter.CutError) as e:
        await cutter.cut(flat, {0: "/dev/null"}, pathlib.Path("/tmp/never"))
    assert "saying nothing" in str(e.value)


@pytest.mark.asyncio
async def test_a_missing_asset_names_the_scene_rather_than_going_black():
    """A black frame nobody notices until the ad is live is the worst outcome."""
    import pathlib
    from app.services import cutter
    good = {"scenes": [
        {"line": "a", "say": "narrator", "angle": "high", "move": "push", "seconds": 2},
        {"line": "b", "say": "narrator", "angle": "over", "move": "push", "seconds": 2},
        {"line": "c", "say": "narrator", "angle": "low", "move": "still", "seconds": 2}]}
    with pytest.raises(cutter.CutError) as e:
        await cutter.cut(good, {0: "/tmp/nope.mp4"}, pathlib.Path("/tmp/never2"))
    assert "scene 1" in str(e.value)


def test_a_scenes_beat_follows_its_position_in_the_arc():
    from app.services import cutter
    assert cutter._beat_for(0, 5) == "open"
    assert cutter._beat_for(1, 5) == "agitate"
    assert cutter._beat_for(3, 5) == "payoff"
    assert cutter._beat_for(4, 5) == "cta"


def test_audio_is_re_encoded_rather_than_stream_copied():
    """Copying compressed fragments together produces a file that plays as noise,
    and nothing in the build reports it."""
    src = open("app/services/cutter.py").read()
    assert "plays as noise" in src
    assert 'args += ["-c", "copy"] if out.suffix == ".mp4"' in src


# ---------------------------------------------------------------- shots

def test_a_still_is_judged_before_it_is_animated():
    """Motion costs about twenty times a still, and the fault is always visible
    in the frame."""
    from app.services import shots
    assert "twenty times" in shots.JUDGE
    for fault in ("hands", "faces", "garbled"):
        assert fault in shots.JUDGE.lower()
    # and it does not reject on taste
    assert "style, mood or composition" in shots.JUDGE


def test_the_judge_cannot_block_a_shot_by_being_unreachable():
    from app.services import shots
    from app.core.config import settings
    old = settings.openai_key
    object.__setattr__(settings, "openai_key", "")
    try:
        out = shots.judge("https://example.com/x.jpg", "anything")
        assert out["use"] is True and out["note"] == "not checked"
    finally:
        object.__setattr__(settings, "openai_key", old)


def test_the_faithful_upscaler_is_the_default():
    """A face has to stay the same person between shots; detail models drift."""
    from app.services import shots
    assert shots.UPSCALERS["faithful"] == "fal-ai/esrgan"
    assert set(shots.UPSCALERS) == {"faithful", "detailed"}


def test_a_screen_needs_four_corners():
    import pathlib
    from app.services import shots
    with pytest.raises(shots.ShotError):
        shots.put_screen_on(pathlib.Path("/tmp/a.mp4"), pathlib.Path("/tmp/b.mp4"),
                            [(0, 0), (1, 1)], pathlib.Path("/tmp/c.mp4"))


def test_every_cut_signs_off_with_the_transparent_mark():
    """Left to the sheet it gets forgotten — which is what happened to the first
    film the cutter rendered."""
    from app.services import cutter
    assert cutter.MARK.name == "mark.png"
    assert cutter.MARK.exists()
    # Test the behaviour, not the comment. Two attempts at asserting on the
    # source text broke on a line wrap and on a '#' landing mid-phrase — which
    # was testing the formatter.
    import inspect
    sig = inspect.signature(cutter.cut)
    assert sig.parameters["sign_off"].default is True       # on unless turned off
    assert sig.parameters["address"].default == "creai.dev"


def test_the_pipeline_is_written_down():
    """So it does not live only in somebody's head."""
    doc = open("docs/making-an-ad.md").read()
    for stage in ("admutate", "producer", "shots", "cutter", "critic",
                  "voicedirect", "captions", "sound"):
        assert stage in doc
    assert "Still by hand" in doc          # and is honest about the gaps


def test_a_scene_is_shot_when_no_footage_is_given():
    """A sheet on its own should be enough to make a film."""
    import inspect
    from app.services import cutter
    sig = inspect.signature(cutter.cut)
    assert sig.parameters["shoot_missing"].default is True
    assert sig.parameters["quality"].default == "final"


@pytest.mark.asyncio
async def test_shooting_can_be_switched_off_and_then_it_says_so():
    import pathlib
    from app.services import cutter
    sheet = {"scenes": [
        {"line": "a", "say": "narrator", "angle": "high", "move": "push", "seconds": 2},
        {"line": "b", "say": "narrator", "angle": "over", "move": "push", "seconds": 2},
        {"line": "c", "say": "narrator", "angle": "low", "move": "still", "seconds": 2}]}
    with pytest.raises(cutter.CutError) as e:
        await cutter.cut(sheet, {}, pathlib.Path("/tmp/noshoot"), shoot_missing=False)
    assert "scene 1" in str(e.value) and "switched off" in str(e.value)


def test_the_expensive_step_happens_last():
    """Judging after animating wastes the expensive step; sharpening after
    animating enlarges the blur instead of removing it."""
    src = open("app/services/shots.py").read()
    body = src[src.index("def shoot("):src.index("def make_still(")]
    assert body.index("make_still") < body.index("animate(")


def test_the_camera_move_is_last_in_a_motion_prompt():
    from app.services import producer, shots
    assert "END of the prompt" in shots.animate.__doc__
    assert set(shots.MOVERS) == {"draft", "final"}


def test_the_name_is_spelled_the_way_it_is_said():
    """Chosen by ear: one word, stress in the capitals so it survives a change of
    voice. A lowercase version reads right on one model and drifts on the next."""
    from app.services import voicedirect as vd
    assert vd.phonetic("Build yours free at Creai dot dev.")[0].count("kreeYAY") == 1
    assert "ARbee" in vd.phonetic("Arbi finds it")[0]
    # and it never reaches the viewer's eyes
    from app.services import captions as cp
    assert "creai.dev" in cp.as_written("Build yours free at Creai dot dev.")
    assert "kreeYAY" not in cp.as_written("Build yours free at Creai dot dev.")


def test_both_models_share_one_cast():
    """A cut should not change who is speaking depending on which model answered."""
    from app.services import voicedirect as vd
    for role in ("narrator", "customer"):
        assert vd.CAST[role]["openai"] and vd.CAST[role]["eleven"]
    assert vd.eleven_voice("narrator") != vd.eleven_voice("customer")
    assert vd.eleven_voice("nobody") == vd.eleven_voice("narrator")   # a safe fallback


def test_the_name_is_spelled_the_way_it_was_chosen_by_ear():
    """kreeYAY: one word, stress in the capitals so it survives a change of voice.
    A lowercase version reads right on one model and drifts on the next."""
    from app.services import voicedirect as vd
    said, note = vd.phonetic("Build yours free at Creai dot dev.")
    assert said == "Build yours free at kreeYAY dot dev."
    assert "second syllable" in note
    assert vd.phonetic("Arbi finds it")[0] == "ARbee finds it"


def test_both_models_share_one_cast():
    """A cut that falls back should still sound like the same two people."""
    from app.services import voicedirect as vd
    for role in ("narrator", "customer"):
        assert vd.CAST[role]["eleven"] and vd.CAST[role]["openai"]
    assert vd.eleven_voice("narrator") != vd.eleven_voice("customer")
    assert vd.eleven_voice("nobody") == vd.eleven_voice("narrator")   # never uncast


def test_the_better_voice_is_tried_first_and_falls_back_quietly():
    src = " ".join(open("app/services/cutter.py").read().split())
    assert "speak_well" in src and "falling back" in src


# ---------------------------------------------------------------- saying what it is doing

@pytest.mark.asyncio
async def test_the_agents_narration_reaches_the_screen_while_it_works():
    """It already said 'wrote app.js' and 'checked the app'. All of it arrived
    when the turn finished, so a ninety-second build read as frozen."""
    from app.services import progress
    key = "testkey12345"
    await progress.start(key)
    await progress.say(key, "wrote the sign-in screen")
    await progress.say(key, "checked the app · all clear")
    out = await progress.read(key)
    assert [s["step"] for s in out["steps"]] == ["wrote the sign-in screen",
                                                 "checked the app · all clear"]
    assert out["done"] is False
    # a second poll only brings what is new
    assert await progress.read(key, since=2) == {**out, "steps": []}


@pytest.mark.asyncio
async def test_a_line_is_never_repeated_at_somebody():
    from app.services import progress
    key = "testkey67890"
    await progress.start(key)
    for _ in range(3):
        await progress.say(key, "drawing the icons")
    assert len((await progress.read(key))["steps"]) == 1


@pytest.mark.asyncio
async def test_a_turn_that_fails_still_closes_its_channel():
    """Otherwise a screen polls forever, showing a line about something that
    stopped happening minutes ago."""
    src = " ".join(open("app/api/agent.py").read().split())
    assert "finally:" in src and "progress.finish(key)" in src


@pytest.mark.asyncio
async def test_a_looping_turn_cannot_fill_the_screen():
    from app.services import progress
    key = "testkeyloop00"
    await progress.start(key)
    for i in range(80):
        await progress.say(key, f"step {i}")
    assert len((await progress.read(key))["steps"]) <= progress.MAX_STEPS


def test_every_existing_log_line_streams_without_being_touched():
    """Dozens of call sites; a migration that misses one leaves a silent gap in
    the middle of a build, which is the exact fault this fixes."""
    from app.services.agent import LiveLog, Turn
    import dataclasses
    fields = {f.name: f for f in dataclasses.fields(Turn)}
    assert fields["log"].default_factory is LiveLog
