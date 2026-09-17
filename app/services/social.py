"""
Social sign-in (OpenID Connect). Google first; the shape fits Microsoft and
Apple too.

Security choices:
  * Authorization code flow with PKCE, a one-time `state` and a `nonce`.
  * The ID token comes straight from the provider's token endpoint over TLS,
    authenticated with our client secret (OIDC Core 3.1.3.7), and its issuer,
    audience, expiry and nonce are checked.
  * An existing account is linked by email only when the provider says the
    email is verified.
  * The session token never appears in a URL: the browser gets a one-time
    handoff code and swaps it for the token with a POST.
"""

import base64
import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from ..core import tenancy as T
from ..core.config import settings
from ..core.db import conn
from . import vault

START_TTL = timedelta(minutes=10)
HANDOFF_TTL = timedelta(minutes=2)

PROVIDERS = {
    "google": {
        "name": "Google",
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "issuers": ("https://accounts.google.com", "accounts.google.com"),
        "scope": "openid email profile",
        "id": lambda: settings.google_client_id,
        "secret": lambda: settings.google_client_secret,
    },
}


class SocialError(RuntimeError):
    pass


def enabled() -> list[dict]:
    return [{"provider": k, "name": v["name"]} for k, v in PROVIDERS.items()
            if v["id"]() and v["secret"]()]


def _conf(provider: str) -> dict:
    p = PROVIDERS.get(provider)
    if not p or not (p["id"]() and p["secret"]()):
        raise SocialError("that sign-in option isn't available")
    return p


def redirect_uri(provider: str) -> str:
    return f"{settings.public_url.rstrip('/')}/v1/auth/{provider}/callback"


APP_RETURN = "creai://auth"


async def start(provider: str, client: str = "web") -> str:
    p = _conf(provider)
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    async with conn() as c:
        await c.execute("DELETE FROM login_states WHERE expires_at < now()")
        await c.execute(
            """INSERT INTO login_states (id, kind, provider, verifier, nonce, client, expires_at)
               VALUES ($1,'start',$2,$3,$4,$5,$6)""",
            state, provider, verifier, nonce, "app" if client == "app" else "web",
            datetime.now(timezone.utc) + START_TTL)
    return p["authorize"] + "?" + urlencode({
        "client_id": p["id"](), "redirect_uri": redirect_uri(provider),
        "response_type": "code", "scope": p["scope"], "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "prompt": "select_account",
    })


def _claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise SocialError("the sign-in response was malformed")


async def client_for(state: str | None) -> str:
    if not state:
        return "web"
    async with conn() as c:
        return (await c.fetchval("SELECT client FROM login_states WHERE id=$1", state)) or "web"


async def finish(provider: str, code: str, state: str) -> str:
    """Complete sign-in. Returns a one-time handoff code for the browser."""
    p = _conf(provider)
    async with conn() as c:
        row = await c.fetchrow(
            """DELETE FROM login_states WHERE id=$1 AND kind='start' AND provider=$2
               AND expires_at > now() RETURNING verifier, nonce""", state, provider)
    if not row:
        raise SocialError("this sign-in link has expired — please try again")

    async with httpx.AsyncClient(timeout=20) as x:
        r = await x.post(p["token"], data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri(provider), "client_id": p["id"](),
            "client_secret": p["secret"](), "code_verifier": row["verifier"]})
    if r.status_code >= 400 or "id_token" not in r.json():
        raise SocialError(f"{p['name']} didn't confirm the sign-in")

    claims = _claims(r.json()["id_token"])
    if claims.get("iss") not in p["issuers"]:
        raise SocialError("unexpected issuer")
    aud = claims.get("aud")
    if (aud if isinstance(aud, list) else [aud]).count(p["id"]()) != 1:
        raise SocialError("token was not issued for CreAI")
    if int(claims.get("exp") or 0) < time.time():
        raise SocialError("the sign-in expired — please try again")
    if not secrets.compare_digest(str(claims.get("nonce", "")), row["nonce"]):
        raise SocialError("sign-in could not be verified")
    subject, email = str(claims.get("sub") or ""), (claims.get("email") or "").lower().strip()
    if not subject or not email:
        raise SocialError(f"{p['name']} didn't share an email address")
    verified = claims.get("email_verified") in (True, "true")

    async with conn() as c:
        linked = await c.fetchval(
            """SELECT u.email FROM identities i JOIN users u ON u.id=i.user_id
               WHERE i.provider=$1 AND i.subject=$2""", provider, subject)
        if linked:
            email = linked
        else:
            exists = await c.fetchval("SELECT 1 FROM users WHERE email=$1", email)
            if exists and not verified:
                raise SocialError("that email already has an account — sign in with your email code")
            if not verified:
                raise SocialError(f"please verify your email with {p['name']} first")
        token, _ = await T.start_session(c, email, claims.get("name"))
        if not linked:
            uid = await c.fetchval("SELECT id FROM users WHERE email=$1", email)
            await c.execute(
                """INSERT INTO identities (provider, subject, user_id, email) VALUES ($1,$2,$3,$4)
                   ON CONFLICT (provider, subject) DO NOTHING""", provider, subject, uid, email)
        handoff = secrets.token_urlsafe(32)
        await c.execute(
            """INSERT INTO login_states (id, kind, token_enc, expires_at)
               VALUES ($1,'handoff',$2,$3)""",
            hashlib.sha256(handoff.encode()).hexdigest(), vault.seal({"t": token}),
            datetime.now(timezone.utc) + HANDOFF_TTL)
    return handoff


async def redeem_handoff(handoff: str) -> str:
    async with conn() as c:
        row = await c.fetchrow(
            """DELETE FROM login_states WHERE id=$1 AND kind='handoff' AND expires_at > now()
               RETURNING token_enc""", hashlib.sha256(handoff.encode()).hexdigest())
    if not row:
        raise SocialError("that sign-in has expired — please try again")
    return vault.open_(row["token_enc"])["t"]
