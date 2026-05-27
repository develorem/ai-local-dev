"""Goals CRUD."""

from fastapi import APIRouter, Depends, HTTPException

from server.db.connection import db_dep
from server.events import emit
from server.models import Goal, GoalCreate, GoalUpdate
from server.util import new_id, utcnow_iso, json_dumps

router = APIRouter(prefix="/goals", tags=["goals"])


def _row_to_goal(row) -> dict:
    return {
        "id": row["id"], "project_id": row["project_id"],
        "title": row["title"], "description_md": row["description_md"],
        "status": row["status"], "priority": row["priority"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


@router.post("", status_code=201)
def create_goal(body: GoalCreate, conn=Depends(db_dep)):
    proj = conn.execute("SELECT id FROM projects WHERE id = ?", (body.project_id,)).fetchone()
    if not proj:
        raise HTTPException(404, detail={"error": {"code": "project_not_found"}})

    gid = new_id()
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO goals (id, project_id, title, description_md, status, priority,
                           created_at, updated_at)
        VALUES (?, ?, ?, ?, 'submitted', ?, ?, ?)
        """,
        (gid, body.project_id, body.title, body.description_md, body.priority, now, now),
    )
    # Auto-create the planner task in 'ready' state. Phase 3+ (with Agent) will pick it up.
    ptid = new_id()
    conn.execute(
        """
        INSERT INTO tasks (id, project_id, goal_id, type, title, description_md,
                           status, priority, depends_on, acceptance_criteria,
                           payload, attempt_count, max_attempts, created_at)
        VALUES (?, ?, ?, 'plan', ?, ?, 'ready', ?, '[]', '[]', ?, 0, 3, ?)
        """,
        (ptid, body.project_id, gid,
         f"Plan: {body.title}",
         f"Decompose the goal '{body.title}' into an ordered task list.",
         body.priority, json_dumps({"goal_id": gid}), now),
    )
    emit(conn, "goal.created", "goal", gid,
         project_id=body.project_id, goal_id=gid, actor="user",
         detail={"title": body.title})
    emit(conn, "task.created", "task", ptid,
         project_id=body.project_id, goal_id=gid, task_id=ptid, actor="system",
         detail={"title": f"Plan: {body.title}", "type": "plan"})
    conn.commit()

    row = conn.execute("SELECT * FROM goals WHERE id = ?", (gid,)).fetchone()
    return {"goal": _row_to_goal(row), "plan_task_id": ptid}


@router.get("")
def list_goals(project_id: str | None = None, status: str | None = None,
               limit: int = 50, conn=Depends(db_dep)):
    limit = max(1, min(limit, 500))
    where, params = [], []
    if project_id:
        where.append("project_id = ?"); params.append(project_id)
    if status:
        where.append("status = ?"); params.append(status)
    q = "SELECT * FROM goals"
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    return {"items": [_row_to_goal(r) for r in rows], "next_cursor": None}


@router.get("/{goal_id}")
def get_goal(goal_id: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if not row:
        raise HTTPException(404)

    plans = [dict(r) for r in conn.execute(
        "SELECT id, version, status, created_at, approved_at FROM plans "
        "WHERE goal_id = ? ORDER BY version DESC",
        (goal_id,),
    ).fetchall()]

    tasks = [dict(r) for r in conn.execute(
        "SELECT id, title, type, status, priority, branch_name, assigned_agent_id, created_at "
        "FROM tasks WHERE goal_id = ? ORDER BY created_at ASC",
        (goal_id,),
    ).fetchall()]

    return {"goal": _row_to_goal(row), "plans": plans, "tasks": tasks}


@router.patch("/{goal_id}")
def update_goal(goal_id: str, body: GoalUpdate, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    fields, params = [], []
    for f in ("title", "description_md", "priority"):
        v = getattr(body, f)
        if v is not None:
            fields.append(f"{f} = ?"); params.append(v)
    if not fields:
        return _row_to_goal(row)
    fields.append("updated_at = ?"); params.append(utcnow_iso())
    params.append(goal_id)
    conn.execute(f"UPDATE goals SET {', '.join(fields)} WHERE id = ?", params)
    emit(conn, "goal.updated", "goal", goal_id,
         project_id=row["project_id"], goal_id=goal_id, actor="user", detail={})
    conn.commit()
    row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    return _row_to_goal(row)


@router.post("/{goal_id}/abandon")
def abandon_goal(goal_id: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if not row:
        raise HTTPException(404)

    now = utcnow_iso()
    # Cancel non-terminal tasks
    cancelled = conn.execute(
        """
        UPDATE tasks SET status='cancelled', finished_at=?,
               notes = notes || char(10) || ?
        WHERE goal_id = ?
          AND status NOT IN ('done','failed','cancelled')
        """,
        (now, f"[{now}] cancelled: goal abandoned", goal_id),
    ).rowcount

    # Dismiss pending questions on those tasks
    dismissed = conn.execute(
        """
        UPDATE questions SET status='dismissed'
        WHERE status = 'pending'
          AND task_id IN (SELECT id FROM tasks WHERE goal_id = ?)
        """,
        (goal_id,),
    ).rowcount

    conn.execute(
        "UPDATE goals SET status='abandoned', updated_at=? WHERE id = ?",
        (now, goal_id),
    )
    emit(conn, "goal.abandoned", "goal", goal_id,
         project_id=row["project_id"], goal_id=goal_id, actor="user",
         detail={"tasks_cancelled": cancelled, "questions_dismissed": dismissed})
    conn.commit()
    return {"ok": True, "tasks_cancelled": cancelled, "questions_dismissed": dismissed}
