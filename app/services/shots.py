"""
The pieces between a prompt and a shot.

A generated still is not a shot. It is soft where it needs to be sharp, it is
sometimes wrong in ways a prompt cannot fix, and it has no product in it. Three
steps sit between the two, and each was being done by hand:

  upscale   a soft input makes soft video. Animating a 576-wide still and then
            scaling the result to 1080 is upscaling the blur along with the
            picture. Sharpen first, animate second.

  judge     look at the still before spending on motion. Generating video from a
            bad frame costs twenty times what regenerating the frame costs, and
            the fault is always visible in the frame.

  composite the product on a screen inside the scene. A generated phone shows a
            smudge where an interface should be — reliably, because no video
            model renders text. So the real screen is put there afterwards, with
            a perspective transform onto the four corners it should occupy.

And the end card, which had no home and so kept being rebuilt: the mark, the
address, and the reason to act.
"""

import base64
import json
import logging
import subprocess
from pathlib import Path

import httpx

from ..core.config import settings

log = logging.getLogger("creai.shots")

UPSCALERS = {
    # Clarity adds detail and can drift; ESRGAN is faithful and fast. For a face
    # that has to stay the same person between shots, faithful wins.
    "faithful": "fal-ai/esrgan",
    "detailed": "fal-ai/clarity-upscaler",
}

JUDGE_MODEL = "gpt-6-astra"

JUDGE = """You are checking one generated frame before it is animated, which costs about
twenty times more than making the frame again.

Return ONLY JSON: {"use": true|false, "faults": ["..."], "fix": "..."}

Reject it for: hands or fingers that are wrong, faces that are distorted, text or logos
that are garbled, objects merging into each other, a subject facing away when the shot
needs them facing camera, obvious duplication, or lighting that contradicts what was asked.

Do not reject it for style, mood or composition you merely disagree with. `fix` is a short
addition to the original prompt that would avoid the fault — not a new prompt."""


class ShotError(RuntimeError):
    pass


def _fal(model: str, body: dict, timeout: int = 300) -> dict:
    if not settings.fal_key:
        raise ShotError("image tools aren't configured")
    r = httpx.post(f"https://fal.run/{model}", timeout=timeout,
                   headers={"Authorization": f"Key {settings.fal_key}",
                            "Content-Type": "application/json"}, json=body)
    if r.status_code >= 400:
        log.error("%s failed: %s %s", model, r.status_code, r.text[:200])
        raise ShotError(f"{model.rsplit('/', 1)[-1]} failed")
    return r.json()


def upscale(image_url: str, kind: str = "faithful") -> str:
    """A sharper version of the same picture.

    Done before animation, never after: a video model given a soft frame produces
    soft video, and scaling that up afterwards enlarges the blur.
    """
    model = UPSCALERS.get(kind, UPSCALERS["faithful"])
    out = _fal(model, {"image_url": image_url})
    url = (out.get("image") or {}).get("url")
    if not url:
        raise ShotError("no upscaled image came back")
    return url


def judge(image_url: str, wanted: str) -> dict:
    """Whether a still is worth animating, and what to add to the prompt if not."""
    if not settings.openai_key:
        return {"use": True, "faults": [], "fix": "", "note": "not checked"}
    try:
        r = httpx.post("https://api.openai.com/v1/chat/completions", timeout=120,
                       headers={"Authorization": f"Bearer {settings.openai_key}",
                                "Content-Type": "application/json"},
                       json={"model": JUDGE_MODEL, "max_completion_tokens": 600,
                             "response_format": {"type": "json_object"},
                             "messages": [
                                 {"role": "system", "content": JUDGE},
                                 {"role": "user", "content": [
                                     {"type": "text", "text": f"This was asked for: {wanted}"},
                                     {"type": "image_url", "image_url": {"url": image_url}}]}]})
        if r.status_code >= 400:
            return {"use": True, "faults": [], "fix": "", "note": "reviewer unavailable"}
        out = json.loads(r.json()["choices"][0]["message"]["content"])
        return {"use": bool(out.get("use", True)),
                "faults": [str(f)[:120] for f in (out.get("faults") or [])][:5],
                "fix": str(out.get("fix") or "")[:200]}
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
        # A judge that can block a shot when it is merely unreachable is worse
        # than no judge.
        log.warning("judge failed: %s", exc)
        return {"use": True, "faults": [], "fix": "", "note": "not checked"}


def put_screen_on(scene: Path, screen: Path, corners: list[tuple[int, int]], out: Path) -> Path:
    """Place a real screen recording onto a surface inside a scene.

    `corners` are the four points of the screen in the scene, clockwise from top
    left. A generated phone shows a smudge where an interface should be; this
    puts the actual product there instead, which is the only way the software in
    an advertisement is legible.
    """
    if len(corners) != 4:
        raise ShotError("a screen needs four corners")
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = corners
    chain = (f"[1:v]perspective="
             f"x0={x0}:y0={y0}:x1={x1}:y1={y1}:x2={x3}:y2={y3}:x3={x2}:y3={y2}"
             f":sense=destination:eval=init[warp];"
             f"[0:v][warp]overlay=0:0:format=auto[v]")
    done = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(scene), "-i", str(screen),
         "-filter_complex", chain, "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         str(out)], capture_output=True, text=True)
    if done.returncode != 0:
        raise ShotError(done.stderr.strip()[-200:] or "the screen could not be placed")
    return out


