"""
Read a customer's existing website so marketing sounds like them.

The fetch is locked down, because the URL comes from a user or a model:
http(s) only, standard ports, every hop (including redirects) must resolve to
a public address, a size cap and a short timeout. What comes back is plain
extracted text and a few style hints, never raw HTML.
"""

import asyncio
import ipaddress
import re
import socket
from collections import Counter
from html import unescape
from urllib.parse import urljoin, urlparse

import httpx

MAX_BYTES = 2_000_000
MAX_REDIRECTS = 3
TIMEOUT = 10


class FetchError(RuntimeError):
    pass


def _public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return bool(infos)


def normalise(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise FetchError("add a website address")
    scheme = re.match(r"^([a-z][a-z0-9+.-]*):", url, re.I)
    if "://" in url and not re.match(r"^https?://", url, re.I):
        raise FetchError("only web addresses (http or https) can be read")
    if scheme and "://" not in url and not re.match(r"^[^:/]+:\d+(/|$)", url):
        raise FetchError("only web addresses (http or https) can be read")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    try:
        port = p.port
    except ValueError:
        raise FetchError("that doesn't look like a website address")
    if p.scheme not in ("http", "https") or not p.hostname or "." not in p.hostname and p.hostname != "localhost":
        raise FetchError("that doesn't look like a website address")
    if port not in (None, 80, 443):
        raise FetchError("only standard web addresses can be read")
    return url


async def fetch(url: str) -> tuple[str, str]:
    """Return (final_url, html)."""
    url = normalise(url)
    loop = asyncio.get_running_loop()
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False,
                                 headers={"User-Agent": "Creai-BrandReader/1.0"}) as x:
        for _ in range(MAX_REDIRECTS + 1):
            host = urlparse(url).hostname
            if not await loop.run_in_executor(None, _public, host):
                raise FetchError("that address isn't a public website")
            async with x.stream("GET", url) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = normalise(urljoin(url, r.headers["location"]))
                    continue
                if r.status_code >= 400:
                    raise FetchError(f"the site answered with an error ({r.status_code})")
                if "html" not in r.headers.get("content-type", "html"):
                    raise FetchError("that address isn't a web page")
                body = b""
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        break
                return url, body.decode(r.encoding or "utf-8", errors="replace")
    raise FetchError("too many redirects")


def _meta(html: str, key: str) -> str:
    m = re.search(r'<meta[^>]+(?:name|property)=["\']%s["\'][^>]*content=["\']([^"\']*)' % re.escape(key), html, re.I) \
        or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:name|property)=["\']%s["\']' % re.escape(key), html, re.I)
    return unescape(m.group(1)).strip() if m else ""


def extract(url: str, html: str) -> dict:
    clean = re.sub(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", html)
    title = unescape((re.search(r"(?is)<title[^>]*>(.*?)</title>", clean) or [None, ""])[1]).strip()
    heads = [re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", h))).strip()
             for h in re.findall(r"(?is)<h[1-3][^>]*>(.*?)</h[1-3]>", clean)]
    heads = [h for h in heads if 2 < len(h) < 140][:20]
    text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", clean))).strip()
    colors = Counter(c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}\b", html))
    for c in ("#ffffff", "#000000"):
        colors.pop(c, None)
    image = _meta(html, "og:image")
    return {
        "url": url,
        "title": title[:160],
        "description": (_meta(html, "description") or _meta(html, "og:description"))[:400],
        "site_name": _meta(html, "og:site_name")[:80],
        "headings": heads,
        "theme_color": _meta(html, "theme-color"),
        "colors": [c for c, _ in colors.most_common(6)],
        "image": urljoin(url, image) if image else "",
        "text": text[:6000],
    }


async def read(url: str) -> dict:
    final, html = await fetch(url)
    return extract(final, html)
