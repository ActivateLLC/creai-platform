"""
Genres, and what each one actually needs.

The brief knows Godot and knows what makes a game feel good in general. It knew
nothing about genre — so a runner, a puzzler and a shooter all came out built the
same way, and each was wrong in its own particular manner. A runner with no
difficulty curve is over in twenty seconds. A puzzler that cannot be undone is
abandoned at the first mistake. A shooter with no recoil feels like pointing.

Each entry here is what somebody who has shipped one would tell somebody who has
not: the shape of the loop, the numbers that matter, the mistake everyone makes
first, and what it takes to look right rather than merely work.

Two things deliberately absent.

Networked multiplayer. Godot can do it; this platform cannot. A game is exported
to WebAssembly and served as a static file, so there is no authoritative server,
no lag compensation and no matchmaking. Promising player-versus-player over a
network would be promising infrastructure that does not exist. Local multiplayer
— split screen, shared keyboard, pass the phone — works today and is in here.

Binary assets. The builder takes text source only, so every genre below is
described in terms of what can be drawn in code or written as a shader. When an
asset pipeline exists this file gets richer; until then, saying "use a sprite
sheet" would be advice nobody can follow.
"""

import logging

log = logging.getLogger("creai.genres")


GENRES: dict[str, dict] = {
    "runner": {
        "what": "One input, constant forward motion, obstacles that arrive faster.",
        "loop": "Survive, nearly die, beat your number, go again within two seconds.",
        "numbers": "Start slow enough to feel easy. Raise speed about 6% every 15 seconds, "
                   "and introduce one new obstacle type per minute, never two at once.",
        "feel": "Coyote time — let a jump register for ~100ms after leaving the ground, or it "
                "reads as unresponsive even though the code is right. Buffer an input pressed "
                "just before landing.",
        "mistake": "Difficulty that starts where it should end. The first fifteen seconds must "
                   "be winnable by somebody who has never played.",
        "looks": "Parallax in three layers, the nearest moving fastest. The player and the "
                 "danger are the only saturated things on screen.",
    },
    "arcade": {
        "what": "A single mechanic, scored, over in two minutes.",
        "loop": "Learn the rule in five seconds, master it in five minutes, chase the number "
                "for an hour.",
        "numbers": "A run should last 60–180 seconds. Longer and a bad run wastes real time; "
                   "shorter and mastery never lands.",
        "feel": "Every point scored gets a sound, a scale-up and a number that floats away. "
                "The score is the game, so make collecting it physical.",
        "mistake": "Adding a second mechanic before the first is satisfying. One verb, "
                   "perfected, beats three that are limp.",
        "looks": "High contrast and a lot of empty space. The eye must find the player in one "
                 "frame.",
    },
    "puzzle": {
        "what": "A solvable state, a set of legal moves, and a goal the player can see.",
        "loop": "Understand, try, fail cheaply, understand better.",
        "numbers": "Solvable in under two minutes per level for the first ten levels. Every "
                   "level must be completable — generate and verify, never ship an unsolvable "
                   "board.",
        "feel": "Undo is mandatory and instant. Without it, a mistake becomes a reason to "
                "stop, and a puzzle game lives or dies on willingness to experiment.",
        "mistake": "Hiding the rules. A puzzle should be hard because the solution is hard, "
                   "never because the player cannot tell what is legal.",
        "looks": "Grid alignment to the pixel, generous spacing, and state shown by shape as "
                 "well as colour so it works for colourblind players.",
    },
    "platformer": {
        "what": "Jumping, gravity, and geometry that rewards knowing it.",
        "loop": "See a gap, judge it, make it or learn from missing.",
        "numbers": "Jump apex around 0.35s, gravity heavier on the way down than the way up "
                   "(about 1.8x). Variable height — release early, rise less.",
        "feel": "Coyote time, input buffering, and a landing squash. These three are most of "
                "why a good platformer feels good and a bad one feels slippery.",
        "mistake": "Realistic physics. Real gravity feels awful; tune for the hand, not for "
                   "Newton.",
        "looks": "Silhouettes that read instantly — the player shape must be distinguishable "
                 "from the scenery at a glance.",
    },
    "shooter": {
        "what": "Aim, fire, hit, feedback. Two dimensions, or first person in 3D.",
        "loop": "Threat appears, is read, is answered, and the answer feels good.",
        "numbers": "Time-to-kill of roughly 0.3–0.8s for common enemies. Fire rate and damage "
                   "are one decision, not two. Telegraph every enemy attack for at least "
                   "0.4s before it lands.",
        "feel": "Recoil, a muzzle flash, a hit flash on the target, and a different sound for "
                "hit versus miss. A shot with no answer is pointing, not shooting.",
        "mistake": "Enemies that damage the player with no warning. Unfair is felt "
                   "immediately and forgiven never.",
        "looks": "Dark environment, bright projectiles and enemies. Anything that can hurt "
                 "you must be the brightest thing in frame.",
        "note": "Single-player or local only on this platform: there is no game server, so "
                "networked player-versus-player cannot be built here.",
    },
    "tower-defence": {
        "what": "A path, placeable defences, waves of attackers, an economy.",
        "loop": "Read the wave, spend, watch it work or not, adjust.",
        "numbers": "Waves every 20–40 seconds with a visible countdown. Income should let a "
                   "player afford roughly one new tower per two waves early on.",
        "feel": "Show range on hover or drag, before placement is committed. Show damage "
                "numbers. The player must be able to see why something failed.",
        "mistake": "An economy nobody can reason about. If the player cannot predict whether "
                   "they can afford the next tower, every decision is a guess.",
        "looks": "The path is the clearest thing on screen. Towers read by shape, not colour "
                 "alone.",
    },
    "idle": {
        "what": "Numbers that grow, choices about what grows faster.",
        "loop": "Spend, watch it climb, unlock a new way to spend.",
        "numbers": "Each upgrade tier roughly 10–15x the last. First meaningful unlock within "
                   "30 seconds, or nobody stays.",
        "feel": "Progress must be visible while idle — a bar filling, a counter ticking. "
                "Offline progress on return, and say how much was earned while away.",
        "mistake": "A first upgrade that takes minutes. The opening should feel almost "
                   "generous.",
        "looks": "Legible large numbers with proper formatting — 1.2M, not 1200000.",
    },
    "rhythm": {
        "what": "Input timed against a beat.",
        "loop": "Anticipate, hit, hear it land.",
        "numbers": "A hit window of about 120ms for good, 60ms for perfect. Latency "
                   "calibration is mandatory — browsers add 50–150ms and it varies by device.",
        "feel": "Audio feedback must be sample-accurate; visual can lag slightly, audio "
                "cannot. Drive the whole game from the audio clock, never from _process.",
        "mistake": "Timing against frame time. Frames drift, music does not.",
        "looks": "Everything pulses on the beat, including the background. A rhythm game that "
                 "is visually still is fighting itself.",
    },
    "local-versus": {
        "what": "Two players, one device. Split keyboard, split screen, or pass the phone.",
        "loop": "One round, quick, then immediately again.",
        "numbers": "A round of 30–90 seconds. Rematch in one tap, never a menu.",
        "feel": "Both players must see their own state without hunting for it. Colour-code "
                "each player and keep that colour consistent everywhere.",
        "mistake": "Hidden information on a shared screen. Design for both players seeing "
                   "everything, or use turns with a hand-over screen.",
        "looks": "A clear dividing line, mirrored layouts, and scores that both players can "
                 "read from their own side.",
        "note": "This is how two people play on this platform. It needs no server and works "
                "today.",
    },
}


