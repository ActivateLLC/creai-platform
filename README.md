# CreAI Platform

Build-to-launch. Generate a site, register a domain, deploy it, market it.

Competitors stop at a preview. This goes to a domain that sells something — DNS,
TLS, social channels and payments included. That last third is the wedge.

## Where this is

**Phase 0 — the launch chain.** Auth, projects, domains, DNS, the approval queue
and the dashboard payload.

**Phase 1, started — the agent.** A conversation beside a live preview
(`app/web`, served at `/`). The agent edits a validated site spec, records facts,
queues post drafts and suggests actions; it has no tool that registers, publishes,
spends or connects anything. Deployment to customer domains is Phase 2.

Phase 0 is deliberately first: if deploy → DNS → TLS cannot be made reliable and
repeatable, the wedge does not hold and the plan should change before any
generation work starts.

## Licence policy — not negotiable

Every dependency is MIT, Apache-2.0, BSD or ISC. No AGPL, no Polyform, no
source-available licence anywhere a customer touches. This platform is resold;
a restrictive dependency is a business risk, not a detail.

Postiz is AGPL and runs as a **separate service**, called over HTTP. None of its
code is vendored here.

## Layout

```
app/
  core/config.py     every external dependency, named in one place
  core/db.py         schema; tenant isolation rules
  core/auth.py       passwordless sign-in, sessions
  services/dns.py    Cloudflare; the rules about what we never touch
  services/mailer.py Resend; best-effort, never raises into a request
  services/agent.py  the conversation loop; tools are bound to one draft or project
  services/site.py   site spec and renderer; the model never writes HTML
  api/               auth, projects, domains, approvals, dashboard, agent
  web/               the app itself: chat, live preview, sign-in, posts, domain
```

## Two rules the code enforces

**Tenant isolation.** Every query is scoped by `account_id`. One misrouted job
publishing tenant A's content to tenant B's account is a business-ending
incident, not a bug.

**Nothing acts without approval.** Every generated artefact — a post, a DNS
change, a filing — lands in `approvals` as `pending`. No code path publishes a
pending row. Approving records a decision; a worker performs it, so a slow
provider can never make a click hang or double-fire.

## DNS safety

`services/dns.py` writes only the records needed to route a site and never
touches `MX`, `NS`, `SRV`, `CAA` or `SOA`. Nobody's email goes down because they
pointed a domain at us. Propagation is confirmed by resolving from outside, not
by trusting our own write.

## Environment

| Variable | For |
| --- | --- |
| `DATABASE_URL` | Postgres. Schema is created on boot |
| `SECRET_KEY` | Session signing |
| `PUBLIC_URL` | This API's public address, used for CORS |
| `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` | DNS |
| `ORIGIN_CNAME` | Where customer domains point (apex and www, both CNAME) |
| `ANTHROPIC_API_KEY`, `AGENT_MODEL` | The agent. Without a key, `/v1/agent/*` returns 503 |
| `RESEND_API_KEY`, `MAIL_FROM` | Sign-in codes and notifications |
| `RAILWAY_TOKEN`, `RAILWAY_PROJECT_ID` | Deploys (Phase 2) |
| `POSTIZ_URL`, `POSTIZ_API_KEY` | Publishing (Phase 3) |

`GET /health` reports which of these are configured rather than claiming to be
fine. A deployment missing its DNS credential is not healthy in any useful sense.

## Not built yet

- Deploys to customer domains (Phase 2); the agent's streaming replies
- The worker that executes approved rows
- Registrar integration — `/v1/domains/search` returns `availability: unknown`
  rather than inventing prices. A fake "available" becomes a failed purchase,
  which is worse than an honest unknown.
- Stripe Connect, company formation, per-seat roles

## Design

The interface is specified separately: a design system with tokens, components
and guidelines, and clickable prototypes of onboarding, the launch flow and the
dashboard. Build from those rather than inventing screens.
