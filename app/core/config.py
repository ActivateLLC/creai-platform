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


def _token(name: str) -> str:
    """API keys never contain whitespace or quotes; drop any a copy-paste added."""
    raw = os.environ.get(name, "")
    return "".join(ch for ch in raw if not ch.isspace() and ch not in "\"'\u200b\ufeff")


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

    # DNS — Cloudflare, a zone-scoped token (Zone:Zone:Read, Zone:DNS:Edit)
    cloudflare_token: str = _token("CLOUDFLARE_API_TOKEN")
    cloudflare_account_id: str = _token("CLOUDFLARE_ACCOUNT_ID")

    # Buying domains — a different token, and necessarily so: Registrar lives on an
    # account-owned token (Registrar Domains: Admin) and a zone token cannot reach it,
    # while this one cannot edit DNS. Falls back to the DNS token so an older
    # deployment keeps working, but they are not interchangeable.
    cloudflare_registrar_token: str = (_token("CLOUDFLARE_REGISTRAR_TOKEN")
                                       or _token("CLOUDFLARE_API_TOKEN"))

    # where a customer domain points
    origin_ip: str = _req("ORIGIN_IP", "76.76.21.21")
    origin_cname: str = _req("ORIGIN_CNAME", "cname.creai.dev")

    # email — Resend
    resend_key: str = _req("RESEND_API_KEY", "")
    mail_from: str = _req("MAIL_FROM", "Creai <hello@creai.dev>")

    # publishing — self-hosted Postiz, called over HTTP only.
    # Postiz is AGPL: it runs as a separate service and none of its code is
    # vendored here.
    postiz_url: str = _req("POSTIZ_URL", "")
    postiz_key: str = _req("POSTIZ_API_KEY", "")

    # social sign-in
    google_client_id: str = _req("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = _req("GOOGLE_CLIENT_SECRET", "")

    # other model providers
    meta_api_key: str = os.getenv("META_API_KEY") or os.getenv("MODEL_API_KEY", "")
    meta_api_base: str = os.getenv("META_API_BASE", "https://api.meta.ai/v1")
    hf_token: str = os.getenv("HF_TOKEN", "")
    image_model: str = os.getenv("IMAGE_MODEL", "Tongyi-MAI/Z-Image-Turbo")
    image_provider: str = os.getenv("IMAGE_PROVIDER", "fal-ai")

    # social publishing — Creai's self-hosted Postiz
    postiz_url: str = _req("POSTIZ_URL",
                           "https://postiz-v2113-production-ed5a.up.railway.app/api/public/v1")
    postiz_api_key: str = _req("POSTIZ_API_KEY", "")

    # agent — Anthropic, called over HTTP
    anthropic_key: str = _req("ANTHROPIC_API_KEY", "")
    agent_model: str = _req("AGENT_MODEL", "claude-fable-5-1")          # "Best"
    agent_fast_model: str = _req("AGENT_FAST_MODEL", "claude-sonnet-5")  # "Fast"

    # payments — in-app panel on Stripe Elements (cards, Apple Pay, Google Pay, Klarna)
    # the renderer service (screenshots and app smoke tests)
    render_url: str = _req("RENDER_URL", "")
    render_token: str = _token("RENDER_TOKEN")

    # the game builder (Godot web exports)
    build_url: str = _req("BUILD_URL", "")
    build_token: str = _token("BUILD_TOKEN")

    # Where published games are served. A host of their own, never the app's: a game
    # page that is cross-origin isolated is not sandboxed, and unsandboxed game code
    # must never share an origin with the app's session storage. Unset means games
    # publish single-threaded under the app's sandbox, which runs everywhere.
    games_url: str = _req("GAMES_URL", "")

    # file uploads — Railway Bucket (S3-compatible)
    assets_bucket: str = _token("ASSETS_BUCKET")
    assets_key_id: str = _token("ASSETS_ACCESS_KEY_ID")
    assets_secret: str = _token("ASSETS_SECRET_ACCESS_KEY")
    assets_region: str = _token("ASSETS_REGION")
    assets_endpoint: str = _token("ASSETS_ENDPOINT")
    stripe_key: str = _token("STRIPE_SECRET_KEY")
    stripe_publishable_key: str = _token("STRIPE_PUBLISHABLE_KEY")
    stripe_webhook_secret: str = _token("STRIPE_WEBHOOK_SECRET")

    verify_prefix: str = "_creai-verify"

    @property
    def configured(self) -> dict:
        return {
            "database": bool(self.database_url),
            "dns": bool(self.cloudflare_token),
            "deploy": bool(self.railway_token),
            "email": bool(self.resend_key),
            "publishing": bool(self.postiz_url and self.postiz_key),
            "games": bool(self.build_url and self.build_token),
            "review": bool(self.render_url and self.render_token),
            "uploads": bool(self.assets_bucket and self.assets_key_id and self.assets_secret and self.assets_endpoint),
            "registrar": bool(self.cloudflare_registrar_token and self.cloudflare_account_id),
            "hosting": bool(self.railway_token and os.getenv("RAILWAY_SERVICE_ID")),
            "agent": bool(self.anthropic_key),
            "billing": self.stripe_keys_ok() and not STRIPE_REJECTED,
        }

    def stripe_keys_ok(self) -> bool:
        """Secret key must be secret (sk_/rk_), publishable must be publishable (pk_).
        A swapped pair is treated as not configured, so a secret never reaches a browser."""
        return (self.stripe_key.startswith(("sk_", "rk_"))
                and self.stripe_publishable_key.startswith("pk_")
                and bool(self.stripe_webhook_secret))

    def missing_for(self, capability: str) -> bool:
        return not self.configured.get(capability, False)


# Set when Stripe refuses the key (401). Billing then reads as off until a restart with a good key.
STRIPE_REJECTED = False

settings = Settings()
