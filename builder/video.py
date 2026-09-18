"""
Turning a manifest into a film.

This is the ffmpeg work that made the Short, generalised. The API decides what a
video is — writes the scenes, buys the pictures, speaks the lines — and hands
this a manifest of local files with timings. Nothing here calls a model or knows
what a brand is; it cuts, lays audio under, burns captions and encodes.

Two decisions worth keeping:

Scene length follows the voice, not the other way round. A scene is as long as
its line takes to say, plus a beat. Fitting speech into a fixed slot is how ads
end up sounding rushed at the end of every sentence.

Captions are burned in, always. Most of a feed is watched on mute, and a caption
that arrives as a separate track arrives too late to matter.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
SHAPES = {"vertical": (1080, 1920), "square": (1080, 1080), "wide": (1920, 1080)}
BG = "0x0B0F14"


class RenderError(RuntimeError):
    pass


def _run(args: list[str]) -> None:
    done = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise RenderError(done.stderr.strip()[-400:] or "ffmpeg failed")


def _esc(t: str) -> str:
    return (t.replace("\\", "").replace(":", "\\:")
             .replace("'", "\u2019").replace("%", "\\%").replace("—", "-"))


def _wrap(t: str, width: int) -> str:
    words, out, line = (t or "").split(), [], ""
    for w in words:
        if len(line) + len(w) + 1 > width and line:
            out.append(line); line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return "\n".join(out)


def _caption(text: str, dur: float, w: int, h: int) -> str:
    """Low in the frame, boxed, and large enough to read at arm's length."""
    if not text:
        return ""
    size = max(40, int(w * 0.066))
    # DejaVu Bold runs about 0.62 em per character; leave a margin either side so
    # a long line wraps instead of running off the frame.
    width = max(12, int((w * 0.86) / (size * 0.62)))
    y = int(h * 0.68)
    return (f",drawtext=fontfile={FONT}:text='{_esc(_wrap(text, width))}'"
            f":fontcolor=#F4F1EA:fontsize={size}:x=(w-text_w)/2:y={y}:line_spacing=14"
            f":box=1:boxcolor=0x0B0F14@0.78:boxborderw={int(size*0.44)}"
            f":enable='between(t,0.05,{dur})'")


def _scene(sc: dict, out: Path, w: int, h: int) -> None:
    """One scene: a still given motion, a clip cropped to shape, or a card."""
    dur = float(sc.get("seconds") or 3)
    cap = _caption(sc.get("caption") or "", dur, w, h)
    src = sc.get("file")
    kind = sc.get("kind", "still")

    if kind == "card":
        title = _esc(sc.get("title") or "")
        sub = _esc(sc.get("subtitle") or "")
        vf = (f"drawtext=fontfile={FONT}:text='{title}':fontcolor=#F4F1EA"
              f":fontsize={int(w*0.16)}:x=(w-text_w)/2:y=(h-text_h)/2-{int(h*0.06)}")
        if sub:
            vf += (f",drawtext=fontfile={FONT}:text='{sub}':fontcolor=#5FC6A0"
                   f":fontsize={int(w*0.056)}:x=(w-text_w)/2:y=(h/2)+{int(h*0.04)}")
        _run(["-f", "lavfi", "-i", f"color=c={BG}:s={w}x{h}:d={dur}:r=30",
              "-vf", vf, "-pix_fmt", "yuv420p", str(out)])
        return

    if not src or not Path(src).exists():
        raise RenderError(f"scene {sc.get('id','?')} has no file to show")

    if kind == "clip":
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}{cap}")
        _run(["-stream_loop", "-1", "-i", src, "-t", str(dur), "-vf", vf, "-an",
              "-pix_fmt", "yuv420p", "-r", "30", str(out)])
        return

    # a still, given a slow push so it does not sit dead on screen
    frames = max(2, int(dur * 30))
    fit = sc.get("fit", "cover")
    base = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
            if fit == "cover" else
            f"scale={w-80}:-2:flags=lanczos,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:{BG}")
    z0, z1 = float(sc.get("zoom_from", 1.0)), float(sc.get("zoom_to", 1.09))
    cy = float(sc.get("focus", 0.5))
    zoom = (f"zoompan=z='{z0}+({z1}-{z0})*on/{frames}':x='iw/2-(iw/zoom/2)'"
            f":y='ih*{cy}-(ih/zoom/2)':d={frames}:s={w}x{h}:fps=30")
    _run(["-loop", "1", "-i", src, "-t", str(dur), "-vf", f"{base},{zoom}{cap}",
          "-pix_fmt", "yuv420p", "-r", "30", str(out)])


