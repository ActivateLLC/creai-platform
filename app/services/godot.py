"""
Godot game projects: real engine games the agent writes as files.

A game project holds ordinary Godot source (project.godot, .tscn scenes, .gd
scripts) in the same project_files table apps use. The builder service turns that
into a WebAssembly export; this module owns everything either side of it — what the
agent is allowed to write, whether a project looks exportable before we spend a
build on it, the build job and its progress, and the published release.

Two things shape the design:

Exports are slow (30-90 seconds), so a build is a job with progress the person can
watch, not something that happens inside a chat turn.

Exports are big (a trivial game is 37 MB of wasm, 9 MB gzipped), so release files
live in the bucket, not in Postgres, and are served through the API rather than a
presigned bucket URL — a redirect to storage can't carry the resource-policy header
that cross-origin isolation requires.

Threads: Godot's threaded web export only runs on a cross-origin-isolated page, and
COOP/COEP are ignored on the opaque origin that published games otherwise get. So
games build single-threaded by default, which runs anywhere, and threaded builds are
only allowed when a separate games host is configured (see GAMES_URL).
"""

import asyncio
import logging
import re
import time

from ..core.config import settings
from ..core.db import conn

log = logging.getLogger("creai.godot")

# Text source only. Binary art and audio come from the asset library later; keeping
# the project text-only means it fits project_files and stays reviewable.
PATH = re.compile(r"^(?:[a-z0-9][a-z0-9_-]{0,40}/){0,3}"
                  r"[a-z0-9][a-z0-9_.-]{0,60}\.(godot|gd|tscn|tres|cfg|gdshader|json|md|txt|svg)$")
ENTRY = "project.godot"
MAX_FILE = 200_000
MAX_FILES = 120
MAX_TOTAL = 2_000_000

# Build economics. Credits are cents, so this is 5c a minute of build time — a little
# over what the container costs, and a build is a deliberate act, not a keystroke.
CREDITS_PER_MINUTE = 5
MIN_BUILD_CREDITS = 5
MAX_BUILD_MINUTES = 6

STATES = ("queued", "importing", "exporting", "packaging", "done", "failed")
PROGRESS = {"queued": 5, "importing": 25, "exporting": 55, "packaging": 85,
            "done": 100, "failed": 100}


class GameError(ValueError):
    pass


# ---------------------------------------------------------------- the starter

