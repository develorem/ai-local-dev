# 07 — Agent executor (port from v1, don't reinvent)

This is the hardest, highest-value part of the system and the place v1 sank the
most effort. **Wiring "an OllamaExecutor" is the easy 20% — calling a model. The
80% that matters is the per-task pipeline around it** (two-pass implement, prompt
budgeting, context selection, verification + retry, diff recovery). Port that
pipeline; don't write a naive "send task to model, hope for code" loop.

v1 reference (the working **Python** implementation to mine) is bundled with
these docs at **`docs/reference/v1-agent/`** (see its README for a file map) —
`orchestrai_agent/handlers/{plan,implement,review,revise,reindex,preview,ci_fix}.py`,
`prompt_context.py`, `prompts/*.md`, `workspace.py`, `file_outline.py`,
`ollama_client.py`, `response_parser.py`, `prompt_metrics.py`, `loop.py`,
`hub_client.py`, plus `scripts/orchestrai-serve`. It's reference to port from, not
to run (Python; v2 is TS).

## How it fits in v2
The v2 agent is **standalone**: it polls the API for a task, runs the right
pipeline locally **against its own git workspace**, commits/pushes, and reports
status/results via the API. Two layers:

- **Executor** = the LLM-call abstraction. `generate(prompt, opts) → {text, stats}`.
  Implementations: `EchoExecutor` (canned, for CI / no-GPU) and `OllamaExecutor`
  (real model). This is the small part — keep it behind an interface.
- **Handlers / pipelines** = per-task-type logic that *uses* the executor: build
  prompt → call executor → parse JSON → act (write files / apply diff / run
  checks) → verify → report. This is where the value is.

Task types (v1 set): `plan`, `implement`, `review`, `revise` (also "task repair"),
`reindex`, `preview`, `discuss`, `respond_to_ci_failure`. The big three are
**plan**, **implement** (two-pass), and **review**.

## Executor (the easy layer)
- One call shape; options that proved out for a 14B local model:
  `num_ctx` from the model's configured context (16384 in v1), `temperature: 0`,
  `seed: 42` (determinism/repro), `num_predict` per pass (pass1 ~1024,
  pass2/plan ~4096, fix ~2048). Return generation stats (tokens, tok/s, wall).
- `EchoExecutor` returns deterministic canned JSON so the whole pipeline + CI run
  without a GPU. Never delete it.

## JSON output discipline
Models wrap output in prose / fences. v1's `extract_json`:
- Pull the first fenced ```json block if present, else the first balanced `{...}`.
- Be lenient (strip fences, trailing commas where feasible).
- **Validate the parsed object against the expected schema; on failure, return a
  `fix_needed`/retry rather than crashing.** In v2 use **zod** schemas (shared
  package) for every pass's expected shape.

## The core: two-pass IMPLEMENT pipeline
Splitting into two passes is the single most important design choice — it keeps
each prompt small and lets the model *choose what context it needs* before it
writes code.

**Pass 1 (planning — no code written).** Prompt = project context + **document
index** (not bodies) + task + acceptance criteria + a relevance-ranked
**workspace tree** (capped) + retry context (if retrying). Model returns:
```json
{ "files_to_read": ["path", ...],
  "documents_to_read": ["exact doc title", ...],
  "files_to_write_or_modify": [{"path": "...", "intent": "..."}],
  "commands_to_run_for_verification": ["..."],
  "diff_plan_md": "2-6 sentences",
  "questions": [] }
```
If `questions` is non-empty → report `needs_human` (don't guess).

**Between passes:** read the requested files from the workspace (cap total chars;
**outline** large files — keep signatures, elide unrelated bodies, see
`file_outline.maybe_outline`), and fetch the **full text of requested documents**
(by title, from the index). Keep both bounded.

**Pass 2 (production).** Prompt = project context + doc index + **full text of
requested files + requested docs** + the pass-1 plan + task + criteria. Model returns:
```json
{ "files": [{"path": "...", "content": "<full file>"}],
  "diff": "<unified diff, optional>",
  "commands_to_run": ["..."],
  "expected_outcomes": [{"cmd_idx": 0, "expect_exit": 0}],
  "notes_md": "...", "questions": [], "discoveries": [] }
```
- **Strongly prefer whole-file `files[]` over `diff`.** Local-model diffs are
  unreliable. Apply `files[]` first; only use `diff` for partial edits to large
  files the model was given in full.

**Apply → verify → fix loop:**
1. Write `files[]` (refuse path traversal / absolute paths).
2. If `diff` present, `git apply --check` then apply; on failure, **auto-repair**
   the diff (re-prefix orphaned context lines, recompute `@@` hunk counts) and
   retry once; if still failing, **recover as full files** (ask the model to
   resend the affected files whole — always applies).
3. Commit.
4. Run the verification commands (bounded timeout, capture exit/stdout/stderr).
5. If any fail, run an **inline fix loop** (≤ `MAX_FIX_ITERATIONS`, v1 used 3):
   feed the model the *actual failing command output* + current file contents,
   let it correct, re-run. Stop when green or when it gives up.
6. Report `success` (all green) or `fix_needed` (with the failure context) so the
   attempt-level retry can take over.

## Prompt budgeting / weak-model economy (non-negotiable for local models)
The binding constraint is the model's context window (~43K usable chars at 16K
tokens in v1; P95 prompt ~5K). Every section is measured and bounded:
- Emit `prompt.metrics` per call (size of each section: project context, doc
  index, files, retry block, etc.) so you can *measure* before optimizing.
- **Workspace tree** is relevance-ranked (task-keyword + entry-point + pinned
  files) and capped (~1.2KB), not a full dump.
- **Large files are outlined**, not pasted whole (keep signatures + bodies that
  match task keywords; elide the rest with `# body elided (N lines)`).