THEMES: dict[str, dict] = {
    "neon": {
        "palette": "near-black background, one electric accent, one cool secondary",
        "how": "glow heavy, additive trails behind anything that moves, thin bright lines on "
               "dark. Restraint is what separates neon from a mess: one accent, not five.",
    },
    "paper": {
        "palette": "warm off-white, ink dark, one muted accent",
        "how": "flat shapes, visible grain from a shader, slight rotation on elements so "
               "nothing is perfectly square. No glow at all — glow breaks the illusion.",
    },
    "noir": {
        "palette": "greyscale with a single red or amber",
        "how": "hard vignette, high contrast, most of the screen in shadow. The one colour "
               "appears only on what matters.",
    },
    "pastel": {
        "palette": "soft desaturated hues, nothing pure white or pure black",
        "how": "rounded shapes, gentle easing, low contrast — which means the player needs "
               "shape or motion to stand out, not brightness.",
    },
    "terminal": {
        "palette": "black, one phosphor green or amber",
        "how": "monospace type, scanlines and a slight curve from a shader, text that types "
               "itself in. Everything aligns to a character grid.",
    },
    "sunset": {
        "palette": "deep indigo to warm orange gradient, dark silhouettes",
        "how": "a gradient shader doing the work, everything in the foreground reduced to "
               "silhouette. Very cheap to draw, and reads as considered.",
    },
}


def brief_for(genre: str, theme: str = "") -> str:
    """What to tell the agent when somebody asks for this kind of game."""
    g = GENRES.get((genre or "").lower().strip())
    if not g:
        return ""
    lines = [f"This is a {genre}. {g['what']}",
             f"The loop: {g['loop']}",
             f"Numbers that matter: {g['numbers']}",
             f"Feel: {g['feel']}",
             f"The mistake to avoid: {g['mistake']}",
             f"How it should look: {g['looks']}"]
    if g.get("note"):
        lines.append(f"Note: {g['note']}")
    t = THEMES.get((theme or "").lower().strip())
    if t:
        lines += ["", f"Visual theme — {theme}: {t['palette']}. {t['how']}"]
    return "\n".join(lines)


def guess(words: str) -> str:
    """Which genre somebody is describing, or "" when it is not clear.

    Deliberately conservative: applying the wrong genre's rules is worse than
    applying none, because they contradict each other. A runner wants rising
    speed; a puzzle wants none.
    """
    text = (words or "").lower()
    for genre, hints in (
        ("runner", ("endless", "runner", "dodge", "jump over", "obstacles")),
        ("shooter", ("shoot", "shooter", "gun", "aim", "fps", "bullet")),
        ("puzzle", ("puzzle", "match", "solve", "tiles", "sudoku", "logic")),
        ("platformer", ("platform", "jumping", "levels", "mario")),
        ("tower-defence", ("tower", "defence", "defense", "waves")),
        ("idle", ("idle", "clicker", "incremental", "tycoon")),
        ("rhythm", ("rhythm", "beat", "music game", "tempo")),
        ("local-versus", ("two player", "2 player", "versus", "against a friend",
                          "same screen", "split screen")),
        ("arcade", ("arcade", "high score", "score attack")),
    ):
        if any(h in text for h in hints):
            return genre
    return ""
