"""Plans: read-only endpoint for fetching a plan's full content + outline."""

from fastapi import APIRouter, Depends, HTTPException

from server.db.connection import db_dep
from server.util import json_loads

router = APIRouter(prefix="/plans", tags=["plans"])


def _row_to_plan(row) -> dict:
    return {
        "id": row["id"],
        "goal_id": row["goal_id"],
        "version": row["version"],
        "content_md": row["content_md"],
        "task_outline": json_loads(row["task_outline"], []),
        "status": row["status"],
        "approval_question_id": row["approval_question_id"],
        "created_at": row["created_at"],
        "approved_at": row["approved_at"],
        "approval_notes": row["approval_notes"],
    }


@router.get("/{plan_id}")
def get_plan(plan_id: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    return {"plan": _row_to_plan(row)}
