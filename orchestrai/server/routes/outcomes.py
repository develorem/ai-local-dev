"""Outcomes CRUD (formerly 'goals'). An outcome has tasks.

The DB foreign-key column is still named `goal_id` (see migration 007) — the
entity is 'outcome' everywhere user-facing (table, route, models, payload keys).
"""

from fastapi import APIRouter, Depends, HTTPException

from server.db.connection import db_dep
from server.events import emit
from server.models import Outcome, OutcomeCreate, OutcomeUpdate
from server.util import new_id, utcnow_iso, json_dumps

router = APIRouter(prefix="/outcomes", tags=["outcomes"])


def _row_to_outcome(row) -> dict:
    return {
        "id": row["id"], "project_id": row["project_id"],
        "title": row["title"], "description_md": row["description_md"],
        "status": row["status"], "priority": row["priority"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


@router.post("", status_code=201)
def create_outcome(body: OutcomeCreate, conn=Depends(db_dep)):
    proj = conn.execute("SELECT id FROM projects WHERE id = ?", (body.project_id,)).fetchone()
    if not proj:
        raise HTTPException(404, detail={"error": {"code": "project_not_found"}})

    gid = new_id()
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO outcomes (id, project_id, title, description_md, status, priority,
                              created_at, updated_at)
        VALUES (?, ?, ?, ?, 'submitted', ?, ?, ?)
        """,
        (gid, body.project_id, body.title, body.description_md, body.priority, now, now),
    )
    # Auto-create the planner task in 'ready' state for the worker to pick up.
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
         f"Decompose the outcome '{body.title}' into an ordered task list.",
         body.priority, json_dumps({"goal_id": gid}), now),
    )
    emit(conn, "outcome.created", "outcome", gid,
         project_id=body.project_id, goal_id=gid, actor="user",
         detail={"title": body.title})
    emit(conn, "task.created", "task", ptid,
         project_id=body.project_id, goal_id=gid, task_id=ptid, actor="system",
         detail={"title": f"Plan: {body.title}", "type": "plan"})
    conn.commit()

    row = conn.execute("SELECT * FROM outcomes WHERE id = ?", (gid,)).fetchone()
    return {"outcome": _row_to_outcome(row), "plan_task_id": ptid}


@router.get("")
def list_outcomes(project_id: str | None = None, status: str | None = None,
                  limit: int = 50, conn=Depends(db_dep)):
    limit = max(1, min(limit, 500))
    where, params = [], []
    if project_id:
        where.append("project_id = ?"); params.append(project_id)
    if status:
        where.append("status = ?"); params.append(status)
    q = "SELECT * FROM outcomes"
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    return {"items": [_row_to_outcome(r) for r in rows], "next_cursor": None}


@router.get("/{outcome_id}")
def get_outcome(outcome_id: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM outcomes WHERE id = ?", (outcome_id,)).fetchone()
    if not row:
        raise HTTPException(404)

    plans = [dict(r) for r in conn.execute(
        "SELECT id, version, status, created_at, approved_at FROM plans "
        "WHERE goal_id = ? ORDER BY version DESC",
        (outcome_id,),
    ).fetchall()]

    tasks = [dict(r) for r in conn.execute(
        "SELECT id, title, type, status, priority, branch_name, assigned_agent_id, created_at "
        "FROM tasks WHERE goal_id = ? ORDER BY created_at ASC",
        (outcome_id,),
    ).fetchall()]

    return {"outcome": _row_to_outcome(row), "plans": plans, "tasks": tasks}


@router.patch("/{outcome_id}")
def update_outcome(outcome_id: str, body: OutcomeUpdate, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM outcomes WHERE id = ?", (outcome_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    fields, params = [], []
    for f in ("title", "description_md", "priority"):
        v = getattr(body, f)
        if v is not None:
            fields.append(f"{f} = ?"); params.append(v)
    if not fields:
        return _row_to_outcome(row)
    fields.append("updated_at = ?"); params.append(utcnow_iso())
    params.append(outcome_id)
    conn.execute(f"UPDATE outcomes SET {', '.join(fields)} WHERE id = ?", params)
    emit(conn, "outcome.updated", "outcome", outcome_id,
         project_id=row["project_id"], goal_id=outcome_id, actor="user", detail={})
    conn.commit()
    row = conn.execute("SELECT * FROM outcomes WHERE id = ?", (outcome_id,)).fetchone()
    return _row_to_outcome(row)


@router.post("/{outcome_id}/abandon")
def abandon_outcome(outcome_id: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM outcomes WHERE id = ?", (outcome_id,)).fetchone()
    if not row:
        raise HTTPException(404)

    now = utcnow_iso()
    cancelled = conn.execute(
        """
        UPDATE tasks SET status='cancelled', finished_at=?,
               notes = notes || char(10) || ?
        WHERE goal_id = ?
          AND status NOT IN ('done','failed','cancelled')
        """,
        (now, f"[{now}] cancelled: outcome abandoned", outcome_id),
    ).rowcount

    dismissed = conn.execute(
        """
        UPDATE questions SET status='dismissed'
        WHERE status = 'pending'
          AND task_id IN (SELECT id FROM tasks WHERE goal_id = ?)
        """,
        (outcome_id,),
    ).rowcount

    conn.execute(
        "UPDATE outcomes SET status='abandoned', updated_at=? WHERE id = ?",
        (now, outcome_id),
    )
    emit(conn, "outcome.abandoned", "outcome", outcome_id,
         project_id=row["project_id"], goal_id=outcome_id, actor="user",
         detail={"tasks_cancelled": cancelled, "questions_dismissed": dismissed})
    conn.commit()
    return {"ok": True, "tasks_cancelled": cancelled, "questions_dismissed": dismissed}
