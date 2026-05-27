# OrchestrAi — API

REST + WebSocket interface served by the orchestrator on `:8080`. UI consumes it directly. Anyone could in principle write an alternative UI or a CLI against the same surface.

## Conventions

- Base path: `/api/` (e.g. `/api/goals`)
- Content type: `application/json` (UTF-8)
- All IDs are ULIDs (26 chars, sortable strings)
- All timestamps are ISO-8601 UTC strings
- All bodies use `snake_case`
- Status codes are standard: 2xx success, 4xx client error, 5xx server error
- Error envelope:
  ```json
  {"error": {"code": "task_not_found", "message": "no task with id 01H...", "details": {}}}
  ```
- Listing endpoints support `?limit=` (default 50, max 500) and `?cursor=` (opaque, server-set). Responses include `{items: [...], next_cursor: "..."}`.
- No auth in v1 — binds to `localhost` only. A reverse-proxy or auth shim could front it later.

## Resource map

```
GET    /api/health                          → service liveness
GET    /api/settings                        → all settings as key/value object
PATCH  /api/settings                        → update one or more settings

POST   /api/goals                           → submit a new goal
GET    /api/goals                           → list goals
GET    /api/goals/{id}                      → goal detail
PATCH  /api/goals/{id}                      → edit title/description/priority
POST   /api/goals/{id}/abandon              → cancel goal + all open children

GET    /api/tasks                           → list tasks (filterable)
GET    /api/tasks/{id}                      → task detail
PATCH  /api/tasks/{id}                      → edit a task (only allowed in some states)
POST   /api/tasks/{id}/cancel               → cancel a task (cascades to children)
POST   /api/tasks/{id}/retry                → reset failed→ready, optionally edit
POST   /api/tasks/{id}/notes                → append a note to a task

GET    /api/plans/{id}                      → plan detail (markdown + outline)
GET    /api/goals/{id}/plans                → all plan versions for a goal

GET    /api/questions                       → list questions (filter by status)
GET    /api/questions/{id}                  → question detail
POST   /api/questions/{id}/answer           → answer a pending question

POST   /api/discussions                     → start a new discussion thread
GET    /api/discussions                     → list discussions
GET    /api/discussions/{id}                → discussion detail with all messages
POST   /api/discussions/{id}/messages       → post a user message (triggers agent reply)
POST   /api/discussions/{id}/close          → close the thread

GET    /api/proposed-actions                → list pending proposed actions
POST   /api/proposed-actions/{id}/apply     → apply a proposed action
POST   /api/proposed-actions/{id}/reject    → dismiss it

GET    /api/events                          → query historical events
WS     /api/events                          → live event stream (broadcasts)
```

---

## Goals

### `POST /api/goals`

Submit a high-level goal. Creates a `goal` row in `submitted` status and a `plan` task in `ready` status.

Request:
```json
{
  "title": "Add user authentication",
  "description_md": "Add email/password signup + login...",
  "priority": "normal"
}
```

Response `201`:
```json
{
  "goal": {
    "id": "01H...",
    "title": "...",
    "description_md": "...",
    "status": "submitted",
    "priority": "normal",
    "created_at": "2026-05-27T...",
    "updated_at": "2026-05-27T..."
  },
  "plan_task_id": "01H..."
}
```

Events emitted: `goal.created`, `task.created`.

### `GET /api/goals`

Query params: `status` (csv), `priority` (csv), `limit`, `cursor`.

Response `200`:
```json
{
  "items": [
    {
      "id": "01H...",
      "title": "...",
      "status": "active",
      "priority": "normal",
      "task_counts": {"ready": 3, "in_progress": 1, "done": 5, "blocked_on_human": 1},
      "open_question_count": 1,
      "created_at": "...",
      "updated_at": "..."
    }
  ],
  "next_cursor": null
}
```

### `GET /api/goals/{id}`

Response `200` includes the goal + a summary of its plan(s) and tasks:
```json
{
  "goal": { ... },
  "plans": [{"id": "01H...", "version": 1, "status": "approved"}],
  "tasks": [{"id": "01H...", "title": "...", "status": "done", "type": "implement"}, ...],
  "discussions": [{"id": "01H...", "title": "...", "status": "open"}]
}
```

### `POST /api/goals/{id}/abandon`

Request body: optional `{ "reason": "..." }`.

Effect: goal → `abandoned`. All non-terminal child tasks → `cancelled`. All pending questions on those tasks → `dismissed`. Open discussions linked to the goal → `closed`.

Response `200`: `{ "ok": true, "tasks_cancelled": 3, "questions_dismissed": 1 }`.

---

## Tasks

### `GET /api/tasks`

Query: `goal_id`, `status` (csv), `type` (csv), `priority` (csv), `limit`, `cursor`.

```json
{
  "items": [
    {
      "id": "01H...",
      "goal_id": "01H...",
      "type": "implement",
      "title": "...",
      "status": "ready",
      "priority": "normal",
      "depends_on": ["01H..."],
      "attempt_count": 0,
      "created_at": "...",
      "started_at": null,
      "finished_at": null
    }
  ],
  "next_cursor": "..."
}
```

