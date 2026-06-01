# v1 agent — reference source (read-only)

This is the **v1 OrchestrAi agent**, copied here verbatim as the reference
implementation for `docs/07-agent-executor.md` ("port from v1, don't reinvent").

- **Language: Python.** v2 is TypeScript — this is here to **port from**, not to
  run or import. Read it for the *logic and lessons*, then reimplement in TS.
- It's the standalone worker: it claimed tasks from the v1 hub over REST and ran
  the execution pipeline locally. v2 keeps the same pipeline but the agent is
  fully standalone (owns its workspace, pushes to git, reports via API/MCP).

## Where the value is (map to doc 07)
- `orchestrai_agent/handlers/implement.py` — the **two-pass implement pipeline**
  (the core): pass-1 plan, file read/outline, doc fetch, pass-2 diff/files, apply,
  verify, inline fix loop, corrupt-diff recovery, retry-context budgeting.
- `orchestrai_agent/handlers/plan.py` — planner output, validation, enum coercion,
  acceptance-criteria sanitization, `tools_required`.
- `orchestrai_agent/handlers/review.py` + `prompts/reviewer.md` — review +
  the `kind:http` start-probe-stop server lifecycle (via `scripts/orchestrai-serve`).
- `orchestrai_agent/prompt_context.py` — document-index + secret-names blocks
  (names only, never values).
- `orchestrai_agent/workspace.py` — clone, `read_files`, `write_files`,
  `apply_diff` + diff auto-repair, relevance-ranked tree, repo-doc scan.
- `orchestrai_agent/file_outline.py` — large-file outlining (signatures + relevant
  bodies, elide the rest).
- `orchestrai_agent/ollama_client.py`, `response_parser.py` (extract_json),
  `prompt_metrics.py`, `loop.py` (the claim/run loop), `hub_client.py` (the exact
  API calls the agent relied on — useful for the v2 executor↔API contract).
- `handlers/{revise,reindex,preview,discuss,ci_fix,pr_review}.py` + matching
  `prompts/*.md` — the secondary pipelines.

## Don'ts
- Don't wire this into the v2 build or CI. It's documentation.
- Don't copy v1's hub-driven assumptions — v2 agents are standalone (see doc 07,
  "What's different in v2").