# What every game begins from. Upgraded deliberately: a starter that trips the
# craft checks teaches the shape of a prototype, and whatever it demonstrates is
# what gets built on. So it ships with a shader, glow, eased motion and type that
# varies — the four things that separate a Godot project from a game.
STARTER = {
    "project.godot": """config_version=5

[application]
config/name="New game"
run/main_scene="res://main.tscn"
config/features=PackedStringArray("4.5", "GL Compatibility")

[display]
window/size/viewport_width=480
window/size/viewport_height=720
window/stretch/mode="canvas_items"
window/stretch/aspect="keep"

[rendering]
renderer/rendering_method="gl_compatibility"
environment/defaults/default_clear_color=Color(0.055, 0.067, 0.086, 1)
""",

    # A full-screen shader is the cheapest way to stop a game looking like a
    # prototype: a gradient that drifts, and a vignette that frames the play.
    "backdrop.gdshader": """shader_type canvas_item;

uniform float warmth : hint_range(0.0, 1.0) = 0.0;

void fragment() {
	vec2 uv = SCREEN_UV;
	vec3 low = vec3(0.055, 0.067, 0.086);
	vec3 high = mix(vec3(0.098, 0.125, 0.165), vec3(0.16, 0.11, 0.09), warmth);
	vec3 col = mix(high, low, smoothstep(0.0, 1.0, uv.y));
	float edge = distance(uv, vec2(0.5)) * 1.25;
	col *= 1.0 - smoothstep(0.35, 0.95, edge) * 0.55;   // vignette
	COLOR = vec4(col, 1.0);
}
""",

    "main.tscn": """[gd_scene load_steps=4 format=3]

[ext_resource type="Script" path="res://main.gd" id="1"]
[ext_resource type="Shader" path="res://backdrop.gdshader" id="2"]

[sub_resource type="ShaderMaterial" id="ShaderMaterial_1"]
shader = ExtResource("2")

[node name="Main" type="Node2D"]
script = ExtResource("1")

[node name="Backdrop" type="ColorRect" parent="."]
material = SubResource("ShaderMaterial_1")
offset_right = 480.0
offset_bottom = 720.0

[node name="Glow" type="WorldEnvironment" parent="."]

[node name="Title" type="Label" parent="."]
offset_left = 32.0
offset_top = 392.0
offset_right = 448.0
offset_bottom = 444.0
text = "Describe your game"
horizontal_alignment = 1
theme_override_font_sizes/font_size = 34

[node name="Hint" type="Label" parent="."]
offset_left = 32.0
offset_top = 444.0
offset_right = 448.0
offset_bottom = 472.0
text = "in the chat"
horizontal_alignment = 1
theme_override_font_sizes/font_size = 15
modulate = Color(1, 1, 1, 0.55)
""",

    "main.gd": """extends Node2D

# Creai will replace this with your game. What is here is the floor, not the
# ceiling: a shader backdrop, glow, eased motion and type that varies. Keep
# those when you build over it.

var t := 0.0

func _ready() -> void:
	var env := Environment.new()
	env.background_mode = Environment.BG_CANVAS
	env.glow_enabled = true
	env.glow_intensity = 0.9
	env.glow_bloom = 0.15
	$Glow.environment = env

	# entrances overshoot slightly and settle — never linear
	$Title.modulate.a = 0.0
	$Title.position.y += 14.0
	var tw := create_tween().set_parallel(true)
	tw.tween_property($Title, "modulate:a", 1.0, 0.5).set_trans(Tween.TRANS_CUBIC)
	tw.tween_property($Title, "position:y", $Title.position.y - 14.0, 0.6) \
		.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)

func _process(delta: float) -> void:
	t += delta
	queue_redraw()

func _draw() -> void:
	var y := 250.0 + sin(t * 1.6) * 18.0
	# emissive against a dark field, so the glow has something to catch
	draw_circle(Vector2(240, y), 30.0, Color(0.37, 0.98, 0.70))
	draw_circle(Vector2(240, y), 44.0, Color(0.37, 0.98, 0.70, 0.12))
"""
}


# ---------------------------------------------------------------- files

def check(path: str, content: str) -> None:
    if not PATH.match(path or "") or ".." in path:
        raise GameError(f"'{path}' isn't an allowed file name "
                        "(lowercase folders, .godot .gd .tscn .tres .cfg .gdshader .json .md .txt .svg)")
    if len(content.encode()) > MAX_FILE:
        raise GameError(f"{path} is too large ({len(content)} characters; limit {MAX_FILE})")
    if path.endswith(".gd") and re.search(r"\bOS\s*\.\s*execute\b|\bClassDB\b|"
                                          r"\bHTTPClient\b|\bHTTPRequest\b", content):
        raise GameError(f"{path} uses an API games can't use here "
                        "(running programs or calling other sites). Keep the game self-contained.")


async def files(project_id: int, org_id: int) -> dict[str, str]:
    async with conn() as c:
        rows = await c.fetch(
            "SELECT path, content FROM project_files WHERE project_id=$1 AND org_id=$2 ORDER BY path",
            project_id, org_id)
    return {r["path"]: r["content"] for r in rows}


async def write(project_id: int, org_id: int, changes: dict[str, str]) -> list[str]:
    current = await files(project_id, org_id)
    for path, content in changes.items():
        check(path, content)
    merged = {**current, **changes}
    if len(merged) > MAX_FILES:
        raise GameError(f"a game can have at most {MAX_FILES} files")
    if sum(len(v.encode()) for v in merged.values()) > MAX_TOTAL:
        raise GameError("the game's source is too large; simplify or split it")
    async with conn() as c:
        async with c.transaction():
            for path, content in changes.items():
                await c.execute(
                    """INSERT INTO project_files (org_id, project_id, path, content)
                       VALUES ($1,$2,$3,$4)
                       ON CONFLICT (project_id, path) DO UPDATE
                         SET content=EXCLUDED.content, updated_at=now()""",
                    org_id, project_id, path, content)
    return sorted(changes)


