# OrchestrAi — Database Schema

SQLite, single file at `/data/orchestrai.db` inside the **Hub** container, persisted via a Docker volume. WAL mode for concurrent reads from the HTTP layer while writers (worker + agent endpoints) commit.

The Hub holds the database. **Agents have no local persistent state** — they pull tasks and push results to the Hub. If an Agent dies, the Hub re-claims its leased tasks and any other Agent can pick them up.

## Connection setup

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
```

## Conventions

- IDs: ULIDs as `TEXT PRIMARY KEY` (26 chars, sortable)
- Timestamps: ISO-8601 UTC strings
- Enums: `TEXT` with `CHECK` constraints
- JSON: small structured blobs in `TEXT` columns, queried as text
- FKs: `ON DELETE CASCADE` for tight ownership; `SET NULL` where the parent is a loose reference (e.g. events keep their dangling FK after deletion for audit)

## Entity overview

| Table | Owns | Owned by |
|---|---|---|
| `projects` | repos, goals, context | — |
| `project_repos` | (rows in tasks via `repo_id`) | project |
| `goals` | tasks, plans, discussions | project |
| `tasks` | questions, child tasks, events | goal, project, repo |
| `plans` | — | goal |
| `questions` | answers | task |
| `discussions` | messages, proposed_actions | goal or task |
| `messages` | — | discussion |
| `proposed_actions` | — | discussion |
| `agents` | leases on tasks (denormalized to tasks.assigned_agent_id) | — |
| `secrets` | accesses | — |
| `secret_accesses` | — | secret, agent, task |
| `events` | — | (loose refs to any entity) |
| `settings` | — | — |

## Tables

### `projects`

A logical product or system the agent works on. Owns one or more git repos plus shared context.

```sql
CREATE TABLE projects (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  slug            TEXT NOT NULL UNIQUE,
  description_md  TEXT NOT NULL DEFAULT '',
  context_md      TEXT NOT NULL DEFAULT '',
                    -- the "what is this project" doc agents read when picking up tasks
                    -- keep terse — see PROMPTS.md "Token-efficient context"
  status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                    'active', 'archived'
                  )),
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  archived_at     TEXT
);
```

### `project_repos`

A git repository belonging to a project. A microservices project will have several. Each row carries enough info for an agent to clone and work on it.

```sql
CREATE TABLE project_repos (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name            TEXT NOT NULL,            -- e.g. "api-gateway"
  role            TEXT,                     -- 'service' | 'frontend' | 'infra' |
                                            -- 'shared-lib' | 'docs' — terse hint
  url             TEXT NOT NULL,            -- git URL (https or ssh)
  default_branch  TEXT NOT NULL DEFAULT 'main',
  description_md  TEXT NOT NULL DEFAULT '', -- 1-2 sentences max; LLM context
  created_at      TEXT NOT NULL,
  UNIQUE(project_id, name)
);
```

### `goals`

High-level user-supplied objectives. Always belong to a project.

```sql
CREATE TABLE goals (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title           TEXT NOT NULL,
  description_md  TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN (
                    'submitted', 'planning', 'active', 'done', 'rejected', 'abandoned'
                  )),
  priority        TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN (
                    'low', 'normal', 'high', 'critical'
                  )),
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE INDEX idx_goals_project ON goals(project_id, status);
```

### `tasks`

The atomic unit of agent work. Now carries a project, an optional repo, a branch, and lease columns.

```sql
CREATE TABLE tasks (
  id                    TEXT PRIMARY KEY,
  project_id            TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  goal_id               TEXT REFERENCES goals(id) ON DELETE CASCADE,
  parent_task_id        TEXT REFERENCES tasks(id) ON DELETE CASCADE,
  repo_id               TEXT REFERENCES project_repos(id) ON DELETE SET NULL,
                          -- nullable: not every task touches a specific repo
                          -- (e.g. a discussion task)
  branch_name           TEXT,
                          -- nullable: tasks operating on a repo carry their
                          -- feature branch name; the branch IS the lock
  type                  TEXT NOT NULL CHECK (type IN (
                          'plan',
                          'implement',
                          'review',                 -- post-implement acceptance check
                          'review_pr',              -- review a PR (peer/CI-style)
                          'respond_to_ci_failure',
                          'discuss',
                          'revise'
                        )),
  title                 TEXT NOT NULL,
  description_md        TEXT NOT NULL,
  status                TEXT NOT NULL CHECK (status IN (
                          'created', 'ready', 'in_progress',
                          'blocked_on_dep', 'blocked_on_human', 'review',
                          'done', 'failed', 'cancelled'
                        )),
  priority              TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN (
                          'low', 'normal', 'high', 'critical'
                        )),
  depends_on            TEXT NOT NULL DEFAULT '[]',
  acceptance_criteria   TEXT NOT NULL DEFAULT '[]',
  payload               TEXT NOT NULL DEFAULT '{}',
  result                TEXT,
  error                 TEXT,
  notes                 TEXT NOT NULL DEFAULT '',
  attempt_count         INTEGER NOT NULL DEFAULT 0,
  max_attempts          INTEGER NOT NULL DEFAULT 3,

  -- Lease (set when an Agent claims the task)
  assigned_agent_id     TEXT REFERENCES agents(id) ON DELETE SET NULL,
  lease_expires_at      TEXT,

  created_at            TEXT NOT NULL,
  started_at            TEXT,
  finished_at           TEXT
);

