"""Host the OrchestrAi MCP tools inside the hub over streamable HTTP.

Connecting any MCP client (e.g. Claude Code) is then just a URL — no local
script, Python, or path:
    claude mcp add --transport http orchestrai http://localhost:6724/mcp

The MCP tools (orchestrai_mcp) normally reach the hub over HTTP. Hosted here
in-process, that would mean the hub calling itself over the loopback — a sync
call from inside the event loop risks deadlock. So we inject a backend that
dispatches straight to the route functions with a real DB connection instead.
No loopback, no duplicated business logic.
"""

from urllib.parse import parse_qs, urlparse

import orchestrai_mcp
from server.db.connection import get_db
from server.models import ProjectCreate, ProjectUpdate, TaskCreate, TaskStatusUpdate
from server.routes import projects as P
from server.routes import tasks as T


def _hub_dispatch(method: str, path: str, body: dict | None = None):
    """Route an orchestrai_mcp `_api(method, path, body)` call to the in-process
    handler. Mirrors the small slice of the REST surface the tools use."""
    u = urlparse(path)
    p, q = u.path, parse_qs(u.query)
    parts = [seg for seg in p.split("/") if seg]  # e.g. ['tasks', '<id>', 'status']
    with get_db() as conn:
        if method == "GET" and p == "/projects":
            return P.list_projects(status=None, limit=500, conn=conn)
        if method == "POST" and p == "/projects":
            return P.create_project(ProjectCreate(**body), conn=conn)
        if method == "PATCH" and len(parts) == 2 and parts[0] == "projects":
            return P.update_project(parts[1], ProjectUpdate(**body), conn=conn)
        if method == "POST" and p == "/tasks":
            return T.create_task(TaskCreate(**body), conn=conn)
        if method == "GET" and p == "/tasks":
            return T.list_tasks(project_id=q.get("project_id", [None])[0],
                                limit=500, conn=conn)
        if method == "GET" and len(parts) == 2 and parts[0] == "tasks":
            return T.get_task(parts[1], conn=conn)
        if method == "POST" and len(parts) == 3 and parts[0] == "tasks" and parts[2] == "status":
            return T.set_task_status(parts[1], TaskStatusUpdate(**body), conn=conn)
        if method == "POST" and len(parts) == 3 and parts[0] == "tasks" and parts[2] == "notes":
            return T.append_note(parts[1], body or {}, conn=conn)
    raise RuntimeError(f"MCP backend: unmapped call {method} {p}")


orchestrai_mcp.set_backend(_hub_dispatch)

mcp_server = orchestrai_mcp.mcp
# Calling this lazily creates the session manager (which the hub lifespan runs).
# We mount the manager's raw ASGI handler rather than this wrapper app: the
# wrapper has an inner route at "/", so a bare POST /mcp (no trailing slash)
# 405s. The handler ignores the sub-path, so both /mcp and /mcp/ work.
mcp_server.streamable_http_app()
mcp_asgi = mcp_server.session_manager.handle_request
