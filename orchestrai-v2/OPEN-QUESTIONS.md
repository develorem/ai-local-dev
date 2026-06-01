# Decisions to make

Fill in the **Answer:** line under each question (free text — a letter, a
sentence, "go with R", whatever). Each question has enough context to decide
without back-and-forth. **R:** = my recommendation. Once you've answered, I'll
fold the answers into the design docs and we scaffold the new repo.

Skip any you're unsure about and write "discuss" — we'll talk those through.

---

## Already decided (context only — no answer needed)
- Greenfield, new repo; v1 kept as reference.
- Node.js + TypeScript front & back; MCP via the official TS SDK.
- Two distributions: **self-hosted** (one-off + support sub, single-tenant) and
  **cloud** (monthly sub, multi-tenant). One codebase, `DEPLOY_MODE` flag.
- Single container, four tiers: **UI / BFF / API / MCP**, over a shared service core.
- Auth: **username/password AND Google**, both modes. Self-hosted pre-seeds `admin`.
- Agents are downloadable units (worker + model runtime) that connect back securely.
- Cloud plans: **Free / Pro $4.95 / Team $19.90**.

---

## B. Tech stack (these define the repo skeleton — highest priority)

### B2 — Frontend framework
**Matters:** sets the whole UI codebase and how the BFF is hosted.
**Options:**
- **Next.js (React):** biggest ecosystem; can host the **UI and BFF in one process**
  (API routes / server actions), which fits the single-container model; lots of
  hiring/AI-tooling familiarity.
- **SvelteKit:** leaner, less boilerplate, also does UI+BFF in one; smaller ecosystem.
**R:** Next.js — ecosystem + UI+BFF-in-one + it's the most "standard" for this.
**Answer:** React front end, don't care for BFF. Probably will consider react native for mobile apps in future.

### B3 — API framework (the public/agent surface)
**Matters:** structure of the programmatic API + service core.
**Options:**
- **NestJS:** opinionated, DI, modules, decorators; keeps a clean service-core
  boundary; more ceremony.
- **Fastify / Hono:** lean, fast, minimal; you impose your own structure.
**R:** NestJS (the structure pays off as the domain grows and keeps transports
thin). Pick Fastify/Hono if you prefer minimalism.
**Answer:** Go with your recommendation.

### B4 — Database engine + ORM
**Matters:** one of the few hard-to-reverse choices.
**Options (engine):**
- **Postgres everywhere** (bundled into the self-hosted image): one dialect, no
  drift between cloud & self-hosted; heavier self-host image (~150MB + a process).
- **Postgres (cloud) + SQLite (self-hosted):** lighter self-host footprint, but two
  dialects to test and subtle behaviour differences.
**Options (ORM):** **Drizzle** (SQL-first, excellent TS types, light) vs **Prisma**
(more magic, great DX, heavier, historically weaker on raw SQL/perf).
**R:** **Postgres everywhere + Drizzle.**
**Answer:** SqlLite for self hosted. For cloud, we may also look at Postgres but might launch with sqllite on Cloudflare.

### B5 — Process model inside the container
**Matters:** how UI/BFF/API/MCP run together.
**Options:**
- **One Node process** serving all four (MCP & API mounted as sub-apps). Simplest;
  shared memory; in-process core calls.
- **Supervised processes** (s6/pm2): stronger isolation, independent restarts; more moving parts.
**R:** One Node process for v1 of v2 (simplest, and the core is a shared library
anyway); split later only if a tier needs independent scaling.
**Answer:** No each should be independently deployable of each other. A bug in API? Only API gets updated.

### B6 — Auth implementation
**Options:** a library (**Lucia**, **Auth.js/NextAuth**) vs hand-rolled sessions.
**R:** Auth.js if we go Next.js (covers Google + credentials + sessions cleanly),
else Lucia. Avoid hand-rolling.
**Answer:** Go with your recommendation.

### B7 — Monorepo tooling
**R:** pnpm workspaces + Turborepo; packages: `core`, `shared` (zod types), `web`,
`api`, `mcp`, `agent`. Any objection?
**Answer:** Go with your recommendation.

---

## C. Architecture & deploy modes

