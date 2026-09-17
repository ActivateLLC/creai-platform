"""
Serving customer domains from CreAI's Railway service.

Each domain is added to the service through Railway's public API, which returns
the DNS records it needs (a CNAME to route traffic and a TXT to prove ownership).
Railway then issues the certificate. The app serves the right site by Host header.
"""

import os

import httpx

from ..core.config import settings

API = "https://backboard.railway.com/graphql/v2"

CREATE = """mutation($input: CustomDomainCreateInput!) {
  customDomainCreate(input: $input) {
    id domain
    status { dnsRecords { hostlabel fqdn recordType requiredValue status purpose } certificateStatus verified }
  }
}"""

STATUS = """query($projectId: String!, $environmentId: String!, $serviceId: String!) {
  domains(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId) {
    customDomains { id domain status { dnsRecords { hostlabel fqdn recordType requiredValue status }
                    certificateStatus verified } }
  }
}"""


class HostingError(RuntimeError):
    pass


def _ids() -> dict:
    return {"projectId": os.getenv("RAILWAY_PROJECT_ID", ""),
            "environmentId": os.getenv("RAILWAY_ENVIRONMENT_ID", ""),
            "serviceId": os.getenv("RAILWAY_SERVICE_ID", "")}


def configured() -> bool:
    return bool(settings.railway_token and all(_ids().values()))


async def _gql(query: str, variables: dict) -> dict:
    if not configured():
        raise HostingError("custom domain hosting isn't switched on yet")
    async with httpx.AsyncClient(timeout=30) as x:
        r = await x.post(API, json={"query": query, "variables": variables},
                         headers={"Authorization": f"Bearer {settings.railway_token}"})
    data = r.json() if r.content else {}
    if r.status_code >= 400 or data.get("errors"):
        msg = "; ".join(e.get("message", "") for e in data.get("errors") or []) or f"status {r.status_code}"
        raise HostingError(f"the host refused the domain: {msg}")
    return data["data"]


async def domains() -> dict[str, dict]:
    data = await _gql(STATUS, _ids())
    return {d["domain"]: d for d in data["domains"]["customDomains"]}


async def attach(host: str) -> dict:
    """Add a hostname (idempotent). Returns {id, records, certificate, verified}."""
    existing = (await domains()).get(host)
    d = existing or (await _gql(CREATE, {"input": {**_ids(), "domain": host}}))["customDomainCreate"]
    st = d.get("status") or {}
    return {"id": d["id"], "records": st.get("dnsRecords") or [],
            "certificate": st.get("certificateStatus"), "verified": bool(st.get("verified"))}
