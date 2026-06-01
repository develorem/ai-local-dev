# 02 — Architecture (decided)

## One codebase, two deploy modes
A single TS codebase ships in two modes (`DEPLOY_MODE = cloud | selfhosted`):

| | Cloud | Self-hosted |
|---|---|---|
| Platform | Cloudflare (Workers/Pages/D1) | Docker (compose bundle) |
| Tenancy | Multi-tenant (orgs/members/roles) | Single-tenant (one implicit org) |
| Billing | Stripe subscription | One-off license + optional support sub |
| Admin | Sign-up | Pre-seeded `admin` (env-seeded, forced change) |
| Data | D1 (SQLite) | SQLite file |
| TLS | Cloudflare | Bundled Caddy / self-signed (LAN) |

Mode gates a *small* surface: tenancy enforcement, billing, admin-seed, sign-up,
and model-catalog source. Everything else is identical. (Observability is a
tracked action item — see 00-decisions.)

## Four independently deployable tiers
UI, BFF, API, MCP are **separate services**, each deployable on its own — "a bug
in the API means only the API redeploys." They communicate over HTTP (no shared
process). The domain logic lives in one place: the **API owns `core` + the DB**
and exposes the stable `/v1` contract; everything else is a client of it.

```
 browser ─▶ UI (React SPA) ─▶ BFF ─▶ API ─┐
 mobile (later) ───────────────────▶ API  ├─▶ core ─▶ DB
 LLM tools ──────────────▶ MCP ───▶ API   │
 agents ───────────────────────────▶ API ─┘
```

- **API** — the single domain owner. Public `/v1` REST + the agent contract +
  auth (issues JWTs) + secrets/vault. Imports `core`; the only tier touching the DB.
- **BFF** — web backend-for-frontend: web session/JWT ergonomics + screen-shaped
  aggregation. Calls the API. Serves the React SPA's needs only.
- **MCP** — LLM-tool surface; a thin client of the API (so MCP can never drift from
  or bypass the API contract — the v1 coupling bug is structurally impossible).
- **UI** — React SPA; calls the BFF.

External tools/agents therefore choose **API (REST) or MCP** — both resolve to the
same API contract. Mobile (later) calls the API directly (its own BFF if needed).

## Cloud topology (Cloudflare — launch on the FREE tier)
- UI → **Pages** (free); BFF/API/MCP → **Workers** (free, ~100k req/day, BFF↔API
  via service bindings); DB → **D1** (free tier); scheduler → **Cron Triggers**
  (free); KV/R2 free if needed. Start on `*.pages.dev`/`*.workers.dev`; custom
  domain optional later.
- Independent tiers map cleanly to separate Workers/Pages deploys.
- **Known scale trigger:** agents poll for work, so enough busy agents can exceed
  the free 100k req/day cap → move to **Workers Paid ($5/mo, 10M req/mo)**. Not a
  launch blocker (local agents cost us nothing — they run on user hardware).
- **Leased agents (later)** → **Containers (paid)**; a Pro+ revenue feature, so
  deferred and cost-covered. The free launch doesn't depend on it.

## Self-hosted topology
- Same code as Node containers, shipped as a **one-command `docker compose`
  bundle** (UI, BFF, API, MCP + Caddy for TLS) — still independently updatable
  images. SQLite file on a volume. Single implicit org. Pre-seeded `admin`.
- Must support **fully air-gapped** operation (no outbound internet): local
  license verification, no required phone-home.

## The executor (agent) is separate, and standalone
Agents are **not** part of the server. An agent is a downloadable, fully
standalone unit that runs locally, **owns its own git workspace**, does the work
on its own machine, and only **pulls tasks / reports status** via the API (or
MCP). It has its own local dashboard UI. See `03-agents-and-models.md`.

## Data
Greenfield SQLite schema via Drizzle (drizzle-kit migrations). Carry the v1 entity
set (projects, outcomes, tasks, plans, questions, agents, secrets/vault,
documents+index, scheduled tasks, events) — including the **project↔secrets** and
**project↔agents** join tables. One schema serves both modes; self-hosted resolves
to a single implicit org. Schema is migratable/deployable via drizzle-kit.