### C3 — What differs between cloud and self-hosted, beyond the obvious?
Obvious differences: tenancy enforcement, billing, admin-seed, sign-up flow.
**Question:** anything else you want mode-gated? e.g. model catalog source
(cloud-served vs in-image), telemetry/analytics, rate limits, feature flags?
**R:** keep the gated surface tiny — tenancy + billing + admin-seed + catalog
source. Everything else identical.
**Answer:** Fine for now, but lets add an action item to deal with observability later on.

### C4 — BFF→core and API→core: in-process or HTTP?
**Matters:** coupling & performance. In-process = call the shared `core` library
directly (fast, simple, but all tiers share a deploy). HTTP = true network
boundary (independently deployable, more overhead/complexity).
**R:** in-process calls to the shared core (it's one container anyway); revisit if
we ever split tiers across machines.
**Answer:** Proper tiers, no shared processes. Isn't this the same question as earlier?

### C5 — Public API versioning
**Matters:** the API is an external contract (agents + third-party tools).
**Question:** version from day one (`/v1/...`) — yes/no? Deprecation policy now or later?
**R:** ship `/v1` from the start; formal deprecation policy later.
**Answer:** Yes /v1

---

## D. Data & schema

### D2 — Carry the v1 entity model?
v1 entities: projects, outcomes, tasks, plans, questions, agents, secrets/vault,
documents (+ index), scheduled tasks, events, discussions/proposed-actions.
**Question:** keep all of these? Any to drop, rename, or rethink before we model them?
(Notably: "outcomes" was a rename of "goals"; "discussions/proposed-actions" was
lightly used.)
**R:** keep the core set; drop/defer discussions+proposed-actions unless you want
the chat-to-mutate-tasks feature early.
**Answer:** Yes but we also need project level joins to secrets and agents. We should have this already though?

### D3 — One schema, single implicit org in self-hosted — confirm?
Keeps one data model; self-hosted always resolves to one org and skips
invitations/limits. Alternative: a separate, simpler self-hosted schema (more code).
**R:** one schema, implicit org.
**Answer:** Yes 1 schema. Schema should be updateable also, some kind of schema/data deploy tool?

### D4 — Carry the v1 document-index design?
(Mechanical headings + one-line LLM "purpose"; fetch full doc on demand; reindex
on change; also indexes repo docs.) It worked well in v1.
**R:** carry it as-is.
**Answer:** Go with your recommendation.

---

## E. Auth & tenancy

### E2 — Session mechanism
**Options:** **cookie sessions** (server-side, easy revoke, simple) vs **JWT +
refresh** (stateless, scales horizontally, revocation is harder).
**R:** cookie sessions (server-side) — simpler, revocable; we're not at the scale
where stateless JWT matters.
**Answer:** Do you mean for UI or for the agents? Your question wasn't clear. For UI, consider that in future auth will also come from other sources like a mobile app. This means JWT would be better right?

### E3 — Self-hosted `admin` seeding
**Options:** (a) credentials **baked into the image** (must force-change on first
login); (b) **seeded from env** on first boot (`ADMIN_EMAIL`/`ADMIN_PASSWORD`),
forced change. (a) is zero-config but a known default until changed; (b) is safer.
**R:** (b) env-seeded + forced change on first login; fall back to a generated
password printed to container logs if env not set.
**Answer:** Go with your recommendation.

### E4 — Cloud org model
Carry v1: a user can belong to **multiple orgs**; roles **owner / admin / member**;
invitations by email.
**Question:** confirm, or change the roles/membership model?
**R:** carry as-is.
**Answer:** Go with your recommendation.

### E5 — Extra providers / email
**Question:** need GitHub or SSO/SAML at launch, or later? Do we need transactional
email at launch (verify address, password reset, invitations) — and via which
provider (Resend/Postmark/SES)?
**R:** Google + user/pass at launch; GitHub/SSO later; wire one email provider
(Resend) for cloud invites + password reset.
**Answer:** Go with your recommendation.

---

## F. Agents & models (the novel area — most important to get right)

### F1 — Executor contract + transport
One protocol for all executors: `register → claim → task-envelope → submit-result
→ events/heartbeat → fetch-secret → clone-info`.
**Question (transport):** **HTTPS REST + polling** (simple, firewall-friendly,
stateless) vs a **persistent WebSocket** for the claim/heartbeat loop (lower
latency, push, but more connection management). Agents may sit behind home NATs.
**R:** HTTPS REST with the agent polling/long-polling for work + heartbeat — works
through NAT with no inbound ports, simplest to secure. Add WebSocket later if latency matters.
**Answer:** Go with your recommendation.

### F2 — What does "download an agent" actually produce?
**Options:**
- (a) **Generic prebuilt image + injected config:** the UI gives you a `docker run`
  / `docker-compose.yml` + `.env` carrying server URL, agent token, chosen model.
  No per-user image build. Cheapest, instant; user runs a compose file.
- (b) **Tailored per-user image** built server-side + pushed to a registry; user
  just pulls. Cleanest UX but needs a build pipeline + registry + per-user build cost.
- (c) **Installer/CLI**: `npx orchestrai-agent init <token>` that writes the compose
  and pulls the image.
**R:** (a)/(c) combined — a generic image + a generated compose/.env download, and
optionally a one-line CLI. Zero build infra; one file to run.
**Answer:** Might put this as a decision for further discussion in the new repo. I want it to be super easy for users.

### F3 — "Create agent" wizard inputs
Proposed fields: agent **name**, **GPU/VRAM** (auto-detected by the test container
or entered), **OS**, **model** (recommended or chosen), **org** (cloud), **project
access + role**, `execution` mode (hub-workspace vs own-workspace).
**Question:** anything to add/remove? Should access/role be set here or after it connects?
**R:** the above; set project access here, editable later.
**Answer:** Go with your recommendation.

### F4 — Model catalog
**Question:** which models are on offer, and where does the catalog live —
**served from the cloud** (so it can update without a new release) or **baked into
the app/image**? Self-hosted air-gap implications (ties to A3).
**R:** a cloud-served catalog with a baked-in fallback list; entries carry VRAM
needs + our benchmark scores.
**Answer:** Not sure our benchmark scores are valid for other people with different hardware? Also the catalog would need to verify model availability. Even during our tests, you were suggesting models that were no longer available, so we need a way to keep this list fresh. This could be an out of band process that simply checks availability via ollama cli calls or something else that makes sense.

### F5 — The "test/benchmark" container
Reuse v1's `test-harness/` (it already runs a model×context sweep and scores
throughput + quality). Flow: user runs the test container → it measures their GPU
→ returns a **recommended model** → feeds the create-agent wizard.
**Question:** mandatory step or optional? Should results be uploaded to the
instance (to inform the wizard) or kept local and pasted/imported?
**R:** optional but encouraged; results POST to the instance to pre-fill the wizard;
also viewable locally.
**Answer:** Yes start with that but we need to make it more user friendly and completely automatable. It needs a UI probably too. So user installs the docker container, then hits a known URL for it, where they can start the test and look at results, or see what's in progress, etc. This is probably a whole app just on its own. 

