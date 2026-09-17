"""
Godot projects, exported for the web.

The builder service does the heavy part (import, export, WebAssembly). This is the
thin client: it sends the project's files, gets the built files back, and puts them
where the platform can publish them. Exports take 30-90 seconds, so callers run this
as a job rather than inside a chat turn.
"""

import base64
import logging

import httpx

from ..core.config import settings

log = logging.getLogger("creai.builds")
TIMEOUT = 300
MAX_PROJECT = 40 * 1024 * 1024


class BuildError(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.build_url and settings.build_token)


async def export_web(files: dict[str, str | bytes], *, threads: bool = False) -> dict:
    """files: path -> text, or bytes for binary. Returns {'files': {name: bytes}, 'log': str}.

    threads=True produces a faster export that only runs on a cross-origin-isolated
    page; the caller decides whether it has somewhere to serve that from.
    """
    if not configured():
        raise BuildError("the game builder isn't switched on yet")
    payload, total = {}, 0
    for path, content in files.items():
        data = content.encode() if isinstance(content, str) else content
        total += len(data)
        payload[path] = ("base64:" + base64.b64encode(data).decode()) if isinstance(content, bytes) else content
    if total > MAX_PROJECT:
        raise BuildError("that project is too large to export")
    async with httpx.AsyncClient(timeout=TIMEOUT) as x:
        try:
            r = await x.post(settings.build_url.rstrip("/") + "/export/web",
                             json={"files": payload, "threads": bool(threads)},
                             headers={"X-Build-Token": settings.build_token})
        except httpx.HTTPError as exc:
            raise BuildError(f"the builder wasn't reachable: {exc}")
    if r.status_code == 422:
        raise BuildError("the export failed:\n" + (r.json().get("detail") or "")[-1200:])
    if r.status_code >= 400:
        raise BuildError(f"the builder returned {r.status_code}")
    out = r.json()
    return {"files": {name: base64.b64decode(b64) for name, b64 in out["files"].items()},
            "bytes": out.get("bytes", 0), "log": out.get("log", "")}
