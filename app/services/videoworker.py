"""
Running a render.

The plan says what the video is; this buys what it needs and hands the pieces to
the builder, which has ffmpeg. It is the only place that spends money on a video,
so it is also the place that refuses to spend it twice.

Three rules it keeps:

Reuse what has not changed. Every scene's pictures are stored against a
fingerprint of the words and the look that produced them. Change one line and one
scene is bought again; the rest are read back from the bucket.

Time the scene to the line. A scene is as long as its sentence takes to say, plus
a beat, measured after the voice exists rather than guessed beforehand. A plan's
"seconds" is a hint for pricing, not a slot the speech is squeezed into.

Fail loudly and specifically. A render that dies says which scene and why,
because "rendering failed" costs the person another paid attempt to find out.
"""

import asyncio
import base64
import hashlib
import logging

import httpx

from ..core.config import settings
from ..core.db import conn
from . import assets, images, speech, video

log = logging.getLogger("creai.videoworker")

# Generating pictures is the slow part; a few at once keeps a render tolerable
# without hammering the provider.
AT_ONCE = 3
BEAT = 0.34            # the pause left after a line, so it does not feel rushed


class WorkerError(RuntimeError):
    pass


def _key(project_id: int, scene_id: str, fingerprint: str, ext: str) -> str:
    return f"video/{project_id}/{scene_id}-{fingerprint}.{ext}"


async def _seconds_of(mp3: bytes) -> float:
    """How long a line takes to say. Measured from the audio, never estimated:
    an estimate that is wrong by half a second is wrong on every scene."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", "-",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate(mp3)
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


async def _picture(scene: dict, project_id: int, shape: str) -> bytes:
    """The still for a scene: read back if we already bought it, generated if not."""
    fp = video._fingerprint(scene)
    key = _key(project_id, scene.get("id", "s"), fp, "jpg")
    have = await assets.blob(key)
    if have:
        log.info("scene %s reused", scene.get("id"))
        return have
    shape_hint = {"vertical": "portrait", "square": "square"}.get(shape, "landscape")
    url = await images.generate(scene.get("prompt", ""), shape_hint)
    async with httpx.AsyncClient(timeout=120) as x:
        data = (await x.get(url)).content
    await assets.put_blob(key, data, "image/jpeg")
    return data


async def _voice(scene: dict, project_id: int, voice_name: str) -> tuple[bytes, float]:
    line = (scene.get("line") or "").strip()
    if not line:
        return b"", 0.0
    fp = hashlib.sha256(f"{line}|{voice_name}".encode()).hexdigest()[:16]
    key = _key(project_id, scene.get("id", "s"), fp, "mp3")
    data = await assets.blob(key)
    if not data:
        data = await speech.speak(line, voice_name)
        await assets.put_blob(key, data, "audio/mpeg")
    return data, await _seconds_of(data)


async def run(video_id: int) -> None:
    """Take one video from 'rendering' to 'ready', or say why not."""
    async with conn() as c:
        row = await c.fetchrow(
            "SELECT id, org_id, project_id, plan, shape FROM videos WHERE id=$1", video_id)
    if not row:
        return
    plan = row["plan"] or {}
    scenes = plan.get("scenes") or []
    voice_name = plan.get("voice") or "ash"
    project_id, shape = row["project_id"], row["shape"]

    try:
        if not (settings.build_url and settings.build_token):
            raise WorkerError("video rendering isn't switched on yet")

        # pictures first, a few at a time
        gate = asyncio.Semaphore(AT_ONCE)

        async def picture(sc):
            if sc.get("source") != "generated":
                return None
            async with gate:
                try:
                    return await _picture(sc, project_id, shape)
                except (images.ImageError, httpx.HTTPError) as exc:
                    raise WorkerError(f"scene {sc.get('id','?')}: {exc}") from exc

        pictures = await asyncio.gather(*(picture(s) for s in scenes))
        await _progress(video_id, 45)

        # then the lines, which also decide how long each scene runs
        payload = []
        for sc, pic in zip(scenes, pictures):
            said, secs = await _voice(sc, project_id, voice_name)
            runs = round(max(secs + BEAT, float(sc.get("seconds") or 2.5)), 2) if secs \
                else float(sc.get("seconds") or 2.5)
            kind = {"generated": "still", "footage": "clip", "card": "card",
                    "upload": "still"}.get(sc.get("source"), "still")
            payload.append({
                "id": str(sc.get("id", "")), "kind": kind, "caption": sc.get("caption",
                                                                             sc.get("line", "")),
                "seconds": runs, "fit": sc.get("fit", "cover"),
                "focus": float(sc.get("focus", 0.5)),
                "title": sc.get("title", ""), "subtitle": sc.get("subtitle", ""),
                "data": base64.b64encode(pic).decode() if pic else None,
                "voice": base64.b64encode(said).decode() if said else None})
        await _progress(video_id, 70)

        body = {"shape": shape, "scenes": payload}
        bed = plan.get("music_key") and await assets.blob(plan["music_key"])
        if bed:
            body["music"] = base64.b64encode(bed).decode()

        async with httpx.AsyncClient(timeout=900) as x:
            r = await x.post(f"{settings.build_url.rstrip('/')}/render/video", json=body,
                             headers={"X-Build-Token": settings.build_token})
        if r.status_code >= 400:
            detail = (r.json().get("detail") if r.headers.get("content-type", "").startswith(
                "application/json") else r.text[:300])
            raise WorkerError(str(detail)[:300])
        out = r.json()

        # Stored as real assets rather than loose blobs, so the finished film is
        # something the person can open, download and attach like any other file.
        mp4 = base64.b64decode(out["mp4"])
        made = await assets.store_bytes(row["org_id"], name=f"video-{video_id}.mp4",
                                        mime="video/mp4", data=mp4, project_id=project_id)
        mp4_key = made["token"]
        poster_key = None
        if out.get("poster"):
            shot = await assets.store_bytes(row["org_id"], name=f"video-{video_id}.jpg",
                                            mime="image/jpeg",
                                            data=base64.b64decode(out["poster"]),
                                            project_id=project_id)
            poster_key = shot["token"]

        await video.finish(video_id, asset_key=mp4_key, poster_key=poster_key,
                           seconds=float(out.get("seconds") or 0),
                           credits=video.estimate(plan))
        log.info("video %s rendered: %ss, %s bytes", video_id, out.get("seconds"), len(mp4))

    except (WorkerError, speech.SpeechError, httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("video %s failed: %s", video_id, exc)
        await video.fail(video_id, str(exc))


async def _progress(video_id: int, pct: int) -> None:
    async with conn() as c:
        await c.execute("UPDATE videos SET progress=$2, updated_at=now() WHERE id=$1",
                        video_id, pct)


async def sweep(limit: int = 2) -> int:
    """Pick up videos left rendering — after a restart, or when the request that
    started one went away. Claiming is done in SQL so two instances cannot take
    the same job."""
    async with conn() as c:
        rows = await c.fetch(
            """UPDATE videos SET updated_at=now() WHERE id IN (
                 SELECT id FROM videos
                 WHERE state='rendering' AND updated_at < now() - interval '4 minutes'
                 ORDER BY id LIMIT $1 FOR UPDATE SKIP LOCKED)
               RETURNING id""", limit)
    for r in rows:
        asyncio.create_task(run(r["id"]))
    return len(rows)
