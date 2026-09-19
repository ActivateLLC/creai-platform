"""
Tests for the Gemini/polish boundary.

The point of these tests is not "does polish.py work" — it's "is it actually
impossible for this to grow into a second orchestrator by accident". Every test
here is defending a specific claim made in polish.py's docstring.
"""
import pytest

from app.core.config import settings
from app.services import models, polish


def test_gemini_model_has_tools_disabled_in_the_registry():
    m = models.REGISTRY["gemini-2.5-flash"]
    assert m.provider == "gemini"
    assert m.tools is False, (
        "gemini-2.5-flash must have tools=False in the registry — this is the "
        "first line of defense against it being handed a tool loop by accident"
    )
    assert m.fallback == (), (
        "a polish-only model should not silently fall back to (or from) an "
        "orchestration model — that would blur the boundary this file exists to keep"
    )


def test_gemini_provider_function_never_builds_tool_schema():
    """_gemini() should not translate `tools` into Gemini's function-calling
    format at all — it should be structurally incapable of it today, not just
    unused by convention. (It's fine that the parameter exists in the
    signature — every provider function shares one signature — the point is
    the body never acts on it.)"""
    import inspect

    src = inspect.getsource(models._gemini)
    # split off the docstring (delimited by the first pair of triple-quotes);
    # the docstring legitimately explains what Gemini's schema WOULD look like
    # if this were ever extended, so it must be excluded from the check.
    body = src.split('"""', 2)[-1]
    assert "FunctionDeclaration" not in body
    assert "functionDeclarations" not in body
    assert '"tools"' not in body and "['tools']" not in body


def _patch_settings(**kw):
    """settings is a frozen dataclass instantiated once at import time —
    object.__setattr__ is the supported way to mutate individual fields in a
    test without breaking that contract everywhere else."""
    for k, v in kw.items():
        object.__setattr__(settings, k, v)


def test_configured_checks_the_right_key_per_provider():
    """Regression test: configured() originally only knew about anthropic vs
    meta, so any gemini model would fall through to checking meta_api_key.
    That means a Gemini model could report itself unconfigured (or wrongly
    configured) depending on an unrelated key."""
    original = (settings.gemini_api_key, settings.meta_api_key)
    try:
        _patch_settings(gemini_api_key="", meta_api_key="some-unrelated-meta-key")
        assert models.configured("gemini-2.5-flash") is False, (
            "a Gemini model must not report as configured because META_API_KEY "
            "happens to be set"
        )

        _patch_settings(gemini_api_key="real-key")
        assert models.configured("gemini-2.5-flash") is True
    finally:
        _patch_settings(gemini_api_key=original[0], meta_api_key=original[1])


@pytest.mark.asyncio
async def test_suggest_never_raises_on_provider_failure(monkeypatch):
    """A polish suggestion is optional and cosmetic. If Gemini is down, rejects
    the request, or the account has a billing problem, that must never surface
    as an error to the person — it's not a required step in any flow."""

    async def boom(*a, **kw):
        raise models.ModelAccountProblem("no credit")

    original = settings.gemini_api_key
    _patch_settings(gemini_api_key="real-key")   # so suggest() gets past the
                                                  # "not configured" short-circuit
                                                  # and actually reaches complete()
    monkeypatch.setattr(models, "complete", boom)
    try:
        out = await polish.suggest("a login screen for a plumbing app")
    finally:
        _patch_settings(gemini_api_key=original)
    assert out == {"ok": False, "reason": "unavailable", "suggestions": []}


@pytest.mark.asyncio
async def test_suggest_reports_unconfigured_without_calling_the_provider(monkeypatch):
    original = settings.gemini_api_key
    _patch_settings(gemini_api_key="")
    called = False

    async def spy(*a, **kw):
        nonlocal called
        called = True

    monkeypatch.setattr(models, "complete", spy)
    try:
        out = await polish.suggest("a dashboard")
    finally:
        _patch_settings(gemini_api_key=original)
    assert out["ok"] is False
    assert called is False, "suggest() must short-circuit before calling models.complete"


@pytest.mark.asyncio
async def test_suggest_passes_an_empty_tools_list(monkeypatch):
    """Even though the registry entry has tools=False, polish.py's own call
    site must also pass tools=[] explicitly — defense in depth, not reliance
    on a single flag elsewhere in the codebase."""
    seen = {}

    async def capture(model_key, messages, tools, system, max_tokens=2048):
        seen["tools"] = tools
        return {"text": "- make the button bigger", "usage": {}}

    original = settings.gemini_api_key
    _patch_settings(gemini_api_key="real-key")
    monkeypatch.setattr(models, "complete", capture)
    try:
        await polish.suggest("a checkout button")
    finally:
        _patch_settings(gemini_api_key=original)
    assert seen["tools"] == []