### `GET /api/tasks/{id}`

Includes full payload, result, notes, history (event roll-up), and open questions:
```json
{
  "task": {
    "id": "01H...",
    "goal_id": "...",
    "parent_task_id": null,
    "type": "implement",
    "title": "...",
    "description_md": "...",
    "status": "blocked_on_human",
    "priority": "normal",
    "depends_on": [],
    "acceptance_criteria": [...],
    "attempt_count": 1,
    "max_attempts": 3,
    "payload": {...},
    "result": null,
    "error": null,
    "notes": "...",
    "created_at": "...",
    "started_at": "...",
    "finished_at": null
  },
  "questions": [{"id": "01H...", "kind": "clarification", "prompt_md": "...", ...}],
  "history": [
    {"id": "...", "ts": "...", "kind": "task.created", "actor": "user", "detail": {}},
    {"id": "...", "ts": "...", "kind": "task.status_changed", "actor": "system",
     "detail": {"from": "ready", "to": "in_progress"}},
    ...
  ],
  "children": [{"id": "01H...", "title": "...", "status": "ready"}, ...]
}
```

### `PATCH /api/tasks/{id}`

Allowed fields and the states in which each may be edited:
- `title`, `description_md`, `priority`, `acceptance_criteria` — editable in any non-terminal state
- `depends_on` — editable only in `created`, `ready`, `blocked_on_dep`, `failed`, `blocked_on_human`
- `max_attempts` — editable any time
- Anything else: returns `409 conflict` with `error.code = "field_not_editable_in_status"`

### `POST /api/tasks/{id}/cancel`

Transitions task → `cancelled`. Cascades to all descendants. Pending questions → `dismissed`.

### `POST /api/tasks/{id}/retry`

Only valid in `failed` state. Optional body to edit fields before retrying:
```json
{ "reset_attempts": true, "edits": {"description_md": "..."} }
```
Transitions back to `ready`.

### `POST /api/tasks/{id}/notes`

Append a human note onto the task. Useful for "I noticed X — keep this in mind."

```json
{ "note_md": "remember to use PostgreSQL JSONB not JSON" }
```

---

## Plans

### `GET /api/plans/{id}`

```json
{
  "plan": {
    "id": "01H...",
    "goal_id": "01H...",
    "version": 1,
    "content_md": "...full markdown plan...",
    "task_outline": [
      {
        "title": "scaffold FastAPI app",
        "type": "implement",
        "depends_on_titles": [],
        "acceptance_criteria": [...]
      },
      ...
    ],
    "status": "approved",
    "approval_question_id": "01H...",
    "created_at": "...",
    "approved_at": "...",
    "approval_notes": "Approve, but skip the deploy task for now"
  }
}
```

### `GET /api/goals/{id}/plans`

All versions for a goal — useful when a revise task supersedes an earlier plan.

---

## Questions

### `GET /api/questions`

Query: `status` (default `pending`), `task_id`, `kind`.

```json
{
  "items": [
    {
      "id": "01H...",
      "task_id": "01H...",
      "kind": "clarification",
      "prompt_md": "...",
      "options": [{"label": "snake_case", "value": "snake_case"}, ...],
      "status": "pending",
      "created_at": "...",
      "task_title": "Add endpoint /users",
      "goal_title": "Add user auth"
    }
  ],
  "next_cursor": null
}
```

### `POST /api/questions/{id}/answer`

```json
{
  "answer_md": "use snake_case for new endpoints",
  "answer_value": "snake_case"     // only for choice/confirm kinds
}
```

Effect: question → `answered`. If this was the last open question on its task, task transitions back to `ready` and is picked up on the next worker cycle. If kind is `plan_approval`:
- `answer_value="approve"` → plan → `approved`, plan's `task_outline` is instantiated into real tasks, goal → `active`
- `answer_value="approve_with_edits"` → enqueues a `revise` task with `answer_md` as the edit request, plan stays `draft` until revised
- `answer_value="reject"` → plan → `rejected`, goal → `rejected`
- `answer_value="discuss"` → opens a new discussion linked to the goal, plan stays `draft`

---

## Discussions

### `POST /api/discussions`

Start a new discussion thread.

```json
{
  "title": "Should we use Redis for caching?",
  "goal_id": "01H...",       // optional
  "task_id": "01H...",       // optional; mutually exclusive with goal_id
  "initial_message_md": "Optional first user message"
}
```

If `initial_message_md` is present, the discussion is created with that message AND a `discuss` task is enqueued at `priority=critical`.

### `POST /api/discussions/{id}/messages`

Send a user message in an existing discussion.

```json
{ "content_md": "..." }
```

Effect: message inserted. If no `discuss` task is currently in-flight for this discussion, enqueue one at `priority=critical`. If one is already pending or in-progress, append the new message and let the existing task handle it.

### `GET /api/discussions/{id}`

