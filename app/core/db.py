"""
Database: organisations, members, and strict tenant scoping.

The model: a **user** is a person with an email. An **organisation** is the tenant
that owns everything. A **membership** joins them with a role. Every business
table is keyed to `org_id`, never to a user — so ownership survives a person
leaving, and no query can span tenants by following a user.

Platform admins are a separate table, not a flag on a user. A compromised
customer account must never be one boolean away from reading every tenant.
"""

import json
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg

from .config import settings

_pool: Optional[asyncpg.Pool] = None

ROLES = ("owner", "admin", "member", "viewer")

SCHEMA = """
-- ---------------------------------------------------------------- identity
CREATE TABLE IF NOT EXISTS users (
  id            BIGSERIAL PRIMARY KEY,
  email         TEXT UNIQUE NOT NULL,
  name          TEXT,
  surface       TEXT NOT NULL DEFAULT 'guided',   -- guided | developer
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS organizations (
  id            BIGSERIAL PRIMARY KEY,
  name          TEXT NOT NULL,
  slug          TEXT UNIQUE NOT NULL,
  plan          TEXT NOT NULL DEFAULT 'free',
  seats         INT  NOT NULL DEFAULT 3,
  status        TEXT NOT NULL DEFAULT 'active',   -- active | suspended
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memberships (
  org_id     BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role       TEXT NOT NULL DEFAULT 'member',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, user_id)
);
CREATE INDEX IF NOT EXISTS memberships_user_idx ON memberships(user_id);

CREATE TABLE IF NOT EXISTS invitations (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  email       TEXT NOT NULL,
  role        TEXT NOT NULL DEFAULT 'member',
  token_hash  TEXT UNIQUE NOT NULL,
  invited_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
  expires_at  TIMESTAMPTZ NOT NULL,
  accepted_at TIMESTAMPTZ,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Platform staff. Deliberately its own table: never a column on users, so no
-- customer account can be escalated into cross-tenant access by flipping a bit.
CREATE TABLE IF NOT EXISTS platform_admins (
  user_id    BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  level      TEXT NOT NULL DEFAULT 'support',     -- support | engineer | owner
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every cross-tenant read is recorded: who, when, which tenant, and why.
CREATE TABLE IF NOT EXISTS admin_access_log (
  id         BIGSERIAL PRIMARY KEY,
  admin_id   BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  org_id     BIGINT REFERENCES organizations(id) ON DELETE SET NULL,
  action     TEXT NOT NULL,
  reason     TEXT,
  at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS admin_log_idx ON admin_access_log(at DESC);

-- ---------------------------------------------------------------- sessions
CREATE TABLE IF NOT EXISTS login_codes (
  code_hash   TEXT PRIMARY KEY,
  email       TEXT NOT NULL,
  expires_at  TIMESTAMPTZ NOT NULL,
  used_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash  TEXT PRIMARY KEY,
  user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  org_id      BIGINT REFERENCES organizations(id) ON DELETE SET NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen   TIMESTAMPTZ
);

-- Work done before there is an account. No org_id by design: a draft belongs to
-- nobody until it is claimed. Expires, so abandoned drafts do not accumulate.
CREATE TABLE IF NOT EXISTS drafts (
  id         BIGSERIAL PRIMARY KEY,
  token      TEXT UNIQUE NOT NULL,
  brief      TEXT NOT NULL,
  answers    JSONB NOT NULL DEFAULT '{}'::jsonb,
  claimed_at TIMESTAMPTZ,
  claimed_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS drafts_expiry_idx ON drafts(expires_at);

-- ---------------------------------------------------------------- tenant data
-- Everything below is keyed to org_id. No business table is keyed to a user:
-- ownership belongs to the tenant, not the person who happened to click.
CREATE TABLE IF NOT EXISTS projects (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  created_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
  name        TEXT NOT NULL,
  path        TEXT NOT NULL DEFAULT 'launch',
  status      TEXT NOT NULL DEFAULT 'draft',
  brief       TEXT,
  answers     JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS projects_org_idx ON projects(org_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS domains (
  id           BIGSERIAL PRIMARY KEY,
  org_id       BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id   BIGINT REFERENCES projects(id) ON DELETE SET NULL,
  name         TEXT NOT NULL,
  source       TEXT NOT NULL DEFAULT 'registered',
  zone_id      TEXT,
  verify_token TEXT,
  status       TEXT NOT NULL DEFAULT 'pending',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  verified_at  TIMESTAMPTZ,
  UNIQUE (org_id, name)
);
CREATE INDEX IF NOT EXISTS domains_org_idx ON domains(org_id);

CREATE TABLE IF NOT EXISTS dns_records (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  domain_id   BIGINT NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
  type        TEXT NOT NULL,
  name        TEXT NOT NULL,
  value       TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending',
  provider_id TEXT,
  checked_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS dns_org_idx ON dns_records(org_id, domain_id);

CREATE TABLE IF NOT EXISTS deployments (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id  BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  status      TEXT NOT NULL DEFAULT 'queued',
  url         TEXT,
  log         JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS deploy_org_idx ON deployments(org_id, created_at DESC);

-- Per-tenant channel credentials, encrypted by the application before they
-- reach this table.
CREATE TABLE IF NOT EXISTS channels (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  network     TEXT NOT NULL,
  handle      TEXT,
  external_id TEXT,
  secret_enc  BYTEA,
  status      TEXT NOT NULL DEFAULT 'connected',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (org_id, network, external_id)
);

-- Every generated artefact waits here for a decision by a member allowed to
-- make it. No code path publishes a pending row.
CREATE TABLE IF NOT EXISTS approvals (
  id            BIGSERIAL PRIMARY KEY,
  org_id        BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id    BIGINT REFERENCES projects(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL,
  payload       JSONB NOT NULL,
  state         TEXT NOT NULL DEFAULT 'pending',
  scheduled_for TIMESTAMPTZ,
  decided_by    BIGINT REFERENCES users(id) ON DELETE SET NULL,
  decided_at    TIMESTAMPTZ,
  executed_at   TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS approvals_queue_idx ON approvals(org_id, state, created_at DESC);

CREATE TABLE IF NOT EXISTS events (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id  BIGINT REFERENCES projects(id) ON DELETE SET NULL,
  actor_id    BIGINT REFERENCES users(id) ON DELETE SET NULL,
  kind        TEXT NOT NULL,
  detail      TEXT,
  at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_org_idx ON events(org_id, at DESC);

-- Credits. An append-only ledger: the balance is the sum, nothing is ever
-- edited in place. `ref` makes grants and purchases idempotent — a Stripe
-- session id or a signup marker can only ever be credited once.
CREATE TABLE IF NOT EXISTS credit_ledger (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  delta       INT NOT NULL,
  reason      TEXT NOT NULL,          -- signup | purchase | usage | refund | adjustment
  ref         TEXT UNIQUE,
  detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
  actor_id    BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS credit_ledger_org_idx ON credit_ledger(org_id, created_at DESC);

-- Connections to a customer's existing tools (Webflow first). Tokens are
-- encrypted by the application before they reach this table.
CREATE TABLE IF NOT EXISTS connections (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  provider    TEXT NOT NULL,
  label       TEXT,
  secret_enc  BYTEA NOT NULL,
  meta        JSONB NOT NULL DEFAULT '{}'::jsonb,
  status      TEXT NOT NULL DEFAULT 'connected',
  created_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (org_id, provider)
);

-- In-flight OAuth handshakes: which workspace started it, and the PKCE secret.
CREATE TABLE IF NOT EXISTS oauth_states (
  state       TEXT PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider    TEXT NOT NULL,
  verifier    TEXT NOT NULL,
  expires_at  TIMESTAMPTZ NOT NULL
);

-- Social sign-in: which outside account belongs to which person.
CREATE TABLE IF NOT EXISTS identities (
  provider    TEXT NOT NULL,
  subject     TEXT NOT NULL,
  user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  email       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (provider, subject)
);

-- Short-lived sign-in handshakes and one-time handoff codes (no org yet).
CREATE TABLE IF NOT EXISTS login_states (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,          -- start | handoff
  provider    TEXT,
  verifier    TEXT,
  nonce       TEXT,
  token_enc   BYTEA,
  expires_at  TIMESTAMPTZ NOT NULL
);

-- Our own registration with each provider (platform-level, not per tenant).
CREATE TABLE IF NOT EXISTS oauth_clients (
  provider      TEXT NOT NULL,
  redirect_uri  TEXT NOT NULL,
  client_id     TEXT NOT NULL,
  secret_enc    BYTEA,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (provider, redirect_uri)
);
"""

