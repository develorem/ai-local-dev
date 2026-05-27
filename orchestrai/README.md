# OrchestrAi

A local-first agentic coding platform that turns a single-GPU machine into a personal autonomous developer. You give it large coding goals, it decomposes them, runs the work sequentially, asks you questions when it's blocked, and accepts new goals or design discussions at any time — all visible in a live browser dashboard.

> **Status: design phase.** This folder currently contains the design docs. No runtime code yet. The implementation will be built against these docs.

## The shape of it

```
You ──submit goal──▶ ┌────────────────┐ ──tasks──▶ Local LLM (Ollama, GPU)
                     │                │
                     │   OrchestrAi   │ ──files──▶ Sandbox container (Linux)
                     │   service      │
                     │                │ ──events──▶ Live UI (browser tab)
                     └────────────────┘
                            ▲
You ◄─── questions ─────────┤
You ── answers/discussions ▶┘
```

One Python service, one local LLM, throwaway Linux sandboxes for code execution, and a browser UI you keep open while you work. Everything runs from `docker compose up`.

## Core ideas in one breath

- **Single-GPU = single-threaded inference** → accept it, build concurrency at the task layer instead.
- **Everything is a task in a queue** → adding a goal, planning, approving, discussing, implementing — all are typed tasks the same loop handles.
- **Async human-in-the-loop** → when a task needs your input, the orchestrator parks it and works other tasks. You answer on your time. It picks back up when free.
- **Disposable Linux sandboxes** → the agent never runs commands on your host. It runs them in throwaway containers with the project workspace mounted.
- **Observable state** → live browser dashboard shows what the agent is doing, what it's waiting on you for, full event history per task.

## Design docs (start here)

| Doc | What's in it |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The high-level design — components, data flows, state machines, what's in v1 vs not |
| [`docs/SCHEMA.md`](docs/SCHEMA.md) | The SQLite schema, table by table, with rationale |
| [`docs/API.md`](docs/API.md) | REST endpoints and WebSocket events the UI consumes |
| [`docs/PROMPTS.md`](docs/PROMPTS.md) | System prompts and structured-output contracts for each LLM mode (planner, implementer, reviewer, discusser, …) |
| [`docs/SANDBOX.md`](docs/SANDBOX.md) | How the execution containers work — image, mounts, lifecycle, security |
| [`docs/SETUP.md`](docs/SETUP.md) | Getting it running on a fresh machine — Docker, WSL2, GPU verification |

Read them in roughly that order. ARCHITECTURE gives the model; the others fill in the contracts.

## Why this exists

The repo's parent `test-harness/` work selected a local model and Ollama configuration that delivers an agent-grade experience on a single RTX 5080: `qwen2.5-coder:14b` at 16K context, ~80 tok/s, with perfect tool-call reliability. That's the *engine*. OrchestrAi is the *vehicle* — the thing that takes a "build me a feature" and turns it into reviewed, working code.

See [`../docs/RECOMMENDATION.md`](../docs/RECOMMENDATION.md) for the model decision and [`../docs/FINDINGS.md`](../docs/FINDINGS.md) for the discovery work that led to it.

## Where this is heading

Phase 1 (current): design docs. Stable enough to implement against.
Phase 2: schema + API skeleton + worker loop + read-only UI. Hand-written tasks.json seed; watch tasks flow.
Phase 3: per-LLM-mode integration (planner, implementer, reviewer).
Phase 4: discussions, proposed-actions, plan-approval flow.
Phase 5: sandbox per-task with snapshots; harden for shareability.

No promises on timelines — this is one-person hobby-grade work — but the design is structured so each phase produces a usable artifact.

## Relationship to the test-harness

- The harness measures model behavior in isolation. OrchestrAi is the runtime that uses the chosen model.
- The harness's `ollama_client.py` is the seed of OrchestrAi's LLM layer; we'll fork it into `server/llm/`.
- The harness's quality problems (code generation, tool-call reliability, long-context bug-find) are unit tests for the model. They tell us nothing about real multi-task agent behavior — that's what OrchestrAi will eventually let us measure end-to-end.

## Non-goals (intentional limits)

- Multi-user / team workflows
- Cloud-hosted variant
- Auth (single-user local only)
- Replacing your IDE — OrchestrAi is a *peer*, not a *plugin*. You can have it building feature X in one tab while you hand-code feature Y in your editor.
- Drop-in compatibility with OpenAI/Claude APIs — it's purpose-built around local model constraints and async human-in-the-loop, not a general agent SDK.

## Open questions parked in the docs

Each doc has its own "out of scope" or "open questions" section. The biggest unresolved one is whether per-task sandboxes with workspace snapshots should be in v1 (currently scoped to v2). Read the SANDBOX doc and form an opinion before we start cutting code.

## Feedback loop

This README and the six design docs are intended to be **edited in place** as we refine the design. Treat them as the source of truth. Implementation will follow. If something here is wrong or ambiguous, fix the doc first, then write the code.
