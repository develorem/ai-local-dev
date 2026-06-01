# 04 — Auth & tenancy (decided)

## User auth
- **Username / password** (argon2id) **and Google OAuth**, in both deploy modes.
- **JWT** (access + refresh), issued by the **API** — chosen over cookie sessions
  because **mobile (React Native) is coming**, and tokens work cleanly for native
  apps and the web SPA alike. The BFF manages the web SPA's token ergonomics; the
  API is the auth authority. (Refresh-token rotation + revocation list.)
- **GitHub / SSO** later. **Email** via **Resend** (cloud: invites, password reset,
  verification).
- Self-hosted: pre-seeded **`admin`** — **env-seeded** on first boot
  (`ADMIN_EMAIL`/`ADMIN_PASSWORD`), **forced change on first login**; if env unset,
  generate a password and print it to the container logs.

## Agent auth (separate from user auth)
- Agents do **not** use user JWTs. Each agent gets a **per-agent bearer token**,
  tenant-bound, hashed at rest, revocable/rotatable from the UI (see 03).

## Tenancy
- **Cloud = multi-tenant.** Users, **organizations**, members with roles
  **owner / admin / member**, invitations by email. A user may belong to **many
  orgs**. (Carried from v1.)
- **Self-hosted = single-tenant.** Same schema, but always resolves to **one
  implicit org**; invitations/plan-limits are skipped.
- **Enforcement lives in `core`** (every service call takes an explicit principal;
  resources resolve to an org; access is checked there) — never bolted onto a
  transport handler (the v1 mistake). Self-hosted runs the same checks, trivially
  satisfied by the single org.
- The schema includes the **project↔secrets** and **project↔agents** joins so
  access is scoped per project, not just per org (carried from v1).

## Transport security
- **TLS everywhere** — cloud (Cloudflare) and self-hosted (bundled Caddy /
  self-signed for pure-LAN). No HTTP exception, to avoid a divergent code path.

## Carried-forward
- Exact JWT lifetimes + refresh/rotation/revocation mechanics.
- Whether a separate mobile BFF is added when React Native lands.