def end_card(out: Path, *, logo: Path, address: str = "creai.dev",
             headline: str = "Build yours free",
             note: str = "free to start - nothing goes live until you say so",
             seconds: float = 4.0, w: int = 1080, h: int = 1920,
             font: str | None = None, heavy: str | None = None) -> Path:
    """The last four seconds: the mark, what to do, and where.

    Use the transparent mark. The plated logo carries a dark rounded square that
    shows as a box on anything that is not exactly that grey — which is every
    background except the one it was designed against.
    """
    from . import captions as cp
    font = font or cp.FONT
    heavy = heavy or cp.FONT_HEAVY
    if not logo.exists():
        raise ShotError("the logo is missing")

    vf = ("[1:v]scale=230:-1[lg];[0:v][lg]overlay=(W-w)/2:H/2-500[bg];"
          f"[bg]drawtext=fontfile={heavy}:text='{headline}':fontcolor={cp.INK}"
          f":fontsize=96:x=(w-text_w)/2:y=(h-text_h)/2-80:shadowcolor=black@0.5:shadowy=3,"
          f"drawtext=fontfile={heavy}:text='{address}':fontcolor=#5FC6A0"
          f":fontsize=104:x=(w-text_w)/2:y=(h/2)+60,"
          f"drawtext=fontfile={font}:text='{note}':fontcolor={cp.INK}@0.55"
          f":fontsize=34:x=(w-text_w)/2:y=(h/2)+220[v]")
    done = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"color=c=0x0B0F14:s={w}x{h}:d={seconds}:r=30", "-i", str(logo),
         "-filter_complex", vf, "-map", "[v]", "-pix_fmt", "yuv420p", str(out)],
        capture_output=True, text=True)
    if done.returncode != 0:
        raise ShotError(done.stderr.strip()[-200:] or "the end card could not be made")
    return out


# Motion. Wan is cheap enough to draft with; Veo is sharper and costs about the
# same per second, so the choice is quality of motion rather than money.
MOVERS = {
    "draft": ("fal-ai/wan-i2v", {"resolution": "480p", "num_frames": 81}),
    "final": ("fal-ai/wan-i2v", {"resolution": "720p", "num_frames": 81}),
}


def animate(image_url: str, motion: str, out: Path, *, quality: str = "final",
            seconds: float = 5.0) -> Path:
    """Give a still motion, and write the clip to disk.

    The camera move belongs at the END of the prompt. Appended after the scene it
    reduces drift; embedded mid-sentence the model tends to reinterpret the
    subject instead of the camera.
    """
    model, extra = MOVERS.get(quality, MOVERS["final"])
    out_json = _fal(model, {"prompt": motion, "image_url": image_url, **extra}, timeout=900)
    url = (out_json.get("video") or {}).get("url")
    if not url:
        raise ShotError("no clip came back")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(httpx.get(url, timeout=600).content)
    if out.stat().st_size == 0:
        raise ShotError("the clip downloaded empty")
    return out


def shoot(scene: dict, out: Path, *, quality: str = "final") -> dict:
    """A scene description becomes a clip: generate, judge, sharpen, animate.

    The whole reason this exists in one place is that the order matters and is
    easy to get wrong — judging after animating wastes the expensive step, and
    sharpening after animating enlarges the blur instead of removing it.
    """
    from . import producer
    look = producer.prompt_for(scene)
    still = make_still(look, "portrait_16_9", sharpen=True, check=True)
    motion = f"{scene.get('shows', '')}. {producer.MOVES.get(scene.get('move', 'still'), '')}."
    clip = animate(still["url"], motion, out, quality=quality,
                   seconds=float(scene.get("seconds") or 4))
    return {"clip": clip, "still": still["url"], "attempts": still.get("attempt", 1),
            "faults": still.get("faults", []), "sharpened": still.get("sharpened")}


def make_still(prompt: str, shape: str = "portrait_16_9", *, sharpen: bool = True,
               check: bool = True, tries: int = 2) -> dict:
    """A still worth animating: generated, sharpened, and looked at.

    Retries once with the judge's own suggested addition rather than the same
    prompt, because generating the identical request again mostly produces the
    identical fault.
    """
    last = {}
    ask = prompt
    for attempt in range(max(1, tries)):
        out = _fal("fal-ai/flux/dev", {"prompt": ask, "image_size": shape, "num_images": 1})
        url = (out.get("images") or [{}])[0].get("url")
        if not url:
            raise ShotError("no image came back")
        verdict = judge(url, prompt) if check else {"use": True, "faults": [], "fix": ""}
        last = {"url": url, "attempt": attempt + 1, **verdict}
        if verdict.get("use"):
            break
        if verdict.get("fix"):
            ask = f"{prompt}. {verdict['fix']}"
            log.info("retrying still: %s", verdict["faults"][:1])
    if sharpen:
        try:
            last["url"] = upscale(last["url"])
            last["sharpened"] = True
        except ShotError as exc:
            log.warning("not sharpened: %s", exc)
            last["sharpened"] = False
    return last
