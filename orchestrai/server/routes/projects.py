"""Project CRUD + summary."""

from fastapi import APIRouter, Depends, HTTPException

from server.db.connection import db_dep
from server.events import emit
from server.models import Project, ProjectCreate, ProjectUpdate
from server.util import new_id, utcnow_iso, json_loads

router = APIRouter(prefix="/projects", tags=["projects"])


def _row_to_project(row) -> dict:
    try:
        tools = json_loads(row["tools"], {}) if "tools" in row.keys() else {}
    except Exception:
        tools = {}
    return {
        "id": row["id"],
        "name": row["name"],
        "slug": row["slug"],
        "description_md": row["description_md"],
        "context_md": row["context_md"],
        "status": row["status"],
        "execution_mode": (row["execution_mode"] if "execution_mode" in row.keys()
                           else "manual"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived_at": row["archived_at"],
        "tools": tools,
    }


@router.post("", response_model=Project, status_code=201)
def create_project(body: ProjectCreate, conn=Depends(db_dep)):
    now = utcnow_iso()
    pid = new_id()
    try:
        conn.execute(
            """
            INSERT INTO projects (id, name, slug, description_md, context_md,
                                  status, execution_mode, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (pid, body.name, body.slug, body.description_md, body.context_md,
             body.execution_mode, now, now),
        )
        emit(conn, "project.created", "project", pid,
             project_id=pid, actor="user",
             detail={"name": body.name, "slug": body.slug})
        conn.commit()
    except Exception as e:
        conn.rollback()
        if "UNIQUE" in str(e):
            raise HTTPException(409, detail={"error": {"code": "slug_taken",
                                                       "message": f"slug '{body.slug}' already exists"}})
        raise

    row = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    return _row_to_project(row)


@router.get("")
def list_projects(status: str | None = None, limit: int = 50, conn=Depends(db_dep)):
    limit = max(1, min(limit, 500))
    if status:
        rows = conn.execute(
            "SELECT * FROM projects WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM projects ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    items = []
    for r in rows:
        p = _row_to_project(r)
        stats = conn.execute(
            """
            SELECT
              SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS goals_active,
              SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS goals_done
            FROM goals WHERE project_id = ?
            """,
            (p["id"],),
        ).fetchone()
        task_stats = conn.execute(
            """
            SELECT
              SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END) AS ready,
              SUM(CASE WHEN status='in_progress' THEN 1 ELSE 0 END) AS in_progress,
              SUM(CASE WHEN status='blocked_on_human' THEN 1 ELSE 0 END) AS blocked_on_human,
              SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done
            FROM tasks WHERE project_id = ?
            """,
            (p["id"],),
        ).fetchone()
        q_count = conn.execute(
            """
            SELECT COUNT(*) AS n FROM questions q
            JOIN tasks t ON t.id = q.task_id
            WHERE q.status = 'pending' AND t.project_id = ?
            """,
            (p["id"],),
        ).fetchone()
        p["stats"] = {
            "goals_active": int(stats["goals_active"] or 0),
            "goals_done": int(stats["goals_done"] or 0),
            "tasks_ready": int(task_stats["ready"] or 0),
            "tasks_in_progress": int(task_stats["in_progress"] or 0),
            "tasks_blocked_on_human": int(task_stats["blocked_on_human"] or 0),
            "tasks_done": int(task_stats["done"] or 0),
            "open_questions": int(q_count["n"] or 0),
        }
        items.append(p)
    return {"items": items, "next_cursor": None}


@router.get("/{project_id}")
def get_project(project_id: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    project = _row_to_project(row)

    repos = [dict(r) for r in conn.execute(
        "SELECT * FROM project_repos WHERE project_id = ? ORDER BY name",
        (project_id,),
    ).fetchall()]

    goals = [dict(r) for r in conn.execute(
        "SELECT id, title, status, priority, created_at FROM goals "
        "WHERE project_id = ? ORDER BY created_at DESC LIMIT 50",
        (project_id,),
    ).fetchall()]

    tasks = [dict(r) for r in conn.execute(
        "SELECT id, title, type, status, priority, assigned_agent_id, "
        "       repo_id, branch_name, created_at "
        "FROM tasks WHERE project_id = ? "
        "ORDER BY CASE status "
        "  WHEN 'in_progress' THEN 0 WHEN 'blocked_on_human' THEN 1 "
        "  WHEN 'ready' THEN 2 WHEN 'blocked_on_dep' THEN 3 "
        "  WHEN 'review' THEN 4 WHEN 'done' THEN 5 ELSE 6 END, "
        "created_at DESC LIMIT 100",
        (project_id,),
    ).fetchall()]

    return {"project": project, "repos": repos, "goals": goals, "tasks": tasks}


@router.patch("/{project_id}", response_model=Project)
def update_project(project_id: str, body: ProjectUpdate, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(404)

    fields, params = [], []
    for f in ("name", "description_md", "context_md", "execution_mode"):
        v = getattr(body, f)
        if v is not None:
            fields.append(f"{f} = ?")
            params.append(v)
    if not fields:
        return _row_to_project(row)

    fields.append("updated_at = ?")
    params.append(utcnow_iso())
    params.append(project_id)

    conn.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id = ?", params)
    emit(conn, "project.updated", "project", project_id,
         project_id=project_id, actor="user",
         detail={"changed": [f.split(" = ")[0] for f in fields if " = ?" in f]})
    conn.commit()

    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return _row_to_project(row)


@router.post("/{project_id}/archive")
def archive_project(project_id: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    now = utcnow_iso()
    conn.execute(
        "UPDATE projects SET status='archived', archived_at=?, updated_at=? WHERE id = ?",
        (now, now, project_id),
    )
    emit(conn, "project.archived", "project", project_id,
         project_id=project_id, actor="user", detail={})
    conn.commit()
    return {"ok": True}
