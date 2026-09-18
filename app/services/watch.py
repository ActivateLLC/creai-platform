"""
Watching the things a customer has put online.

A site does not usually break loudly. A DNS record left behind by something that
was switched off a year ago keeps resolving — to somebody else's edge, which
returns somebody else's error page. A certificate lapses. A host stops answering.
None of it sends an email. The owner finds out when a customer mentions it, or
never.

So Creai checks its own work: for every domain and published site, does DNS point
where we think it does, does the page answer, and is the certificate still good
for a while yet.

Two rules this keeps.

Only report what is definitely wrong. A watcher that cries wolf is muted within a
week, and then it is worse than nothing because everyone believes it is working.
Anything uncertain — a lookup that timed out, a redirect somewhere plausible — is
silence, not a warning.

Say what to do, not what happened. "NXDOMAIN on the CNAME" helps nobody. "That
address points at Vercel, which no longer has anything there — point it back at
Creai" is a sentence someone can act on.
"""

import asyncio
import logging
import socket
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import dns.asyncresolver
import httpx

from ..core.db import conn

log = logging.getLogger("creai.watch")

TIMEOUT = 8
CERT_WARN_DAYS = 14

# Hosts that are somebody else's edge. If a Creai customer's domain resolves to
# one of these, it is pointing at a service we did not set up — almost always
# something they switched off and forgot, now serving a stranger's error page.
OTHER_EDGES = {
    "vercel-dns": "Vercel", "vercel.app": "Vercel",
    "netlify": "Netlify", "herokudns": "Heroku",
    "ghs.google": "Google", "wixdns": "Wix", "squarespace": "Squarespace",
    "shopify": "Shopify", "webflow": "Webflow", "github.io": "GitHub Pages",
}


@dataclass
class Finding:
    """One thing that is wrong, said the way a person would say it."""
    where: str
    severity: str            # broken | soon
    what: str
    fix: str = ""

    def as_dict(self) -> dict:
        return {"where": self.where, "severity": self.severity,
                "what": self.what, "fix": self.fix}


@dataclass
class Report:
    checked: int = 0
    findings: list[Finding] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"checked": self.checked, "ok": not self.findings,
                "findings": [f.as_dict() for f in self.findings]}


async def _cname(host: str) -> str | None:
    try:
        r = dns.asyncresolver.Resolver()
        r.lifetime = TIMEOUT
        ans = await r.resolve(host, "CNAME")
        return str(ans[0]).rstrip(".").lower()
    except Exception:
        return None


async def _foreign_edge(host: str) -> str | None:
    """Whose edge this points at, if it is not ours."""
    target = await _cname(host)
    if not target:
        return None
    for needle, who in OTHER_EDGES.items():
        if needle in target:
            return who
    return None


async def _reach(url: str) -> tuple[int | None, str]:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as x:
            r = await x.get(url)
            return r.status_code, ""
    except httpx.HTTPError as exc:
        return None, type(exc).__name__


def _cert_days(host: str) -> int | None:
    """Days left on the certificate, or None if it cannot be read. Blocking, so
    it is called in a thread."""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                after = tls.getpeercert().get("notAfter")
        when = datetime.strptime(after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        return (when - datetime.now(timezone.utc)).days
    except Exception:
        return None


async def check_host(host: str) -> list[Finding]:
    """Everything wrong with one address."""
    out: list[Finding] = []

    whose = await _foreign_edge(host)
    if whose:
        out.append(Finding(
            host, "broken",
            f"This address points at {whose}, not at Creai.",
            f"If you stopped using {whose}, that record is left over. Point it back at "
            "Creai and the site comes straight back."))

    code, err = await _reach(f"https://{host}")
    if code is None:
        out.append(Finding(host, "broken", "Nothing answered at this address.",
                           "Check the address is still pointed at Creai." if not whose else ""))
    elif code >= 500:
        out.append(Finding(host, "broken", f"The address returns an error ({code}).",
                           "" if whose else "The site is published but the host is not serving it."))
    elif code == 404 and not whose:
        out.append(Finding(host, "broken", "The address answers, but there is nothing there.",
                           "Publish the site, or point the address at the right project."))

    if code is not None:
        days = await asyncio.to_thread(_cert_days, host)
        if days is not None and 0 <= days <= CERT_WARN_DAYS:
            out.append(Finding(host, "soon",
                               f"The security certificate runs out in {days} days.",
                               "Creai renews these automatically; if it does not, the address "
                               "stops working."))
        elif days is not None and days < 0:
            out.append(Finding(host, "broken", "The security certificate has run out.",
                               "Visitors will see a warning before they see the site."))
    return out


async def check_org(org_id: int, limit: int = 25) -> Report:
    """Every address this workspace has put online."""
    async with conn() as c:
        rows = await c.fetch(
            """SELECT name FROM domains
               WHERE org_id=$1 AND status IN ('live','verified','active')
               ORDER BY id LIMIT $2""", org_id, limit)
    hosts = [r["name"] for r in rows if r["name"]]

    report = Report(checked=len(hosts))
    if not hosts:
        return report
    found = await asyncio.gather(*(check_host(h) for h in hosts), return_exceptions=True)
    for item in found:
        if isinstance(item, list):
            report.findings.extend(item)
        else:
            # A watcher that throws is a watcher nobody trusts. Failing to check
            # is not the same as finding a problem, so it is silence.
            log.warning("watch failed: %s", item)
    return report