- **Retry context is hard-capped** (v1: 4KB total) and carries only the *useful*
  signal: the LAST failing command's error, a one-line summary of prior attempts,
  and the current on-disk content of files the last attempt touched.
- Conditional sections: only inject web/HTTP guidance for web tasks, test-writing
  guidance for test tasks, etc. (`kind_hint`).

## Document-index context (carry v1's design)
Don't inject document bodies. Inject the **index** (title + one-line purpose +
section headings) in pass 1; the model lists which docs it needs in
`documents_to_read`; fetch those bodies for pass 2. (See v1 `prompt_context.py`
+ the doc-index design.) Same for repo docs.

## Secrets (security — carry exactly)
Prompts get secret **names + descriptions only, never values**. The task declares
`secrets_needed`; at runtime the agent fetches a value from the audited
`/secrets/{name}/value` endpoint (org-scoped, time-limited, logged). Values never
enter a prompt or a log.

## PLAN pipeline
Single pass. Prompt = project context + doc index + the outcome. Returns:
```json
{ "plan_md": "...", "tasks": [{title, type, priority, kind_hint,
  description_md, acceptance_criteria, depends_on_titles}], "questions": [],
  "tools_required": {python_packages:[], node_packages:[]} }
```
- Validate + **coerce unknown enums to safe defaults** (don't fail the whole plan
  on a bad `type`/`priority`/`kind_hint`).
- **Sanitize acceptance criteria** the runtime can't satisfy (v1 strips pytest
  `--cov*` flags because coverage isn't installed — a class of "criterion can
  never pass" bugs). Generalize: scrub commands the agent image can't run.
- `tools_required` is unioned into the project's tool list on plan approval; the
  agent installs missing packages before a task runs.

## REVIEW pipeline (+ the kind:http lifecycle)
Reviews check acceptance criteria. For **web/HTTP** criteria, the reviewer must
**start the app, probe it, then stop it** — v1 uses `orchestrai-serve --port N --
<cmd>` which backgrounds the server, waits until the port is reachable, and exits
0; the reviewer hits it, then `orchestrai-serve --stop N`. (This was a real v1 bug
class: reviews of web tasks hung or failed because nothing managed the server's
lifecycle.) Verification commands must always **exit** — never `--reload`/`--watch`.

## Other handlers (brief)
- **revise / task-repair:** when a task fails after max attempts, a repair pass
  diagnoses and rewrites the task (description/criteria) so a fresh attempt can
  succeed — guarded so it runs at most once (loop guard). This is the
  "self-healing" principle in practice.
- **reindex:** regenerate the one-line doc purposes when content changes
  (mechanical headings are recomputed on save; purpose is the model bit).
- **discuss:** produce a chat reply + optional proposed actions. (Defer in v2
  unless wanted.)

## Hard-won gotchas (carry these forward)
- **attempt_count off-by-one:** v1's claim increments `attempt_count` to 1 on the
  *first* try, so "first attempt" = `== 1`, "retry" = `>= 2`. Whatever v2's claim
  semantics are, define this explicitly — retry context gating depends on it.
- **`kind_hint` enum** (`web|test|algo|refactor|data|other`) drives conditional
  prompt sections; default it sanely.
- **Subprocess hygiene:** run each command in its own process group and kill the
  whole group on timeout (double-fork servers otherwise hang on pipe reads).
- **`orchestrai-serve` log truncates on start** (don't let the fix loop read stale
  output from a previous run).
- **Preinstalled packages:** keep an allow-list of what's in the agent image;
  anything else must be declared by the planner, not pip-installed inline.

## What's different in v2 (so the port isn't blind)
- **Language:** TypeScript, not Python. Reuse zod for all pass schemas.
- **Standalone agent:** the agent owns the workspace end-to-end — clone, branch,
  commit, **push to the configured repo** — and reports status/results via the
  API (REST polling) or MCP. There is no server-side workspace and no hub-driven
  handler split; the whole pipeline runs in the agent.
- **Model-agnostic:** the executor targets whatever model the user installed via
  Ollama; the image self-installs the model. Read the model's real context window
  and budget against it (don't hard-code 16K).
- **Agent has its own local UI:** surface current task, model, system specs, and
  recent runs there.

## Suggested port order
1. Executor interface + EchoExecutor + OllamaExecutor (+ JSON/zod parsing).
2. A thin task loop: poll → claim → run → report (prove it with Echo first).
3. Implement pipeline pass 1 → file read/outline + doc fetch → pass 2 → apply →
   verify → inline fix loop → report. (This alone makes the agent useful.)
4. Prompt budgeting + `prompt.metrics` + workspace-tree ranking + file outlining.
5. Plan pipeline (+ validation/sanitization). Then review (+ http lifecycle).
6. Retry context, diff auto-repair + full-file recovery, task-repair, reindex.

Measure-first throughout: ship, watch the metrics, then tune. This pipeline is
iterative by nature — v1 took many rounds to get the prompts and budgets right.