async def delete(project_id: int, org_id: int, path: str) -> bool:
    if path == ENTRY:
        raise GameError("project.godot describes the whole game and can't be deleted")
    async with conn() as c:
        r = await c.execute("DELETE FROM project_files WHERE project_id=$1 AND org_id=$2 AND path=$3",
                            project_id, org_id, path)
    return r.endswith("1")


async def seed(project_id: int, org_id: int) -> None:
    if not await files(project_id, org_id):
        await write(project_id, org_id, STARTER)


# ---------------------------------------------------------------- review

MAIN_SCENE = re.compile(r'run/main_scene\s*=\s*"res://([^"]+)"')
EXT_RESOURCE = re.compile(r'\[ext_resource[^\]]*\bpath\s*=\s*"res://([^"]+)"')
PRELOAD = re.compile(r'\b(?:preload|load)\s*\(\s*"res://([^"]+)"\s*\)')
SCENE_HEADER = re.compile(r'^\[gd_scene\b')


# Emoji are worse in a game than on a page: Godot draws them through the project
# font, so they arrive as tofu boxes or flat monochrome, and they can't be styled,
# animated or atlased like the rest of the art.
EMOJI = re.compile("[\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF"
                   "\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u2049\u203C]")


def review(game_files: dict[str, str]) -> dict:
    """What a careful engineer would check before spending a build on this.

    Every problem here is one the export would otherwise discover slowly and report
    obscurely, so catching them costs the person nothing instead of a build minute.
    """
    problems: list[str] = []
    notes: list[str] = []

    for path, content in sorted(game_files.items()):
        hit = EMOJI.search(content or "")
        if hit:
            problems.append(f"{path} uses {hit.group(0)} as an icon or label. Godot draws emoji "
                            "through the project font, so they come out as boxes or flat grey. "
                            "Draw the shape with a Polygon2D, a Sprite2D or a TextureRect instead.")
            break

    project = game_files.get(ENTRY)
    if not project:
        return {"ok": False, "problems": ["project.godot is missing — every game needs one"], "notes": []}

    m = MAIN_SCENE.search(project)
    if not m:
        problems.append('project.godot has no run/main_scene — the export has nothing to open')
    elif m.group(1) not in game_files:
        problems.append(f'run/main_scene points at res://{m.group(1)}, which no file provides')

    if "config/features" not in project:
        notes.append('project.godot has no config/features; add PackedStringArray("4.5", "GL Compatibility")')
    if 'renderer/rendering_method="gl_compatibility"' not in project:
        notes.append("the web export is most reliable with the GL Compatibility renderer")

    for path, content in game_files.items():
        if path.endswith(".tscn") and not SCENE_HEADER.match(content.lstrip()):
            problems.append(f"{path} doesn't start with a [gd_scene] header")
        for ref in set(EXT_RESOURCE.findall(content)) | set(PRELOAD.findall(content)):
            if ref not in game_files:
                problems.append(f"{path} references res://{ref}, which doesn't exist")
        if path.endswith(".gd"):
            problems.extend(_script_problems(path, content))

    scripts = {p for p in game_files if p.endswith(".gd")}
    used = set()
    for content in game_files.values():
        used |= set(EXT_RESOURCE.findall(content)) | set(PRELOAD.findall(content))
    for orphan in sorted(scripts - used - {"main.gd"}):
        notes.append(f"{orphan} isn't attached to any scene or loaded anywhere")

    notes.extend(_craft(game_files))
    return {"ok": not problems, "problems": problems[:20], "notes": notes[:10]}


