"""Project repos CRUD + the agent clone-info endpoint."""

import json
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from server.db.connection import db_dep
from server.events import emit
from server.models import Repo, RepoCreate, RepoUpdate
from server.services import access, doc_index
from server.services.crypto import decrypt
from server.util import new_id, utcnow_iso

router = APIRouter(tags=["repos"])


def _agent_with_task_in_project(authorization: Optional[str], project_id: str, conn):
    """Authorise an agent lease token that holds an in-progress task in this
    project (same contract as clone-info). Returns the agent row."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, detail={"error": {"code": "missing_token"}})
    tok = authorization.split(None, 1)[1].strip()
    agent = conn.execute("SELECT * FROM agents WHERE lease_token = ?", (tok,)).fetchone()
    if not agent:
        raise HTTPException(401, detail={"error": {"code": "invalid_lease"}})
    ctid = agent["current_task_id"]
    task = conn.execute("SELECT project_id, status FROM tasks WHERE id = ?",
                        (ctid,)).fetchone() if ctid else None
    if not task or task["status"] != "in_progress" or task["project_id"] != project_id:
        raise HTTPException(403, detail={"error": {"code": "no_matching_task",
                            "message": "need an in-progress task in this project"}})
    return agent


def _row_to_repo(row) -> dict:
    return {
        "id": row["id"], "project_id": row["project_id"], "name": row["name"],
        "role": row["role"], "url": row["url"],
        "default_branch": row["default_branch"],
        "description_md": row["description_md"],
        "auth_secret_name": (row["auth_secret_name"]
                             if "auth_secret_name" in row.keys() else None),
        "start_command": (row["start_command"]
                          if "start_command" in row.keys() else None),
        "created_at": row["created_at"],
    }


@router.post("/projects/{project_id}/repos", response_model=Repo, status_code=201)
def create_repo(project_id: str, body: RepoCreate, request: Request, conn=Depends(db_dep)):
    proj = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not proj:
        raise HTTPException(404, detail={"error": {"code": "project_not_found"}})
    access.assert_project(request, conn, project_id)

    rid = new_id()
    now = utcnow_iso()
    try:
        conn.execute(
            """
            INSERT INTO project_repos (id, project_id, name, role, url,
                                       default_branch, description_md,
                                       auth_secret_name, start_command, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (rid, project_id, body.name, body.role, body.url,
             body.default_branch, body.description_md, body.auth_secret_name,
             body.start_command, now),
        )
        emit(conn, "repo.created", "repo", rid,
             project_id=project_id, actor="user",
             detail={"name": body.name, "url": body.url})
        conn.commit()
    except Exception as e:
        conn.rollback()
        if "UNIQUE" in str(e):
            raise HTTPException(409, detail={"error": {"code": "repo_name_taken"}})
        raise

    row = conn.execute("SELECT * FROM project_repos WHERE id = ?", (rid,)).fetchone()
    return _row_to_repo(row)


@router.get("/projects/{project_id}/repos")
def list_repos(project_id: str, request: Request, conn=Depends(db_dep)):
    access.assert_project(request, conn, project_id)
    rows = conn.execute(
        "SELECT * FROM project_repos WHERE project_id = ? ORDER BY name",
        (project_id,),
    ).fetchall()
    return {"items": [_row_to_repo(r) for r in rows]}


