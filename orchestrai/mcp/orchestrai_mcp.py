"""OrchestrAi MCP tools (shared by the stdio entry point and the hub-hosted
HTTP mount).

Lets an outside agent (e.g. Claude Code) track its work as OrchestrAi tasks so a
human can watch and manage it in the OrchestrAi UI. Projects are created in
'manual' execution mode, so the OrchestrAi worker tracks the tasks without
running them — the calling agent owns the work.

All hub access goes through a single `_api(method, path, body)` indirection. By
default that's an HTTP client (stdlib urllib), so the stdio server runs anywhere
with Python 3.10+ and only needs `mcp`. When the hub hosts these tools in-process
it calls `set_backend(...)` to dispatch straight to the route layer (no loopback
HTTP, no event-loop deadlock).
"""

import json
import os
import re
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

HUB_URL = os.environ.get("ORCHESTRAI_HUB_URL", "http://localhost:6724").rstrip("/")
TOKEN = os.environ.get("ORCHESTRAI_TOKEN")
DEFAULT_SLUG = os.environ.get("ORCHESTRAI_PROJECT_SLUG")
DEFAULT_NAME = os.environ.get("ORCHESTRAI_PROJECT_NAME")

mcp = FastMCP("orchestrai", stateless_http=True)
# Mounted at /mcp by the hub; serve the handler at the mount root so the endpoint
# is exactly /mcp (not /mcp/mcp). Ignored by the stdio transport.
mcp.settings.streamable_http_path = "/"

# slug -> project id memo. Global truth (a slug maps to one project), so it is
# safe to share across clients — there is NO per-client mutable state, which is
# what lets the same tools serve many agents over HTTP at once.
_PROJECT_CACHE: dict[str, str] = {}

# Friendly status vocabulary <-> the Hub's task status enum.
_TO_ENUM = {
    "todo": "created", "in_progress": "in_progress", "blocked": "blocked_on_human",
    "done": "done", "cancelled": "cancelled",
}
_TO_FRIENDLY = {
    "created": "todo", "ready": "todo", "in_progress": "in_progress",
    "blocked_on_dep": "blocked", "blocked_on_human": "blocked", "review": "in_progress",
    "done": "done", "failed": "failed", "cancelled": "cancelled",
}