### F6 — Model packaging in the agent image
**Options:** baked-in (instant/offline, ~9GB+ image) vs **pull-on-first-run** into
a volume (small image, flexible, needs network + first-run wait).
**R:** hybrid — image ships the runtime (Ollama) only; pulls the chosen model on
first run into a persistent volume.
**Answer:** No not baked in. I think that the image needs to self install the model. I don't want to hold the models anywhere, too expensive (size). Perhaps the agent needs its own ui as well, something that says what model is running, system specs, etc. Like a simple dashboard page. And that UI would also let you choose other models to download and try, etc. 

### F7 — Execution model
`execution = hub_workspace` (server owns the workspace, agent returns a diff) vs
`own_workspace` (agent works in its own checkout and reports). Support **both**?
**R:** support both; the self-hosted/downloaded worker uses `hub_workspace`;
frontier agents (Claude) use `own_workspace`.
**Answer:** You mean the git workspace? Always has to be on the agent. Agents need to be completely stand alone and run locally, and just update via api or mcp server their status and grab new tasks.

### F8 — Multiple agents per instance
Can one instance have several agents (different models/roles) connected at once?
**R:** yes — it's core to the "fleet" idea and to per-project role coverage.
**Answer:** What do you mean per instance? For my tenant, I can definitely connect more than one agent. I might even build an on premise GPU capability for running multiple agents, and developers lend their claude code instances after hours. IN fact, we might even consider a plugin for that, where a developer can pledge their claude code instance to other uses (Ie orchestrAi) outside of work hours (would be a schedule they setup).

