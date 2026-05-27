# OrchestrAi — API

REST + WebSocket served by the **Hub** on `:6724`. Two clients consume it:
- **Browser UI** (anyone on `localhost`)
- **Agents** (any number, talk over the compose network)

## Conventions

- Base path: `/api/`
- JSON bodies (`application/json`, UTF-8)
- IDs are ULIDs (26-char strings)
- Timestamps are ISO-8601 UTC strings
- `snake_case` everywhere
- Listing endpoints: `?limit=` (default 50, max 500), `?cursor=`; returns `{items, next_cursor}`
- Error envelope: `{"error": {"code": "...", "message": "...", "details": {}}}`
- No auth in v1 — Hub binds to `localhost`. **Agents authenticate with the `lease_token`** issued on register; sent as `Authorization: Bearer <token>`.

## Resource map

```
GET    /api/health
GET    /api/settings
PATCH  /api/settings

POST   /api/projects
GET    /api/projects
GET    /api/projects/{id}
PATCH  /api/projects/{id}
POST   /api/projects/{id}/archive

POST   /api/projects/{id}/repos
GET    /api/projects/{id}/repos
GET    /api/repos/{id}
PATCH  /api/repos/{id}
DELETE /api/repos/{id}

POST   /api/goals
GET    /api/goals
GET    /api/goals/{id}
PATCH  /api/goals/{id}
POST   /api/goals/{id}/abandon

GET    /api/tasks
GET    /api/tasks/{id}
PATCH  /api/tasks/{id}
POST   /api/tasks/{id}/cancel
POST   /api/tasks/{id}/retry
POST   /api/tasks/{id}/notes

GET    /api/plans/{id}
GET    /api/goals/{id}/plans

GET    /api/questions
GET    /api/questions/{id}
POST   /api/questions/{id}/answer

POST   /api/discussions
GET    /api/discussions
GET    /api/discussions/{id}
POST   /api/discussions/{id}/messages
POST   /api/discussions/{id}/close

GET    /api/proposed-actions
POST   /api/proposed-actions/{id}/apply
POST   /api/proposed-actions/{id}/reject

GET    /api/events
WS     /api/events

# Agent endpoints (auth: Bearer lease_token from /register)
POST   /api/agents/register
POST   /api/agents/{id}/heartbeat
POST   /api/agents/{id}/claim
POST   /api/agents/{id}/release
GET    /api/agents
GET    /api/agents/{id}

# Agent-only convenience endpoints
POST   /api/tasks/{id}/events           # agent reports progress mid-task
POST   /api/tasks/{id}/result           # agent submits final result
GET    /api/projects/{id}/context       # agent fetches project context block
GET    /api/repos/{id}                  # agent fetches repo metadata

# Secrets (agent-only fetch; UI uses other endpoints to manage)
GET    /api/secrets                     # UI: list (NEVER returns values)
POST   /api/secrets                     # UI: create
PATCH  /api/secrets/{name}              # UI: update value or metadata
DELETE /api/secrets/{name}              # UI: delete
GET    /api/secrets/{name}/accesses     # UI: audit log
GET    /api/secrets/{name}/value        # AGENT-ONLY: fetch decrypted value
                                        # requires task lease referencing this secret
```

---

## Projects

### `POST /api/projects`

```json
{
  "name": "Locate2u Microservices",
  "slug": "locate2u-microservices",
  "description_md": "...",
  "context_md": "..."
}
```

`context_md` is the agent-facing project description. Keep it terse — see `PROMPTS.md` "Token-efficient context".

### `GET /api/projects/{id}`

```json
{
  "project": { ... },
  "repos": [ { "id": "...", "name": "api-gateway", "role": "service", ... } ],
  "stats": {
    "goals_active": 2, "goals_done": 5,
    "tasks_ready": 3, "tasks_in_progress": 1, "tasks_blocked_on_human": 1,
    "open_questions": 1,
    "open_discussions": 2
  }
}
```

### `POST /api/projects/{id}/archive`

Soft-archives — keeps all data but hides from default listings.