def _craft(files: dict[str, str]) -> list[str]:
    """What separates a game that exports from a game worth playing.

    These are notes, never problems: none of them stops a build, and a checker
    that blocks a release over a missing vignette would be ignored within a week.
    But a game that trips several of them will look like a prototype however
    correct the code is, and nothing else in the pipeline can see that.
    """
    source = "\n".join(v or "" for k, v in files.items()
                       if k.endswith((".gd", ".tscn", ".godot", ".gdshader")))
    notes = []

    if not any(k.endswith(".gdshader") for k in files):
        notes.append("No shader. A full-screen ColorRect with twenty lines of .gdshader — a "
                     "gradient that shifts with the score, a vignette that tightens under "
                     "pressure — is the largest visual upgrade available without art files.")
    if "WorldEnvironment" not in source and "glow_enabled" not in source:
        notes.append("No WorldEnvironment with glow. Emissive colour on simple shapes is what "
                     "stops flat rectangles looking like flat rectangles.")
    if "create_tween" in source or "Tween" in source:
        if "TRANS_" not in source:
            notes.append("Tweens with no transition curve run linear, which is the clearest "
                         "signal nobody directed the motion. TRANS_CUBIC or TRANS_BACK with "
                         "EASE_OUT costs one line.")
    if "position_smoothing" not in source and "Camera2D" in source:
        notes.append("The camera is locked. position_smoothing_enabled and a small drift "
                     "toward where the player is heading reads as deliberate.")
    sizes = set(re.findall(r"font_size\s*=\s*(\d+)", source))
    if len(sizes) == 1:
        notes.append("Every label is the same size. Varying type hard — a score at 64 and a "
                     "hint at 14 — is the difference between designed and placeholder.")
    if "shake" not in source.lower() and "Camera2D" in source:
        notes.append("Nothing shakes, flashes or squashes on impact. A few frames of response "
                     "to a hit is most of what 'feel' means.")
    return notes


