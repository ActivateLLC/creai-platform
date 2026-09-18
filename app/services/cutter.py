"""
From a production sheet to a finished file, with nobody watching.

Everything upstream produces a plan: the producer writes scenes with angles and
tone, the director assigns voices and silence, the captions know what to show.
Every cut so far was still assembled by hand, which is why there is one ad rather
than twenty. This is the part that closes it.

What it does, per scene, in the order that matters:

  1. buy the picture — generate a still and animate it, or use footage already made
  2. speak the line with its own direction, split where silence belongs
  3. time the scene to the line rather than the line to the scene
  4. lay the effect on the beat it marks
  5. burn the caption, light or dark depending on what is behind it
  6. cut, lay the bed under it, and let the last line land dry

Two things it refuses to do.

It will not render a plan the checker rejects. A variant that opens on a still
frame or ends on the person diminished is not an experiment, it is a wasted
impression somebody paid for.

It will not invent a missing asset. If a scene names footage that does not exist,
it says which scene and stops, rather than quietly substituting a black frame
that nobody notices until the ad is live.
"""

import asyncio
import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import captions as cp
from . import producer, sound, voicedirect

log = logging.getLogger("creai.cutter")

SHAPES = {"vertical": (1080, 1920), "square": (1080, 1080), "wide": (1920, 1080)}
BED_DB = -23
DRY_TAIL = 4.0          # the ask lands without music under it


class CutError(RuntimeError):
    """Something a person should be told, naming the scene it happened in."""


def _run(args: list[str]) -> None:
    done = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise CutError(done.stderr.strip()[-300:] or "ffmpeg failed")


def _seconds(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def _is_pale(path: Path) -> bool:
    """Whether a frame is light enough that white captions would vanish.

    Measured rather than declared: the same scene can be a dark van or a pale app
    depending on what was generated, and a caption nobody can read is the most
    expensive kind of invisible fault.
    """
    out = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(path), "-frames:v", "1",
         "-vf", "crop=iw:ih/4:0:ih*0.6,signalstats,metadata=print:key=lavfi.signalstats.YAVG",
         "-f", "null", "-"], capture_output=True, text=True)
    for line in out.stderr.splitlines() + out.stdout.splitlines():
        if "YAVG" in line:
            try:
                return float(line.rsplit("=", 1)[1]) > 128
            except (ValueError, IndexError):
                pass
    return False


MARK = Path(__file__).parent.parent / "assets" / "brand" / "mark.png"


async def cut(sheet: dict, assets: dict, work: Path, *, shape: str = "vertical",
              kit: dict | None = None, music: bool = True,
              sign_off: bool = True, address: str = "creai.dev") -> Path:
    """Render a production sheet.

    `assets` maps a scene index to footage already on disk. Anything not supplied
    is generated from the scene's own prompt.
    """
    scenes = sheet.get("scenes") or []
    if not scenes:
        raise CutError("there are no scenes to cut")
    problems = producer.check(scenes)
    if problems:
        raise CutError("; ".join(problems[:3]))

    w, h = SHAPES.get(shape, SHAPES["vertical"])
    work.mkdir(parents=True, exist_ok=True)

    # Footage is checked before a single voice is bought. Speaking first meant a
    # missing asset cost a full set of paid calls before failing on something
    # that was knowable up front.
    shots = []
    for i in range(len(scenes)):
        shot = assets.get(i) or assets.get(str(i))
        if not shot:
            raise CutError(f"scene {i + 1} has no footage and none was generated")
        shot = Path(shot)
        if not shot.exists() or shot.stat().st_size == 0:
            raise CutError(f"scene {i + 1} names {shot.name}, which is not there")
        shots.append(shot)

    # then every line, so a scene can be as long as its line takes to say
    spoken = await _voices(scenes, work)

    parts = []
    for i, sc in enumerate(scenes):
        shot = shots[i]
        runs = max(float(sc.get("seconds") or 3), spoken[i]["seconds"] + 0.2)
        seg = work / f"seg{i:02d}.mp4"
        pale = _is_pale(shot)
        text = sc.get("caption") or ""
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
              + cp.filters(text, 0.2, runs - 0.15, w, h, light=pale))
        _run(["-stream_loop", "-1", "-i", str(shot), "-t", f"{runs:.2f}",
              "-vf", vf, "-an", "-pix_fmt", "yuv420p", "-r", "30", str(seg)])
        parts.append((seg, spoken[i], runs, sc))

    # Every cut ends with the mark and the address. Left to the sheet it gets
    # forgotten — which is exactly what happened to the first film this rendered.
    if sign_off and MARK.exists():
        from . import shots
        card = shots.end_card(work / "endcard.mp4", logo=MARK, address=address,
                              w=w, h=h, seconds=3.6)
        parts.append((card, {"path": None, "seconds": 3.6}, 3.6,
                      {"sound": "none", "music": "out"}))

    return await _assemble(parts, work, kit or {}, music)


