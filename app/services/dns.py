"""
DNS via Cloudflare.

This is the riskiest thing the platform does to a customer: their domain may
already route their email and an existing site. Two rules, enforced in code
rather than remembered:

  1. We write only the records needed to route the site.
  2. We never touch MX, NS, SRV, CAA or SOA. Nobody's email goes down because
     they pointed a domain at us.

Routing: Railway has no static inbound IP, so both the apex and www are CNAMEs
to ORIGIN_CNAME. Cloudflare flattens the apex CNAME, so this is valid at the
root. A CNAME cannot coexist with other records of the same name, so any
A/AAAA already at exactly `@` or `www` is removed first, and any CNAME there is
repointed — each is logged in full (creai.dns) so it can be restored. Nothing else in the zone is touched.

Propagation is confirmed by resolving from outside, not by trusting our own
write — because what the customer sees is what a resolver says.
"""

import asyncio
import logging
import secrets
import socket

import httpx

from ..core.config import settings

API = "https://api.cloudflare.com/client/v4"
NEVER_TOUCH = {"MX", "SRV", "CAA", "NS", "SOA"}
# Record types that conflict with a CNAME at a routed name. Only these, only
# at the routed names, are ever removed.
ROUTED_NAMES = ("@", "www")
log = logging.getLogger("creai.dns")


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.cloudflare_token}",
            "Content-Type": "application/json"}


def verify_token() -> str:
    return "creai-site-verify=" + secrets.token_hex(8)


def _fq(name: str, domain: str) -> str:
    return domain if name == "@" else f"{name}.{domain}"


def wanted_records(domain: str, token: str) -> list[dict]:
    """The complete set the platform writes. Nothing outside this list, ever."""
    if not settings.origin_cname:
        raise RuntimeError("ORIGIN_CNAME is not set")
    return [
        {"type": "CNAME", "name": "@", "content": settings.origin_cname, "proxied": True},
        {"type": "CNAME", "name": "www", "content": settings.origin_cname, "proxied": True},
        {"type": "TXT", "name": settings.verify_prefix, "content": token, "proxied": False},
    ]


async def ensure_zone(domain: str) -> str:
    async with httpx.AsyncClient(timeout=30) as x:
        r = await x.get(f"{API}/zones", headers=_headers(), params={"name": domain})
        data = r.json()
        if data.get("result"):
            return data["result"][0]["id"]
        r = await x.post(f"{API}/zones", headers=_headers(),
                         json={"name": domain,
                               "account": {"id": settings.cloudflare_account_id},
                               "type": "full"})
        out = r.json()
        if not out.get("success"):
            raise RuntimeError(f"could not create zone: {out.get('errors')}")
        return out["result"]["id"]


async def _list_records(x: httpx.AsyncClient, zone_id: str) -> list[dict]:
    """All records, across pages — a busy zone has more than one page."""
    records, page = [], 1
    while True:
        r = await x.get(f"{API}/zones/{zone_id}/dns_records", headers=_headers(),
                        params={"page": page, "per_page": 100})
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"could not list records: {data.get('errors')}")
        records += data.get("result", [])
        info = data.get("result_info") or {}
        if page >= info.get("total_pages", 1):
            return records
        page += 1


async def write_records(zone_id: str, domain: str, token: str) -> list[dict]:
    written, replaced = [], []
    wanted = wanted_records(domain, token)
    async with httpx.AsyncClient(timeout=30) as x:
        existing = await _list_records(x, zone_id)
        routed_fq = {_fq(n, domain) for n in ROUTED_NAMES}

        # Clear conflicts at the routed names only, remembering what was there.
        for e in existing:
            if e["name"] in routed_fq and e["type"] in {"A", "AAAA"}:
                r = await x.delete(f"{API}/zones/{zone_id}/dns_records/{e['id']}",
                                   headers=_headers())
                if not r.json().get("success"):
                    raise RuntimeError(f"could not clear {e['type']} {e['name']}")
                replaced.append({k: e.get(k) for k in
                                 ("type", "name", "content", "proxied", "ttl")})
        by_key = {(e["type"], e["name"]): e for e in existing
                  if not (e["name"] in routed_fq and e["type"] in {"A", "AAAA"})}

        for rec in wanted:
            if rec["type"] in NEVER_TOUCH:
                continue
            fq = _fq(rec["name"], domain)
            found = by_key.get((rec["type"], fq))
            if found and rec["type"] == "CNAME" and found.get("content") != rec["content"]:
                replaced.append({k: found.get(k) for k in
                                 ("type", "name", "content", "proxied", "ttl")})
            body = {"type": rec["type"], "name": fq, "content": rec["content"],
                    "ttl": 1, "proxied": rec["proxied"]}
            if found:
                r = await x.put(f"{API}/zones/{zone_id}/dns_records/{found['id']}",
                                headers=_headers(), json=body)
            else:
                r = await x.post(f"{API}/zones/{zone_id}/dns_records",
                                 headers=_headers(), json=body)
            out = r.json()
            written.append({"type": rec["type"], "name": rec["name"],
                            "value": rec["content"],
                            "provider_id": (out.get("result") or {}).get("id"),
                            "ok": bool(out.get("success")),
                            "errors": out.get("errors") or []})
    if replaced:
        log.warning("dns replaced existing records on %s: %s", domain, replaced)
    return written


def _resolve(host: str) -> list[str]:
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None)})
    except Exception:
        return []


async def check_live(domain: str) -> dict:
    loop = asyncio.get_running_loop()
    apex = await loop.run_in_executor(None, _resolve, domain)
    www = await loop.run_in_executor(None, _resolve, f"www.{domain}")
    return {"apex": bool(apex), "www": bool(www), "addresses": apex}


async def verify_ownership(domain: str, token: str) -> bool:
    """For a domain the customer already owns: look for our TXT record."""
    try:
        import dns.resolver  # dnspython, ISC licence
    except ImportError:
        return False
    loop = asyncio.get_running_loop()

    def _lookup() -> bool:
        try:
            answers = dns.resolver.resolve(f"{settings.verify_prefix}.{domain}", "TXT")
            return any(token in b"".join(r.strings).decode() for r in answers)
        except Exception:
            return False

    return await loop.run_in_executor(None, _lookup)
