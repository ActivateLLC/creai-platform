"""
The game division's specialists. Each test defends a claim the design makes.
"""
import pytest


# ---------------------------------------------------------------- concept

def test_a_clone_with_no_twist_is_refused():
    """Silence here is how a platform ships the same game ten thousand times
    and gets a store's spam rejection for all of them."""
    from app.services import gameconcept as gc
    c = gc._clean({"title": "Blue Bird", "mechanic": "tap to flap", "is_clone": True,
                   "twist": "", "resembles": "Flappy Bird", "thirty_seconds": "tap, die"})
    assert any("clone with no twist" in p for p in gc.check(c))


def test_the_concept_rejects_marketing_language():
    from app.services import gameconcept as gc
    c = gc._clean({"mechanic": "drag", "hook": "an addictive, immersive experience",
                   "thirty_seconds": "x"})
    assert any("marketing language" in p for p in gc.check(c))


def test_the_concept_insists_on_one_mechanic():
    from app.services import gameconcept as gc
    c = gc._clean({"mechanic": "jump and shoot and build and trade and craft and cook",
                   "thirty_seconds": "x"})
    assert any("not one verb" in p for p in gc.check(c))


def test_the_concept_names_the_ceiling_for_the_builder_to_say_first():
    from app.services import gameconcept as gc
    c = gc._clean({"title": "T", "ceiling": "No photorealism can be built here."})
    assert "Say this to the person before building" in gc.brief_from(c)


def test_confidence_defaults_low_rather_than_dressing_up_a_guess():
    from app.services import gameconcept as gc
    assert gc._clean({})["confidence"] == "low"


# ---------------------------------------------------------------- art director

def test_the_art_director_runs_on_the_visual_model_and_never_writes_a_file():
    from app.services import gameart
    assert gameart.MODEL_KEY == "gemini-2.5-flash"
    import ast, inspect
    tree = ast.parse(inspect.getsource(gameart))
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"write", "write_text", "write_bytes"}


def test_the_art_director_catches_a_style_that_cannot_be_drawn():
    from app.services import gameart
    b = {"palette": {"background": "#000", "neutral": "#888", "accent": "#0f0"},
         "style": "photorealistic 3D render with textures", "light": "top-left",
         "construction": ["silhouette first"]}
    assert any("cannot be drawn as SVG" in p for p in gameart.check(b))


def test_the_art_director_requires_a_shared_light_direction():
    """No light direction means every sprite is shaded differently — the
    single most common reason a set looks amateur when each is fine alone."""
    from app.services import gameart
    b = {"palette": {"background": "#000", "neutral": "#888", "accent": "#0f0"},
         "style": "flat", "light": "", "construction": ["x"]}
    assert any("light direction" in p for p in gameart.check(b))


@pytest.mark.asyncio
async def test_a_missing_art_bible_never_stops_a_build():
    from app.services import gameart
    from app.core.config import settings
    original = settings.gemini_api_key
    object.__setattr__(settings, "gemini_api_key", "")
    try:
        out = await gameart.direct({"title": "T", "mechanic": "tap"})
    finally:
        object.__setattr__(settings, "gemini_api_key", original)
    assert out["ok"] is False and out["bible"] is None


# ---------------------------------------------------------------- store readiness

def test_a_thin_game_fails_minimum_functionality():
    """Apple 4.2: 'your app provides a minimal user experience'. No arguing
    with it afterwards."""
    from app.services import storeready
    thin = {"main.gd": "extends Node2D\nfunc _ready(): pass"}
    problems = storeready.minimum_functionality(thin)
    assert any("one script" in p for p in problems)
    assert any("no score" in p for p in problems)
    assert any("no pause" in p for p in problems)


def test_two_of_our_own_games_with_the_same_shape_are_caught():
    """Apple 4.3, and the first rejection tends to take the rest with it."""
    from app.services import storeready
    a = {"title": "Sky Tap", "mechanic": "tap to flap", "resembles": "flappy bird",
         "twist": "gravity flips every ten seconds"}
    b = {"title": "Cloud Tap", "mechanic": "tap to flap", "resembles": "flappy bird",
         "twist": "gravity flips every ten seconds"}
    problems = storeready.spam_check(b, [a])
    assert any("indistinguishable" in p for p in problems)


def test_a_listing_is_checked_against_the_stores_real_limits():
    from app.services import storeready
    bad = {"title": "A title that is far longer than thirty characters allows",
           "subtitle": "", "description": "short", "privacy_policy_url": "",
           "age_rating": "", "icon_sizes": [1024], "screenshots": {}}
    problems = storeready.listing_check(bad)
    assert any("allows 30" in p for p in problems)
    assert any("privacy policy" in p for p in problems)
    assert any("missing iOS icon sizes" in p for p in problems)
    assert any("screenshots" in p for p in problems)


def test_generated_sounding_copy_is_flagged():
    from app.services import storeready
    l = {"title": "T", "description": "The most addictive, immersive game you will ever play. " * 3,
         "privacy_policy_url": "https://x", "age_rating": "4+",
         "icon_sizes": list(storeready.IOS_ICON_SIZES), "screenshots": {"6.7-inch": ["a"]}}
    assert any("generated copy" in p for p in storeready.listing_check(l))


def test_a_game_never_ships_under_creais_account():
    """Domains in their name, payments into their Stripe, apps under their
    account. Not configurable."""
    from app.services import storeready
    problems = storeready.ownership_check({"owner": "creai", "apple_issuer_id": "x",
                                           "apple_key_id": "y", "play_service_account": "z"})
    assert any("never ours" in p for p in problems)
    assert storeready.ownership_check({"owner": "customer", "apple_issuer_id": "x",
                                       "apple_key_id": "y", "play_service_account": "z"}) == []


def test_ready_refuses_to_submit_anything_it_flagged():
    from app.services import storeready
    out = storeready.ready({"a.gd": "x"}, {"title": "T"}, {}, {})
    assert out["ready"] is False
    assert "Not submitted" in out["note"]


# ---------------------------------------------------------------- wiring

def test_the_game_agent_forms_a_concept_before_anything_else():
    from app.services import agent
    g = agent.GAME_EXTRA
    assert "call form_concept with the person's own" in g
    assert g.index("form_concept") < g.index("game_rules")     # concept before genre
    src = open("app/services/agent.py").read()
    game_tools = src[src.index('if intent == "build" and game is not None:'):][:500]
    for t in ("TOOL_CONCEPT", "TOOL_ART", "TOOL_PLAYTEST"):
        assert t in game_tools


def test_art_direction_cannot_run_before_the_concept_exists():
    src = open("app/services/agent.py").read()
    i = src.index('if name == "art_direction":')
    assert "form the concept first" in src[i:i + 500]
