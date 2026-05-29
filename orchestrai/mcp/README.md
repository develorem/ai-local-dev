# OrchestrAi MCP server

Track an AI coding agent's work as OrchestrAi tasks, and manage it from the
OrchestrAi UI — wherever the agent is running. The classic flow:

> "Hey Claude, use OrchestrAi to track your tasks for this project."

Claude calls `use_project`, then creates tasks and marks them in_progress / done
as it works. You watch and steer in the OrchestrAi UI in real time; anything you
add or reprioritize there, Claude picks up the next time it calls `list_tasks`.

Projects are created in **manual** execution mode, so the OrchestrAi worker
*tracks* these tasks but never tries to run them — the calling agent owns the work.
(The OrchestrAi worker itself is just another API client; `auto` mode is the
opt-in that lets it claim and run a project's tasks.)

## Tools

| Tool | What it does |
|------|--------------|
| `use_project(name, slug?)` | Create (if needed) the project to track tasks in; returns its `slug` (pass it to the calls below) + open tasks + a UI link. Call first. |
| `create_task(title, project?, description?, priority?, depends_on?)` | Add a task (starts as `todo`). `project` is the slug from `use_project` (omit to use the configured default). |
| `list_tasks(project?, status?)` | List a project's tasks. Call this to see human changes from the UI. |
| `update_task(task_id, status?, note?)` | Set status (`todo`/`in_progress`/`blocked`/`done`/`cancelled`) and/or append a note. (`task_id` is globally unique — no project needed.) |
| `get_task(task_id)` | Full detail incl. notes + UI link. |

The tools are **stateless** — each carries its own `project`, so one server can
serve many agents/projects at once (this is what makes hub-hosted HTTP work).
Set `ORCHESTRAI_PROJECT_SLUG` to make `project` optional for a single-project setup.

## Setup (Claude Code)

### Recommended: connect to the hub-hosted endpoint (just a URL)

The hub serves these tools over HTTP at `/mcp` — no local script, Python, or
path needed. With the hub running (default `http://localhost:6724`):

```sh
claude mcp add --transport http orchestrai http://localhost:6724/mcp
```

That's it. Any MCP-capable client can connect with the same URL.

### Alternative: run the stdio script locally

For offline use or when the hub is remote. The script's only dependency is
`mcp`; HTTP uses the stdlib.

With [uv](https://docs.astral.sh/uv/) (no venv to manage — deps are ephemeral):

```sh
claude mcp add orchestrai \
  -e ORCHESTRAI_HUB_URL=http://localhost:6724 \
  -- uv run --with mcp python /abs/path/to/orchestrai/mcp/server.py
```

Or with a venv:

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
claude mcp add orchestrai \
  -e ORCHESTRAI_HUB_URL=http://localhost:6724 \
  -- /abs/path/to/.venv/bin/python /abs/path/to/orchestrai/mcp/server.py
```

### Optional environment

| Var | Default | Purpose |
|-----|---------|---------|
| `ORCHESTRAI_HUB_URL` | `http://localhost:6724` | Hub base URL. |
| `ORCHESTRAI_PROJECT_SLUG` / `_NAME` | — | Default project, so tools work without an explicit `use_project` call. |
| `ORCHESTRAI_TOKEN` | — | Bearer token (reserved; the hub is unauthenticated today). |

## Notes

- The UI updates live (WebSocket), so task changes appear the instant the agent
  makes them.
- Control is pull-based for the agent: UI edits (new tasks, reprioritization)
  are seen the next time the agent calls `list_tasks` — so prompt your agent to
  check its task list between steps.
