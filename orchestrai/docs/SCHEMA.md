# OrchestrAi — Database Schema

SQLite, single file at `/data/orchestrai.db` inside the orchestrator container, persisted via a Docker volume. WAL mode for concurrent reads from the HTTP layer while the worker writes.

## Why SQLite

- Single-user, single-host workload — no need for a server DB
- Zero-config, embedded in the orchestrator process
- ACID, durable across crashes
- `sqlite3` CLI lets you inspect and recover from anything
- Migrations are just numbered SQL files in `migrations/`

If we outgrow it (multi-user, hosted), migrating to Postgres is a one-time mechanical job — the schema below is portable.

## Migrations

`server/migrations/` contains versioned files:

```
001_initial.sql
002_add_discussions.sql
003_add_proposed_actions.sql
...
```

The orchestrator on startup queries `PRAGMA user_version`, applies any newer migrations in order, sets the new version. Service refuses to serve traffic until DB is at the expected version.

## Connection setup

```sql
PRAGMA foreign_keys = ON;        -- enforce FK constraints
PRAGMA journal_mode = WAL;       -- concurrent readers + single writer
PRAGMA synchronous = NORMAL;     -- crash-safe with WAL
PRAGMA busy_timeout = 5000;      -- ms; queue waiters instead of erroring
```

## Conventions

- All `id` columns are `TEXT PRIMARY KEY` holding ULIDs (sortable, unique, friendly). Stored as 26-char strings.
- All timestamps are `TEXT NOT NULL` ISO-8601 UTC (`YYYY-MM-DDTHH:MM:SS.sssZ`). SQLite has no native datetime; ISO strings sort correctly and integrate with Python `datetime.fromisoformat`.
- Status / type enum columns are `TEXT` with `CHECK` constraints — explicit, debuggable, no separate enums table.
- JSON columns store small structured blobs (e.g. options, payloads). Queried as text; never indexed by JSON content in v1.
- Foreign keys are `ON DELETE CASCADE` unless explicitly noted, because lifecycles are tightly nested (a goal owns its tasks owns its questions owns its messages).
- Indexes on columns that drive worker pick-order and UI filtering.

## Tables

### `goals`

A top-level objective submitted by the human.

```sql
CREATE TABLE goals (
  id              TEXT PRIMARY KEY,
  title           TEXT NOT NULL,
  description_md  TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN (
                    'submitted',   -- just created, planning task queued but not run
                    'planning',    -- plan task in_progress or plan awaiting approval
                    'active',      -- plan approved, implementation tasks running
                    'done',        -- all implementation tasks done
                    'rejected',    -- plan rejected, no work done
                    'abandoned'    -- user cancelled mid-flight
                  )),
  priority        TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN (
                    'low', 'normal', 'high', 'critical'
                  )),
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE INDEX idx_goals_status ON goals(status, created_at);
```

`priority` on a goal becomes the default priority for child tasks. Critical goals push their tasks above other work.

### `tasks`

The atomic unit of agent work.

```sql
CREATE TABLE tasks (
  id                    TEXT PRIMARY KEY,
  goal_id               TEXT REFERENCES goals(id) ON DELETE CASCADE,
                          -- nullable: standalone discussion tasks may have no goal
  parent_task_id        TEXT REFERENCES tasks(id) ON DELETE CASCADE,
                          -- nullable: top-level tasks have no parent
  type                  TEXT NOT NULL CHECK (type IN (
                          'plan',        -- decompose a goal into a plan + tasks
                          'implement',   -- do actual work
                          'review',      -- check acceptance criteria post-impl
                          'discuss',     -- chat thread turn(s)
                          'revise'       -- modify existing tasks/plan
                        )),
  title                 TEXT NOT NULL,
  description_md        TEXT NOT NULL,
  status                TEXT NOT NULL CHECK (status IN (
                          'created',
                          'ready',
                          'in_progress',
                          'blocked_on_dep',
                          'blocked_on_human',
                          'review',
                          'done',
                          'failed',
                          'cancelled'
                        )),
  priority              TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN (
                          'low', 'normal', 'high', 'critical'
                        )),
  depends_on            TEXT NOT NULL DEFAULT '[]',
                          -- JSON array of task IDs that must be 'done' before
                          -- this task can transition to 'ready'
  acceptance_criteria   TEXT NOT NULL DEFAULT '[]',
                          -- JSON array of strings (free-form, plus
                          -- optionally a machine-checkable {kind, cmd, expect})
  attempt_count         INTEGER NOT NULL DEFAULT 0,
  max_attempts          INTEGER NOT NULL DEFAULT 3,
  payload               TEXT NOT NULL DEFAULT '{}',
                          -- JSON: per-type input (e.g. plan task carries goal_id;
                          -- implement task carries plan section anchors)
  result                TEXT,
                          -- JSON: per-type output (diffs, test outcomes, etc.)
                          -- populated when task transitions to 'done'/'failed'
  error                 TEXT,
                          -- last error if failed/retrying
  notes                 TEXT NOT NULL DEFAULT '',
                          -- accumulated discoveries during execution
  created_at            TEXT NOT NULL,
  started_at            TEXT,
  finished_at           TEXT
);

CREATE INDEX idx_tasks_pickup
  ON tasks(status, priority, created_at)
  WHERE status = 'ready';
CREATE INDEX idx_tasks_goal ON tasks(goal_id, status);
CREATE INDEX idx_tasks_parent ON tasks(parent_task_id);
```

