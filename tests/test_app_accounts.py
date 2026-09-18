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