@pytest.mark.asyncio
async def test_suggest_returns_a_list_the_orchestrator_can_apply_individually(monkeypatch):
    """The output must be structured as discrete suggestions, not a blob of
    prose — the whole point is that Claude (the orchestrator) applies or
    rejects each one, rather than a Gemini suggestion being adopted wholesale."""

    async def capture(*a, **kw):
        return {"text": "- increase padding to 16px\n- use a warmer accent color\n",
                "usage": {}}

    original = settings.gemini_api_key
    _patch_settings(gemini_api_key="real-key")
    monkeypatch.setattr(models, "complete", capture)
    try:
        out = await polish.suggest("a card component")
    finally:
        _patch_settings(gemini_api_key=original)
    assert out["ok"] is True
    assert out["suggestions"] == ["increase padding to 16px", "use a warmer accent color"]


def test_polish_module_never_imports_filesystem_or_build_tools():
    """polish.py must have no access to the things that would let it become an
    orchestrator: no file writes, no godot/app builder calls.

    This checks actual imports and calls, not the docstring's prose — the
    docstring legitimately names these tools as things polish.py must never
    touch, which would false-positive a naive substring search.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(polish))
    imported = set()
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imported.add(alias.name)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)

    forbidden_modules = {"godot", "appfs", "shots", "cutter"}
    forbidden_calls = {"write_files", "check_app", "check_game", "build_game", "open"}

    assert not (imported & forbidden_modules), imported & forbidden_modules
    assert not (called & forbidden_calls), called & forbidden_calls


# ---------------------------------------------------------------- reachability

def _tool_audit():
    import re
    from app.services import agent
    src = open("app/services/agent.py").read()
    defined = {getattr(agent, n)["name"] for n in dir(agent)
               if n.startswith("TOOL_") and isinstance(getattr(agent, n), dict)}
    handled = set(re.findall(r'if name == "([a-z_]+)"', src))
    run_src = src[src.index("async def run("):]
    offered = {getattr(agent, t)["name"]
               for t in set(re.findall(r"TOOL_[A-Z_]+", run_src)) if hasattr(agent, t)}
    return defined, handled, offered


def test_every_tool_has_a_handler_and_every_handler_a_tool():
    """A tool the model can call with nothing behind it fails at runtime with
    a message the person cannot act on."""
    defined, handled, offered = _tool_audit()
    assert defined - handled == set(), f"defined but no handler: {defined - handled}"
    assert handled - defined == set(), f"handler but never defined: {handled - defined}"


def test_every_tool_is_actually_offered_to_the_model():
    """Defined and handled is not the same as reachable. Three specialists sat
    fully built, tested and unreachable because nothing offered them."""
    defined, handled, offered = _tool_audit()
    assert defined - offered == set(), f"never offered to the model: {defined - offered}"


def test_every_specialist_has_a_caller():
    """hypothesis, critic and polish were each one file, one SYSTEM, one model,
    fully tested — and dead code, because nothing called them."""
    import pathlib, re
    services = pathlib.Path("app/services")
    specialists = ("hypothesis", "producer", "critic", "polish", "voicelead")
    for name in specialists:
        callers = []
        for f in list(services.glob("*.py")) + list(pathlib.Path("app/api").glob("*.py")):
            if f.stem == name:
                continue
            src = f.read_text()
            if re.search(rf"\b{name}\b", src) and ("import" in src):
                if re.search(rf"from \.+ ?import[^\n]*\b{name}\b|from \.\.services import[^\n]*\b{name}\b|services\.{name}\b", src):
                    callers.append(f.name)
        assert callers, f"{name} has no caller anywhere — it is dead code"


def test_video_projects_are_offered_the_ad_specialists_in_order():
    """Hypothesis before direction before the plan before review. A video
    project offered plan_video alone writes scenes with no argument behind
    them and never has the render checked."""
    src = open("app/services/agent.py").read()
    block = src[src.index("elif video:"):src.index("elif video:") + 400]
    for t in ("TOOL_HYPOTHESIS", "TOOL_DIRECT", "TOOL_PLAN_VIDEO", "TOOL_REVIEW_CUT"):
        assert t in block, f"video projects are not offered {t}"
    order = [block.index(t) for t in ("TOOL_HYPOTHESIS", "TOOL_DIRECT", "TOOL_PLAN_VIDEO", "TOOL_REVIEW_CUT")]
    assert order == sorted(order), "the ad tools are offered out of the order they are used"


def test_the_retired_voice_is_not_hardcoded_anywhere():
    """'ash' was retired by name in the cast. plan_video still had it as a
    literal, so every video plan was casting the rejected voice."""
    import re
    src = open("app/services/agent.py").read()
    assert not re.search(r'"voice":\s*"ash"', src)
    assert "_narrator_voice()" in src
    from app.services import agent
    assert agent._narrator_voice() != "ash"


def test_polish_reads_the_right_store_for_the_project_kind():
    """Apps keep files in appfs, games in godot. A polish pass that read the
    wrong store would react to a file that is not there and say nothing useful."""
    src = open("app/services/agent.py").read()
    i = src.index('if name == "polish_look":')
    block = src[i:i + 900]
    assert "store = godot if game is not None else appfs" in block
