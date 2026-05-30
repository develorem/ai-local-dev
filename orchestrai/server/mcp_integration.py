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

import contextvars
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import orchestrai_mcp
from server.auth import AUTH_ENABLED
from server.db.connection import get_db
from server.models import ProjectCreate, ProjectUpdate, TaskCreate, TaskStatusUpdate
from server.routes import projects as P
from server.routes import tasks as T
from server.util import utcnow_iso

# The agent making the current MCP request (resolved from its bearer token), or
# None for anonymous. Set per-request by the identity wrapper; read by the
# dispatch/route layer (e.g. for attribution and, later, per-project access).
current_agent: contextvars.ContextVar = contextvars.ContextVar("current_agent", default=None)


def _has_grant(conn, agent: dict, project_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM project_agents pa WHERE pa.project_id = ? "
        "AND ((pa.grantee_type = 'agent' AND pa.grantee = ?) "
        "  OR (pa.grantee_type = 'kind'  AND pa.grantee = ?))",
        (project_id, agent["id"], agent["kind"])).fetchone() is not None


def _require_access(conn, agent: dict | None, project_id: str) -> None:
    """Writes via MCP require a registered agent granted access to the project.
    (Reads stay open; the human-facing REST/UI paths don't go through here.)"""
    if agent is None:
        raise RuntimeError("This action needs a registered agent. Register one in "
                           "the OrchestrAi UI (Connect) and connect with its token.")
    if not _has_grant(conn, agent, project_id):
        raise RuntimeError(f"Agent '{agent['name']}' is not granted access to this "
                           "project. Grant it in the OrchestrAi UI (project -> Access).")


def _task_project(conn, task_id: str) -> str:
    r = conn.execute("SELECT project_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not r:
        raise RuntimeError(f"No such task: {task_id}")
    return r["project_id"]


# The route handlers expect a Request to resolve the caller's principal (added
# for tenant scoping). MCP calls them in-process, so we hand them a synthetic
# request carrying an operator principal — the route-level checks bypass, and
# MCP's own per-project grant enforcement (_require_access) stays the real gate.
def _mcp_request():
    return SimpleNamespace(
        state=SimpleNamespace(principal={"kind": "operator", "is_superadmin": True}),
        query_params={}, headers={})


def _hub_dispatch(method: str, path: str, body: dict | None = None):
    """Route an orchestrai_mcp `_api(method, path, body)` call to the in-process
    handler. Mirrors the small slice of the REST surface the tools use, and
    enforces per-project access for the calling agent (current_agent) on writes."""
    u = urlparse(path)
    p, q = u.path, parse_qs(u.query)
    parts = [seg for seg in p.split("/") if seg]  # e.g. ['tasks', '<id>', 'status']
    agent = current_agent.get()
    req = _mcp_request()
    with get_db() as conn:
        # ---- reads: open ----
        if method == "GET" and p == "/projects":
            return P.list_projects(req, status=None, limit=500, conn=conn)
        if method == "GET" and p == "/tasks":
            return T.list_tasks(req, project_id=q.get("project_id", [None])[0],
                                limit=500, conn=conn)
        if method == "GET" and len(parts) == 2 and parts[0] == "tasks":
            return T.get_task(parts[1], req, conn=conn)
        # ---- create a project (use_project): registered agent only; auto-grant it ----
        if method == "POST" and p == "/projects":
            if agent is None:
                raise RuntimeError("Connect with a registered agent's token to create "
                                   "a project (register one in the OrchestrAi UI).")
            proj = P.create_project(ProjectCreate(**body), req, conn=conn)
            conn.execute("INSERT OR IGNORE INTO project_agents (project_id, grantee_type, "
                         "grantee) VALUES (?, 'agent', ?)", (proj["id"], agent["id"]))
            conn.commit()
            return proj
        # ---- writes: require a grant on the target project ----
        if method == "POST" and p == "/tasks":
            _require_access(conn, agent, (body or {}).get("project_id"))
            return T.create_task(TaskCreate(**body), req, conn=conn)
        if method == "POST" and len(parts) == 3 and parts[0] == "tasks" and parts[2] == "status":
            _require_access(conn, agent, _task_project(conn, parts[1]))
            return T.set_task_status(parts[1], TaskStatusUpdate(**body), req, conn=conn)
        if method == "POST" and len(parts) == 3 and parts[0] == "tasks" and parts[2] == "notes":
            _require_access(conn, agent, _task_project(conn, parts[1]))
            return T.append_note(parts[1], body or {}, req, conn=conn)
        if method == "PATCH" and len(parts) == 2 and parts[0] == "projects":
            _require_access(conn, agent, parts[1])
            return P.update_project(parts[1], ProjectUpdate(**body), req, conn=conn)
    raise RuntimeError(f"MCP backend: unmapped call {method} {p}")


orchestrai_mcp.set_backend(_hub_dispatch)


def _resolve_agent(token: str | None) -> dict | None:
    """Look up the agent for a bearer token and mark it connected. Returns
    {id, name, kind} or None for an unknown/absent token (anonymous)."""
    if not token:
        return None
    with get_db() as conn:
        r = conn.execute("SELECT id, name, kind FROM agents WHERE lease_token = ?",
                         (token,)).fetchone()
        if not r:
            return None
        conn.execute("UPDATE agents SET status = 'connected', last_heartbeat_at = ? "
                     "WHERE id = ?", (utcnow_iso(), r["id"]))
        conn.commit()
        return {"id": r["id"], "name": r["name"], "kind": r["kind"]}


async def _send_401(send, msg: str) -> None:
    body = json.dumps({"error": {"code": "unauthorized", "message": msg}}).encode()
    await send({"type": "http.response.start", "status": 401,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


def _with_identity(inner):
    """ASGI wrapper: resolve the request's bearer token to a registered agent,
    mark it connected, and expose it via `current_agent` for the request. When
    auth is enabled, a valid agent token is REQUIRED — no anonymous MCP."""
    async def app(scope, receive, send):
        if scope.get("type") == "http":
            token = None
            for k, v in scope.get("headers", []):
                if k == b"authorization":
                    val = v.decode("latin-1")
                    if val.lower().startswith("bearer "):
                        token = val[7:].strip()
                    break
            agent = _resolve_agent(token)
            if AUTH_ENABLED and agent is None:
                await _send_401(send, "MCP requires a registered agent token. "
                                "Register an agent in the OrchestrAi UI (Connect) "
                                "and connect with its token.")
                return
            current_agent.set(agent)
        await inner(scope, receive, send)
    return app


mcp_server = orchestrai_mcp.mcp
# Calling this lazily creates the session manager (which the hub lifespan runs).
# We mount the manager's raw ASGI handler rather than this wrapper app: the
# wrapper has an inner route at "/", so a bare POST /mcp (no trailing slash)
# 405s. The handler ignores the sub-path, so both /mcp and /mcp/ work. Wrapped to
# identify the calling agent from its bearer token.
mcp_server.streamable_http_app()
mcp_asgi = _with_identity(mcp_server.session_manager.handle_request)