#### depends_on semantics

A JSON array of task IDs. The worker treats a task as eligible for pickup only when:
1. `status = 'ready'`
2. All tasks in `depends_on` have `status = 'done'`

If any dependency is in `'failed'` or `'cancelled'`, this task transitions to `'blocked_on_dep'` and stays there until the dependency resolves or a human intervenes.

#### acceptance_criteria semantics

Mixed: free-form strings (used by Reviewer LLM mode) plus optional structured checks:

```json
[
  "GET /health returns 200 with JSON {status: 'ok'}",
  {"kind": "test", "cmd": "pytest tests/test_health.py", "expect_exit": 0},
  {"kind": "file_exists", "path": "src/routes/health.py"}
]
```

Structured entries are checked deterministically by the Reviewer handler before calling the LLM. The LLM only judges the free-form ones.

#### attempt_count semantics

Incremented at the start of every implementation attempt. After `max_attempts`, the task transitions to `failed` with a `needs_human` flag — the human can:
- Edit the task and reset to `ready`
- Open a discussion about it
- Cancel it

### `plans`

Markdown documents produced by `plan` tasks. One plan per goal (versioned via `version` column).

```sql
CREATE TABLE plans (
  id              TEXT PRIMARY KEY,
  goal_id         TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
  version         INTEGER NOT NULL DEFAULT 1,
  content_md      TEXT NOT NULL,
  task_outline    TEXT NOT NULL DEFAULT '[]',
                    -- JSON: ordered list of task stubs the planner intends to create
                    -- on approval. Shape: [{title, type, depends_on:[...], deps_titles, …}]
  status          TEXT NOT NULL CHECK (status IN (
                    'draft',       -- written by planner, awaiting approval
                    'approved',
                    'rejected',
                    'superseded'   -- replaced by a newer version after a revise task
                  )),
  approval_question_id TEXT REFERENCES questions(id) ON DELETE SET NULL,
                    -- the question the human answers to approve/reject
  created_at      TEXT NOT NULL,
  approved_at     TEXT,
  approved_by     TEXT,            -- 'user' (single-user system, but explicit)
  approval_notes  TEXT
);

CREATE UNIQUE INDEX idx_plans_goal_version ON plans(goal_id, version);
```

On approval, the orchestrator instantiates `task_outline` into real `tasks` rows linked to the goal.

### `questions`

Open asks from the agent to the human. The async coordination primitive.

```sql
CREATE TABLE questions (
  id              TEXT PRIMARY KEY,
  task_id         TEXT REFERENCES tasks(id) ON DELETE CASCADE,
                    -- the task that's blocked waiting on this answer
  kind            TEXT NOT NULL CHECK (kind IN (
                    'plan_approval',
                    'clarification',     -- agent doesn't know what to do
                    'choice',            -- agent has options; needs human pick
                    'confirm',           -- agent wants to confirm a destructive action
                    'discussion'         -- discussion thread requires a response
                  )),
  prompt_md       TEXT NOT NULL,
  options_json    TEXT NOT NULL DEFAULT '[]',
                    -- JSON array of {label, value} for choice/confirm types
  status          TEXT NOT NULL CHECK (status IN (
                    'pending',
                    'answered',
                    'dismissed'          -- task was cancelled before answer
                  )),
  answer_md       TEXT,
  answer_value    TEXT,                 -- selected option.value if choice/confirm
  created_at      TEXT NOT NULL,
  answered_at     TEXT
);

CREATE INDEX idx_questions_pending ON questions(status) WHERE status = 'pending';
CREATE INDEX idx_questions_task ON questions(task_id);
```

