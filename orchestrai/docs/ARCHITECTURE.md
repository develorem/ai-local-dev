# OrchestrAi — Architecture

The high-level design of OrchestrAi, a local-first agentic coding platform. This document defines the conceptual model, components, and data flows that every other doc in this folder elaborates.

## What OrchestrAi is

A long-running service that accepts large coding goals from a human, decomposes them into ordered tasks, executes them sequentially against a local LLM, asks the human questions asynchronously when blocked, accepts ongoing input (new goals, discussions, edits) while it works, and runs every piece of agent-produced code inside a disposable container so the host machine stays safe.

Single-user, local, with a browser UI talking to a local API. Everything ships as `docker compose up`.

## Core thesis

Local GPUs (specifically a single 16 GB RTX 5080) are single-threaded for serious model inference — one model, one request at a time. **OrchestrAi accepts that constraint and builds concurrency at the *work-graph* layer instead.** Tasks are the unit of concurrency. The LLM runs sequentially through the queue. When a task blocks on a human answer, the orchestrator switches to the next ready task. The human catches up to their inbox on their own time. The GPU is always doing useful work; the human is never the bottleneck.

## Unifying abstraction: everything is a task

| Surface action | Internally |
|---|---|
| "Add a new feature" | enqueue a `plan` task |
| "Plan ready for review" | open a `question` on that plan |
| "Approved" (or "approve with edits") | answer the question → enqueue `implement` / `revise` tasks |
| "Discuss this task with me" | enqueue a `discuss` task at `critical` priority |
| Discovery during implementation | spawn child tasks; existing task → `blocked_on_dep` or continues |

There is no special path outside the queue. The same worker loop, the same database, the same UI handles all of them.

## Component diagram

```
┌─────────────────────────────────────────────────────────────────┐
│  Browser UI  (localhost:8080)                                   │
│  - live task board (WebSocket-driven)                           │
│  - inbox (open questions, proposed actions)                     │
│  - goal/discussion compose                                      │
│  - per-task detail: history, diffs, logs                        │
└────────┬────────────────────────────────────────▲───────────────┘
         │ REST (/api/*)                          │ WS (/api/events)
         ▼                                        │
┌─────────────────────────────────────────────────────────────────┐
│  Orchestrator service  (FastAPI, one Python process)            │
│  ┌────────────┐  ┌────────────┐  ┌──────────────────────────┐   │
│  │ HTTP API   │  │ WebSocket  │  │ Worker loop (background) │   │
│  │ routes/    │  │ broadcaster│  │ - pick next ready task   │   │
│  └─────┬──────┘  └─────▲──────┘  │ - call LLM per task type │   │
│        │               │         │ - apply state changes    │   │
│        ▼               │         │ - emit events            │   │
│  ┌───────────────────────────────┴────────────┐               │   │
│  │ Domain layer (services, state machines)    │               │   │
│  └───────────────────────────────────────────▲┘               │   │
└───────────────────────────────────────────────┼────────────────┘
                                                │
                ┌────────────────────────┐      │
                │ SQLite  (mounted vol)  │◄─────┘
                │ tasks/goals/qs/events  │
                └────────────────────────┘

         spawned per task / per goal
         ┌──────────────────────────────────────┐
         │  Sandbox containers  (Linux)         │
         │  - /workspace mounted from host repo │
         │  - run: pytest, npm, terraform, etc. │
         └──────────────────────────────────────┘
                            ▲
                            │ docker exec (over host socket)
                            │
┌─────────────────────────────────────────────────────────────────┐
│  Ollama container  (image: ollama/ollama)                       │
│  - GPU passthrough (--gpus=all)                                 │
│  - OLLAMA_FLASH_ATTENTION=1, OLLAMA_KV_CACHE_TYPE=q8_0          │
│  - host:11434 inside the compose network                        │
└─────────────────────────────────────────────────────────────────┘
```

Three top-level processes, all in Docker, all on the same compose network:
1. **`ollama`** — model server (existing official image, GPU enabled)
2. **`orchestrator`** — FastAPI + worker + UI + DB, the brain
3. **`sandbox`** (template image) — spawned by the orchestrator as throwaway children for code execution

## Components in detail

### Orchestrator service

Single Python process containing:

- **HTTP API** (FastAPI). REST surface. See [`API.md`](API.md).
- **WebSocket broadcaster**. Pushes events to connected UIs as state changes. Strict push-only — no client-to-server commands over WS.
- **Worker loop**. Background async task. Picks next ready task, runs the appropriate state transition, calls the LLM (via Ollama), executes side effects (file I/O via the sandbox, DB writes), emits events.
- **Domain services**. The state-machine logic for goals, tasks, questions, discussions. Pure Python, testable in isolation.
- **DB layer**. SQLite via SQLAlchemy or `sqlite3` + thin DAOs.
- **LLM client**. Thin wrapper over Ollama's `/api/generate` (sync) and `/api/chat` (when tool-calling). Forks from the `ollama_client.py` we built for the test harness.
- **Sandbox driver**. Spawns/execs/destroys sandbox containers via the mounted Docker socket.

