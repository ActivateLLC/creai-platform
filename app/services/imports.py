"""
Hosting a site somebody already built.

Plenty of people don't need anything made. They have a folder, or a repo, and
they want a domain pointed at it and someone else to run the hosting. That is a
smaller ask than building, and refusing it sends them to a competitor for the
part Creai is actually good at.

The constraint that shapes everything here: an imported site runs its own
JavaScript, and generated sites do not. Creai's own pages are safe on
app.creai.dev only because their script-src is pinned to one hash. Put somebody
else's JavaScript on that origin and it can read the signed-in owner's token out
of browser storage.

So imported sites are never served from the app's origin. They go on a custom
domain, or on a separate hosting host if one is configured — a different origin,
where localStorage is a different box and nothing of Creai's is reachable. Until
one of those exists, an import can be uploaded and previewed as inert text but
not served as a live site. That refusal is the feature.
"""

import io
import logging
import posixpath
import re
import zipfile

from ..core.config import settings

log = logging.getLogger("creai.imports")

MAX_FILES = 400
MAX_TOTAL = 40 * 1024 * 1024
MAX_FILE = 12 * 1024 * 1024

# What a static site is made of. Anything else is either a server-side program
# that will never run here, or something we'd rather not serve blind.
TYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".json": "application/json",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".avif": "image/avif", ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml", ".webmanifest": "application/manifest+json",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mp3": "audio/mpeg", ".pdf": "application/pdf",
    ".map": "application/json",
}
# Files that mean "this needs a server we are not running". Better to say so than
# to serve the source of somebody's PHP as plain text to the internet.
SERVER_SIDE = (".php", ".py", ".rb", ".jsp", ".asp", ".aspx", ".cgi", ".pl", ".exe", ".sh")
SKIP = re.compile(r"(^|/)(\.git|\.github|node_modules|__MACOSX|\.DS_Store|\.env)(/|$)")


class ImportError_(ValueError):
    """Something the person importing should be told plainly."""


def hosting_host() -> str:
    """The separate origin imported sites are served from, if one is configured."""
    return (settings.imports_host or "").strip().rstrip("/")


def can_serve_free_address() -> bool:
    return bool(hosting_host())


def _clean(name: str) -> str | None:
    """A safe path inside the bundle, or None to skip it."""
    path = name.replace("\\", "/").lstrip("/")
    path = posixpath.normpath(path)
    if path.startswith("..") or path.startswith("/") or path in (".", ""):
        return None
    if SKIP.search("/" + path):
        return None
    return path


# Where a build puts its output. People zip the whole repo, not the one folder a
# host wants, so finding this is the difference between a working site and a 404
# on its own homepage.
BUILD_DIRS = ("dist", "build", "out", "public", "_site", "site", "www",
              ".output/public", ".next/out", "docs")
# Signs of source that has not been built yet.
SOURCE_MARKERS = ("package.json", "vite.config.js", "vite.config.ts", "next.config.js",
                  "svelte.config.js", "astro.config.mjs", "gatsby-config.js", "angular.json")


def find_root(paths: list[str]) -> str | None:
    """The folder to treat as the site. "" when index.html is already at the top,
    a build folder when the upload is a whole repo, or None when there is none."""
    if "index.html" in paths:
        return ""
    for d in BUILD_DIRS:
        if f"{d}/index.html" in paths:
            return d + "/"
    # Any folder with an index.html, shallowest first, so a stray one deep in the
    # tree never wins over the real thing.
    found = sorted((p for p in paths if p.endswith("/index.html")),
                   key=lambda p: (p.count("/"), len(p)))
    return found[0][:-len("index.html")] if found else None


def _strip_wrapper(paths: list[str]) -> str:
    """A zip of a folder usually has one directory at the top. Drop it, so that
    index.html is where a browser expects rather than one level down."""
    tops = {p.split("/")[0] for p in paths if "/" in p}
    if len(tops) == 1 and not any("/" not in p for p in paths):
        return tops.pop() + "/"
    return ""


