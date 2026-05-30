"""Project repos CRUD."""

from fastapi import APIRouter, Depends, HTTPException

from server.db.connection import db_dep
from server.events import emit
from server.models import Repo, RepoCreate, RepoUpdate
from server.util import new_id, utcnow_iso

router = APIRouter(tags=["repos"])


def _row_to_repo(row) -> dict:
    return {
        "id": row["id"], "project_id": row["project_id"], "name": row["name"],
        "role": row["role"], "url": row["url"],
        "default_branch": row["default_branch"],
        "description_md": row["description_md"],
        "auth_secret_name": (row["auth_secret_name"]
                             if "auth_secret_name" in row.keys() else None),
        "created_at": row["created_at"],
    }


@router.post("/projects/{project_id}/repos", response_model=Repo, status_code=201)
def create_repo(project_id: str, body: RepoCreate, conn=Depends(db_dep)):
    proj = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not proj:
        raise HTTPException(404, detail={"error": {"code": "project_not_found"}})

    rid = new_id()
    now = utcnow_iso()
    try:
        conn.execute(
            """
            INSERT INTO project_repos (id, project_id, name, role, url,
                                       default_branch, description_md,
                                       auth_secret_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (rid, project_id, body.name, body.role, body.url,
             body.default_branch, body.description_md, body.auth_secret_name, now),
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
def list_repos(project_id: str, conn=Depends(db_dep)):
    rows = conn.execute(
        "SELECT * FROM project_repos WHERE project_id = ? ORDER BY name",
        (project_id,),
    ).fetchall()
    return {"items": [_row_to_repo(r) for r in rows]}


@router.get("/repos/{repo_id}")
def get_repo(repo_id: str, conn=Depends(db_dep)):
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
def update_repo(repo_id: str, body: RepoUpdate, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM project_repos WHERE id = ?", (repo_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    fields, params = [], []
    for f in ("name", "role", "url", "default_branch", "description_md", "auth_secret_name"):
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


@router.delete("/repos/{repo_id}")
def delete_repo(repo_id: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM project_repos WHERE id = ?", (repo_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    conn.execute("DELETE FROM project_repos WHERE id = ?", (repo_id,))
    emit(conn, "repo.deleted", "repo", repo_id,
         project_id=row["project_id"], actor="user", detail={})
    conn.commit()
    return {"ok": True}
