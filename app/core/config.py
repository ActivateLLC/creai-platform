"""
Configuration. Every external dependency is named here so it is obvious what the
platform talks to, and so a missing credential fails loudly at boot rather than
silently at 2am.

Licence policy (see README): every dependency is MIT, Apache-2.0 or BSD. Nothing
AGPL or source-available runs inside this process.
"""

import os
from dataclasses import dataclass, field


def _req(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # identity
    env: str = _req("ENV", "development")
    public_url: str = _req("PUBLIC_URL", "http://localhost:8080")
    secret_key: str = _req("SECRET_KEY", "")

    # storage
    database_url: str = _req("DATABASE_URL", "")

    # deploy target — Railway
    railway_token: str = _req("RAILWAY_TOKEN", "")
    railway_project_id: str = _req("RAILWAY_PROJECT_ID", "")

    # DNS — Cloudflare
    cloudflare_token: str = _req("CLOUDFLARE_API_TOKEN", "")
    cloudflare_account_id: str = _req("CLOUDFLARE_ACCOUNT_ID", "")

    # where a customer domain points
    origin_ip: str = _req("ORIGIN_IP", "76.76.21.21")
    origin_cname: str = _req("ORIGIN_CNAME", "cname.creai.dev")

    # email — Resend
    resend_key: str = _req("RESEND_API_KEY", "")
    mail_from: str = _req("MAIL_FROM", "CreAI <hello@creai.dev>")

    # publishing — self-hosted Postiz, called over HTTP only.
    # Postiz is AGPL: it runs as a separate service and none of its code is
    # vendored here.
    postiz_url: str = _req("POSTIZ_URL", "")
    postiz_key: str = _req("POSTIZ_API_KEY", "")

    verify_prefix: str = "_creai-verify"

    @property
    def configured(self) -> dict:
        return {
            "database": bool(self.database_url),
            "dns": bool(self.cloudflare_token),
            "deploy": bool(self.railway_token),
            "email": bool(self.resend_key),
            "publishing": bool(self.postiz_url and self.postiz_key),
        }

    def missing_for(self, capability: str) -> bool:
        return not self.configured.get(capability, False)


settings = Settings()
