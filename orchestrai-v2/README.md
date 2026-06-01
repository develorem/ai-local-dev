# OrchestrAi v2 — design

Clean-slate design for OrchestrAi, to be lifted into a dedicated new repo.

**Why v2:** the v1 implementation (in `../orchestrai`) evolved out of a different
project (a local-model benchmark harness), so several foundational choices —
one transport-coupled API, Python throughout, an ad-hoc container split — are
wrong for where the product is going. v1 stays as a **reference** (do not delete);
v2 starts fresh with the decisions below.

## Status
Design **decisions made** (kickoff brief + answered questionnaire). Canonical
record is `docs/00-decisions.md`; the other docs expand each area. `OPEN-QUESTIONS.md`
is kept as the raw answered record. Remaining open items are listed as
**Action items / carried-forward** in `docs/00-decisions.md` to resolve in the new
repo. Next step: lift this folder into the dedicated OrchestrAi repo and scaffold.

## Documents
- `docs/00-decisions.md` — **canonical decisions log + carried-forward items (start here)**
- `docs/01-vision.md` — product, positioning, guiding principles
- `docs/02-architecture.md` — tiers (UI/BFF/API/MCP), deploy modes, Cloudflare/self-hosted
- `docs/03-agents-and-models.md` — executor contract, agent appliance + UI, model catalog, benchmark app
- `docs/04-auth-and-tenancy.md` — auth (JWT + per-agent tokens), single vs multi-tenant
- `docs/05-distribution-and-billing.md` — self-hosted vs cloud, plans, licensing
- `docs/06-tech-stack.md` — the decided stack (Hono/React/Drizzle/SQLite/Cloudflare)
- `docs/07-agent-executor.md` — **the agent execution pipeline to PORT from v1 (don't reinvent)**
- `docs/reference/v1-agent/` — **the v1 agent source (Python) bundled as read-only
  reference for doc 07. Copy this over too — it's what "port from v1" ports from.**
- `OPEN-QUESTIONS.md` — the answered questionnaire (raw record)

## Decisions
See **`docs/00-decisions.md`** for the canonical, current decision set (it
supersedes the original kickoff bullets — e.g. the four tiers are now
*independently deployable* rather than one container, and the cloud stack is
Hono + Drizzle + SQLite/D1 on Cloudflare). The raw answered questionnaire is in
`OPEN-QUESTIONS.md`.