CREATE INDEX idx_tasks_pickup
  ON tasks(status, priority, created_at)
  WHERE status = 'ready' AND assigned_agent_id IS NULL;
CREATE INDEX idx_tasks_lease_expiry
  ON tasks(lease_expires_at)
  WHERE status = 'in_progress' AND lease_expires_at IS NOT NULL;
CREATE INDEX idx_tasks_project ON tasks(project_id, status);
CREATE INDEX idx_tasks_goal ON tasks(goal_id, status);
CREATE INDEX idx_tasks_repo_branch ON tasks(repo_id, branch_name);
CREATE INDEX idx_tasks_assigned ON tasks(assigned_agent_id) WHERE assigned_agent_id IS NOT NULL;
```

#### Branch-as-lease semantics

If `repo_id` + `branch_name` are set, the Hub's claim logic refuses to assign a *different* task touching the same `(repo_id, branch_name)` to a *different* agent until the first is released. One task per branch in-flight. Two tasks on different branches in the same repo can run in parallel on different agents.

### `plans`

Markdown plan docs produced by `plan` tasks. Versioned per goal.

```sql
CREATE TABLE plans (
  id                    TEXT PRIMARY KEY,
  goal_id               TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
  version               INTEGER NOT NULL DEFAULT 1,
  content_md            TEXT NOT NULL,
  task_outline          TEXT NOT NULL DEFAULT '[]',
  status                TEXT NOT NULL CHECK (status IN (
                          'draft', 'approved', 'rejected', 'superseded'
                        )),
  approval_question_id  TEXT REFERENCES questions(id) ON DELETE SET NULL,
  created_at            TEXT NOT NULL,
  approved_at           TEXT,
  approval_notes        TEXT
);

CREATE UNIQUE INDEX idx_plans_goal_version ON plans(goal_id, version);
```

### `questions`

Open asks from the agent to the human. Same role as before, unchanged.

```sql
CREATE TABLE questions (
  id              TEXT PRIMARY KEY,
  task_id         TEXT REFERENCES tasks(id) ON DELETE CASCADE,
  kind            TEXT NOT NULL CHECK (kind IN (
                    'plan_approval', 'clarification', 'choice', 'confirm', 'discussion'
                  )),
  prompt_md       TEXT NOT NULL,
  options_json    TEXT NOT NULL DEFAULT '[]',
  status          TEXT NOT NULL CHECK (status IN ('pending', 'answered', 'dismissed')),
  answer_md       TEXT,
  answer_value    TEXT,
  created_at      TEXT NOT NULL,
  answered_at     TEXT
);

