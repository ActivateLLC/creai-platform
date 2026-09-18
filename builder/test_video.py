"""The render worker, checked without needing a model or a network."""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import video  # noqa: E402

ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _picture(path: Path, w=1200, h=800):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"color=c=#204050:s={w}x{h}", "-frames:v", "1", str(path)], check=True)
    return path


def _voice(path: Path, seconds=2):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"sine=frequency=300:duration={seconds}",
                    "-c:a", "libmp3lame", str(path)], check=True)
    return path


def test_a_long_caption_wraps_instead_of_running_off_the_frame():
    """The first version estimated character width too generously and captions
    ran past the edge — invisible in code, obvious on a phone."""
    line = video._caption("They sign in and see their own invoices today", 3.0, 1080, 1920)
    wrapped = line.split("text='")[1].split("'")[0]
    assert "\n" in wrapped
    assert max(len(p) for p in wrapped.split("\n")) <= 24


def test_a_caption_is_only_drawn_when_there_is_something_to_say():
    assert video._caption("", 3.0, 1080, 1920) == ""


def test_text_that_would_break_the_filter_is_escaped():
    out = video._caption("Costs: 50% off — don't", 2.0, 1080, 1920)
    assert "\\:" in out and "\\%" in out and "'" not in out.split("text='")[1].split("':")[0][1:]


@ffmpeg
def test_it_renders_stills_clips_and_cards_into_one_film():
    work = Path(tempfile.mkdtemp())
    try:
        pic = _picture(work / "a.jpg")
        vo = _voice(work / "a.mp3", 2)
        out = video.render({"shape": "vertical", "scenes": [
            {"id": "1", "kind": "still", "file": str(pic), "caption": "One", "seconds": 2,
             "voice": str(vo)},
            {"id": "2", "kind": "card", "title": "Creai", "subtitle": "creai.dev",
             "seconds": 1.5}]}, work)
        facts = video.probe(out)
        assert facts["width"] == 1080 and facts["height"] == 1920
        assert 3.0 <= facts["seconds"] <= 4.5
    finally:
        shutil.rmtree(work, ignore_errors=True)


@ffmpeg
def test_a_bed_is_mixed_under_the_voice_rather_than_over_it():
    work = Path(tempfile.mkdtemp())
    try:
        pic, vo, bed = _picture(work / "a.jpg"), _voice(work / "v.mp3", 3), _voice(work / "b.mp3", 9)
        out = video.render({"shape": "square", "music": str(bed), "scenes": [
            {"id": "1", "kind": "still", "file": str(pic), "caption": "With a bed",
             "seconds": 3, "voice": str(vo)}]}, work)
        codec = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                                "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(out)],
                               capture_output=True, text=True).stdout.strip()
        assert codec == "aac"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@ffmpeg
def test_a_scene_with_no_picture_says_which_scene():
    work = Path(tempfile.mkdtemp())
    try:
        with pytest.raises(video.RenderError) as e:
            video.render({"shape": "vertical",
                          "scenes": [{"id": "third", "kind": "still", "seconds": 2}]}, work)
        assert "third" in str(e.value)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_an_empty_manifest_is_refused():
    with pytest.raises(video.RenderError):
        video.render({"shape": "vertical", "scenes": []}, Path(tempfile.mkdtemp()))


@ffmpeg
def test_a_silent_scene_does_not_pull_later_lines_out_of_sync():
    """Concatenating only the scenes that speak made every line after a silent
    scene land early, and ended the film when the talking stopped."""
    work = Path(tempfile.mkdtemp())
    try:
        pic, vo = _picture(work / "a.jpg"), _voice(work / "v.mp3", 1.5)
        out = video.render({"shape": "vertical", "scenes": [
            {"id": "1", "kind": "still", "file": str(pic), "seconds": 3.0},          # silent
            {"id": "2", "kind": "still", "file": str(pic), "seconds": 3.0,
             "voice": str(vo)},
            {"id": "3", "kind": "card", "title": "Creai", "seconds": 2.0}]}, work)   # silent
        facts = video.probe(out)
        # the whole film survives, not just the part with a voice in it
        assert 7.5 <= facts["seconds"] <= 8.5, facts
    finally:
        shutil.rmtree(work, ignore_errors=True)