When all questions on a task are answered, the worker transitions the task back to `ready`.

### `discussions`

Multi-turn chat threads linked to a task, goal, or standalone.

```sql
CREATE TABLE discussions (
  id              TEXT PRIMARY KEY,
  goal_id         TEXT REFERENCES goals(id) ON DELETE CASCADE,
  task_id         TEXT REFERENCES tasks(id) ON DELETE CASCADE,
                    -- exactly one of goal_id/task_id is set, or both null
                    -- (standalone "architecture" discussions)
  title           TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
                    'open', 'closed'
                  )),
  created_at      TEXT NOT NULL,
  closed_at       TEXT
);

CREATE INDEX idx_discussions_task ON discussions(task_id) WHERE task_id IS NOT NULL;
CREATE INDEX idx_discussions_goal ON discussions(goal_id) WHERE goal_id IS NOT NULL;
```

### `messages`

Individual turns inside a discussion.

```sql
CREATE TABLE messages (
  id              TEXT PRIMARY KEY,
  discussion_id   TEXT NOT NULL REFERENCES discussions(id) ON DELETE CASCADE,
  role            TEXT NOT NULL CHECK (role IN ('user', 'agent', 'system')),
  content_md      TEXT NOT NULL,
  meta            TEXT NOT NULL DEFAULT '{}',
                    -- JSON: model used, token counts, latency, etc.
  created_at      TEXT NOT NULL
);

CREATE INDEX idx_messages_discussion ON messages(discussion_id, created_at);
```

### `proposed_actions`

Mutations to the task graph that an agent (typically inside a Discusser turn) suggests. Never auto-applied — the human clicks Apply.

```sql
CREATE TABLE proposed_actions (
  id              TEXT PRIMARY KEY,
  discussion_id   TEXT NOT NULL REFERENCES discussions(id) ON DELETE CASCADE,
  message_id      TEXT REFERENCES messages(id) ON DELETE CASCADE,
                    -- the agent message that proposed this action
  action_type     TEXT NOT NULL CHECK (action_type IN (
                    'create_task',
                    'modify_task',
                    'cancel_task',
                    'reorder_dependencies',
                    'edit_plan'
                  )),
  payload         TEXT NOT NULL,
                    -- JSON: action-type-specific. e.g. for modify_task:
                    -- {task_id, changes: {field: new_value, ...}}
  human_summary   TEXT NOT NULL,
                    -- pre-rendered one-liner the UI shows next to Apply
  status          TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN (
                    'proposed', 'applied', 'rejected', 'superseded'
                  )),
  created_at      TEXT NOT NULL,
  applied_at      TEXT,
  applied_by      TEXT
);

CREATE INDEX idx_proposed_actions_discussion ON proposed_actions(discussion_id);
CREATE INDEX idx_proposed_actions_status ON proposed_actions(status) WHERE status = 'proposed';
```

### `events`

Append-only audit log. Every state-changing operation writes an event. Drives the WebSocket stream and the per-task history view.

```sql
CREATE TABLE events (
  id              TEXT PRIMARY KEY,        -- ULID, sortable
  ts              TEXT NOT NULL,
  kind            TEXT NOT NULL,
                    -- e.g. 'task.created', 'task.status_changed',
                    -- 'question.answered', 'discussion.message', etc.
                    -- Hierarchical dotted strings; UI filters by prefix.
  entity_type     TEXT NOT NULL,
                    -- 'goal' | 'task' | 'question' | 'discussion' | 'message' |
                    -- 'proposed_action' | 'plan'
  entity_id       TEXT NOT NULL,
  goal_id         TEXT REFERENCES goals(id) ON DELETE SET NULL,
                    -- denormalized for filtering events by goal cheaply
  task_id         TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                    -- denormalized for filtering events by task cheaply
  detail          TEXT NOT NULL DEFAULT '{}',
                    -- JSON: kind-specific payload
  actor           TEXT NOT NULL DEFAULT 'system'
                    -- 'system' (orchestrator), 'user' (human action),
                    -- 'agent' (LLM-driven action)
);

CREATE INDEX idx_events_ts ON events(ts);
CREATE INDEX idx_events_entity ON events(entity_type, entity_id, ts);
CREATE INDEX idx_events_goal ON events(goal_id, ts) WHERE goal_id IS NOT NULL;
CREATE INDEX idx_events_task ON events(task_id, ts) WHERE task_id IS NOT NULL;
```

