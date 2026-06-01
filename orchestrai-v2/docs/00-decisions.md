# 00 — Decisions log (canonical)

Distilled from the answered `OPEN-QUESTIONS.md` (which stays as the raw record).
This is the single source of truth; the other docs expand on it.

## Stack
- Node + TS end-to-end. Monorepo: **pnpm + Turborepo**.
- **Hono** for API/BFF/MCP (runs on Cloudflare Workers *and* Node — replaces the
  earlier NestJS pick because the cloud target is Cloudflare).
- **React** SPA (web); **React Native** mobile later.
- **Drizzle + SQLite** everywhere — **Cloudflare D1** (cloud) / SQLite file
  (self-hosted); drizzle-kit for migrations/deploys.
- **Cloud platform: Cloudflare, launch on the FREE tier** — Pages (UI) + Workers
  (BFF/API/MCP, Hono) + D1 (data) + Cron Triggers (scheduler), all free; KV/R2 free
  if needed; start on `*.pages.dev`/`*.workers.dev`. Containers (leased agents) is
  paid + deferred. **The free/Workers target is what locks Hono over NestJS**
  (NestJS would need paid Containers).
- MCP via the official TS SDK. **Stripe** billing. **Resend** email. **Caddy** TLS (self-hosted).

## Architecture
- One codebase; `DEPLOY_MODE = cloud | selfhosted`. Mode-gated surface kept tiny
  (tenancy, billing, admin-seed, sign-up, catalog source).
- **Four independently deployable tiers: UI, BFF, API, MCP** (a bug in one redeploys
  only that one). HTTP between them; no shared process.
- **API owns the domain (`core`) + DB** and the `/v1` contract; BFF and MCP are
  **clients of the API**; UI is a client of the BFF; agents + mobile call the API.
- Public API versioned **`/v1`** from day one.
- Self-hosted = `docker compose` bundle; cloud = Cloudflare deploys.

## Auth & tenancy
- Users: **username/password + Google**, **JWT** (mobile-ready), API is the auth
  authority. GitHub/SSO later. Self-hosted `admin` env-seeded + forced change.
- Cloud multi-tenant (orgs, roles owner/admin/member, email invites, multi-org per
  user); self-hosted single implicit org. Same schema. Enforcement in `core`.
- Project↔secrets and project↔agents joins retained.
- Agents authenticate with a separate per-agent bearer token.
- **TLS everywhere.**

## Agents & models
- One executor contract; agents are **fully standalone**, **own their git
  workspace**, pull tasks/report via API/MCP (no server-side workspace).
- HTTPS REST + polling transport. Per-agent bearer token; connection delivered as
  an `mcp.json` + unique URL at create-time.
- Agent image = **runtime only, self-installs the model on first run**; agent has
  **its own local dashboard UI** (model, specs, pull/switch models).
- **Model catalog** with an **out-of-band availability/freshness checker**; scores
  are hardware-relative/indicative.
- **Benchmark/advisor is its own app** with a web UI (productized v1 test-harness).
- Many agents per tenant. Future: on-prem multi-GPU host; "pledge Claude Code
  after-hours on a schedule" plugin.
- **Leased agents** = cloud-only, provisioned (Cloudflare Containers / openclaw +
  local LLM), designed-for now / built later.

## Data
- Greenfield. Carry v1 entities (projects, outcomes, tasks, plans, questions,
  agents, secrets/vault, documents+index, scheduled tasks, events). Carry the
  document-index design as-is. Defer discussions/proposed-actions unless wanted.
- Secrets vault carried as-is (encrypted, write-only values, per-task audited fetch).

## Billing & distribution
- Cloud: Free / Pro $4.95 / Team $19.90 (Free = entry, no separate trial). Stripe.
- Self-hosted: purchase-gated (sign up → pay → license + download link). One-off +
  optional support sub (prices TBD). **Signed, offline-verifiable license with
  expiry**; fully air-gap capable.

## Action items / carried-forward (resolve in the new repo)
- **Workers free request cap (~100k/day) vs agent polling:** tune poll cadence /
  long-poll; move to Workers Paid ($5/mo, 10M req/mo) past a handful of busy
  agents. Known scale trigger, not a launch blocker.
- **Observability** strategy (logging/metrics/tracing across tiers) — C3.
- "Download an agent" exact UX (image+config vs CLI) — make it dead simple — F2.
- Self-hosted pricing, support scope, update/upgrade policy — A2/A4.
- License key library/format; offline expiry + revocation — A3.
- Auth library within Hono; exact JWT lifetimes/rotation — B6/E2.
- Whether cloud later adds Postgres; whether mobile gets its own BFF.
- Model catalog contents + the freshness-checker implementation — F4.