CREATE INDEX idx_questions_pending ON questions(status) WHERE status = 'pending';
CREATE INDEX idx_questions_task ON questions(task_id);
```

### `discussions`

```sql
CREATE TABLE discussions (
  id              TEXT PRIMARY KEY,
  project_id      TEXT REFERENCES projects(id) ON DELETE CASCADE,
  goal_id         TEXT REFERENCES goals(id) ON DELETE CASCADE,
  task_id         TEXT REFERENCES tasks(id) ON DELETE CASCADE,
                    -- at most one of {goal_id, task_id} set; project_id required
  title           TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
  created_at      TEXT NOT NULL,
  closed_at       TEXT
);
```

### `messages`

```sql
CREATE TABLE messages (
  id              TEXT PRIMARY KEY,
  discussion_id   TEXT NOT NULL REFERENCES discussions(id) ON DELETE CASCADE,
  role            TEXT NOT NULL CHECK (role IN ('user', 'agent', 'system')),
  content_md      TEXT NOT NULL,
  meta            TEXT NOT NULL DEFAULT '{}',
  created_at      TEXT NOT NULL
);
```

### `proposed_actions`

```sql
CREATE TABLE proposed_actions (
  id              TEXT PRIMARY KEY,
  discussion_id   TEXT NOT NULL REFERENCES discussions(id) ON DELETE CASCADE,
  message_id      TEXT REFERENCES messages(id) ON DELETE CASCADE,
  action_type     TEXT NOT NULL CHECK (action_type IN (
                    'create_task', 'modify_task', 'cancel_task',
                    'reorder_dependencies', 'edit_plan'
                  )),
  payload         TEXT NOT NULL,
  human_summary   TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN (
                    'proposed', 'applied', 'rejected', 'superseded'
                  )),
  created_at      TEXT NOT NULL,
  applied_at      TEXT,
  applied_by      TEXT
);
```

### `agents`

The disposable workers. Register on boot, heartbeat to keep their leases alive, claim tasks, release on shutdown.

```sql
CREATE TABLE agents (
  id                  TEXT PRIMARY KEY,
  name                TEXT NOT NULL,
                        -- user-set label or auto: "agent@<host>-<pid>"
  host                TEXT,
                        -- machine hostname for human display
  version             TEXT NOT NULL,
                        -- agent code version, for compat checks
  capabilities        TEXT NOT NULL DEFAULT '[]',
                        -- JSON array: ['gpu', 'docker', 'linux', 'has-aws-cli', ...]
                        -- used by Hub to route tasks (v2); v1 ignores
  status              TEXT NOT NULL CHECK (status IN (
                        'connected',    -- has heartbeated recently
                        'idle',         -- connected, no current task
                        'busy',         -- connected, holds task lease
                        'lost',         -- missed heartbeats, leases reclaimed
                        'released'      -- gracefully shut down
                      )),
  lease_token         TEXT NOT NULL,
                        -- random opaque token; required on every agent API call
  last_heartbeat_at   TEXT,
  current_task_id     TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                        -- denormalized for fast UI display; truth is on tasks
  registered_at       TEXT NOT NULL,
  released_at         TEXT
);

CREATE INDEX idx_agents_status ON agents(status);
CREATE INDEX idx_agents_heartbeat ON agents(last_heartbeat_at);
```

### `secrets`

Encrypted-at-rest credentials. The master key lives outside the DB volume (mounted in via Docker secret or env var on the Hub).

```sql
CREATE TABLE secrets (
  name                TEXT PRIMARY KEY,
                        -- e.g. 'GITHUB_TOKEN', 'OPENAI_API_KEY'
  ciphertext          TEXT NOT NULL,
                        -- base64 of (nonce || ciphertext || tag) from AES-256-GCM
  description         TEXT NOT NULL DEFAULT '',
                        -- human-readable purpose; never the value itself
  scope               TEXT NOT NULL DEFAULT 'global',
                        -- 'global' | 'project:<project_id>' | 'repo:<repo_id>'
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  last_accessed_at    TEXT,
  access_count        INTEGER NOT NULL DEFAULT 0
);
```

### `secret_accesses`

Every fetch logged. Drives the per-secret audit view in the UI.

```sql
CREATE TABLE secret_accesses (
  id              TEXT PRIMARY KEY,
  secret_name     TEXT NOT NULL REFERENCES secrets(name) ON DELETE CASCADE,
  agent_id        TEXT REFERENCES agents(id) ON DELETE SET NULL,
  task_id         TEXT REFERENCES tasks(id) ON DELETE SET NULL,
  ts              TEXT NOT NULL,
  result          TEXT NOT NULL CHECK (result IN ('issued', 'denied')),
  reason          TEXT
);