@router.get("/repos/{repo_id}")
def get_repo(repo_id: str, request: Request, conn=Depends(db_dep)):
    access.assert_repo(request, conn, repo_id)
    row = conn.execute("SELECT * FROM project_repos WHERE id = ?", (repo_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    active_branches = [r["branch_name"] for r in conn.execute(
        """
        SELECT DISTINCT branch_name FROM tasks
        WHERE repo_id = ? AND status = 'in_progress' AND branch_name IS NOT NULL
        """,
        (repo_id,),
    ).fetchall()]
    recent_tasks = [dict(r) for r in conn.execute(
        """
        SELECT id, title, type, status, branch_name, created_at, finished_at
        FROM tasks WHERE repo_id = ?
        ORDER BY created_at DESC LIMIT 20
        """,
        (repo_id,),
    ).fetchall()]
    return {"repo": _row_to_repo(row),
            "active_branches": active_branches,
            "recent_tasks": recent_tasks}


@router.patch("/repos/{repo_id}", response_model=Repo)
def update_repo(repo_id: str, body: RepoUpdate, request: Request, conn=Depends(db_dep)):
    access.assert_repo(request, conn, repo_id)
    row = conn.execute("SELECT * FROM project_repos WHERE id = ?", (repo_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    fields, params = [], []
    for f in ("name", "role", "url", "default_branch", "description_md",
              "auth_secret_name", "start_command"):
        v = getattr(body, f)
        if v is not None:
            fields.append(f"{f} = ?")
            params.append(v)
    if not fields:
        return _row_to_repo(row)
    params.append(repo_id)
    conn.execute(f"UPDATE project_repos SET {', '.join(fields)} WHERE id = ?", params)
    emit(conn, "repo.updated", "repo", repo_id,
         project_id=row["project_id"], actor="user", detail={})
    conn.commit()
    row = conn.execute("SELECT * FROM project_repos WHERE id = ?", (repo_id,)).fetchone()
    return _row_to_repo(row)


@router.get("/repos/{repo_id}/clone-info")
def clone_info(repo_id: str, authorization: Optional[str] = Header(default=None),
               conn=Depends(db_dep)):
    """Agent-only. Returns what the worker needs to clone this repo into its
    workspace: url, default_branch, and the decrypted auth token (if the repo
    has an auth_secret_name and its scope allows this project). Authorised by a
    valid lease token whose agent holds an in-progress task in the repo's
    project; token issuance is audited like any other secret access."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, detail={"error": {"code": "missing_token"}})
    tok = authorization.split(None, 1)[1].strip()
    agent = conn.execute("SELECT * FROM agents WHERE lease_token = ?", (tok,)).fetchone()
    if not agent:
        raise HTTPException(401, detail={"error": {"code": "invalid_lease"}})
    repo = conn.execute("SELECT * FROM project_repos WHERE id = ?", (repo_id,)).fetchone()
    if not repo:
        raise HTTPException(404)
    ctid = agent["current_task_id"]
    task = conn.execute("SELECT project_id, status FROM tasks WHERE id = ?",
                        (ctid,)).fetchone() if ctid else None
    if not task or task["status"] != "in_progress" or task["project_id"] != repo["project_id"]:
        raise HTTPException(403, detail={"error": {"code": "no_matching_task",
                            "message": "need an in-progress task in this repo's project"}})
    token_val = None
    sname = repo["auth_secret_name"] if "auth_secret_name" in repo.keys() else None
    if sname:
        srow = conn.execute("SELECT ciphertext, scope FROM secrets WHERE name = ?",
                            (sname,)).fetchone()
        if srow and (srow["scope"] == "global" or srow["scope"] == f"project:{repo['project_id']}"):
            try:
                token_val = decrypt(srow["ciphertext"])
            except Exception:
                token_val = None
            conn.execute(
                "INSERT INTO secret_accesses (id, secret_name, agent_id, task_id, ts, result, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_id(), sname, agent["id"], ctid, utcnow_iso(),
                 "issued" if token_val else "denied", "git_clone"))
            conn.commit()
    return {"url": repo["url"], "default_branch": repo["default_branch"], "token": token_val}


@router.post("/projects/{project_id}/repo-docs/reconcile")
def reconcile_repo_docs(project_id: str, body: dict,
                        authorization: Optional[str] = Header(default=None),
                        conn=Depends(db_dep)):
    """Agent-only. Reconcile the document index against the repo's reference
    docs. The worker sends a manifest ({repo_path, title, headings, excerpt})
    scanned from its checked-out workspace; we upsert repo-sourced
    project_documents by (project, repo, path), drop ones that disappeared
    upstream, and enqueue a reindex when content changed. Repo doc CONTENT is
    not stored here (only an excerpt for the purpose line) — the full body is
    read from the workspace on demand."""
    _agent_with_task_in_project(authorization, project_id, conn)
    repo_id = (body or {}).get("repo_id")
    if not repo_id:
        raise HTTPException(400, detail={"error": {"code": "repo_id_required"}})
    docs = (body or {}).get("docs") or []
    now = utcnow_iso()

    seen: set[str] = set()
    indexed = 0
    for d in docs:
        if not isinstance(d, dict):
            continue
        rp = (d.get("repo_path") or "").strip()
        if not rp:
            continue
        seen.add(rp)
        title = (d.get("title") or rp).strip()[:200]
        headings = json.dumps(d.get("headings") or [])
        excerpt = (d.get("excerpt") or "")[:4000]
        existing = conn.execute(
            "SELECT id, title, content_md, headings FROM project_documents "
            "WHERE project_id = ? AND repo_id = ? AND repo_path = ? AND source = 'repo'",
            (project_id, repo_id, rp)).fetchone()
        if existing:
            # Only write (and thus risk re-indexing) when something changed.
            if (existing["title"] == title and existing["content_md"] == excerpt
                    and existing["headings"] == headings):
                continue
            conn.execute(
                "UPDATE project_documents SET title = ?, headings = ?, "
                "content_md = ?, updated_at = ? WHERE id = ?",
                (title, headings, excerpt, now, existing["id"]))
            indexed += 1
        else:
            conn.execute(
                "INSERT INTO project_documents (id, project_id, title, content_md, "
                "source, repo_id, repo_path, headings, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'repo', ?, ?, ?, ?, ?)",
                (new_id(), project_id, title, excerpt, repo_id, rp, headings, now, now))
            indexed += 1

    # Drop repo docs that no longer exist upstream.
    removed = 0
    for r in conn.execute(
        "SELECT id, repo_path FROM project_documents "
        "WHERE project_id = ? AND repo_id = ? AND source = 'repo'",
        (project_id, repo_id)).fetchall():
        if r["repo_path"] not in seen:
            conn.execute("DELETE FROM project_documents WHERE id = ?", (r["id"],))
            removed += 1

    doc_index.enqueue_reindex_if_needed(conn, project_id)
    if indexed or removed:
        emit(conn, "repo_docs.reconciled", "project", project_id,
             project_id=project_id, actor="system",
             detail={"repo_id": repo_id, "indexed": indexed, "removed": removed})
    conn.commit()
    return {"indexed": indexed, "removed": removed, "seen": len(seen)}


@router.delete("/repos/{repo_id}")
def delete_repo(repo_id: str, request: Request, conn=Depends(db_dep)):
    access.assert_repo(request, conn, repo_id)
    row = conn.execute("SELECT * FROM project_repos WHERE id = ?", (repo_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    conn.execute("DELETE FROM project_repos WHERE id = ?", (repo_id,))
    emit(conn, "repo.deleted", "repo", repo_id,
         project_id=row["project_id"], actor="user", detail={})
    conn.commit()
    return {"ok": True}
