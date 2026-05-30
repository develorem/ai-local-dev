"""Central tenant access control for project sub-resources.

Everything below a project (tasks, outcomes, documents, repos, previews,
scheduled, questions, plans, discussions, secrets) inherits the project's org.
These helpers enforce that a `user` principal may only touch resources in orgs
they belong to. Trusted principals — the operator/superadmin and worker agents
(the trusted executor) — bypass, so the worker keeps running across all granted
projects.

Usage in a route: add `request: Request` and call e.g.
    assert_project(request, conn, project_id)        # 404 if no access
    assert_task(request, conn, task_id)
    frag, params = project_filter(request, conn)      # for list endpoints
"""

from fastapi import HTTPException, Request

from server.auth import current_principal
from server.services.tenancy import member_role


def is_trusted(p: dict) -> bool:
    return p.get("kind") in ("operator", "agent") or bool(p.get("is_superadmin"))


def accessible_org_ids(conn, p: dict) -> list[str]:
    if is_trusted(p):
        return [r["id"] for r in conn.execute("SELECT id FROM organizations")]
    return [r["org_id"] for r in conn.execute(
        "SELECT org_id FROM org_members WHERE user_id = ?", (p.get("user_id"),))]


def accessible_project_ids(conn, p: dict) -> list[str]:
    orgs = accessible_org_ids(conn, p)
    if not orgs:
        return []
    ph = ",".join("?" * len(orgs))
    return [r["id"] for r in conn.execute(
        f"SELECT id FROM projects WHERE org_id IN ({ph})", orgs)]


def assert_project(request: Request, conn, project_id: str) -> dict:
    """Return the principal, or raise 404 if a user can't reach this project.
    404 (not 403) so we don't leak existence across tenants."""
    p = current_principal(request)
    if is_trusted(p):
        return p
    row = conn.execute("SELECT org_id FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row or not row["org_id"] or member_role(conn, p.get("user_id"), row["org_id"]) is None:
        raise HTTPException(404)
    return p


def _assert_via(request: Request, conn, table: str, id_col: str, id_val: str):
    row = conn.execute(f"SELECT project_id FROM {table} WHERE {id_col} = ?", (id_val,)).fetchone()
    if not row:
        raise HTTPException(404)
    assert_project(request, conn, row["project_id"])
    return row


def assert_task(request, conn, task_id):       return _assert_via(request, conn, "tasks", "id", task_id)
def assert_outcome(request, conn, outcome_id): return _assert_via(request, conn, "outcomes", "id", outcome_id)
def assert_repo(request, conn, repo_id):       return _assert_via(request, conn, "project_repos", "id", repo_id)
def assert_document(request, conn, doc_id):    return _assert_via(request, conn, "project_documents", "id", doc_id)
def assert_scheduled(request, conn, sid):      return _assert_via(request, conn, "scheduled_tasks", "id", sid)
def assert_preview(request, conn, pid):        return _assert_via(request, conn, "preview_servers", "id", pid)


def assert_question(request, conn, question_id):
    row = conn.execute(
        "SELECT q.id, t.project_id FROM questions q LEFT JOIN tasks t ON t.id = q.task_id "
        "WHERE q.id = ?", (question_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    if row["project_id"]:
        assert_project(request, conn, row["project_id"])
    return row


def assert_org(request: Request, conn, org_id: str, roles: set | None = None) -> dict:
    p = current_principal(request)
    if is_trusted(p):
        return p
    role = member_role(conn, p.get("user_id"), org_id)
    if role is None:
        raise HTTPException(404)
    if roles and role not in roles:
        raise HTTPException(403, detail={"error": {"code": "insufficient_role"}})
    return p


def project_filter(request: Request, conn, col: str = "project_id") -> tuple[str, list]:
    """SQL fragment + params restricting `col` to accessible projects. Empty for
    trusted principals (no restriction); '1=0' when the user has no projects."""
    p = current_principal(request)
    if is_trusted(p):
        return "", []
    ids = accessible_project_ids(conn, p)
    if not ids:
        return "1=0", []
    return f"{col} IN (" + ",".join("?" * len(ids)) + ")", ids


def acting_org_id(request: Request, conn) -> str | None:
    """The org a user is acting in: explicit ?org_id / X-Org header (validated),
    else their sole org. Trusted principals: the header/param or org_default."""
    p = current_principal(request)
    requested = request.query_params.get("org_id") or request.headers.get("x-org-id")
    if is_trusted(p):
        return requested or "org_default"
    orgs = accessible_org_ids(conn, p)
    if requested and requested in orgs:
        return requested
    return orgs[0] if len(orgs) == 1 else None