```json
{
  "discussion": {
    "id": "01H...",
    "title": "...",
    "goal_id": "01H...",
    "task_id": null,
    "status": "open",
    "created_at": "..."
  },
  "messages": [
    {"id": "...", "role": "user", "content_md": "...", "created_at": "..."},
    {"id": "...", "role": "agent", "content_md": "...", "created_at": "..."}
  ],
  "proposed_actions": [
    {
      "id": "01H...",
      "action_type": "modify_task",
      "human_summary": "Switch task-014 from in-memory cache to Redis",
      "status": "proposed",
      "payload": {"task_id": "01H...", "changes": {"description_md": "..."}},
      "created_at": "..."
    }
  ]
}
```

---

## Proposed actions

### `POST /api/proposed-actions/{id}/apply`

Applies the proposed mutation to the task graph atomically. Validates that the target entities still exist and the operation is valid in their current state (e.g. cannot `cancel_task` a task already `done`).

Response `200`:
```json
{
  "ok": true,
  "applied_at": "...",
  "side_effects": [
    {"kind": "task.created", "id": "01H..."},
    {"kind": "task.modified", "id": "01H..."}
  ]
}
```

### `POST /api/proposed-actions/{id}/reject`

Mark as `rejected`. No graph changes.

---

## Events (history)

### `GET /api/events`

Query: `since` (ISO timestamp or event ID), `kind` (csv with prefix support, e.g. `task.,question.`), `goal_id`, `task_id`, `limit`, `cursor`.

```json
{
  "items": [
    {
      "id": "01H...",
      "ts": "...",
      "kind": "task.status_changed",
      "entity_type": "task",
      "entity_id": "01H...",
      "goal_id": "01H...",
      "task_id": "01H...",
      "actor": "system",
      "detail": {"from": "ready", "to": "in_progress"}
    }
  ],
  "next_cursor": "..."
}
```

### `WS /api/events` (WebSocket)

Real-time event stream. Push-only — the UI cannot send commands back through the WS.

#### Connection

Client connects to `ws://localhost:8080/api/events`. Optionally with `?since=<event-id>` to replay any events since that ID (server buffers ~1000 recent events for this; older events must be paged via the REST endpoint).

#### Server messages

Each frame is a JSON event with the same shape as the REST `events` row:

```json
{"id": "01H...", "ts": "...", "kind": "task.status_changed", ...}
```

Plus occasional control frames:

```json
{"type": "control", "kind": "heartbeat", "ts": "..."}
{"type": "control", "kind": "buffer_overflow", "missed_since": "01H...", "ts": "..."}
```

`buffer_overflow` means the server dropped events because the client wasn't keeping up. Client should re-fetch the last N events via REST.

## Event kinds

Stable list, hierarchical dotted names. UI subscribes by prefix.

| Kind | Emitted when |
|---|---|
| `goal.created` | new goal inserted |
| `goal.updated` | goal field edited |
| `goal.status_changed` | goal status transitioned |
| `goal.abandoned` | user abandoned a goal |
| `task.created` | new task inserted |
| `task.updated` | task field edited (excluding status) |
| `task.status_changed` | task status transitioned |
| `task.started` | task moved to in_progress |
| `task.completed` | task moved to done |
| `task.failed` | task hit max_attempts |
| `task.cancelled` | task cancelled |
| `task.notes_appended` | note added |
| `question.opened` | new question, task → blocked_on_human |
| `question.answered` | user answered |
| `question.dismissed` | task cancelled before answer |
| `plan.created` | new plan version drafted |
| `plan.approved` | plan approved, tasks created |
| `plan.rejected` | plan rejected |
| `plan.superseded` | replaced by a newer version |
| `discussion.created` | new thread |
| `discussion.message` | new message in a thread (role in detail) |
| `discussion.closed` | thread closed |
| `proposed_action.added` | agent suggested a graph change |
| `proposed_action.applied` | user applied it |
| `proposed_action.rejected` | user rejected it |
| `worker.idle` | worker has no eligible tasks |
| `worker.busy` | worker picked up a task |
| `worker.error` | worker loop caught an unhandled exception |
| `settings.updated` | runtime setting changed |

Detail payloads are documented inline in `server/events.py` (single source of truth) and tested.

## Health

### `GET /api/health`

```json
{
  "status": "ok",
  "version": "0.1.0",
  "ollama": {"reachable": true, "host": "http://ollama:11434"},
  "db": {"schema_version": 3, "ok": true},
  "worker": {"running": true, "last_picked_at": "...", "current_task_id": "01H..."}
}
```

503 if any subsystem fails.

## Settings

### `GET /api/settings`

```json
{ "model.primary": "qwen2.5-coder:14b", "inference.num_ctx": "16384", ... }
```

### `PATCH /api/settings`

```json
{ "model.primary": "deepseek-coder-v2:16b", "inference.num_ctx": "8192" }
```

Effect: settings updated, applied at next LLM call. Some changes (e.g. swapping the model) take effect immediately; no restart needed.

## OpenAPI

The FastAPI server auto-generates `GET /openapi.json` and `/docs` (Swagger UI). These are the live spec — this doc is the design intent.