## Repos

### `POST /api/projects/{id}/repos`

```json
{
  "name": "api-gateway",
  "role": "service",
  "url": "git@github.com:org/api-gateway.git",
  "default_branch": "main",
  "description_md": "Routing + auth middleware"
}
```

### `GET /api/repos/{id}`

```json
{
  "repo": { ... },
  "active_branches": ["feature/auth-X", "feature/billing-Y"],
                       // branches currently held by in-progress tasks
  "recent_tasks": [...]
}
```

## Goals

(Same shape as the original spec, but now `project_id` is required on submit and returned everywhere.)

### `POST /api/goals`

```json
{
  "project_id": "01H...",
  "title": "Add user authentication",
  "description_md": "...",
  "priority": "normal"
}
```

Response includes the auto-created plan task ID.

## Tasks

The schema gained `project_id`, `repo_id`, `branch_name`, `assigned_agent_id`, `lease_expires_at`. The API surface mirrors this. `GET /api/tasks/{id}` now includes the agent currently holding the task (if any) and its branch.

### `GET /api/tasks` — filtering

Query params: `project_id`, `goal_id`, `repo_id`, `branch_name`, `status`, `type`, `priority`, `assigned_agent_id`, `limit`, `cursor`.

### `GET /api/tasks/{id}`

```json
{
  "task": {
    "id": "01H...", "project_id": "01H...", "goal_id": "01H...",
    "repo_id": "01H...", "branch_name": "feature/auth",
    "type": "implement",
    "status": "in_progress",
    "assigned_agent_id": "01H...",
    "lease_expires_at": "2026-05-27T...",
    "attempt_count": 1, "max_attempts": 3,
    ...
  },
  "agent": {
    "id": "01H...", "name": "agent@steven-desktop",
    "status": "busy", "last_heartbeat_at": "..."
  },
  "questions": [ ... ],
  "history": [ ... ],
  "children": [ ... ]
}
```

## Agents

### `POST /api/agents/register`

Called by an agent process when it starts up. **No auth required** for this single endpoint.

```json
{
  "name": "agent@steven-desktop",
  "host": "steven-desktop",
  "version": "0.1.0",
  "capabilities": ["gpu", "docker-cli", "linux", "node", "python"]
}
```

Response:
```json
{
  "agent_id": "01H...",
  "lease_token": "<opaque 64-char random>",
  "hub_version": "0.1.0",
  "heartbeat_interval_sec": 10,
  "lease_timeout_sec": 30
}
```

All subsequent agent calls send `Authorization: Bearer <lease_token>`.

### `POST /api/agents/{id}/heartbeat`

```json
{ "current_task_id": "01H..." }   // optional; null if idle
```

Effect:
- `agents.last_heartbeat_at = now`
- If `current_task_id` is set: extend `tasks.lease_expires_at` to `now + lease_timeout_sec`
- If the agent's lease_token has been invalidated (e.g. Hub restarted with new state) → 401, agent must re-register

### `POST /api/agents/{id}/claim`

Run the atomic claim query. Returns either a task envelope or `204 No Content`.

```json
{
  "task": { ... full task row ... },
  "project": {
    "id": "01H...", "name": "...", "context_md": "..."
  },
  "repo": {
    "id": "01H...", "url": "...", "default_branch": "main", "name": "..."
  },
  "branch_name": "feature/auth",
  "lease_expires_at": "2026-05-27T..."
}
```

The single response bundles everything the agent needs to start work without further round-trips. Project context comes inline. Secrets do NOT — agent must fetch those on demand and only when needed.

### `POST /api/agents/{id}/release`

Graceful shutdown. Body:

```json
{ "release_task": true }   // re-queue current task; default true
```

If `release_task=true` and the agent currently holds a task, that task transitions back to `ready` with a `released_by_agent` note. If false, the task is left in `in_progress` and will be reclaimed by the reaper after lease expiry.

### `GET /api/agents`

UI consumes. Returns list with current status and current task ID.

