# 06 — Tech stack (decided)

## Locked
- **Language:** Node.js + TypeScript, end to end.
- **Cloud platform:** **Cloudflare, launching on the free tier** —
  **Pages** (UI, free), **Workers** (BFF/API/MCP, free ~100k req/day),
  **D1** (SQLite data, free tier), **Cron Triggers** (scheduler, free), KV/R2 free
  if needed. Custom domain optional (start on `*.pages.dev`/`*.workers.dev`).
  Leased agents later use **Containers** (paid — but a Pro+ revenue feature, so
  deferred + cost-covered).
- This free target is **why the framework is Hono, not NestJS**: a free launch must
  run on Workers, NestJS can only run on Cloudflare via **Containers (paid)**, and
  Hono runs unmodified on both Workers (cloud) and Node (self-hosted) — so one
  codebase serves both modes.
- **API/BFF/MCP framework:** **Hono** (Workers-native, also runs on Node/Bun).
- **Frontend:** **React** SPA (web). **React Native** planned for mobile later —
  so the API is the shared contract both consume (web via the BFF, mobile direct).
- **DB:** **SQLite dialect everywhere** — Cloudflare **D1** in cloud, a SQLite file
  in self-hosted. (Postgres is a possible later cloud option; not at launch.)
- **ORM + migrations:** **Drizzle** + **drizzle-kit** (the schema/data deploy tool).
- **Monorepo:** **pnpm workspaces + Turborepo**.
- **MCP:** official MCP TypeScript SDK, running as its own service that calls the API.
- **User auth:** **JWT** (access + refresh) — issued by the API; works for web now
  and mobile later. Agents use a separate per-agent token (see 03/04).
- **Billing:** Stripe. **Email:** Resend (invites, password reset).
- **TLS:** everywhere. Self-hosted bundles a small auto-TLS reverse proxy (Caddy);
  self-signed for pure-LAN.

## Packages (monorepo layout)
```
packages/
  core         # domain logic + services (imported by api); the single source of truth
  shared       # zod schemas + TS types shared across all surfaces
  db           # drizzle schema + migrations (drizzle-kit)
  license      # signed-license issue/verify (offline-verifiable)
apps/
  api          # Hono — public /v1 API + agent contract; owns the domain + DB
  bff          # Hono — web backend-for-frontend (session/JWT ergonomics, aggregation)
  mcp          # MCP server (TS SDK) — a client of the API
  web          # React SPA
  agent        # the downloadable worker (+ its own local dashboard UI)
  benchmark    # the model test/advisor app (own UI) — productized v1 test-harness
```

## Resolved reversal from the questionnaire
- B3 accepted **NestJS**, but the **free-tier Cloudflare launch** settles it:
  free = Workers, NestJS needs Containers (paid), Hono runs on both Workers and
  Node. **LOCKED: Hono, NestJS dropped.** (Revisit only if we ever abandon the
  free-tier / Workers target.)

## Carried-forward stack items
- **Agent polling vs the Workers free cap (~100k req/day):** tune poll cadence /
  long-poll; past a handful of busy agents, move to **Workers Paid ($5/mo, 10M
  req/mo)**. Known scale trigger, not a launch blocker.
- Auth library choice within Hono (Lucia-style sessions adapted, or a thin
  JWT+passport-equivalent) — decide in the repo.
- Whether cloud later adds Postgres alongside D1.
