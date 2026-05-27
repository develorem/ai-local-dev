"""Questions: list pending, answer."""

from fastapi import APIRouter, Depends, HTTPException

from server.db.connection import db_dep
from server.events import emit
from server.models import AnswerQuestion
from server.util import utcnow_iso, json_loads

router = APIRouter(prefix="/questions", tags=["questions"])


def _row_to_question(row) -> dict:
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "kind": row["kind"],
        "prompt_md": row["prompt_md"],
        "options": json_loads(row["options_json"], []),
        "status": row["status"],
        "answer_md": row["answer_md"],
        "answer_value": row["answer_value"],
        "created_at": row["created_at"],
        "answered_at": row["answered_at"],
    }


@router.get("")
def list_questions(status: str = "pending", limit: int = 100, conn=Depends(db_dep)):
    limit = max(1, min(limit, 500))
    rows = conn.execute(
        """
        SELECT q.*, t.title AS task_title, t.project_id AS project_id,
               g.title AS goal_title
        FROM questions q
        LEFT JOIN tasks t ON t.id = q.task_id
        LEFT JOIN goals g ON g.id = t.goal_id
        WHERE q.status = ?
        ORDER BY q.created_at ASC LIMIT ?
        """,
        (status, limit),
    ).fetchall()
    items = []
    for r in rows:
        q = _row_to_question(r)
        q["task_title"] = r["task_title"]
        q["goal_title"] = r["goal_title"]
        q["project_id"] = r["project_id"]
        items.append(q)
    return {"items": items, "next_cursor": None}


@router.get("/{question_id}")
def get_question(question_id: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    return {"question": _row_to_question(row)}


@router.post("/{question_id}/answer")
def answer_question(question_id: str, body: AnswerQuestion, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    if row["status"] != "pending":
        raise HTTPException(409, detail={"error": {"code": "not_pending"}})

    now = utcnow_iso()
    conn.execute(
        "UPDATE questions SET status='answered', answer_md=?, answer_value=?, answered_at=? "
        "WHERE id = ?",
        (body.answer_md, body.answer_value, now, question_id),
    )

    task_row = None
    if row["task_id"]:
        task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["task_id"],)).fetchone()

    emit(conn, "question.answered", "question", question_id,
         project_id=task_row["project_id"] if task_row else None,
         goal_id=task_row["goal_id"] if task_row else None,
         task_id=row["task_id"], actor="user",
         detail={"kind": row["kind"], "answer_value": body.answer_value})

    # If no remaining pending questions on this task, transition it back to ready.
    if task_row and task_row["status"] == "blocked_on_human":
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM questions WHERE task_id = ? AND status='pending'",
            (row["task_id"],),
        ).fetchone()
        if remaining["n"] == 0:
            conn.execute(
                "UPDATE tasks SET status='ready' WHERE id = ?",
                (row["task_id"],),
            )
            emit(conn, "task.status_changed", "task", row["task_id"],
                 project_id=task_row["project_id"], goal_id=task_row["goal_id"],
                 task_id=row["task_id"], actor="system",
                 detail={"from": "blocked_on_human", "to": "ready"})

    conn.commit()
    return {"ok": True}