```json
{
  "items": [
    {
      "id": "01H...", "name": "agent@steven-desktop", "host": "steven-desktop",
      "status": "busy", "last_heartbeat_at": "...",
      "current_task_id": "01H...",
      "registered_at": "..."
    }
  ]
}
```

## Agent task reporting

### `POST /api/tasks/{id}/events`

Progress events from the running agent. Hub broadcasts via WebSocket so the UI can show "agent is reading file X", "agent ran tests, exit 0", etc.

```json
{
  "kind": "task.progress",
  "detail": {
    "step": "implementer_pass_2",
    "summary": "Produced 47-line diff to src/routes/health.py",
    "stats": {"prompt_tokens": 3421, "completion_tokens": 612, "gen_tps": 79.4}
  }
}
```

Free-form `kind` (must start with `task.` namespace). Common ones documented inline in code.

### `POST /api/tasks/{id}/result`

Final result of the task. Marks the task as transitioning to `review` (then deterministic + LLM Reviewer runs Hub-side or in the same Agent) or directly to `done` / `failed` depending on the task type.

```json
{
  "outcome": "success" | "fix_needed" | "failed" | "needs_human",
  "result": {
    "diff": "<unified diff>",
    "commands_run": [
      {"cmd": "pytest tests/test_health.py", "exit": 0, "stdout": "...", "stderr": ""}
    ],
    "branch_pushed": "feature/auth",
    "commits": ["abc123: add /health endpoint"]
  },
  "questions": [],
  "notes_md": "..."
}
```

## Context fetch

### `GET /api/projects/{id}/context`

Agent fetches the project context block when it starts a task in that project.

```json
{
  "project_id": "01H...",
  "name": "...",
  "description_md": "...",
  "context_md": "...",
  "repos": [
    {"name": "api-gateway", "role": "service", "description_md": "..."}
  ]
}
```

Bundled, single response, designed to fit in a single LLM context window. Per `PROMPTS.md`, the agent puts `context_md` and the repo list directly into the LLM system prompt.

## Secrets

### `GET /api/secrets` (UI)

Lists secret NAMES and metadata. Never values.

```json
{
  "items": [
    {
      "name": "GITHUB_TOKEN",
      "description": "GitHub access for cloning + PR comments",
      "scope": "global",
      "created_at": "...", "updated_at": "...",
      "last_accessed_at": "...", "access_count": 17
    }
  ]
}
```

### `POST /api/secrets` (UI)

```json
{
  "name": "GITHUB_TOKEN",
  "value": "<plaintext — encrypted on write>",
  "description": "...",
  "scope": "global"
}
```

Value field is write-only. It's never returned to the UI again.

### `GET /api/secrets/{name}/value` (AGENT ONLY)

Returns the decrypted value. Requires:
- Valid Bearer lease_token
- The agent holds an in-progress task whose payload or implementer prompt declares it needs this secret (Hub validates against task metadata)

```json
{
  "name": "GITHUB_TOKEN",
  "value": "<plaintext>",
  "expires_in_sec": 60         // value should be discarded after use
}
```

Every call logs to `secret_accesses` (issued | denied + reason).

### `GET /api/secrets/{name}/accesses` (UI)

Audit log for one secret. Returns last N issues with timestamps, agent IDs, task IDs.

## Events

### `GET /api/events` and `WS /api/events`

Same as before. Event kinds now include `agent.*`:

- `agent.registered`
- `agent.heartbeat_missed`
- `agent.lost`
- `agent.released`
- `task.claimed` (detail: agent_id)
- `task.lease_extended`
- `task.lease_expired_reclaimed`
- `secret.accessed`
- `secret.created` / `secret.updated` / `secret.deleted`
- `project.created` / `project.updated`
- `repo.created`

## Health

### `GET /api/health`

```json
{
  "status": "ok",
  "version": "0.1.0",
  "ollama": {"reachable": true, "host": "http://ollama:11434"},
  "db": {"schema_version": <N>, "ok": true},
  "agents": {
    "registered": 1,
    "connected": 1,
    "busy": 1,
    "lost": 0
  }
}
```