### Database (SQLite)

Single file at `/data/orchestrai.db` (mounted volume). Why SQLite: zero-config, durable, fast enough for a single-user workload, ACID, easy to inspect with `sqlite3` CLI. Migrations are versioned SQL files; on startup the service applies any pending migrations. Schema details: [`SCHEMA.md`](SCHEMA.md).

### Sandbox containers

Spawned per goal (v1) or per task (v2). The host project repo is mounted at `/workspace`. The agent runs all shell commands inside this container — `pytest`, `npm install`, `terraform plan`, etc. — without ever executing on the host. Lifecycle, mount conventions, and security details: [`SANDBOX.md`](SANDBOX.md).

### Ollama

The official `ollama/ollama` image with GPU passthrough. Env vars set in `docker-compose.yml`:
- `OLLAMA_FLASH_ATTENTION=1`
- `OLLAMA_KV_CACHE_TYPE=q8_0`

These are the proven VRAM-unlock config from the test harness work. Models live in a named volume so they persist across container restarts.

### UI

Static HTML + JS served by the orchestrator at `/`. No build step (or a tiny one — Vite or esbuild if we use Svelte). Talks to `/api/*` over fetch + WebSocket. Designed to feel desktop-like in a browser tab. Real desktop packaging (Tauri) is post-v1.

## Key abstractions

| Entity | Purpose |
|---|---|
| **Goal** | A high-level user-supplied objective. Has a status (planning/active/done/abandoned). Holds many tasks. |
| **Task** | A unit of agent work. Typed (`plan`/`implement`/`review`/`discuss`/`revise`). Has a status, priority, dependencies, attempt count, acceptance criteria. |
| **Plan** | Markdown document produced by a planner task. Approved or rejected via a Question. |
| **Question** | An open ask from the agent to the human. Has prompt, optional structured options, status (pending/answered). Created and resolved by tasks. |
| **Discussion** | A multi-turn chat thread between human and agent, optionally linked to a task or goal. May produce ProposedActions. |
| **Message** | A single turn in a discussion. Role = user/agent. |
| **ProposedAction** | A change to the task graph (create/modify/cancel task) suggested by a discussion, applied only when human clicks Apply. |
| **Event** | Append-only audit log of every state transition. Drives the WebSocket stream. |

Schema for each: [`SCHEMA.md`](SCHEMA.md).

## Task lifecycle

```
                               ┌─────────────┐
            ┌──────────────────┤   created   │
            │                  └──────┬──────┘
            │                         │ deps satisfied
            │                         ▼
            │                  ┌─────────────┐
   ┌────────┴────────┐         │    ready    │◄──────────┐
   │ blocked_on_dep  │◄────────┴──────┬──────┘           │
   └─────────────────┘                │ worker picks up  │
                                      ▼                  │
                               ┌─────────────┐           │
                ┌──────────────┤ in_progress │           │
                │              └──────┬──────┘           │
                │                     │                  │
                ▼                     ▼                  │
       ┌────────────────┐      ┌────────────┐            │
       │blocked_on_human│      │   review   │            │
       └───────┬────────┘      └─────┬──────┘            │
               │ answered            │ ok       fail     │
               └──────────┐          │       ┌───────────┘
                          │          │       │
                          ▼          ▼       ▼
                                ┌────────┐ ┌─────────┐
                                │  done  │ │ failed  │
                                └────────┘ └─────────┘
```

Allowed transitions are enforced in the domain layer. Out-of-order transitions raise.

## Goal lifecycle

```
   submitted ──► planning ──► active ──► done
                    │            │
                    ▼            ▼
                rejected     abandoned
```

A goal stays in `planning` while its `plan` task is running and its approval Question is unanswered. Approval flips it to `active`, which makes its implementation tasks `ready`. The user can `abandon` an active goal at any time, which cancels all its open tasks.

## Question lifecycle

```
   pending ──► answered
       │
       └──► dismissed (by orchestrator if task is cancelled)
```

A task in `blocked_on_human` has one or more `pending` questions. Answering all of them flips the task back to `ready`. The orchestrator's pickup logic will see it next loop iteration.

## Worker loop (pseudocode)

```python
while not shutdown:
    task = db.pick_next_task()    # highest priority, ready, deps satisfied
    if task is None:
        await wait_for_signal(timeout=5s)   # any event that could unblock
        continue

    db.transition(task, "in_progress")
    emit_event("task.started", task)

    try:
        handler = handlers[task.type]      # planner / implementer / etc.
        result = await handler.run(task)
    except CancellationError:
        db.transition(task, "ready")       # preempted by a higher-priority task
        continue

    apply_result(task, result)             # may create questions, child tasks,
                                           # mark done, mark needs human, etc.
```