def _http_api(method: str, path: str, body: dict | None = None):
    """Default backend: call the hub's REST API over HTTP (stdio / remote use)."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(HUB_URL + "/api" + path, data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OrchestrAi {method} {path} -> HTTP {e.code}: "
                           f"{e.read().decode('utf-8', 'replace')[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach the OrchestrAi hub at {HUB_URL} ({e.reason}). "
                           "Is it running? Set ORCHESTRAI_HUB_URL if it's elsewhere.")


# The active backend. The hub overrides this via set_backend() to avoid loopback.
_api = _http_api


def set_backend(fn) -> None:
    """Replace the hub-access backend (used by the in-process hub mount)."""
    global _api
    _api = fn


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "tracked-project"


def _friendly(t: dict, include_notes: bool = False) -> dict:
    out = {
        "id": t["id"],
        "title": t["title"],
        "status": _TO_FRIENDLY.get(t.get("status"), t.get("status")),
        "priority": t.get("priority"),
        "description": t.get("description_md", ""),
    }
    if t.get("depends_on"):
        out["depends_on"] = t["depends_on"]
    if include_notes:
        out["notes"] = t.get("notes", "")
        out["ui_url"] = f"{HUB_URL}/#/tasks/{t['id']}"
    return out


def _find_project_by_slug(slug: str) -> str | None:
    res = _api("GET", "/projects?limit=500") or {}
    for p in res.get("items", res if isinstance(res, list) else []):
        if p.get("slug") == slug:
            return p["id"]
    return None


def _resolve_project(project: str | None) -> str:
    """Map a project slug (or the env default) to its id. No per-client state —
    every call carries its own project, so one server can serve many agents."""
    slug = project or DEFAULT_SLUG
    if not slug:
        raise RuntimeError("No project given. Pass project=<slug>, or call "
                           "use_project(name) first, or set ORCHESTRAI_PROJECT_SLUG.")
    if slug in _PROJECT_CACHE:
        return _PROJECT_CACHE[slug]
    pid = _find_project_by_slug(slug)
    if not pid and slug == DEFAULT_SLUG and DEFAULT_NAME:
        pid = _api("POST", "/projects", {
            "name": DEFAULT_NAME, "slug": slug, "execution_mode": "manual",
            "description_md": "Tasks tracked for an external agent."})["id"]
    if not pid:
        raise RuntimeError(f"No OrchestrAi project '{slug}'. Call "
                           f"use_project('<name>', slug='{slug}') to create it first.")
    _PROJECT_CACHE[slug] = pid
    return pid


def _list(pid: str) -> list[dict]:
    res = _api("GET", f"/tasks?project_id={pid}&limit=500") or {}
    return [_friendly(t) for t in res.get("items", [])]


@mcp.tool()
def use_project(name: str, slug: str | None = None) -> dict:
    """Create (if needed) the OrchestrAi project to track tasks in, and return its
    `slug` — pass that slug to create_task / list_tasks. Created in 'manual'
    execution mode so the OrchestrAi worker tracks the tasks without running them
    (you own the work). Call this FIRST. Returns the project + its open tasks."""
    slug = slug or _slugify(name)
    pid = _find_project_by_slug(slug)
    if pid:
        _api("PATCH", f"/projects/{pid}", {"execution_mode": "manual"})
    else:
        pid = _api("POST", "/projects", {
            "name": name, "slug": slug, "execution_mode": "manual",
            "description_md": "Tasks tracked for an external agent."})["id"]
    _PROJECT_CACHE[slug] = pid
    tasks = _list(pid)
    return {
        "project": slug, "project_id": pid, "name": name,
        "ui_url": f"{HUB_URL}/#/projects/{pid}",
        "open_tasks": [t for t in tasks if t["status"] not in ("done", "cancelled")],
    }


@mcp.tool()
def create_task(title: str, project: str | None = None, description: str = "",
                priority: str = "normal", depends_on: list[str] | None = None) -> dict:
    """Add a task to a project (`project` = the slug from use_project; omit to use
    the configured default). Starts as 'todo'. `priority` is low|normal|high|
    critical. `depends_on` is an optional list of task ids that must finish first.
    Returns the created task including its id."""
    pid = _resolve_project(project)
    t = _api("POST", "/tasks", {
        "project_id": pid, "type": "implement", "title": title,
        "description_md": description, "priority": priority,
        "status": "created", "depends_on": depends_on or []})
    return _friendly(t)


@mcp.tool()
def list_tasks(project: str | None = None, status: str | None = None) -> list[dict]:
    """List a project's tasks (`project` = slug; omit for the configured default),
    sorted in-progress first then by priority. Optional `status` filter:
    todo|in_progress|blocked|done|cancelled. Call this to pick up changes made in
    the OrchestrAi UI — e.g. tasks a human added or reprioritized for you."""
    tasks = _list(_resolve_project(project))
    if status:
        tasks = [t for t in tasks if t["status"] == status]
    return tasks


@mcp.tool()
def update_task(task_id: str, status: str | None = None, note: str | None = None) -> dict:
    """Update a task. `status` is one of todo|in_progress|blocked|done|cancelled —
    set it to in_progress when you start and done when you finish. `note` appends
    a timestamped progress note (visible in the UI). Returns the updated task."""
    if status:
        enum = _TO_ENUM.get(status)
        if not enum:
            raise RuntimeError(f"Unknown status '{status}'. Use one of: {list(_TO_ENUM)}")
        _api("POST", f"/tasks/{task_id}/status", {"status": enum, "note_md": note})
    elif note:
        _api("POST", f"/tasks/{task_id}/notes", {"note_md": note})
    else:
        raise RuntimeError("Provide a status and/or a note to update.")
    t = _api("GET", f"/tasks/{task_id}") or {}
    return _friendly(t.get("task") or t, include_notes=True)


@mcp.tool()
def get_task(task_id: str) -> dict:
    """Full detail of one task, including its notes/history and a UI link."""
    t = _api("GET", f"/tasks/{task_id}") or {}
    return _friendly(t.get("task") or t, include_notes=True)