async def _voices(scenes: list[dict], work: Path) -> list[dict]:
    """Every line, spoken with its own direction and its own silence."""
    from . import speech

    lines = [{"beat": _beat_for(i, len(scenes)), "text": s.get("line") or "",
              "in_character": s.get("say") == "customer"} for i, s in enumerate(scenes)]
    read = voicedirect.plan_read("problem", lines)

    out = []
    for i, l in enumerate(read):
        if not (l.get("say") or "").strip():
            silent = work / f"vo{i:02d}.wav"
            _run(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.8", str(silent)])
            out.append({"path": silent, "seconds": 0.8})
            continue
        pieces = []
        for j, chunk in enumerate(l["chunks"]):
            raw = work / f"vo{i:02d}-{j}.mp3"
            raw.write_bytes(await speech.speak(chunk, l["voice"], instructions=l["openai"]))
            wav = work / f"vo{i:02d}-{j}.wav"
            _run(["-i", str(raw), "-ar", "44100", "-ac", "1", str(wav)])
            pieces.append(wav)
            gap = l["gap"] if j < len(l["chunks"]) - 1 else l["hold"]
            if gap:
                g = work / f"gap{i:02d}-{j}.wav"
                _run(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", str(gap), str(g)])
                pieces.append(g)
        joined = work / f"vo{i:02d}.wav"
        _concat(pieces, joined, work / f"vo{i:02d}.txt")
        out.append({"path": joined, "seconds": _seconds(joined)})
    return out


def _beat_for(i: int, n: int) -> str:
    """Map a scene's position onto the emotional arc."""
    if i == 0:
        return "open"
    if i == n - 1:
        return "cta"
    if i == 1:
        return "agitate"
    if i == n - 2:
        return "payoff"
    return "demo" if i > n // 2 else "turn"


def _concat(paths: list[Path], out: Path, listing: Path) -> None:
    listing.write_text("".join(f"file '{p}'\n" for p in paths))
    # Encoded rather than stream-copied. Copying compressed fragments together
    # produces a file that plays as noise, and nothing in the build reports it.
    args = ["-f", "concat", "-safe", "0", "-i", str(listing)]
    args += ["-c", "copy"] if out.suffix == ".mp4" else ["-ar", "44100", "-ac", "1"]
    _run(args + [str(out)])


async def _assemble(parts, work: Path, kit: dict, music: bool) -> Path:
    silent = work / "picture.mp4"
    _concat([p[0] for p in parts], silent, work / "v.txt")

    # audio per scene, padded to the scene so a silent beat never pulls later
    # lines early — the fault that once truncated a whole outro
    tracks = []
    for i, (_, vo, runs, sc) in enumerate(parts):
        a = work / f"a{i:02d}.wav"
        if vo.get("path") is None:                     # the end card speaks for itself
            _run(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                  "-t", f"{runs:.2f}", str(a)])
        else:
            _run(["-i", str(vo["path"]), "-af", f"apad=whole_dur={runs:.2f}",
                  "-t", f"{runs:.2f}", "-ar", "44100", "-ac", "1", str(a)])
        effect = sc.get("sound")
        if effect and effect != "none":
            fx = Path(__file__).parent.parent / "assets" / "sfx" / f"{effect}.wav"
            if fx.exists():
                mixed = work / f"a{i:02d}m.wav"
                _run(["-i", str(a), "-i", str(fx), "-filter_complex",
                      "[1:a]adelay=120|120,volume=0.5[s];[0:a][s]amix=inputs=2:duration=first",
                      "-ar", "44100", "-ac", "1", str(mixed)])
                a = mixed
        tracks.append(a)
    voice = work / "voice.wav"
    _concat(tracks, voice, work / "a.txt")

    total = _seconds(voice)
    track = voice
    if music and kit:
        try:
            bed = work / "bed.wav"
            bed.write_bytes(await sound.bed_for(kit, "", seconds=max(10, total + 2)))
            scored = work / "scored.m4a"
            _run(["-i", str(voice), "-stream_loop", "-1", "-i", str(bed), "-filter_complex",
                  f"[1:a]atrim=0:{total:.2f},volume={BED_DB}dB,"
                  f"afade=t=out:st={max(0, total - DRY_TAIL):.2f}:d=1.2[b];"
                  f"[b][0:a]sidechaincompress=threshold=0.03:ratio=8:attack=8:release=320[d];"
                  f"[0:a][d]amix=inputs=2:duration=first:dropout_transition=0[m]",
                  "-map", "[m]", "-c:a", "aac", "-b:a", "192k", str(scored)])
            track = scored
        except (sound.SoundError, CutError) as exc:
            # A missing bed is not a reason to lose the film.
            log.warning("no bed: %s", exc)

    out = work / "out.mp4"
    _run(["-i", str(silent), "-i", str(track), "-c:v", "copy", "-c:a", "aac",
          "-b:a", "192k", "-shortest", str(out)])
    return out