Events never get deleted. We may add a TTL/archive job in v2 but for v1 they live forever — they're cheap and they're the source of truth for "what happened, when."

### `settings`

Catch-all for runtime configuration that should persist (model name, num_ctx, etc.). Schema-less key/value.

```sql
CREATE TABLE settings (
  key             TEXT PRIMARY KEY,
  value           TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
```

Initial seed values written by migrations or first-run:

| key | example value | purpose |
|---|---|---|
| `model.primary` | `qwen2.5-coder:14b` | model used for implement/review/plan |
| `model.discusser` | `qwen2.5-coder:14b` | could differ later |
| `inference.num_ctx` | `16384` | context size passed to Ollama |
| `inference.temperature` | `0` | determinism for coding |
| `sandbox.image` | `orchestrai-sandbox:latest` | container image used for code execution |
| `sandbox.lifetime` | `per_goal` | `per_goal` or `per_task` |
| `loop.max_attempts_default` | `3` | default before tasks fail to needs_human |
| `loop.idle_poll_seconds` | `5` | worker sleep when nothing ready |

## Foreign-key cascade rules summary

| From | To | On Delete |
|---|---|---|
| `tasks.goal_id` | `goals.id` | CASCADE |
| `tasks.parent_task_id` | `tasks.id` | CASCADE |
| `plans.goal_id` | `goals.id` | CASCADE |
| `plans.approval_question_id` | `questions.id` | SET NULL |
| `questions.task_id` | `tasks.id` | CASCADE |
| `discussions.goal_id` | `goals.id` | CASCADE |
| `discussions.task_id` | `tasks.id` | CASCADE |
| `messages.discussion_id` | `discussions.id` | CASCADE |
| `proposed_actions.discussion_id` | `discussions.id` | CASCADE |
| `proposed_actions.message_id` | `messages.id` | CASCADE |
| `events.goal_id` | `goals.id` | SET NULL |
| `events.task_id` | `tasks.id` | SET NULL |

Notes:
- Deleting a goal nukes everything under it. This is the intended behavior of `abandon`. (Internally we may keep events around even after entity deletion by setting `SET NULL` on event FKs.)
- Events keep their `task_id` / `goal_id` as orphaned references after deletion; this preserves history. Hence `SET NULL` rather than `CASCADE`.

## Example queries

### Worker pickup query

```sql
SELECT t.* FROM tasks t
WHERE t.status = 'ready'
  AND NOT EXISTS (
    SELECT 1 FROM json_each(t.depends_on) AS dep
    JOIN tasks dt ON dt.id = dep.value
    WHERE dt.status != 'done'
  )
ORDER BY
  CASE t.priority
    WHEN 'critical' THEN 0
    WHEN 'high'     THEN 1
    WHEN 'normal'   THEN 2
    WHEN 'low'      THEN 3
  END,
  t.created_at ASC
LIMIT 1;
```

### Open inbox for the UI

```sql
SELECT q.*, t.title AS task_title, g.title AS goal_title
FROM questions q
LEFT JOIN tasks t ON t.id = q.task_id
LEFT JOIN goals g ON g.id = t.goal_id
WHERE q.status = 'pending'
ORDER BY q.created_at ASC;
```

### Live event stream tail (for WebSocket replay or UI history)

```sql
SELECT * FROM events
WHERE ts > ?
ORDER BY ts ASC
LIMIT 500;
```

## Migrations as the system evolves

The schema above is v1. Migration files are append-only — once a migration ships, it never gets edited. Any change is a new migration. Future expected migrations:

- Per-task sandbox containers (`sandbox_container_id` on tasks)
- Snapshots (`workspace_snapshot_ref` linking tasks to snapshot IDs)
- Multi-project workspaces (a `projects` table; goals get `project_id`)
- Plan diffs across versions
- Per-user attribution (if we ever go multi-user)

Each is a small additive migration. None require redesigning existing tables.