# Tables that must never be read without an org filter. The isolation test reads
# this list, so adding a table here is how it gets covered.
TENANT_TABLES = (
    "projects", "domains", "dns_records", "deployments",
    "channels", "approvals", "events", "credit_ledger", "connections", "oauth_states",
)


async def _init_conn(c: asyncpg.Connection) -> None:
    """Let JSONB columns take and return Python dicts.

    Without this asyncpg demands a pre-serialised string, which is easy to get
    right at one call site and easy to forget at the next. Registering the codec
    once means every query is consistent.
    """
    await c.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads,
                           schema="pg_catalog")
    await c.set_type_codec("json", encoder=json.dumps, decoder=json.loads,
                           schema="pg_catalog")


async def connect() -> None:
    global _pool
    if not settings.database_url:
        return
    _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=8,
                                      init=_init_conn)
    async with _pool.acquire() as c:
        await c.execute(SCHEMA)


async def disconnect() -> None:
    if _pool:
        await _pool.close()


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database not configured")
    return _pool


@asynccontextmanager
async def conn():
    async with pool().acquire() as c:
        yield c


async def log_event(org_id: int, kind: str, detail: str = "",
                    project_id: int | None = None, actor_id: int | None = None) -> None:
    async with conn() as c:
        await c.execute(
            """INSERT INTO events (org_id, project_id, actor_id, kind, detail)
               VALUES ($1,$2,$3,$4,$5)""",
            org_id, project_id, actor_id, kind, detail)
