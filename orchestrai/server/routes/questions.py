"""Questions: list pending, answer.

When a `plan_approval` question is answered:
  - approve              → instantiate the plan's task_outline as ready tasks,
                           transition goal to active, mark plan approved
  - approve_with_edits   → enqueue a `revise` task referencing the plan
  - reject               → mark plan rejected; goal rejected
  - discuss              → open a discussion thread linked to the goal
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from server.db.connection import db_dep
from server.events import emit
from server.models import AnswerQuestion
from server.services import access
from server.util import new_id, utcnow_iso, json_dumps, json_loads

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
def list_questions(request: Request, status: str = "pending", limit: int = 100, conn=Depends(db_dep)):
    limit = max(1, min(limit, 500))
    # Restrict to questions on tasks in the caller's accessible projects.
    frag, fparams = access.project_filter(request, conn, "t.project_id")
    where = "q.status = ?"
    params = [status]
    if frag:
        where += f" AND {frag}"
        params.extend(fparams)
    rows = conn.execute(
        f"""
        SELECT q.*, t.title AS task_title, t.project_id AS project_id,
               g.title AS goal_title
        FROM questions q
        LEFT JOIN tasks t ON t.id = q.task_id
        LEFT JOIN outcomes g ON g.id = t.outcome_id
        WHERE {where}
        ORDER BY q.created_at ASC LIMIT ?
        """,
        (*params, limit),
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
def get_question(question_id: str, request: Request, conn=Depends(db_dep)):
    access.assert_question(request, conn, question_id)
    row = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    return {"question": _row_to_question(row)}


@router.post("/{question_id}/answer")
def answer_question(question_id: str, body: AnswerQuestion, request: Request, conn=Depends(db_dep)):
    access.assert_question(request, conn, question_id)
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
         outcome_id=task_row["outcome_id"] if task_row else None,
         task_id=row["task_id"], actor="user",
         detail={"kind": row["kind"], "answer_value": body.answer_value})

    # Plan-approval has type-specific side effects
    if row["kind"] == "plan_approval" and task_row:
        _handle_plan_approval(conn, task_row, body.answer_value, body.answer_md)
    else:
        # Generic: if no remaining pending questions on the task, ready it
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
                     project_id=task_row["project_id"], outcome_id=task_row["outcome_id"],
                     task_id=row["task_id"], actor="system",
                     detail={"from": "blocked_on_human", "to": "ready"})

    conn.commit()
    return {"ok": True}


def _handle_plan_approval(conn, task_row, answer_value: str | None, answer_md: str | None) -> None:
    """Apply the side effects of a plan_approval question being answered.

    Caller commits.
    """
    outcome_id = task_row["outcome_id"]
    project_id = task_row["project_id"]
    task_id = task_row["id"]
    now = utcnow_iso()

    plan_row = conn.execute(
        "SELECT * FROM plans WHERE outcome_id = ? AND status = 'draft' "
        "ORDER BY version DESC LIMIT 1",
        (outcome_id,),
    ).fetchone()
    if not plan_row:
        return  # nothing to do; orphan approval

    if answer_value == "approve":
        # UNION the plan's tools into the project's permanent registry. We
        # never remove anything — once a project depends on a package, it
        # keeps depending on it; the agent's pip-freeze diff handles the
        # actual installs at task-claim time.
        plan_tools = json_loads(plan_row["tools_required"], {})
        if plan_tools:
            proj = conn.execute(
                "SELECT tools FROM projects WHERE id = ?", (project_id,),
            ).fetchone()
            current = json_loads(proj["tools"], {}) if proj else {}
            merged = dict(current)
            for kind in ("python_packages", "node_packages"):
                existing = list(merged.get(kind) or [])
                seen = {p.strip(): True for p in existing}
                for pkg in (plan_tools.get(kind) or []):
                    s = pkg.strip()
                    if s and s not in seen:
                        existing.append(s)
                        seen[s] = True
                merged[kind] = existing
            conn.execute(
                "UPDATE projects SET tools = ?, updated_at = ? WHERE id = ?",
                (json_dumps(merged), now, project_id),
            )
            emit(conn, "project.tools_updated", "project", project_id,
                 project_id=project_id, actor="system",
                 detail={"added": plan_tools, "now": merged})

        # Instantiate the task_outline
        outline = json_loads(plan_row["task_outline"], [])
        title_to_id: dict[str, str] = {}
        ordered: list[str] = []

        # First pass: insert all tasks with empty depends_on
        for stub in outline:
            tid = new_id()
            title_to_id[stub.get("title", tid)] = tid
            ordered.append(tid)
            conn.execute(
                """
                INSERT INTO tasks (id, project_id, outcome_id, type, title, description_md,
                                   status, priority, depends_on, acceptance_criteria,
                                   payload, attempt_count, max_attempts, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, '[]', ?, ?, 0, 3, ?)
                """,
                (tid, project_id, outcome_id, stub.get("type", "implement"),
                 stub.get("title", "Untitled"),
                 stub.get("description_md", ""),
                 stub.get("priority", "normal"),
                 json_dumps(stub.get("acceptance_criteria", [])),
                 json_dumps(stub.get("payload", {})),
                 now),
            )
            emit(conn, "task.created", "task", tid,
                 project_id=project_id, outcome_id=outcome_id, task_id=tid, actor="system",
                 detail={"title": stub.get("title"), "type": stub.get("type", "implement"),
                         "from_plan": plan_row["id"]})

        # Second pass: resolve depends_on_titles → ids
        for stub, tid in zip(outline, ordered):
            dep_titles = stub.get("depends_on_titles") or []
            dep_ids = [title_to_id.get(t) for t in dep_titles if title_to_id.get(t)]
            if dep_ids:
                conn.execute(
                    "UPDATE tasks SET depends_on = ? WHERE id = ?",
                    (json_dumps(dep_ids), tid),
                )
                # If any deps aren't done yet, mark this task blocked_on_dep
                blocking = conn.execute(
                    "SELECT COUNT(*) AS n FROM tasks "
                    "WHERE id IN (" + ",".join("?" * len(dep_ids)) + ") AND status != 'done'",
                    dep_ids,
                ).fetchone()
                if blocking["n"] > 0:
                    conn.execute(
                        "UPDATE tasks SET status='blocked_on_dep' WHERE id = ?",
                        (tid,),
                    )

        # Approve plan, activate goal, complete planner task
        conn.execute(
            "UPDATE plans SET status='approved', approved_at=?, approval_notes=? WHERE id = ?",
            (now, answer_md, plan_row["id"]),
        )
        conn.execute(
            "UPDATE outcomes SET status='active', updated_at=? WHERE id = ?",
            (now, outcome_id),
        )
        conn.execute(
            "UPDATE tasks SET status='done', finished_at=?, "
            "                  assigned_agent_id=NULL, lease_expires_at=NULL "
            "WHERE id = ?",
            (now, task_id),
        )
        # Free the agent if still holding
        if task_row["assigned_agent_id"]:
            conn.execute(
                "UPDATE agents SET current_task_id=NULL, status='idle' "
                "WHERE id = ? AND current_task_id = ?",
                (task_row["assigned_agent_id"], task_id),
            )

        emit(conn, "plan.approved", "plan", plan_row["id"],
             project_id=project_id, outcome_id=outcome_id, task_id=task_id, actor="user",
             detail={"tasks_instantiated": len(ordered)})
        emit(conn, "goal.status_changed", "goal", outcome_id,
             project_id=project_id, outcome_id=outcome_id, actor="system",
             detail={"to": "active"})
        emit(conn, "task.status_changed", "task", task_id,
             project_id=project_id, outcome_id=outcome_id, task_id=task_id, actor="system",
             detail={"from": "blocked_on_human", "to": "done"})

    elif answer_value == "reject":
        conn.execute(
            "UPDATE plans SET status='rejected' WHERE id = ?",
            (plan_row["id"],),
        )
        conn.execute(
            "UPDATE outcomes SET status='rejected', updated_at=? WHERE id = ?",
            (now, outcome_id),
        )
        conn.execute(
            "UPDATE tasks SET status='cancelled', finished_at=?, "
            "assigned_agent_id=NULL, lease_expires_at=NULL WHERE id = ?",
            (now, task_id),
        )
        emit(conn, "plan.rejected", "plan", plan_row["id"],
             project_id=project_id, outcome_id=outcome_id, task_id=task_id, actor="user",
             detail={"reason": answer_md})

    elif answer_value == "approve_with_edits":
        # Enqueue a revise task carrying the edit request
        rtid = new_id()
        conn.execute(
            """
            INSERT INTO tasks (id, project_id, outcome_id, type, title, description_md,
                               status, priority, depends_on, acceptance_criteria,
                               payload, attempt_count, max_attempts, created_at)
            VALUES (?, ?, ?, 'revise', ?, ?, 'ready', 'normal', '[]', '[]', ?, 0, 3, ?)
            """,
            (rtid, project_id, outcome_id,
             f"Revise plan v{plan_row['version']}",
             answer_md or "Revise the plan per user feedback.",
             json_dumps({"plan_id": plan_row["id"], "edit_request": answer_md or ""}),
             now),
        )
        conn.execute(
            "UPDATE tasks SET status='cancelled', finished_at=?, "
            "assigned_agent_id=NULL, lease_expires_at=NULL WHERE id = ?",
            (now, task_id),
        )
        emit(conn, "task.created", "task", rtid,
             project_id=project_id, outcome_id=outcome_id, task_id=rtid, actor="user",
             detail={"title": "Revise plan", "type": "revise"})

    elif answer_value == "discuss":
        did = new_id()
        conn.execute(
            """
            INSERT INTO discussions (id, project_id, outcome_id, title, status, created_at)
            VALUES (?, ?, ?, ?, 'open', ?)
            """,
            (did, project_id, outcome_id,
             f"Discuss plan v{plan_row['version']}",
             now),
        )
        # Leave the planner task blocked_on_human; user uses the discussion to evolve the plan
        emit(conn, "discussion.created", "discussion", did,
             project_id=project_id, outcome_id=outcome_id, actor="user",
             detail={"title": f"Discuss plan v{plan_row['version']}"})