def read_zip(data: bytes) -> dict[str, bytes]:
    """The files of an uploaded folder, checked. Raises with a sayable reason."""
    if len(data) > MAX_TOTAL:
        raise ImportError_(f"that folder is over {MAX_TOTAL // (1024 * 1024)} MB")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ImportError_("that file isn't a zip folder")

    names = []
    for info in z.infolist():
        if info.is_dir():
            continue
        path = _clean(info.filename)
        if path:
            names.append(path)
    if not names:
        raise ImportError_("that folder is empty")
    prefix = _strip_wrapper(names)

    out: dict[str, bytes] = {}
    total = 0
    for info in z.infolist():
        if info.is_dir():
            continue
        path = _clean(info.filename)
        if not path:
            continue
        if prefix and path.startswith(prefix):
            path = path[len(prefix):]
        if not path:
            continue
        ext = posixpath.splitext(path)[1].lower()
        if ext in SERVER_SIDE:
            raise ImportError_(
                f"{path} needs a server to run it. Creai hosts static sites: HTML, CSS, "
                "JavaScript and assets. Build your project first and import the output folder.")
        if ext not in TYPES:
            continue                      # quietly skip what we can't serve
        if info.file_size > MAX_FILE:
            raise ImportError_(f"{path} is over {MAX_FILE // (1024 * 1024)} MB")
        total += info.file_size
        if total > MAX_TOTAL or len(out) >= MAX_FILES:
            raise ImportError_("that folder has too much in it "
                               f"({MAX_FILES} files or {MAX_TOTAL // (1024 * 1024)} MB)")
        out[path] = z.read(info)

    root = find_root(list(out))
    if root is None:
        if any(p == m or p.endswith("/" + m) for p in out for m in SOURCE_MARKERS):
            raise ImportError_(
                "that looks like source code that hasn't been built yet. Run your build "
                "(npm run build, usually) and upload the folder it produces — normally "
                "dist, build or out — or zip the whole project again once it's there.")
        raise ImportError_("there's no index.html in that folder — that's the page a "
                           "visitor lands on")
    if root:
        # A whole repo was uploaded: keep the built site, drop the source around it.
        out = {p[len(root):]: b for p, b in out.items() if p.startswith(root)}
    return out


def read_loose(named: list[tuple[str, bytes]]) -> dict[str, bytes]:
    """The same checks, for files picked straight out of a folder rather than
    zipped. Browsers hand us each file with the path it had on the person's
    machine, which is enough to rebuild the shape of the site."""
    total, out = 0, {}
    for raw_name, blob in named:
        path = _clean(raw_name)
        if not path:
            continue
        ext = posixpath.splitext(path)[1].lower()
        if ext in SERVER_SIDE:
            raise ImportError_(
                f"{posixpath.basename(path)} needs a server to run it. Creai hosts static "
                "sites: pages, styles, scripts and pictures.")
        if ext not in TYPES:
            continue
        if len(blob) > MAX_FILE:
            raise ImportError_(f"{posixpath.basename(path)} is over "
                               f"{MAX_FILE // (1024 * 1024)} MB")
        total += len(blob)
        if total > MAX_TOTAL or len(out) >= MAX_FILES:
            raise ImportError_("that's more than Creai can host in one site "
                               f"({MAX_FILES} files or {MAX_TOTAL // (1024 * 1024)} MB)")
        out[path] = blob
    if not out:
        raise ImportError_("none of those were files a website is made of — "
                           "pages, styles, scripts or pictures")

    prefix = _strip_wrapper(list(out))
    if prefix:
        out = {p[len(prefix):]: b for p, b in out.items() if p.startswith(prefix)}
    root = find_root(list(out))
    if root is None:
        if any(p == m or p.endswith("/" + m) for p in out for m in SOURCE_MARKERS):
            raise ImportError_(
                "that looks like source code that hasn't been built yet. Run your build "
                "and choose the folder it produces — normally dist, build or out.")
        raise ImportError_("there's no index.html in what you picked — that's the page "
                           "a visitor lands on. Choose the folder that contains it.")
    if root:
        out = {p[len(root):]: b for p, b in out.items() if p.startswith(root)}
    return out


def mime_for(path: str) -> str:
    return TYPES.get(posixpath.splitext(path)[1].lower(), "application/octet-stream")


def entry_for(path: str, files: dict) -> str | None:
    """Resolve a request path to a file, the way a static host would."""
    p = (path or "").strip("/")
    for candidate in ([p] if p else []) + [f"{p}/index.html" if p else "index.html"]:
        if candidate in files:
            return candidate
    return None


# An imported site runs its own code, so it gets a policy that protects the
# visitor without pretending to understand the site: no framing, no plugins, and
# its own scripts only from itself and https.
SERVE_CSP = ("default-src 'self' https: data: blob:; "
             "object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
