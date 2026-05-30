# Overnight build — progress & assumptions log

Started 2026-05-30 (overnight autonomous batch). Update this after every
increment so work is resumable if the session dies. Each increment = one commit.

## How to resume
Read this file + `git log --oneline`. The next unchecked `[ ]` item is where to
continue. The running stack is rebuilt with
`docker compose up -d --build --force-recreate hub agent` from `orchestrai/`.

## Task breakdown (check off as completed)

### A. Multi-tenancy foundation (backend)
- [x] A1. Migration 012: users/orgs/members/invitations/sessions; org_id on projects+agents; default-org backfill. (commit: multi-tenancy backend)
- [x] A2. Auth: sessions + Google OAuth (env-gated) + dev-login; middleware user principal.
- [x] A3. Org CRUD + membership + invitations API.
- [x] A4. Projects/agents org-scoped; list/create/get enforce; operator=superadmin sees all. Validated live.

### B. Billing (Stripe)
- [x] B1. PLAN_LIMITS config + enforcement (projects, agents, invites). Validated (free caps at 2 projects).
- [x] B2. Stripe checkout/portal/webhook (env-gated). Plan sync on webhook events.
- [x] B3. Leasing FOUNDATION: gated by plan via /billing/leasing stub (status=not_implemented). No provisioning — separate session.

### C. UI overhaul  — NOT browser-tested, needs a visual/click pass
- [x] C1. Login (Google/dev/operator) + top nav (brand, Projects dropdown + All Projects, Questions, Agents, Settings, user avatar menu). Sidebar removed.
- [x] C2. Project page → tabs via project-scoped left menu (#/projects/:id/:tab).
- [x] C3. Responsive CSS (hamburger nav, reflowing tabs/tables, mobile kvs).
- [x] C4. /questions page.
- [x] C5. /settings (orgs, members, roles, invitations, plan + Stripe checkout/portal) + create-org modal + /accept-invite/:token.
- [x] C6. Agent add modal (name → create → token + CLI + mcp.json).

### D. Repo structure + landing
- [x] D1. STRUCTURE.md documents monorepo apps + conventions. (Did NOT move orchestrai/ — too risky overnight; see assumptions.)
- [x] D2. landing/ standalone static marketing page (minimal, branding placeholders, pricing tiers).

## STILL TODO (tomorrow)
- **Browser-test the whole UI** end-to-end (login, tabs, settings, invites, agent modal, mobile). It only passed `node --check` + server smoke tests.
- Harden tenant isolation on sub-resource routes + org-scope secrets (security follow-up below).
- Wire real Google + Stripe keys; test OAuth callback + a real checkout + webhook.
- Branding on landing + app; point landing CTAs at the real app URL.
- Leased agents (own session).

## Assumptions made (review tomorrow)
- Google OAuth env-gated: set GOOGLE_CLIENT_ID/SECRET + PUBLIC_BASE_URL; register
  redirect URI `<PUBLIC_BASE_URL>/api/auth/google/callback` in Google console.
  Until then DEV_LOGIN_ENABLED is on (login by email, no password) so the app works.
- Operator token = superadmin user `user_operator`; Default Org = plan 'team'
  (unlimited) so existing data is never constrained.
- Org creation NOT capped per user — each org is independently free/paid. "Create
  an org for free" read as the free PLAN, not a 1-org cap.
- Plan limits: free = 2 projects / 1 own agent / no invites / no leasing;
  pro $4.95 = 10 projects / unlimited agents / invites (max 10 members) / leasing;
  team $19.90 = unlimited everything. Pro's invite + 10-member cap is an
  assumption (user only specified free=no-invite and team=unlimited-users).
- Invitations return a shareable link; email delivery is NOT wired yet.
- Stripe: price ids via STRIPE_PRICE_PRO/TEAM env; webhook needs
  STRIPE_WEBHOOK_SECRET (dev: unsigned JSON accepted when unset). Account TBD.
- Kept the existing `orchestrai/` app where it is (no risky wholesale move);
  D1 will document structure + add landing as a separate app.

## Open follow-ups / risks
- SECURITY (important): tenant isolation is enforced on projects + agents
  (list/create/get) but NOT yet on sub-resources — tasks, outcomes, documents,
  secrets, repos, previews, scheduled, questions. A logged-in user who knows an
  ID could read/modify another org's data via those routes. The UI only surfaces
  a user's own projects, so it's not exposed in normal use, but the API needs a
  per-route project-access check. HARDEN before real multi-tenant exposure.
- SECURITY: global secrets are still global (not org-scoped) — they'd be visible
  across tenants. Scope secrets to org.
- WS /api/events rejects connections without a token (currently 403 for the UI);
  needs to accept the session cookie. Fix in Phase C.
- Leased agents: only the gating stub exists; real provisioning is a separate session.
