"""Godot game projects: file rules, the review that saves a build, jobs, serving."""

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("ENV", "development")

from app.core import db                              # noqa: E402
from app.core.config import settings                 # noqa: E402
from app.core.db import conn                         # noqa: E402
from app.main import app                             # noqa: E402
from app.services import godot                       # noqa: E402

from tests.test_isolation import auth, sign_in       # noqa: E402

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def api():
    object.__setattr__(settings, "secret_key", "test-secret-key-" + "x" * 16)
    await db.connect()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await db.disconnect()


async def make_game(api, email: str) -> tuple[str, int]:
    token = await sign_in(api, email)
    r = await api.post("/v1/projects", json={"name": "Dodger", "path": "game"}, headers=auth(token))
    assert r.status_code == 200, r.text
    return token, r.json()["id"]


# ---------------------------------------------------------------- file rules

def test_only_godot_source_is_accepted():
    godot.check("main.gd", "extends Node2D\n")
    godot.check("scenes/level.tscn", "[gd_scene]\n")
    for bad in ("main.js", "art/sprite.png", "../escape.gd", "Main.GD"):
        with pytest.raises(godot.GameError):
            godot.check(bad, "x")


def test_a_game_cannot_shell_out_or_call_other_sites():
    with pytest.raises(godot.GameError):
        godot.check("main.gd", "extends Node\nfunc _ready():\n\tOS.execute('sh', [])\n")
    with pytest.raises(godot.GameError):
        godot.check("net.gd", "extends Node\nvar r := HTTPRequest.new()\n")


# ---------------------------------------------------------------- review

def test_the_starter_project_is_exportable():
    """Every new game begins here, so this failing means every game begins broken."""
    assert godot.review(godot.STARTER) == {"ok": True, "problems": [], "notes": []}


def test_a_main_scene_that_does_not_exist_is_caught_before_a_build_is_spent():
    files = dict(godot.STARTER)
    files["project.godot"] = files["project.godot"].replace("main.tscn", "level.tscn")
    found = godot.review(files)
    assert not found["ok"]
    assert any("level.tscn" in p for p in found["problems"])


def test_a_dangling_resource_reference_is_caught():
    files = dict(godot.STARTER)
    files["main.gd"] = "extends Node2D\n\nvar s = preload(\"res://player.tscn\")\n"
    found = godot.review(files)
    assert not found["ok"]
    assert any("player.tscn" in p for p in found["problems"])


def test_gdscript_slips_are_caught():
    files = dict(godot.STARTER)
    files["main.gd"] = "extends Node2D\n\nfunc _ready() -> void\n\tpass\n"
    assert any("':'" in p for p in godot.review(files)["problems"])
    files["main.gd"] = "extends Node2D\n\nfunc _ready() -> void:\n\tvar a := 1\n  var b := 2\n"
    assert any("tabs and spaces" in p for p in godot.review(files)["problems"])


def test_a_project_without_project_godot_is_not_a_project():
    assert not godot.review({"main.gd": "extends Node\n"})["ok"]


# ---------------------------------------------------------------- credits

def test_build_time_is_billed_by_the_minute_with_a_floor():
    assert godot.credits_for(5) == godot.MIN_BUILD_CREDITS
    assert godot.credits_for(61) == 2 * godot.CREDITS_PER_MINUTE
    assert godot.credits_for(180) == 3 * godot.CREDITS_PER_MINUTE


def test_threads_need_a_host_of_their_own():
    """Cross-origin isolation is impossible on the opaque origin games otherwise get."""
    object.__setattr__(settings, "games_url", "")
    assert not godot.threads_allowed()
    object.__setattr__(settings, "games_url", "https://play.creai.dev")
    assert godot.threads_allowed()
    object.__setattr__(settings, "games_url", "")


# ---------------------------------------------------------------- the project

async def test_a_new_game_starts_from_a_project_that_exports(api):
    token, pid = await make_game(api, "game-seed@example.com")
    files = await godot.files(pid, await _org(pid))
    assert files["project.godot"] == godot.STARTER["project.godot"]
    r = await api.get(f"/v1/games/{pid}/check", headers=auth(token))
    assert r.status_code == 200 and r.json()["ok"] is True


async def _org(project_id: int) -> int:
    async with conn() as c:
        return await c.fetchval("SELECT org_id FROM projects WHERE id=$1", project_id)


async def test_building_needs_the_builder_switched_on(api):
    token, pid = await make_game(api, "game-off@example.com")
    object.__setattr__(settings, "build_url", "")
    r = await api.post(f"/v1/games/{pid}/build", headers=auth(token))
    assert r.status_code == 503


async def test_a_broken_project_is_refused_before_it_costs_anything(api):
    token, pid = await make_game(api, "game-broken@example.com")
    org = await _org(pid)
    await godot.write(pid, org, {"project.godot": 'config_version=5\n\n[application]\nconfig/name="x"\n'})
    with pytest.raises(godot.GameError):
        await godot.start(pid, org, None)


async def test_publishing_needs_a_finished_build(api):
    token, pid = await make_game(api, "game-pub@example.com")
    r = await api.post(f"/v1/games/{pid}/publish", headers=auth(token))
    assert r.status_code == 409


async def test_an_unpublished_game_is_not_served(api):
    r = await api.get("/g/nobody-here/")
    assert r.status_code == 404


async def test_the_game_address_is_a_directory(api):
    """The export's HTML uses relative paths, so a bare slug has to redirect."""
    r = await api.get("/g/some-game", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/g/some-game/"


async def test_one_workspace_cannot_build_another_workspace_s_game(api):
    token_a, pid = await make_game(api, "game-owner@example.com")
    token_b = await sign_in(api, "game-stranger@example.com")
    for method, path in (("post", f"/v1/games/{pid}/build"),
                         ("get", f"/v1/games/{pid}/check"),
                         ("post", f"/v1/games/{pid}/publish")):
        r = await getattr(api, method)(path, headers=auth(token_b))
        assert r.status_code == 404, f"{path} leaked across workspaces: {r.status_code}"


async def test_a_game_project_gets_the_game_tools_not_the_app_tools(api):
    from app.services import agent
    names = set()

    async def fake_call(messages, tools, system, model, max_tokens=2048):
        names.update(t["name"] for t in tools)
        assert "GODOT GAME" in system
        return {"model": model, "usage": {}, "content": [{"type": "text", "text": "ok"}]}

    real, agent._call = agent._call, fake_call
    try:
        await agent.run("make it jump", {}, project=True, game=(1, 1))
    finally:
        agent._call = real
    assert {"check_game", "build_game", "write_files"} <= names
    assert "check_app" not in names
