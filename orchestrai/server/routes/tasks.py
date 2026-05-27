"""Tasks CRUD + events + atomic claim (claim is at /agents/{id}/claim)."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from server.db.connection import db_dep
from server.events import emit, event_row_to_dict
from server.models import Task, TaskCreate, TaskUpdate
from server.util import new_id, utcnow_iso, json_dumps, json_loads

router = APIRouter(prefix="/tasks", tags=["tasks"])


def row_to_task(row) -> dict:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "goal_id": row["goal_id"],
        "parent_task_id": row["parent_task_id"],
        "repo_id": row["repo_id"],
        "branch_name": row["branch_name"],
        "type": row["type"],
        "title": row["title"],
        "description_md": row["description_md"],
        "status": row["status"],
        "priority": row["priority"],
        "depends_on": json_loads(row["depends_on"], []),
        "acceptance_criteria": json_loads(row["acceptance_criteria"], []),
        "payload": json_loads(row["payload"], {}),
        "result": json_loads(row["result"], None) if row["result"] else None,
        "error": row["error"],
        "notes": row["notes"],
        "attempt_count": row["attempt_count"],
        "max_attempts": row["max_attempts"],
        "assigned_agent_id": row["assigned_agent_id"],
        "lease_expires_at": row["lease_expires_at"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


@router.post("", status_code=201)
def create_task(body: TaskCreate, conn=Depends(db_dep)):
    if not conn.execute("SELECT 1 FROM projects WHERE id = ?", (body.project_id,)).fetchone():
        raise HTTPException(404, detail={"error": {"code": "project_not_found"}})

    tid = new_id()
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO tasks (id, project_id, goal_id, parent_task_id, repo_id, branch_name,
                           type, title, description_md, status, priority,
                           depends_on, acceptance_criteria, payload,
                           attempt_count, max_attempts, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (tid, body.project_id, body.goal_id, body.parent_task_id,
         body.repo_id, body.branch_name, body.type, body.title, body.description_md,
         body.status, body.priority,
         json_dumps(body.depends_on),
         json_dumps(body.acceptance_criteria),
         json_dumps(body.payload),
         body.max_attempts, now),
    )
    emit(conn, "task.created", "task", tid,
         project_id=body.project_id, goal_id=body.goal_id, task_id=tid, actor="user",
         detail={"title": body.title, "type": body.type, "status": body.status})
    conn.commit()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    return row_to_task(row)


@router.get("")
def list_tasks(
    project_id: Optional[str] = None,
    goal_id: Optional[str] = None,
    repo_id: Optional[str] = None,
    branch_name: Optional[str] = None,
    status: Optional[str] = None,
    type: Optional[str] = None,
    assigned_agent_id: Optional[str] = None,
    limit: int = 100,
    conn=Depends(db_dep),
):
    limit = max(1, min(limit, 500))
    where, params = [], []
    for col, val in (("project_id", project_id), ("goal_id", goal_id),
                     ("repo_id", repo_id), ("branch_name", branch_name),
                     ("assigned_agent_id", assigned_agent_id)):
        if val is not None:
            where.append(f"{col} = ?"); params.append(val)
    if status:
        statuses = [s.strip() for s in status.split(",")]
        where.append("status IN (" + ",".join("?" * len(statuses)) + ")")
        params.extend(statuses)
    if type:
        types_ = [t.strip() for t in type.split(",")]
        where.append("type IN (" + ",".join("?" * len(types_)) + ")")
        params.extend(types_)
    q = "SELECT * FROM tasks"
    if where:
        q += " WHERE " + " AND ".join(where)
    q += (" ORDER BY CASE status "
          "  WHEN 'in_progress' THEN 0 WHEN 'blocked_on_human' THEN 1 "
          "  WHEN 'ready' THEN 2 WHEN 'blocked_on_dep' THEN 3 "
          "  WHEN 'review' THEN 4 WHEN 'done' THEN 5 ELSE 6 END, "
          "created_at DESC LIMIT ?")
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    return {"items": [row_to_task(r) for r in rows], "next_cursor": None}


@router.get("/{task_id}")
def get_task(task_id: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404)

    task = row_to_task(row)

    agent = None
    if row["assigned_agent_id"]:
        a = conn.execute(
            "SELECT id, name, host, status, last_heartbeat_at FROM agents WHERE id = ?",
            (row["assigned_agent_id"],),
        ).fetchone()
        if a:
            agent = dict(a)

    questions = [dict(r) for r in conn.execute(
        "SELECT id, kind, prompt_md, options_json, status, answer_md, "
        "       answer_value, created_at, answered_at "
        "FROM questions WHERE task_id = ? ORDER BY created_at ASC",
        (task_id,),
    ).fetchall()]
    for q in questions:
        q["options"] = json_loads(q.pop("options_json"), [])

    history = [event_row_to_dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE task_id = ? ORDER BY ts ASC LIMIT 200",
        (task_id,),
    ).fetchall()]

    children = [dict(r) for r in conn.execute(
        "SELECT id, title, type, status FROM tasks "
        "WHERE parent_task_id = ? ORDER BY created_at ASC",
        (task_id,),
    ).fetchall()]

    return {"task": task, "agent": agent, "questions": questions,
            "history": history, "children": children}


@router.patch("/{task_id}")
def update_task(task_id: str, body: TaskUpdate, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    fields, params, changed = [], [], []
    if body.title is not None:
        fields.append("title = ?"); params.append(body.title); changed.append("title")
    if body.description_md is not None:
        fields.append("description_md = ?"); params.append(body.description_md); changed.append("description_md")
    if body.priority is not None:
        fields.append("priority = ?"); params.append(body.priority); changed.append("priority")
    if body.depends_on is not None:
        if row["status"] in ("in_progress", "done", "cancelled"):
            raise HTTPException(409, detail={"error": {
                "code": "field_not_editable_in_status",
                "message": "cannot edit depends_on while task is " + row["status"]}})
        fields.append("depends_on = ?"); params.append(json_dumps(body.depends_on)); changed.append("depends_on")
    if body.acceptance_criteria is not None:
        fields.append("acceptance_criteria = ?")
        params.append(json_dumps(body.acceptance_criteria))
        changed.append("acceptance_criteria")
    if body.max_attempts is not None:
        fields.append("max_attempts = ?"); params.append(body.max_attempts); changed.append("max_attempts")
    if not fields:
        return row_to_task(row)
    params.append(task_id)
    conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", params)
    emit(conn, "task.updated", "task", task_id,
         project_id=row["project_id"], goal_id=row["goal_id"],
         task_id=task_id, actor="user", detail={"changed": changed})
    conn.commit()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row_to_task(row)


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    if row["status"] in ("done", "failed", "cancelled"):
        return {"ok": True, "noop": True}
    now = utcnow_iso()
    # cascade to children
    cascaded = conn.execute(
        """
        WITH RECURSIVE descendants(id) AS (
          SELECT id FROM tasks WHERE parent_task_id = ?
          UNION ALL
          SELECT t.id FROM tasks t JOIN descendants d ON t.parent_task_id = d.id
        )
        UPDATE tasks SET status='cancelled', finished_at=?
        WHERE id IN (SELECT id FROM descendants)
          AND status NOT IN ('done','failed','cancelled')
        """,
        (task_id, now),
    ).rowcount

    conn.execute(
        "UPDATE tasks SET status='cancelled', finished_at=? WHERE id = ?",
        (now, task_id),
    )
    conn.execute(
        "UPDATE questions SET status='dismissed' "
        "WHERE status='pending' AND task_id = ?",
        (task_id,),
    )
    emit(conn, "task.cancelled", "task", task_id,
         project_id=row["project_id"], goal_id=row["goal_id"],
         task_id=task_id, actor="user",
         detail={"cascaded_children": cascaded})
    conn.commit()
    return {"ok": True, "cascaded_children": cascaded}


@router.post("/{task_id}/retry")
def retry_task(task_id: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    if row["status"] != "failed":
        raise HTTPException(409, detail={"error": {"code": "not_in_failed_state"}})
    conn.execute(
        "UPDATE tasks SET status='ready', attempt_count=0, error=NULL, "
        "finished_at=NULL WHERE id = ?",
        (task_id,),
    )
    emit(conn, "task.retried", "task", task_id,
         project_id=row["project_id"], goal_id=row["goal_id"],
         task_id=task_id, actor="user", detail={})
    conn.commit()
    return {"ok": True}


@router.post("/{task_id}/notes")
def append_note(task_id: str, body: dict, conn=Depends(db_dep)):
    note = (body or {}).get("note_md", "").strip()
    if not note:
        raise HTTPException(400, detail={"error": {"code": "note_required"}})
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    now = utcnow_iso()
    stamped = f"[{now}] {note}"
    conn.execute(
        "UPDATE tasks SET notes = CASE WHEN notes='' THEN ? "
        "                              ELSE notes || char(10) || ? END WHERE id = ?",
        (stamped, stamped, task_id),
    )
    emit(conn, "task.notes_appended", "task", task_id,
         project_id=row["project_id"], goal_id=row["goal_id"],
         task_id=task_id, actor="user", detail={"note": note})
    conn.commit()
    return {"ok": True}


@router.post("/{task_id}/events")
def post_task_event(task_id: str, body: dict, conn=Depends(db_dep)):
    """Agent-side: report progress mid-task."""
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    kind = (body or {}).get("kind", "task.progress")
    detail = (body or {}).get("detail", {})
    emit(conn, kind, "task", task_id,
         project_id=row["project_id"], goal_id=row["goal_id"],
         task_id=task_id, agent_id=row["assigned_agent_id"],
         actor=f"agent:{row['assigned_agent_id']}" if row["assigned_agent_id"] else "system",
         detail=detail)
    conn.commit()
    return {"ok": True}