def render(manifest: dict, work: Path) -> Path:
    """Manifest in, mp4 out.

    manifest = {shape, scenes: [{kind, file, caption, seconds, voice}], music, sfx}
    """
    w, h = SHAPES.get(manifest.get("shape", "vertical"), SHAPES["vertical"])
    scenes = manifest.get("scenes") or []
    if not scenes:
        raise RenderError("nothing to render")

    parts, tracks = [], []
    for i, sc in enumerate(scenes):
        seg = work / f"seg{i:03d}.mp4"
        _scene(sc, seg, w, h)
        parts.append(seg)

        # Each scene gets audio of exactly its own length: the line, padded with
        # silence to fill the scene, or silence alone when nobody speaks. Without
        # this a scene with no line pulls every later line out of sync, and the
        # film ends the moment the talking stops.
        dur = float(sc.get("seconds") or 3)
        a = work / f"aud{i:03d}.m4a"
        line = sc.get("voice")
        if line and Path(line).exists():
            _run(["-i", line, "-af", f"apad=whole_dur={dur}", "-t", str(dur),
                  "-c:a", "aac", "-b:a", "160k", str(a)])
        else:
            _run(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                  "-t", str(dur), "-c:a", "aac", "-b:a", "160k", str(a)])
        tracks.append(a)

    (work / "v.txt").write_text("".join(f"file '{p}'\n" for p in parts))
    silent = work / "silent.mp4"
    _run(["-f", "concat", "-safe", "0", "-i", str(work / "v.txt"), "-c", "copy", str(silent)])

    (work / "a.txt").write_text("".join(f"file '{p}'\n" for p in tracks))
    speech = work / "speech.m4a"
    _run(["-f", "concat", "-safe", "0", "-i", str(work / "a.txt"), "-c", "copy", str(speech)])

    track = speech
    bed = manifest.get("music")
    if bed and Path(bed).exists():
        # The bed ducks whenever someone is speaking, which is what separates a
        # bed from noise. Mixed before the video so a failure here costs nothing.
        mixed = work / "mixed.m4a"
        _run(["-i", str(speech), "-stream_loop", "-1", "-i", bed, "-filter_complex",
              "[1:a]volume=-22dB[b];"
              "[b][0:a]sidechaincompress=threshold=0.03:ratio=8:attack=8:release=320[d];"
              "[0:a][d]amix=inputs=2:duration=first:dropout_transition=0[m]",
              "-map", "[m]", "-c:a", "aac", "-b:a", "160k", "-shortest", str(mixed)])
        track = mixed

    out = work / "out.mp4"
    _run(["-i", str(silent), "-i", str(track), "-c:v", "copy", "-c:a", "aac",
          "-b:a", "160k", "-shortest", str(out)])
    return out


def poster(video: Path, out: Path, at: float = 0.4) -> Path:
    """A frame to show before anyone presses play."""
    _run(["-ss", str(at), "-i", str(video), "-frames:v", "1", str(out)])
    return out


def probe(video: Path) -> dict:
    done = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration,size",
         "-show_entries", "stream=width,height", "-of", "json", str(video)],
        capture_output=True, text=True)
    try:
        d = json.loads(done.stdout)
        fmt = d.get("format", {})
        st = (d.get("streams") or [{}])[0]
        return {"seconds": round(float(fmt.get("duration", 0)), 2),
                "bytes": int(fmt.get("size", 0)),
                "width": st.get("width"), "height": st.get("height")}
    except (ValueError, KeyError):
        return {}
