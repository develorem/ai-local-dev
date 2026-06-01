# 01 — Vision & principles

## Product
OrchestrAi is a **local-first agentic coding platform**: your own AI coding team
that runs 24/7. You give it outcomes; it plans, implements, and reviews work
using AI agents you host yourself, accented by frontier models when you choose.

## The edge (why this exists)
Inverted scarcity vs hosted assistants:
- A hosted assistant's binding constraint is **token budget** — finite, metered,
  gone after a few hours.
- A self-hosted fleet's binding constraint is **per-token model capability**, but
  **compute/time is abundant** — it runs continuously on hardware you already own.

So the platform is designed to spend cheap, abundant local compute to compensate
for weaker models: tight context, right-sized tasks, least-privilege per task,
and background self-maintenance (e.g. the document index). Frontier models (Claude,
etc.) plug in as first-class agents for the hard parts.

## Carry-over principles (proven in v1, keep)
- **Measure before optimizing** — instrument, then act on real numbers.
- **Weak-model economy** — convey the most signal in the fewest tokens; size tasks
  and context to the *executor*, not to a frontier model.
- **Self-healing agents** — if a human must diagnose and re-word a task for the
  agent, the product has failed; build recovery in.
- **Document index** — a seek index (mechanical headings + a one-line purpose),
  fetch full docs on demand; refreshed on change. (See v1 for the working design.)
- **One orchestration contract for every executor** — self-hosted worker,
  downloaded agent, frontier agent, and future leased cloud agents all speak the
  same protocol; only the *executor implementation* differs.

## What changes from v1
- TypeScript end-to-end (was Python).
- A real **service/domain core** with thin transport surfaces (UI/BFF/API/MCP) —
  v1's logic was welded to its HTTP handlers, which coupled the API, UI, and MCP.
- Explicit **deploy modes** (cloud multi-tenant vs self-hosted single-tenant) from
  one codebase.
- **Downloadable, model-configurable agents** as a first-class product surface
  (v1 assumed everyone runs the author's exact model).

## Non-goals (for now)
- Leased (cloud-provisioned, on-demand) agents — designed-for but built later.
- Migrating any v1 data — v2 is greenfield.
