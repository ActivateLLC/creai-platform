"""
Images for posts, generated through Hugging Face Inference Providers.

One HF token reaches many hosted models; the model and provider are settings.
The default, Z-Image Turbo, is Apache-2.0 licensed, so generated images can be
used commercially. The flow follows Hugging Face's own client for fal-ai:
submit to the queue, poll status, fetch the result.
"""

import asyncio
import logging

import httpx

from ..core.config import settings

log = logging.getLogger("creai.images")

ROUTER = "https://router.huggingface.co"
HUB = "https://huggingface.co"
POLL_SECONDS = 0.8
MAX_WAIT = 90
SIZES = {"square": "square_hd", "portrait": "portrait_4_3", "landscape": "landscape_16_9"}

_mapping: dict[tuple[str, str], str] = {}


class ImageError(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.hf_token)


def media_key() -> str:
    return f"image:{settings.image_model}"


async def _provider_id(x: httpx.AsyncClient) -> str:
    key = (settings.image_model, settings.image_provider)
    if key not in _mapping:
        r = await x.get(f"{HUB}/api/models/{settings.image_model}",
                        params={"expand[]": "inferenceProviderMapping"},
                        headers={"Authorization": f"Bearer {settings.hf_token}"})
        entry = ((r.json() if r.status_code == 200 else {})
                 .get("inferenceProviderMapping") or {}).get(settings.image_provider)
        if not entry or entry.get("status") != "live":
            raise ImageError(f"{settings.image_model} isn't live on {settings.image_provider}")
        _mapping[key] = entry["providerId"]
    return _mapping[key]


async def generate(prompt: str, shape: str = "square") -> str:
    """Return a URL of a freshly generated image."""
    if not configured():
        raise ImageError("image generation isn't configured")
    prompt = (prompt or "").strip()[:1500]
    if not prompt:
        raise ImageError("describe the image")
    headers = {"Authorization": f"Bearer {settings.hf_token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as x:
        pid = await _provider_id(x)
        base = f"{ROUTER}/{settings.image_provider}"
        r = await x.post(f"{base}/{pid}?_subdomain=queue", headers=headers, json={
            "prompt": prompt, "image_size": SIZES.get(shape, "square_hd"),
            "num_images": 1, "enable_safety_checker": True, "output_format": "jpeg"})
        if r.status_code >= 400:
            raise ImageError(f"image request failed ({r.status_code})")
        queued = r.json()
        model_path = httpx.URL(queued["response_url"]).path
        status_url = f"{base}{model_path}/status?_subdomain=queue"
        result_url = f"{base}{model_path}?_subdomain=queue"
        status, waited = queued.get("status"), 0.0
        while status != "COMPLETED":
            if waited > MAX_WAIT:
                raise ImageError("image generation timed out")
            await asyncio.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
            s = await x.get(status_url, headers=headers)
            if s.status_code >= 400:
                raise ImageError("image status check failed")
            status = s.json().get("status")
            if status in ("FAILED", "ERROR"):
                raise ImageError("image generation failed")
        out = (await x.get(result_url, headers=headers)).json()
    images = out.get("images") or []
    if out.get("has_nsfw_concepts") and any(out["has_nsfw_concepts"]):
        raise ImageError("the image was blocked by the safety filter")
    if not images or not str(images[0].get("url", "")).startswith("https://"):
        raise ImageError("no image came back")
    return images[0]["url"]


async def generate_many(prompts: list[tuple[int, str, str]], limit: int = 4) -> dict[int, str]:
    """{index: url} for the prompts that succeeded; failures are logged, not raised."""
    sem = asyncio.Semaphore(limit)
    done: dict[int, str] = {}

    async def one(i, prompt, shape):
        async with sem:
            try:
                done[i] = await generate(prompt, shape)
            except (ImageError, httpx.HTTPError, KeyError, ValueError) as exc:
                log.warning("image %s failed: %s", i, exc)

    await asyncio.gather(*(one(*p) for p in prompts))
    return done
