# OrchestrAi

A local-first agentic coding platform. You give it large coding goals, it decomposes them, runs the work, asks you questions when blocked, and accepts new goals or discussions at any time — all visible in a live browser dashboard.

> **Status: design phase.** Runtime code not built yet. This folder is the design.

## Shape of the system

```
Browser UI  ◄──REST + WebSocket──►  Hub  ◄──REST──►  Agent  ──subprocess──►  /workspace
                                     │                │
                                     │                ▼
                                     │              Ollama (GPU)
                                     ▼
                                   SQLite
                                   (state, secrets, audit)
```

- **Hub**: long-running service. State, API, UI, secrets, audit log. One per deployment.
- **Agent**: disposable worker. Pulls tasks, runs them, reports results. One or more per deployment. Kill any time; new one picks up where it left off.
- **Ollama**: the local model server. GPU passthrough. Single model, single-threaded inference.

All three ship as containers: `docker compose up`.

## Core ideas

- **Hub-and-spoke, not orchestrator-and-sandbox.** The Hub holds state; Agents do work. Agents are stateless and replaceable.
- **Everything is a task in a queue.** Goals, plans, approvals, discussions, PR reviews, CI-failure responses — same model, different task type.
- **Branch-as-lease.** Workspace contention is solved by git feature branches, not by Hub-side locking. Two agents on different branches → parallel safe. Two agents on the same branch → Hub serializes.
- **Async human-in-the-loop.** Blocked tasks don't stall the agent. It picks the next ready task. You answer in your inbox; the agent revisits when free.
- **Secrets never leave the Hub.** Agents fetch values per-task via an authenticated endpoint, inject them into subprocess envs, shred after use. The LLM prompt only ever sees the *name* `$GITHUB_TOKEN`.
- **Token-efficient context.** Project metadata is structured bullets, not prose. Stays under 2K tokens of overhead so the model has room to think.

## Design docs

Read in this order:

| Doc | What's in it |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The shape — components, data flows, state machines |
| [`docs/SCHEMA.md`](docs/SCHEMA.md) | Every table, every column, the atomic-claim query |
| [`docs/API.md`](docs/API.md) | REST + WebSocket contracts, agent endpoints, event kinds |
| [`docs/PROMPTS.md`](docs/PROMPTS.md) | Per-mode LLM prompts (Planner, Implementer, Reviewer, PR-Reviewer, CI-Fixer, Discusser, Revisor), token-budget rules |
| [`docs/EXECUTION.md`](docs/EXECUTION.md) | What runs where — Agent image, security boundary, subprocess discipline |
| [`docs/SECRETS.md`](docs/SECRETS.md) | The vault, encryption, inject-don't-leak protocol, audit |
| [`docs/UI.md`](docs/UI.md) | Five screens: Agents, Agent detail, Projects, Task detail, Vault |
| [`docs/SETUP.md`](docs/SETUP.md) | Docker, WSL2, GPU passthrough, first run |

## Where this is heading

| Phase | Deliverable |
|---|---|
| **1** (current) | Design docs. Stable enough to implement against. |
| **2** | Hub skeleton: schema + migrations + REST + WebSocket. Hand-fed seed tasks. UI is read-only Agents/Tasks board. |
| **3** | Agent skeleton: register, heartbeat, claim, release. One task type (`implement`) wired end-to-end against Ollama. |
| **4** | Planner + Reviewer modes. Plan approval flow. Project + repo CRUD in UI. |
| **5** | Discussions + proposed-actions. Secrets vault + UI. |
| **6** | PR review + CI-failure tasks. Webhook ingestion. |

No timelines — hobby pace. Each phase produces something usable.

## Relationship to the test-harness

- The `test-harness/` sibling chose the model + Ollama config that makes this work: `qwen2.5-coder:14b @ 16K` with `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0`. That's the *engine*.
- OrchestrAi is the *vehicle* — what turns "build me a feature" into reviewed, working code on a real branch.
- The harness's `ollama_client.py` seeds `hub/server/llm/` (and eventually the Agent's LLM client too).
- The harness's quality problems are unit tests for the model. OrchestrAi will let us measure real multi-task agent behavior end-to-end.

## Non-goals

- Multi-user / team workflows (single user, localhost-only)
- Cloud-hosted variant (everything is local; if you have an Anthropic API key set, it goes into the secrets vault for tasks that need it — but the Hub itself is local)
- Auth (single-user, localhost-bound)
- Replacing your IDE — OrchestrAi is a *peer*: it can build feature X on a branch while you hand-code feature Y in your editor. Merges go through normal review.
- Cross-machine state sync (single Hub per deployment; agents can live on other machines and talk back over the network)

## Open questions parked in the docs

- Should webhook ingestion (GitHub PR opened, CI failed) be in v1 or wait? (`ARCHITECTURE.md` parks it for v2.)
- Should we ever support per-task agent capabilities (e.g. "this task requires GPU-on-host" → routed to specific agent)? Schema already has `agents.capabilities`; UI/routing doesn't use it yet.
- How aggressive should the prompt-token budget be? Current rule is ≤2300 tokens of overhead (see `PROMPTS.md`); could tighten further with smarter context pruning.

## Feedback loop

These docs are the source of truth. **Edit them in place** as the design evolves. Implementation follows the docs, not the other way around. If something here is wrong, fix the doc first.
