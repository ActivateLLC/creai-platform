"""
Creai builder — turns a Godot project into something that plays in a browser.

The agent writes ordinary Godot files (project.godot, .tscn scenes, .gd scripts) and
posts them here. This service writes them to a scratch folder, adds a web export
preset, runs Godot headless, and returns the exported files. No GPU, no editor, no
install for the person: they get a link.

Guards: a project is bounded in files and bytes, paths can't escape the folder, the
export runs with a timeout in a throwaway directory, and one shared token gates the
service. Godot runs with --headless and no network use of its own.
"""

import asyncio
import base64
import os
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

import video

TOKEN = os.getenv("BUILD_TOKEN", "")
GODOT = os.getenv("GODOT_BIN", "godot")
VERSION = os.getenv("GODOT_VERSION", "4.5.1")
TIMEOUT = int(os.getenv("BUILD_TIMEOUT", "240"))
MAX_FILES = 200
MAX_BYTES = 40 * 1024 * 1024
SAFE = re.compile(r"^(?!\.)(?:[A-Za-z0-9_][A-Za-z0-9._-]{0,60}/){0,6}[A-Za-z0-9_][A-Za-z0-9._-]{0,80}$")
ALLOWED_SUFFIX = {".godot", ".gd", ".tscn", ".tres", ".cfg", ".import", ".json", ".txt", ".md",
                  ".png", ".jpg", ".jpeg", ".webp", ".svg", ".ogg", ".wav", ".mp3", ".glb", ".gltf",
                  ".ttf", ".otf", ".shader", ".gdshader"}

# include_filter/exclude_filter are read unconditionally by the exporter: leaving
# them out works but logs an error on every build, which buries real ones.
PRESET = """[preset.0]
name="Web"
platform="Web"
runnable=true
export_filter="all_resources"
include_filter=""
exclude_filter=""
export_path="build/index.html"

[preset.0.options]
variant/extensions_support=false
variant/thread_support={threads}
vram_texture_compression/for_desktop=false
vram_texture_compression/for_mobile=false
html/export_icon=true
html/canvas_resize_policy=2
html/focus_canvas_on_start=true
progressive_web_app/enabled=false
"""


def preset(threads: bool) -> str:
    """Threaded exports are faster but only run on a cross-origin-isolated page.
    The platform decides which it can serve; the builder just does as it's told."""
    return PRESET.format(threads="true" if threads else "false")


app = FastAPI(title="Creai builder")


def check(token: str | None):
    if not TOKEN or token != TOKEN:
        raise HTTPException(401, "bad token")


class BuildIn(BaseModel):
    files: dict[str, str] = Field(description="path -> text, or base64: prefixed for binary")
    name: str = Field("game", max_length=60)
    threads: bool = Field(False, description="threaded export; needs a cross-origin-isolated page")


def _write(root: Path, files: dict[str, str]) -> int:
    total = 0
    if len(files) > MAX_FILES:
        raise HTTPException(400, f"too many files (limit {MAX_FILES})")
    for path, content in files.items():
        if not SAFE.match(path or "") or ".." in path:
            raise HTTPException(400, f"unsafe path: {path}")
        if Path(path).suffix.lower() not in ALLOWED_SUFFIX:
            raise HTTPException(400, f"unsupported file type: {path}")
        data = base64.b64decode(content[7:]) if content.startswith("base64:") else content.encode()
        total += len(data)
        if total > MAX_BYTES:
            raise HTTPException(413, "project is too large")
        out = (root / path).resolve()
        if not str(out).startswith(str(root.resolve())):
            raise HTTPException(400, f"unsafe path: {path}")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
    if not (root / "project.godot").exists():
        raise HTTPException(400, "project.godot is missing")
    return total


async def _run(args: list[str], cwd: Path) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, "the export took too long")
    return proc.returncode or 0, out.decode(errors="replace")[-4000:]


@app.get("/health")
async def health():
    code, out = await _run([GODOT, "--headless", "--version"], Path("/tmp"))
    have_ff = shutil.which("ffmpeg") is not None
    return {"ok": code == 0, "godot": out.strip()[:40], "video": have_ff, "version": VERSION}


class Clip(BaseModel):
    """One scene: what to show, for how long, and what is said over it."""
    id: str = ""
    kind: str = "still"                 # still | clip | card
    data: str | None = None             # base64 picture or footage
    voice: str | None = None            # base64 mp3 of the line
    caption: str = ""
    seconds: float = 3.0
    fit: str = "cover"
    focus: float = 0.5
    zoom_from: float = 1.0
    zoom_to: float = 1.09
    title: str = ""
    subtitle: str = ""


class VideoIn(BaseModel):
    shape: str = "vertical"
    scenes: list[Clip] = []
    music: str | None = None            # base64 bed


def _stash(work: Path, name: str, b64: str | None) -> str | None:
    """Assets arrive as base64 so the builder needs no credentials of its own."""
    if not b64:
        return None
    p = work / name
    p.write_bytes(base64.b64decode(b64))
    return str(p)


@app.post("/render/video")
async def render_video(body: VideoIn, x_build_token: str | None = Header(None)):
    """Cut a film from scenes that already have their pictures and their voice."""
    check(x_build_token)
    if not shutil.which("ffmpeg"):
        raise HTTPException(503, "this builder has no ffmpeg")
    work = Path(tempfile.mkdtemp(prefix="creai-vid-"))
    try:
        scenes = []
        for i, sc in enumerate(body.scenes):
            scenes.append({
                "id": sc.id or str(i), "kind": sc.kind, "caption": sc.caption,
                "seconds": sc.seconds, "fit": sc.fit, "focus": sc.focus,
                "zoom_from": sc.zoom_from, "zoom_to": sc.zoom_to,
                "title": sc.title, "subtitle": sc.subtitle,
                "file": _stash(work, f"src{i:03d}" + (".mp4" if sc.kind == "clip" else ".jpg"),
                               sc.data),
                "voice": _stash(work, f"vo{i:03d}.mp3", sc.voice)})
        manifest = {"shape": body.shape, "scenes": scenes,
                    "music": _stash(work, "bed.mp3", body.music)}
        try:
            out = video.render(manifest, work)
        except video.RenderError as exc:
            raise HTTPException(422, f"the render failed: {exc}")
        shot = video.poster(out, work / "poster.jpg")
        facts = video.probe(out)
        return {"ok": True, "mp4": base64.b64encode(out.read_bytes()).decode(),
                "poster": base64.b64encode(shot.read_bytes()).decode(), **facts}
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.post("/export/web")
async def export_web(body: BuildIn, x_build_token: str | None = Header(None)):
    check(x_build_token)
    work = Path(tempfile.mkdtemp(prefix="creai-"))
    try:
        _write(work, body.files)
        (work / "export_presets.cfg").write_text(preset(body.threads))
        (work / "build").mkdir(exist_ok=True)
        # import assets first: a cold project has no .godot cache, and export needs one
        await _run([GODOT, "--headless", "--import"], work)
        code, log = await _run([GODOT, "--headless", "--export-release", "Web", "build/index.html"], work)
        made = sorted(p for p in (work / "build").iterdir() if p.is_file())
        if not made or not (work / "build" / "index.html").exists():
            raise HTTPException(422, f"the export produced nothing. Godot said:\n{log[-1500:]}")
        files, total = {}, 0
        for p in made:
            data = p.read_bytes()
            total += len(data)
            files[p.name] = base64.b64encode(data).decode()
        return {"ok": code == 0, "files": files, "bytes": total,
                "threads": body.threads, "log": log[-1500:]}
    finally:
        shutil.rmtree(work, ignore_errors=True)
