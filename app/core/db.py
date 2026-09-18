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
-- A post that will go out on its own unless it is stopped. The window is when it
-- becomes cancellable-until rather than waiting on a yes.
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS autonomous BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS holds_until TIMESTAMPTZ;

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

-- App projects: the code the agent writes, and the data those apps save.
CREATE TABLE IF NOT EXISTS project_files (
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id  BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  path        TEXT NOT NULL,
  content     TEXT NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, path)
);
CREATE TABLE IF NOT EXISTS app_records (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id  BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  collection  TEXT NOT NULL,
  data        JSONB NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS app_records_idx ON app_records(project_id, collection, id);
ALTER TABLE app_records ADD COLUMN IF NOT EXISTS app_user_id BIGINT;
CREATE INDEX IF NOT EXISTS app_records_owner_idx ON app_records(project_id, collection, app_user_id);

-- People who sign in to an app a customer built. Their own accounts, not Creai
-- accounts: scoped to one project, and deleted with it. The password hash is
-- scrypt; the app never sees it and neither does the owner.
CREATE TABLE IF NOT EXISTS app_users (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id  BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  email       TEXT NOT NULL,
  name        TEXT,
  pw          TEXT NOT NULL,
  blocked     BOOLEAN NOT NULL DEFAULT false,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  seen_at     TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS app_users_email_idx ON app_users(project_id, lower(email));

-- Files handed over by the people who use an app: a receipt, a photo of the job,
-- a signed form. Scoped to the app, and to one of its users when the collection
-- is private. Served through the API, never by a public URL.
CREATE TABLE IF NOT EXISTS app_files (
  id           BIGSERIAL PRIMARY KEY,
  org_id       BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id   BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  collection   TEXT NOT NULL,
  app_user_id  BIGINT,
  name         TEXT NOT NULL,
  mime         TEXT NOT NULL,
  kind         TEXT NOT NULL,
  size         BIGINT NOT NULL,
  key          TEXT NOT NULL,
  token        TEXT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS app_files_idx ON app_files(project_id, collection, id DESC);
CREATE INDEX IF NOT EXISTS app_files_owner_idx ON app_files(project_id, app_user_id);

-- The customer's own Stripe account, so their buyers pay them and not us.
CREATE TABLE IF NOT EXISTS payment_accounts (
  id              BIGSERIAL PRIMARY KEY,
  org_id          BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id      BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE UNIQUE,
  stripe_account  TEXT NOT NULL,
  ready           BOOLEAN NOT NULL DEFAULT false,
  checked_at      TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every charge started through an app, with Creai's fee recorded at the time.
CREATE TABLE IF NOT EXISTS payments (
  id           BIGSERIAL PRIMARY KEY,
  org_id       BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id   BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  app_user_id  BIGINT,
  session_id   TEXT NOT NULL UNIQUE,
  amount       BIGINT NOT NULL,
  currency     TEXT NOT NULL,
  fee          BIGINT NOT NULL,
  label        TEXT,
  reference    TEXT,
  status       TEXT NOT NULL DEFAULT 'open',
  settled_at   TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS payments_idx ON payments(project_id, id DESC);

-- Every fault the reviewers catch before a reply goes out. Individually these are
-- already handled; in aggregate they say where the agent is weak, which is the
-- only list worth working from. Kept short: the shape of the fault, not the copy.
CREATE TABLE IF NOT EXISTS quality_findings (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id  BIGINT REFERENCES projects(id) ON DELETE SET NULL,
  surface     TEXT NOT NULL,          -- site | app | game
  fault       TEXT NOT NULL,          -- a normalised shape, not the sentence
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS quality_findings_idx ON quality_findings(created_at DESC);
CREATE INDEX IF NOT EXISTS quality_findings_fault_idx ON quality_findings(fault, created_at DESC);

-- A site somebody already built, uploaded or pulled from a repo. The files live
-- in the bucket; this remembers what is in the release and where it may be served.
CREATE TABLE IF NOT EXISTS site_imports (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id  BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  slug        TEXT NOT NULL,
  source      TEXT NOT NULL,            -- upload | repo
  origin      TEXT,                     -- the repo, when it came from one
  manifest    JSONB NOT NULL,           -- path -> {key, mime, size}
  files       INT NOT NULL,
  bytes       BIGINT NOT NULL,
  live        BOOLEAN NOT NULL DEFAULT false,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS site_imports_idx ON site_imports(project_id, id DESC);
CREATE INDEX IF NOT EXISTS site_imports_slug_idx ON site_imports(slug, id DESC);

-- A video: a script cut into scenes, each with its own picture, motion and line.
-- The plan is kept apart from the render so a person can change a line without
-- paying to generate everything again.
CREATE TABLE IF NOT EXISTS videos (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id  BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  created_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
  title       TEXT NOT NULL DEFAULT '',
  shape       TEXT NOT NULL DEFAULT 'vertical',   -- vertical | square | wide
  plan        JSONB NOT NULL DEFAULT '{}'::jsonb, -- scenes, voice, music brief
  state       TEXT NOT NULL DEFAULT 'draft',      -- draft|rendering|ready|failed
  progress    INTEGER NOT NULL DEFAULT 0,
  seconds     NUMERIC,
  credits     INTEGER,
  asset_key   TEXT,                               -- the finished mp4 in the bucket
  poster_key  TEXT,
  error       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS videos_project_idx ON videos(project_id, id DESC);
CREATE INDEX IF NOT EXISTS videos_state_idx ON videos(state, id) WHERE state='rendering';

-- Workspace spending cap (credits per calendar month; NULL = no cap) and the
-- registrant contact for domains (encrypted).
CREATE TABLE IF NOT EXISTS org_settings (
  org_id       BIGINT PRIMARY KEY REFERENCES organizations(id) ON DELETE CASCADE,
  monthly_cap  INTEGER,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE org_settings ADD COLUMN IF NOT EXISTS registrant_enc TEXT;
ALTER TABLE org_settings ADD COLUMN IF NOT EXISTS low_alert_at TIMESTAMPTZ;
ALTER TABLE org_settings ADD COLUMN IF NOT EXISTS empty_alert_at TIMESTAMPTZ;
-- The moment the owner said the brand is right. Autonomy is gated on it, because
-- posting in somebody's voice before they have agreed what their voice is is the
-- one mistake that cannot be taken back.
ALTER TABLE org_settings ADD COLUMN IF NOT EXISTS brand_confirmed_at TIMESTAMPTZ;
-- One switch that stops everything, instantly, across every channel.
ALTER TABLE org_settings ADD COLUMN IF NOT EXISTS social_paused BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE org_settings ADD COLUMN IF NOT EXISTS weekly_post_cap INTEGER NOT NULL DEFAULT 14;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS checked_at TIMESTAMPTZ;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS last_opened_at TIMESTAMPTZ;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS thumb_token TEXT;
-- Shown in the public gallery, only ever because the owner chose to. Hidden is a
-- separate flag so support can take something down without silently flipping the
-- owner's own choice back off.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS showcase BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS showcase_hidden BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS remixes INT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS projects_showcase_idx ON projects(showcase, updated_at DESC)
  WHERE showcase AND NOT showcase_hidden;
ALTER TABLE domains ADD COLUMN IF NOT EXISTS hosting JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE domains ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE domains ADD COLUMN IF NOT EXISTS renewal_credits INTEGER;


-- Errors reported by app previews, so fixing them can be free.
CREATE TABLE IF NOT EXISTS app_errors (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id  BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  message     TEXT NOT NULL,
  free_fixes  INTEGER NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, message)
);

-- Plans (Stripe subscriptions). One row per workspace; only webhooks change it.
CREATE TABLE IF NOT EXISTS subscriptions (
  org_id               BIGINT PRIMARY KEY REFERENCES organizations(id) ON DELETE CASCADE,
  stripe_customer      TEXT,
  stripe_subscription  TEXT,
  plan                 TEXT NOT NULL DEFAULT 'free',
  interval             TEXT,
  status               TEXT NOT NULL DEFAULT 'none',
  current_period_end   TIMESTAMPTZ,
  cancel_at_period_end BOOLEAN NOT NULL DEFAULT false,
  domain_claimed       BOOLEAN NOT NULL DEFAULT false,
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Files people upload: photos, videos (with extracted stills as children) and PDFs.
CREATE TABLE IF NOT EXISTS assets (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id  BIGINT REFERENCES projects(id) ON DELETE SET NULL,
  parent_id   BIGINT REFERENCES assets(id) ON DELETE CASCADE,
  created_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
  kind        TEXT NOT NULL,                  -- image | video | pdf
  mime        TEXT NOT NULL,
  name        TEXT NOT NULL,
  size        BIGINT NOT NULL,
  width       INTEGER,
  height      INTEGER,
  duration    REAL,
  key         TEXT NOT NULL UNIQUE,
  token       TEXT NOT NULL UNIQUE,
  status      TEXT NOT NULL DEFAULT 'pending', -- pending | ready | deleted
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS assets_org_idx ON assets(org_id, status, id DESC);

-- Launch readiness: the person's decisions on checklist items, and the agent's ideas.
CREATE TABLE IF NOT EXISTS readiness_decisions (
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id  BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  item        TEXT NOT NULL,
  decision    TEXT NOT NULL,                 -- skip | later
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, item)
);
CREATE TABLE IF NOT EXISTS readiness_suggestions (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id  BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title       TEXT NOT NULL,
  why         TEXT NOT NULL,
  request     TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'open',  -- open | applied | skipped
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS readiness_suggestions_idx ON readiness_suggestions(project_id, status);

-- Published site snapshots (rendered HTML — sites never carry script).
CREATE TABLE IF NOT EXISTS site_releases (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id  BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  slug        TEXT NOT NULL,
  html        TEXT NOT NULL,
  live        BOOLEAN NOT NULL DEFAULT true,
  created_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS site_releases_slug_idx ON site_releases(slug, id DESC);
CREATE INDEX IF NOT EXISTS site_releases_project_idx ON site_releases(project_id, id DESC);

-- A priced offer for a domain, valid for a few minutes, so the customer pays
-- exactly what they were shown.
CREATE TABLE IF NOT EXISTS domain_quotes (
  id          TEXT PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  domain      TEXT NOT NULL,
  cost_usd    NUMERIC(10,2) NOT NULL,
  credits     INTEGER NOT NULL,
  renewal_credits INTEGER,
  state       TEXT NOT NULL DEFAULT 'open',      -- open | buying | bought | failed
  expires_at  TIMESTAMPTZ NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE domain_quotes ADD COLUMN IF NOT EXISTS included BOOLEAN NOT NULL DEFAULT false;

-- Published app snapshots and their public addresses.
CREATE TABLE IF NOT EXISTS app_releases (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id  BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  slug        TEXT NOT NULL,
  files       JSONB NOT NULL,
  site        JSONB NOT NULL DEFAULT '{}'::jsonb,
  live        BOOLEAN NOT NULL DEFAULT true,
  created_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS app_releases_slug_idx ON app_releases(slug, id DESC);
CREATE INDEX IF NOT EXISTS app_releases_project_idx ON app_releases(project_id, id DESC);

-- Godot exports. A build is slow enough to be a job the person watches, and big
-- enough that its files live in the bucket: `prefix` is where, `names` is what.
CREATE TABLE IF NOT EXISTS game_builds (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id  BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  created_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
  state       TEXT NOT NULL DEFAULT 'queued',  -- queued|importing|exporting|packaging|done|failed
  progress    INTEGER NOT NULL DEFAULT 0,
  threads     BOOLEAN NOT NULL DEFAULT false,
  prefix      TEXT,
  names       JSONB NOT NULL DEFAULT '[]'::jsonb,
  bytes       BIGINT,
  seconds     INTEGER,
  credits     INTEGER,
  log         TEXT,
  error       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS game_builds_project_idx ON game_builds(project_id, id DESC);

-- A published game points at the build it serves, so publishing costs nothing.
CREATE TABLE IF NOT EXISTS game_releases (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id  BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  build_id    BIGINT NOT NULL REFERENCES game_builds(id) ON DELETE CASCADE,
  slug        TEXT NOT NULL,
  title       TEXT NOT NULL DEFAULT '',
  live        BOOLEAN NOT NULL DEFAULT true,
  created_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS game_releases_slug_idx ON game_releases(slug, id DESC);
CREATE INDEX IF NOT EXISTS game_releases_project_idx ON game_releases(project_id, id DESC);

-- Social accounts connected through Postiz, each owned by exactly one workspace.
CREATE TABLE IF NOT EXISTS social_channels (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  postiz_id   TEXT NOT NULL UNIQUE,
  network     TEXT NOT NULL,
  identifier  TEXT NOT NULL,
  name        TEXT,
  picture     TEXT,
  status      TEXT NOT NULL DEFAULT 'active',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Posting without asking first, per channel, and only after the owner has said the
-- brand is right. Off until they turn it on.
ALTER TABLE social_channels ADD COLUMN IF NOT EXISTS autonomous BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS social_channels_org_idx ON social_channels(org_id, network);

-- Short-lived sign-in handshakes and one-time handoff codes (no org yet).
CREATE TABLE IF NOT EXISTS login_states (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,          -- start | handoff
  provider    TEXT,
  verifier    TEXT,
  nonce       TEXT,
  token_enc   BYTEA,
  client      TEXT,
  expires_at  TIMESTAMPTZ NOT NULL
);
ALTER TABLE login_states ADD COLUMN IF NOT EXISTS client TEXT;

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
    "channels", "approvals", "events", "credit_ledger", "connections", "oauth_states", "social_channels", "project_files", "app_records", "app_users", "app_files", "payment_accounts", "payments", "quality_findings", "site_imports", "videos", "org_settings", "app_errors", "app_releases", "site_releases", "domain_quotes", "subscriptions", "readiness_decisions", "readiness_suggestions", "assets",
    "game_builds", "game_releases",
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
        # Balances can't go negative any more; bring any from before that fix back to zero, once.
        await c.execute(
            """INSERT INTO credit_ledger (org_id, delta, reason, ref, detail)
               SELECT org_id, -SUM(delta), 'adjustment', 'floor-2026-09:' || org_id,
                      '{"why": "balance brought back to zero after a pricing fix"}'::jsonb
               FROM credit_ledger GROUP BY org_id HAVING SUM(delta) < 0
               ON CONFLICT (ref) DO NOTHING""")


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