---

## G. Security

### G1 — Agent credential
**Options:** **bearer token** (per-agent, tenant-bound, revocable/rotatable in UI)
vs **mutual TLS** (client certs — stronger, more setup). Token format: opaque
random (look up in DB) vs signed (JWT carrying org/agent).
**R:** opaque per-agent bearer token over TLS, stored hashed, revocable + rotatable
from the UI. mTLS is overkill for now.
**Answer:** Whichever will work easiest for the majority of ai agent types out on the market today (claude code, openclaw, github cli, codex, etc).

### G2 — Transport security per mode
**R:** TLS **required** for cloud agent connections; self-hosted on a LAN may use
plain HTTP (user's call, with a warning) — matches v1 stance.
**Question:** OK, or require TLS everywhere?
**Answer:** Why make it different? It adds complexity. TLS everywhere.

### G3 — Tenant binding + server discovery
How the downloaded agent knows **which** server + tenant: the generated config
embeds the server URL + the tenant-bound token. Self-hosted LAN: user pastes the
server's LAN URL (or mDNS discovery later).
**Question:** confirm config-embed approach; want LAN auto-discovery now or later?
**R:** config-embed now; LAN auto-discovery later.
**Answer:** Yes that could work, but also the UI I mentioned above might also be a good approach? On the cloud or locally hosted instances of the server, they can get a unique url for connecting their clients when they create the agent. In fact the downloaded file mcp.json could include this information too.

### G4 — Secrets vault
Carry v1: encrypted-at-rest, **write-only** values (never shown after creation),
fetched per-task by the agent via an audited endpoint, org-scoped.
**R:** carry as-is.
**Answer:** Yes.

---

## A. Distribution & licensing (self-hosted)

### A2 — Pricing
**Question:** self-hosted **one-off price**? **Support subscription** price + what
it includes (updates? SLA? priority support?)? Cloud plan prices are set
(Free/$4.95/$19.90) — change anything there?
**Answer:** Nah its fine to get started with.

### A3 — Self-hosted licensing / activation + air-gap
**Options:** (a) **honor system** (no enforcement); (b) **license key** validated
locally (offline-friendly); (c) **phone-home activation** (online check, can gate
updates/catalog). Big question: must self-hosted run **fully air-gapped**, or may
it contact us for licensing / model catalog / updates?
**R:** license key validated locally + an *optional* online check for catalog/
updates; fully functional air-gapped (degraded catalog). Keeps it honest without
forcing connectivity.
**Answer:** Yes license key makes sense. I'm not sure what kind of smarts a license key can have these days, but a mechanism to have it expire would be nice. We do need to support totally closed off locally hosted servers. Think ultra secure environments, where the server would not have an outbound internet connection.

### A4 — Update / upgrade policy
**Question:** free updates within a major version, paid major upgrades? Or updates
gated by an active support sub?
**Answer:** Don't need to answer this now.

### A5 — Download channel
**Question:** does the cloud site host the self-host download + license issuance,
or a separate distribution channel?
**R:** cloud site issues licenses + links the image; image pulled from a registry.
**Answer:** I think cloud sign on is needed for purchasing. Then they pay, get a license and a download link. Cannot download before purchase.

---

## H. Billing

### H2 — Billing provider & options
**R:** Stripe (carry v1's checkout + customer-portal + webhook shape).
**Question:** trials (e.g. 14-day Pro)? Annual billing option? Confirm Stripe.
**Answer:** Trial is already defined I believe, I defined it for you last night. Everything else as per my description. Other billing options can be added on later.

### H3 — Leased cloud agents
`DEFERRED` — designed-for in the executor contract, built in a later phase. No
answer needed now; flag if you want it sooner.
**Answer:** Don't need it now, but the system needs to prepare for it. An example - cloudflare actually supports openclaw deployments. The idea would be that they would use our portal to add 3 cloud hosted agents, they setup the payment, and we then deploy 3 cloudflare openclaw instances with a local llm in the container, and connect them to the customer's cloud tenant. Hosted agents I think will only work for the cloud scenario, not local.

---

## Anything I missed?
Add questions/areas here and I'll work them in:
**Notes:**