Pick order: `priority DESC, created_at ASC` among rows with `status='ready'` AND `all deps met`. Critical-priority tasks (discussions, cancellations) jump the queue — but only at task boundaries, never mid-LLM-call.

## Data flow: user submits a goal

```
1. User types goal in UI, clicks Submit
2. UI: POST /api/goals { title, description }
3. Server: INSERT goal (status=planning) + INSERT plan task (status=ready)
   → event: goal.created, task.created
4. WebSocket broadcasts both events
5. UI renders the new goal with a "Planning queued" badge

LATER, when worker loop picks up the plan task:
6. Worker: handler.plan.run(task)
7. LLM call: Planner prompt + goal info → structured plan
8. Server: INSERT plan row, INSERT question (kind=plan_approval)
   → task → blocked_on_human, event: task.blocked, question.opened
9. UI inbox shows the new question
```

## Data flow: user answers a question

```
1. UI: POST /api/questions/{id}/answer { answer_text, choice? }
2. Server validates, INSERT answer onto question row
   → event: question.answered
3. If this was the last pending question on a task, transition task → ready
   → event: task.ready
4. If this was a plan_approval question:
   - approved → goal → active, INSERT child tasks from plan
   - rejected → goal → rejected, no children
5. Worker loop wakes up via signal and picks up newly-ready tasks
```

## Data flow: discussion with mutation

```
1. User opens chat on task-014, types "What if we use Redis?"
2. UI: POST /api/discussions { task_id: 14 }  → discussion id
3. UI: POST /api/discussions/{id}/messages { content }
4. Server: INSERT message (role=user)
   + INSERT discuss task (priority=critical, linked to discussion)
5. Worker finishes current task, picks up discuss task
6. LLM: Discusser prompt + discussion history + linked task context
7. Output includes message + optional proposed_actions[]
8. Server: INSERT message (role=agent), INSERT proposed_actions
   → event: discussion.message, proposed_actions.added
9. UI shows agent reply + an "Apply" button per proposed action
10. User clicks Apply on one
11. UI: POST /api/proposed-actions/{id}/apply
12. Server: validates, mutates task graph (create/modify/cancel),
    marks proposed_action applied
    → events for every affected task
```

## Concurrency model

- One LLM call at any moment (single GPU)
- One worker loop, single-threaded async
- DB access is single-writer (SQLite WAL mode for concurrent reads from API thread)
- WebSocket broadcasts are fanned out from a single emit point inside the worker/API
- Sandbox containers run in parallel with the LLM and with each other (each task gets one; the orchestrator doesn't wait on a sandbox to spawn another task's LLM call) — but per-task work is still serial within that task

## Preemption: discussions and cancellations

A `critical` priority task (e.g. a new discussion message) becomes eligible while a `normal` task is `in_progress`. The orchestrator does NOT kill the running LLM call. It waits for the current LLM call to complete (typically ≤30 seconds), saves intermediate state, transitions the running task back to `ready` (or to a recoverable substate), then picks up the critical task.

This bounds the worst-case "user-waiting-for-agent-to-notice-me" latency to roughly one LLM call. We do not implement true mid-call abort in v1.

## Failure modes and recovery

| Failure | Behavior |
|---|---|
| Orchestrator crash mid-task | On restart, `in_progress` tasks reset to `ready` with a `restart_recovery` note. Idempotent design assumed in handlers. |
| LLM timeout / 5xx | Task transitions to `ready` with retry; after N retries → `needs_human`. |
| Sandbox container crash | Same as LLM timeout — handler captures, retry. |
| User-issued task cancel | Task → `cancelled`. Open children → cancelled cascade. Open questions on it → dismissed. |
| Schema drift on new orchestrator version | Migrations apply on startup. Service does not start serving until DB is at the expected schema version. |

## Out of scope for v1

Explicit non-goals for first version, to keep scope contained:

- Multi-user / multi-machine
- Auth (it's local, single user; UI binds to `localhost`)
- Plugins / custom task types beyond the built-in five
- Cloud-hosted LLM fallback (everything is local)
- Per-task container snapshots (single shared sandbox per goal in v1)
- Mid-call LLM abort (we wait for the call to finish)
- Plan diff/merge across overlapping goals
- True desktop app packaging (Tauri/Electron)
- Multi-project workspaces (one repo at a time per OrchestrAi instance)

Some of these are explicit v2 candidates documented in their respective sub-docs.

## Cross-references

- Data model: [`SCHEMA.md`](SCHEMA.md)
- REST + WebSocket contract: [`API.md`](API.md)
- LLM prompts per task type: [`PROMPTS.md`](PROMPTS.md)
- Execution containers: [`SANDBOX.md`](SANDBOX.md)
- Getting it running: [`SETUP.md`](SETUP.md)
