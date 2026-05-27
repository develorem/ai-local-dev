# OrchestrAi — Architecture

The high-level design of OrchestrAi: a local-first agentic coding platform built around a long-running **Hub** service and one or more **Agent** workers that pull work and run it inside their own containers.

## Core thesis

A single local GPU is single-threaded for serious model inference. Build concurrency at the *work-graph* layer instead of trying to parallelize inference. Make the agent disposable so the system survives crashes, upgrades, and (eventually) multi-machine scale-out. Source of truth lives in the Hub's database; everything else can be regenerated.

## Two roles, two containers

```
┌────────────────────────────────────────────────────────────┐
│  OrchestrAi Hub                                            │
│  - HTTP API + WebSocket                                    │
│  - SQLite (persistent state)                               │
│  - Browser UI                                              │
│  - Secret vault (encrypted at rest)                        │
│  - Reaper (clean up stale agent leases)                    │
│  - Reads/writes only its own volumes                       │
└──────────────┬─────────────────────────────────────────────┘
               ▲ REST + WebSocket
               │ (agent is the client; pull-based)
               │
┌──────────────┴─────────────────────────────────────────────┐
│  OrchestrAi Agent  (disposable; one or more)               │
│  - On boot: register with Hub, get a lease token           │
│  - Loop: claim next task → run it → report results         │
│  - Holds NO persistent state                               │
│  - Has its own filesystem for git clones, scratch space    │
│  - Talks to Ollama via the Hub's compose network           │
└──────────────┬─────────────────────────────────────────────┘
               ▲
               │
┌──────────────┴─────────────────────────────────────────────┐
│  Ollama                                                    │
│  - GPU passthrough, flash-attn, KV-quant                   │
│  - Serves the chosen model on :11434                       │
└────────────────────────────────────────────────────────────┘
```

Three containers in the default deployment: `hub`, `agent`, `ollama`. The agent can be stopped, restarted, or replaced any time. The Hub goes on serving.

## Unifying abstraction: everything is a task

The same database holds work of every kind. Adding a feature, planning, approving, discussing, implementing, reviewing a PR, responding to a failed CI build — all are typed tasks the same worker loop processes.

| Surface action | Internally |
|---|---|
| "Add a new project" | create `project` row + initial setup tasks |
| "Add a feature to project X" | enqueue a `plan` task scoped to project X |
| "Plan ready for review" | a `question` of kind `plan_approval` |
| "Approve plan" | answer the question → instantiate plan's task outline |
| "Review this PR" | enqueue a `review_pr` task with the PR URL in payload |
| "CI failed on branch X" | enqueue a `respond_to_ci_failure` task with logs in payload |
| "Discuss something with me" | a `discuss` task at `critical` priority |

No separate paths. Everything goes through the same queue.

## Projects own repos

