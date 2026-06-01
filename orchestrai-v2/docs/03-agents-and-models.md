# 03 — Agents & models (decided)

## One executor contract, standalone executors
Every executor — a downloaded local agent, a frontier agent (Claude Code,
Codex, etc.), and future leased cloud agents — speaks **one protocol**:
`register → claim → task-envelope → submit-result → events/heartbeat →
fetch-secret`.

Agents are **fully standalone**: each runs on its own machine, **owns its own git
workspace**, does the work locally, and only **pulls tasks and reports status/
results** back via the API (or MCP). There is no server-side workspace. (So the
v1 "hub-owned workspace" idea is dropped — the workspace is always on the agent.)

**Transport:** HTTPS REST with the agent polling/long-polling for work +
heartbeat. Works through home NAT with no inbound ports; simplest to secure.
TLS required (everywhere). WebSocket can come later if latency demands it.

## Credential & secure tenant binding
- **Per-agent bearer token**, tenant-bound, presented on every call (the standard
  most AI agents already support: an `Authorization: Bearer` header in their MCP
  config). Stored hashed; revocable + rotatable from the UI.
- At agent-create time the instance generates a **unique connection URL + an
  `mcp.json`** carrying the server URL + token — that file *is* the config the
  user drops into their tool. (Works for cloud and self-hosted.)
- TLS everywhere; cloud authorizes every agent action against the token's org +
  project grants.

## The agent is itself a small app (with its own UI)
The agent image ships the **runtime only (Ollama)** — **no models baked in**
(too large/expensive to host). It **self-installs the chosen model on first run**
into a volume. It serves a **local dashboard UI**: what model is running, system
specs, and the ability to pull / try / switch models. So an agent is a
self-contained appliance the user runs and manages locally.

## "Create a local agent" flow
From the UI (cloud or self-hosted): a wizard collects **name, GPU/VRAM (auto-
detected or entered), OS, model (recommended or chosen), org (cloud), project
access + role**. It returns a runnable artifact + the `mcp.json`/connection URL.
- **Open (deferred to the new repo):** exactly what the "download" is — generic
  prebuilt image + generated config, an installer/CLI, etc. **Design goal: make
  it dead simple for the user.** (F2.)

## Model catalog (must stay fresh)
- A maintained catalog of installable models with VRAM needs and *indicative*
  scores. **Scores are hardware-relative** — they guide, they don't promise.
- **Availability must be verified** (models get pulled/renamed upstream). An
  **out-of-band freshness job** checks availability (e.g. via Ollama) and prunes
  dead entries. Catalog is cloud-served with a baked-in fallback (ties to the
  air-gap requirement).

## The benchmark / model-advisor app (its own app)
Productize v1's `test-harness` into a standalone app with its own web UI:
- User installs the benchmark container, opens its known local URL, **starts a
  run, watches progress, views results** — fully automatable.
- It measures the user's actual GPU and **recommends a model**; results can POST
  back to the instance to **pre-fill the create-agent wizard**, or stay local.

## Fleet: many agents, and lending capacity
- A tenant can connect **many agents** (different models/roles) at once — core to
  the "fleet" idea and per-project role coverage.
- Future concepts (designed-for, not built): an **on-prem multi-GPU** host running
  several agents; and a **"pledge" plugin** letting a developer lend their Claude
  Code instance to OrchestrAi on a **schedule** (e.g. after work hours).

## Leased (cloud-hosted) agents — designed-for, built later
Cloud-only. The portal lets a customer add N hosted agents + set up payment; we
**provision them** (e.g. Cloudflare Containers running a local-LLM image such as
openclaw) and connect them to the customer's tenant — they speak the same
executor contract as any other agent. Not built now, but the contract + data
model must accommodate it. (Not available for self-hosted.)