def _script_problems(path: str, src: str) -> list[str]:
    out = []
    body = [ln for ln in src.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if body and not re.match(r"^\s*(@\w+|extends\b|class_name\b)", body[0]):
        out.append(f"{path} should start with 'extends' (or a class_name/annotation above it)")
    mixed = any(ln.startswith("\t") for ln in src.splitlines()) and \
        any(re.match(r"^ {2,}\S", ln) for ln in src.splitlines())
    if mixed:
        out.append(f"{path} mixes tabs and spaces for indentation; GDScript wants one or the other")
    for i, ln in enumerate(src.splitlines(), 1):
        if re.search(r"\bfunc\s+\w+\s*\([^)]*\)\s*(->\s*[\w\[\], ]+)?\s*$", ln) and not ln.rstrip().endswith(":"):
            out.append(f"{path} line {i}: a func declaration needs a trailing ':'")
            break
    return out


# ---------------------------------------------------------------- builds

def threads_allowed() -> bool:
    """Threaded exports need cross-origin isolation, which needs a host of their own.

    COOP and COEP are both ignored on an opaque origin, and a published game must not
    share an origin with the app — session tokens live in the app's browser storage.
    """
    return bool(settings.games_url)


def credits_for(seconds: float) -> int:
    minutes = max(1, int(seconds // 60) + (1 if seconds % 60 else 0))
    return max(MIN_BUILD_CREDITS, minutes * CREDITS_PER_MINUTE)


async def estimate(project_id: int, org_id: int) -> int:
    """What to reserve before starting. Bigger projects take longer to import."""
    fs = await files(project_id, org_id)
    total = sum(len(v.encode()) for v in fs.values())
    return credits_for(60 if total < 200_000 else 120)


async def start(project_id: int, org_id: int, user_id: int | None, *,
                threads: bool = False) -> dict:
    """Create the job row. The caller runs it; this is what the person sees first."""
    if threads and not threads_allowed():
        raise GameError("threaded builds need a games host configured; this game will build "
                        "single-threaded, which runs everywhere")
    fs = await files(project_id, org_id)
    found = review(fs)
    if not found["ok"]:
        raise GameError("the project isn't exportable yet: " + "; ".join(found["problems"][:3]))
    async with conn() as c:
        running = await c.fetchval(
            """SELECT id FROM game_builds WHERE project_id=$1
               AND state NOT IN ('done','failed') LIMIT 1""", project_id)
        if running:
            raise GameError("this game is already building")
        row = await c.fetchrow(
            """INSERT INTO game_builds (org_id, project_id, created_by, state, threads)
               VALUES ($1,$2,$3,'queued',$4) RETURNING id, created_at""",
            org_id, project_id, user_id, threads)
    return {"id": row["id"], "state": "queued", "progress": PROGRESS["queued"],
            "threads": threads, "at": row["created_at"].isoformat()}


async def _set(build_id: int, state: str, **fields) -> None:
    sets = ["state=$2", "progress=$3"]
    args: list = [build_id, state, PROGRESS[state]]
    for k, v in fields.items():
        args.append(v)
        sets.append(f"{k}=${len(args)}")
    if state in ("done", "failed"):
        sets.append("finished_at=now()")
    async with conn() as c:
        await c.execute(f"UPDATE game_builds SET {', '.join(sets)} WHERE id=$1", *args)


async def run(build_id: int, project_id: int, org_id: int) -> None:
    """Export the game, charge for the time it took, store the result.

    Failures are told plainly and cost nothing if the builder itself was unreachable:
    the person shouldn't pay for our service being down, only for engine time they used.
    """
    from . import billing, builds
    started = time.monotonic()
    threads = False
    try:
        async with conn() as c:
            threads = await c.fetchval("SELECT threads FROM game_builds WHERE id=$1", build_id)
        fs = await files(project_id, org_id)
        await _set(build_id, "importing")
        out = await builds.export_web(fs, threads=bool(threads))
        await _set(build_id, "packaging", log=out.get("log", "")[-4000:])
        total = await _store(org_id, project_id, build_id, out["files"])
        seconds = time.monotonic() - started
        credits = credits_for(seconds)
        await billing.spend(org_id, None, credits, "build", f"build:{build_id}",
                            {"project_id": project_id, "seconds": round(seconds),
                             "bytes": total, "threads": bool(threads)})
        await _set(build_id, "done", seconds=round(seconds), bytes=total, credits=credits)
    except builds.BuildError as exc:
        seconds = time.monotonic() - started
        charged = 0
        if "wasn't reachable" not in str(exc) and "isn't switched on" not in str(exc):
            charged = credits_for(seconds)
            await billing.spend(org_id, None, charged, "build", f"build:{build_id}",
                                {"project_id": project_id, "failed": True})
        await _set(build_id, "failed", error=str(exc)[:2000], seconds=round(seconds), credits=charged)
    except Exception as exc:                                  # our fault: never charged
        log.exception("build %s failed unexpectedly", build_id)
        await _set(build_id, "failed", error=f"the build stopped unexpectedly: {exc}"[:2000])


async def _store(org_id: int, project_id: int, build_id: int, made: dict[str, bytes]) -> int:
    """Put the export in the bucket and record it. Files are named by the build."""
    from . import assets
    prefix = f"games/{org_id}/{project_id}/{build_id}"
    total = 0
    for name, data in made.items():
        total += len(data)
        await assets.put_blob(f"{prefix}/{name}", data, mime_for(name))
    async with conn() as c:
        await c.execute("UPDATE game_builds SET prefix=$2, names=$3 WHERE id=$1",
                        build_id, prefix, sorted(made))
    return total


MIMES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".wasm": "application/wasm", ".pck": "application/octet-stream",
         ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
         ".json": "application/json", ".webmanifest": "application/manifest+json",
         ".worker.js": "text/javascript; charset=utf-8", ".side.wasm": "application/wasm"}


def mime_for(name: str) -> str:
    for suffix, mime in MIMES.items():
        if name.endswith(suffix):
            return mime
    return "application/octet-stream"


async def status(build_id: int, project_id: int, org_id: int) -> dict | None:
    async with conn() as c:
        r = await c.fetchrow(
            """SELECT id, state, progress, error, seconds, bytes, credits, threads, created_at
               FROM game_builds WHERE id=$1 AND project_id=$2 AND org_id=$3""",
            build_id, project_id, org_id)
    if not r:
        return None
    return {"id": r["id"], "state": r["state"], "progress": r["progress"],
            "error": r["error"], "seconds": r["seconds"], "bytes": r["bytes"],
            "credits": r["credits"], "threads": r["threads"],
            "at": r["created_at"].isoformat()}


async def latest(project_id: int, org_id: int) -> dict | None:
    async with conn() as c:
        r = await c.fetchval(
            "SELECT id FROM game_builds WHERE project_id=$1 AND org_id=$2 ORDER BY id DESC LIMIT 1",
            project_id, org_id)
    return await status(r, project_id, org_id) if r else None