A project (the user's product/system) owns one or more git repos. A microservices product has many: `api-gateway`, `user-service`, `billing-service`, `infra`, `frontend`, etc. The Hub stores:

- `projects` — one row per product, including project-level context (stack, conventions, key facts)
- `project_repos` — many per project, each with a git URL, role tag, brief description

Goals belong to a project. Tasks carry both the project and (if relevant) the specific repo + feature branch they operate on. Branch is the lock — see "Branch-as-lease" below.

## Hub responsibilities

Single Python process (FastAPI) containing:

- **REST API + WebSocket** — UI and Agents consume both. Push-only WebSocket; agents always pull via REST.
- **Worker support services** — task pickup arbiter (atomic claim), reaper (lease expiry), question routing, plan instantiation.
- **Domain layer** — goal / task / question / discussion state machines. Pure Python.
- **DB layer** — SQLite + migrations.
- **Secret vault** — AES-256-GCM encryption, master key from a path outside the DB volume.
- **Event emission** — every state change writes an event row + broadcasts on WebSocket.

The Hub does **not** run LLM calls or shell commands itself. It coordinates; agents execute.

## Agent responsibilities

The Agent is a small loop:

```
1. On boot: POST /api/agents/register → {agent_id, lease_token}
2. Loop:
     a. POST /api/agents/{id}/claim
        ← either a task (with project context + repo + branch in payload), or 204 no-content
     b. If 204: sleep, heartbeat, retry
     c. If task:
        - Fetch project context, repo info, secrets (if needed) — all from Hub
        - Clone the repo if not already on disk; checkout the branch
        - Run the LLM call(s) for this task type (see PROMPTS.md)
        - Execute resulting commands locally (subprocess in this same container)
        - Commit + push results to origin (for code-producing tasks)
        - Report progress events + final result to Hub
3. Periodically: POST /api/agents/{id}/heartbeat (extends task lease)
4. On clean shutdown: POST /api/agents/{id}/release
```

Agent state lives entirely in the Hub. The Agent's container has only:
- Git clones (will be re-cloned if the container is replaced)
- An installed toolchain (Python, Node, git, common build tools — baked into the image)
- A small helper CLI: `orchestrai-secrets`, `orchestrai-report`, `orchestrai-fetch-context`

No DB, no logs that matter, no caches that can't be rebuilt.

## Branch-as-lease

Workspace contention is solved by git, not by Hub-level locks:

- Each task that touches a repo declares `(repo_id, branch_name)` in its payload
- The Hub's atomic claim refuses to assign a task to agent B if a different agent A already holds a task with the same `(repo_id, branch_name)` in-flight
- Two tasks on the *same* repo but *different* branches → can run on different agents in parallel
- Two tasks on the *same* branch → serialized, one agent at a time

This works because agents push their changes to a shared origin. The next agent starts by cloning fresh from origin (or pulling) — it never inherits stale local state.

## Task lifecycle (with leases)

```
                              ┌───────────┐
                              │  created  │
                              └─────┬─────┘
                                    │
                                    ▼
                              ┌───────────┐
              ┌───────────────┤   ready   │◄────────────┐
              │  agent claims └─────┬─────┘             │
              ▼                     │                   │
       ┌─────────────┐              │                   │
       │ in_progress │              │ lease expires     │
       │  (leased)   │──────────────┘                   │
       └─────┬───────┘                                  │
             │                                          │
   ┌─────────┴─────────┐                                │
   ▼                   ▼                                │
blocked_on_human  blocked_on_dep                        │
   │                   │                                │
   │ answered          │ deps met                       │
   └──────────┬────────┘                                │
              │                                         │
              ▼                                         │
        ┌──────────┐  ok    ┌──────────┐                │
        │  review  │───────►│   done   │                │
        └────┬─────┘        └──────────┘                │
             │  fail                                    │
             ├─► attempt < max → ready ─────────────────┘
             └─► attempt = max → failed
```

A task in `in_progress` is always held by an agent under a time-bounded lease (default 30s). Heartbeats extend the lease. Missed heartbeats → Hub reclaims → task back to `ready` → next agent picks it up.

## Goal lifecycle

```
submitted ─► planning ─► active ─► done
                │           │
                ▼           ▼
            rejected    abandoned
```

`planning` means the `plan` task is running or its approval question is open. Approval flips to `active`, which makes the implementation tasks eligible for claim.

## Worker pickup (the atomic primitive)

The Hub never decides "agent X should run task Y." It exposes a `claim` endpoint that returns the next ready task using a single atomic SQL statement (see `SCHEMA.md`). Agents poll. The DB does the arbitration. This is the same pattern Sidekiq / Celery / etc. use, scaled down to a single SQLite.

## Data flow: user submits a goal

```
1. UI: POST /api/goals { project_id, title, description }
2. Hub: INSERT goal (status=planning) + INSERT plan task (status=ready)
3. Event: goal.created, task.created → broadcast on WebSocket
4. UI re-renders board

LATER, an Agent claims the plan task:
5. Agent: POST /api/agents/{id}/claim → receives the plan task
6. Agent: fetches project context (one or two short markdown blocks)
7. Agent: makes Planner LLM call (Ollama)
8. Agent: POSTs intermediate events + the final plan markdown + task outline
9. Hub: INSERTs plan row, INSERTs approval Question, transitions task to blocked_on_human
10. Agent: releases the task (its work is done; waiting on human is not the agent's problem)
11. UI inbox shows the new approval question
```

## Data flow: human answers a question

```
1. UI: POST /api/questions/{id}/answer
2. Hub: INSERT answer, transition task back to ready (if all qs answered)
3. Event: question.answered + task.status_changed
4. Worker pickup query becomes satisfiable
5. Next agent that polls claims the task and continues
```

## Data flow: PR review

```
1. User clicks "Review PR" on a task or in the project view, pastes PR URL
   (or, in a more integrated v2, a webhook from GitHub creates this automatically)
2. Hub: INSERT review_pr task, status=ready, payload={pr_url, ...}
3. Agent claims it
4. Agent: fetches PR diff (via gh CLI using injected GITHUB_TOKEN)
5. Agent: LLM call in Reviewer mode against the diff
6. Agent: posts comments / approval / request-changes via gh CLI
7. Agent: reports task done with summary
```

The agent never holds the GitHub token in its LLM prompts — only in its subprocess env. See `SECRETS.md`.

## Concurrency & preemption

- **At the Hub:** SQLite serializes writes; reads scale with WAL. One worker thread is enough for v1.
- **At each Agent:** one task at a time per agent (single LLM stream). Multiple agents = parallel work across different branches/projects.
- **Preemption:** `critical` tasks (e.g. a new discussion message) become claimable while a `normal` task is in-progress. The Agent currently working on the normal task finishes its current LLM call (typically ≤30s), heartbeats stop, the lease expires, the task goes back to `ready` (gracefully), and the agent's next claim grabs the critical one. We do not implement mid-LLM-call abort in v1.

## Failure modes

| Failure | What happens |
|---|---|
| Agent crash mid-task | Lease expires → Hub reclaims → another agent picks up |
| Hub crash | On restart, the reaper sweep cleans expired leases. Agents reconnect on next heartbeat / re-register if their lease_token is rejected. |
| Network blip between Hub and Agent | Agent retries with backoff; lease expires if blip is long; agent re-registers and resumes |
| Ollama down | Tasks fail with retry; after max_attempts → `needs_human` |
| Schema migration | Hub refuses to serve until migrations applied; Agents see 503 and back off |
| Bad LLM output | Per-mode validators reject; retry with feedback; failed twice → `needs_human` |

## Out of scope for v1

- Multi-user / auth (single-user, localhost-bound)
- Cloud-hosted LLM fallback (everything is local Ollama)
- Multi-machine agent fleet (designed for; not deployed)
- Mid-LLM-call abort
- Cross-branch coordination (e.g. a task that synthesizes work from N branches)
- Auto-cloning the user's local repo (must be on a git remote first)
- Webhooks (GitHub PR opened, CI failed) — auto-converted to tasks in v2

## Cross-references

- Data model: [`SCHEMA.md`](SCHEMA.md)
- API contract: [`API.md`](API.md)
- LLM prompts per task type: [`PROMPTS.md`](PROMPTS.md)
- Agent execution environment: [`EXECUTION.md`](EXECUTION.md)
- Secret vault: [`SECRETS.md`](SECRETS.md)
- UI layout: [`UI.md`](UI.md)
- Getting it running: [`SETUP.md`](SETUP.md)
