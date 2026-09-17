"""DNS writes against a fake Cloudflare: route the site, touch nothing else."""

import asyncio
import json

import httpx

from app.core.config import settings
from app.services import dns

DOMAIN = "example-shop.com"
EXISTING = [
    {"id": "a1", "type": "A", "name": DOMAIN, "content": "1.2.3.4", "proxied": False, "ttl": 300},
    {"id": "m1", "type": "MX", "name": DOMAIN, "content": "mail.host", "proxied": False, "ttl": 300},
    {"id": "c1", "type": "CNAME", "name": "www." + DOMAIN, "content": "old.host",
     "proxied": False, "ttl": 300},
    {"id": "t1", "type": "TXT", "name": DOMAIN, "content": "v=spf1 -all", "proxied": False, "ttl": 300},
]


def test_routes_apex_by_cname_and_leaves_email_alone(monkeypatch):
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.method == "GET":
            return httpx.Response(200, json={"success": True, "result": EXISTING,
                                             "result_info": {"total_pages": 1}})
        return httpx.Response(200, json={"success": True, "result": {"id": "new"}})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    out = asyncio.run(dns.write_records("zone", DOMAIN, "tok"))

    touched = " ".join(path for _, path, _ in calls)
    assert "/m1" not in touched and "/t1" not in touched          # MX and SPF untouched
    assert ("DELETE", "/client/v4/zones/zone/dns_records/a1", None) in calls
    writes = [(b["type"], b["name"], b["content"]) for m, _, b in calls if m in ("POST", "PUT")]
    assert ("CNAME", DOMAIN, settings.origin_cname) in writes
    assert ("CNAME", "www." + DOMAIN, settings.origin_cname) in writes
    assert len(out) == 3 and all(r["ok"] for r in out)
    assert all(set(r) >= {"type", "name", "value", "provider_id", "ok"} for r in out)