CREATE INDEX idx_secret_accesses_secret ON secret_accesses(secret_name, ts);
CREATE INDEX idx_secret_accesses_agent ON secret_accesses(agent_id, ts);
```

### `events`

Append-only audit + UI feed. Same as before, with `project_id` denormalized for cheap per-project history queries.

```sql
CREATE TABLE events (
  id              TEXT PRIMARY KEY,
  ts              TEXT NOT NULL,
  kind            TEXT NOT NULL,
  entity_type     TEXT NOT NULL,
  entity_id       TEXT NOT NULL,
  project_id      TEXT REFERENCES projects(id) ON DELETE SET NULL,
  goal_id         TEXT REFERENCES goals(id) ON DELETE SET NULL,
  task_id         TEXT REFERENCES tasks(id) ON DELETE SET NULL,
  agent_id        TEXT REFERENCES agents(id) ON DELETE SET NULL,
  actor           TEXT NOT NULL DEFAULT 'system',
                    -- 'system' | 'user' | 'agent:<id>'
  detail          TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_events_ts ON events(ts);
CREATE INDEX idx_events_entity ON events(entity_type, entity_id, ts);
CREATE INDEX idx_events_project ON events(project_id, ts) WHERE project_id IS NOT NULL;
CREATE INDEX idx_events_task ON events(task_id, ts) WHERE task_id IS NOT NULL;
CREATE INDEX idx_events_agent ON events(agent_id, ts) WHERE agent_id IS NOT NULL;
```

### `settings`

```sql
CREATE TABLE settings (
  key             TEXT PRIMARY KEY,
  value           TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
```

Seed values:

| key | default | purpose |
|---|---|---|
| `model.primary` | `qwen2.5-coder:14b` | model used for all modes |
| `inference.num_ctx` | `16384` | Ollama context size |
| `inference.temperature` | `0` | determinism |
| `loop.max_attempts_default` | `3` | per task |
| `loop.idle_poll_seconds` | `5` | Hub re-checks for work |
| `agent.heartbeat_interval_sec` | `10` | agent → Hub |
| `agent.lease_timeout_sec` | `30` | how stale before Hub reclaims |
| `secret.master_key_path` | `/run/secrets/master_key` | mounted from host |

## The atomic claim query

The Hub's single most important query — guarantees no two agents grab the same task. SQLite supports `UPDATE … RETURNING` since 3.35.

```sql
UPDATE tasks
SET assigned_agent_id = :agent_id,
    status            = 'in_progress',
    started_at        = COALESCE(started_at, :now),
    lease_expires_at  = datetime(:now, '+' || :lease_seconds || ' seconds'),
    attempt_count     = attempt_count + 1
WHERE id = (
  SELECT t.id FROM tasks t
  WHERE t.status = 'ready'
    AND t.assigned_agent_id IS NULL
    -- dependencies all done
    AND NOT EXISTS (
      SELECT 1 FROM json_each(t.depends_on) AS dep
      JOIN tasks dt ON dt.id = dep.value
      WHERE dt.status != 'done'
    )
    -- no other agent already on the same branch
    AND NOT EXISTS (
      SELECT 1 FROM tasks ot
      WHERE ot.status = 'in_progress'
        AND ot.repo_id = t.repo_id
        AND ot.branch_name = t.branch_name
        AND t.branch_name IS NOT NULL
    )
  ORDER BY
    CASE t.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                    WHEN 'normal' THEN 2 ELSE 3 END,
    t.created_at ASC
  LIMIT 1
)
RETURNING *;
```

Zero rows returned ⇒ no work. One row ⇒ agent got it.

## The reaper (runs every 15s on the Hub)

```sql
-- Mark expired-lease tasks as ready again, clearing the agent ref
UPDATE tasks
SET status            = 'ready',
    assigned_agent_id = NULL,
    lease_expires_at  = NULL,
    notes             = notes || char(10)
                        || '[' || :now || '] reclaimed: agent lease expired'
WHERE status = 'in_progress'
  AND lease_expires_at < :now;

-- Mark stale agents lost
UPDATE agents
SET status = 'lost'
WHERE status IN ('connected', 'idle', 'busy')
  AND last_heartbeat_at < datetime(:now, '-30 seconds');
```

## Foreign-key cascade summary

| From | To | On Delete |
|---|---|---|
| `project_repos.project_id` | `projects.id` | CASCADE |
| `goals.project_id` | `projects.id` | CASCADE |
| `tasks.project_id` | `projects.id` | CASCADE |
| `tasks.goal_id` | `goals.id` | CASCADE |
| `tasks.parent_task_id` | `tasks.id` | CASCADE |
| `tasks.repo_id` | `project_repos.id` | SET NULL |
| `tasks.assigned_agent_id` | `agents.id` | SET NULL |
| `plans.goal_id` | `goals.id` | CASCADE |
| `questions.task_id` | `tasks.id` | CASCADE |
| `discussions.project_id` | `projects.id` | CASCADE |
| `discussions.goal_id` | `goals.id` | CASCADE |
| `discussions.task_id` | `tasks.id` | CASCADE |
| `messages.discussion_id` | `discussions.id` | CASCADE |
| `proposed_actions.discussion_id` | `discussions.id` | CASCADE |
| `secret_accesses.secret_name` | `secrets.name` | CASCADE |
| `events.*` | (various) | SET NULL |

Events keep their dangling FK after parent deletion — they're the historical record.

## What's gone vs the previous version

Removed entirely:
- `workspaces` table — workspaces are not persistent Hub-side state anymore. They live inside the Agent container as transient git clones. Source of truth = origin remote.
- `goals.workspace_id` (never made it in; removed from the design)

Added:
- `projects`, `project_repos`
- `agents`, `secrets`, `secret_accesses`
- `tasks.{repo_id, branch_name, assigned_agent_id, lease_expires_at}`
- `events.{project_id, agent_id}` denormalized for fast filtering
