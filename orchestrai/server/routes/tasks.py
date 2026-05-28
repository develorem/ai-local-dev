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

    # For plan / revise tasks, include the plans for this goal so the UI can
    # render the actual plan content alongside a plan_approval question.
    plans = []
    if row["type"] in ("plan", "revise") and row["goal_id"]:
        plan_rows = conn.execute(
            """
            SELECT id, version, status, content_md, task_outline,
                   created_at, approval_question_id
            FROM plans WHERE goal_id = ?
            ORDER BY version DESC LIMIT 10
            """,
            (row["goal_id"],),
        ).fetchall()
        for p in plan_rows:
            plans.append({
                "id": p["id"], "version": p["version"], "status": p["status"],
                "content_md": p["content_md"],
                "task_outline": json_loads(p["task_outline"], []),
                "created_at": p["created_at"],
                "approval_question_id": p["approval_question_id"],
            })

    return {"task": task, "agent": agent, "questions": questions,
            "history": history, "children": children, "plans": plans}


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
    # Clear EVERY field that could keep the claim query from picking this up:
    # status, attempt_count, error, finished_at, assigned_agent_id, lease_expires_at.
    # Forgetting any one of these strands the task in 'ready' but unclaimable.
    conn.execute(
        """
        UPDATE tasks
        SET status            = 'ready',
            attempt_count     = 0,
            error             = NULL,
            finished_at       = NULL,
            assigned_agent_id = NULL,
            lease_expires_at  = NULL
        WHERE id = ?
        """,
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


@router.post("/{task_id}/result")
def post_task_result(task_id: str, body: dict, conn=Depends(db_dep)):
    """Agent submits the final result of a task.

    The Hub dispatches per task type:
      - plan: store plan row + create approval Question + task -> blocked_on_human
      - implement / review / review_pr / respond_to_ci_failure: success -> done;
        fix_needed -> ready (retry); needs_human -> blocked_on_human with a question
      - discuss / revise: stored on result; status moves to done unless outcome says otherwise
    """
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    if row["status"] not in ("in_progress", "review"):
        raise HTTPException(409, detail={"error": {
            "code": "wrong_state",
            "message": f"cannot accept result while status={row['status']}",
        }})

    body = body or {}
    outcome = body.get("outcome", "success")
    result = body.get("result", {})
    questions = body.get("questions", []) or []
    notes = body.get("notes_md")
    now = utcnow_iso()

    # Common: stash result + (optionally) notes
    if notes:
        conn.execute(
            "UPDATE tasks SET notes = CASE WHEN notes='' THEN ? "
            "                              ELSE notes || char(10) || ? END WHERE id = ?",
            (f"[{now}] {notes}", f"[{now}] {notes}", task_id),
        )

    new_status = None
    plan_id = None

    # A revise task can be in two modes:
    #   - PLAN revise: result contains plan_md/tasks → create new plan version
    #   - TASK repair: result contains verdict ('rewrite' or 'escalate_to_human')
    #     and the agent has already PATCHed + retried the failed task. Just
    #     mark this revise task as done; no plan creation, no approval question.
    is_task_repair = (
        row["type"] == "revise"
        and outcome == "success"
        and isinstance(result, dict)
        and "verdict" in result
        and "failed_task_id" in result
    )
    if is_task_repair:
        new_status = "done"

    elif row["type"] in ("plan", "revise") and outcome == "success":
        # Plan tasks: persist a plan row + open a plan_approval question
        plan_md = result.get("plan_md", "")
        task_outline = result.get("tasks", [])
        plan_questions = result.get("questions", []) or []

        # If the planner asked clarifying questions, stay blocked_on_human without writing a plan
        if plan_questions and not plan_md:
            for q in plan_questions:
                _insert_question(conn, task_id, q.get("kind", "clarification"),
                                 q.get("prompt_md", ""), q.get("options", []))
            new_status = "blocked_on_human"
        else:
            # Find next plan version. Supersede any prior draft.
            prev = conn.execute(
                "SELECT MAX(version) AS v FROM plans WHERE goal_id = ?",
                (row["goal_id"],),
            ).fetchone()
            next_version = (prev["v"] or 0) + 1
            conn.execute(
                "UPDATE plans SET status='superseded' WHERE goal_id = ? AND status='draft'",
                (row["goal_id"],),
            )
            plan_id = new_id()
            tools_required = result.get("tools_required") or {}
            conn.execute(
                """
                INSERT INTO plans (id, goal_id, version, content_md, task_outline,
                                   tools_required, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)
                """,
                (plan_id, row["goal_id"], next_version,
                 plan_md, json_dumps(task_outline),
                 json_dumps(tools_required), now),
            )
            # Move the goal to 'planning' if not already
            if row["goal_id"]:
                conn.execute(
                    "UPDATE goals SET status='planning', updated_at=? "
                    "WHERE id = ? AND status IN ('submitted','planning','active')",
                    (now, row["goal_id"]),
                )
            # Approval question
            qid = _insert_question(
                conn, task_id, "plan_approval",
                f"Plan v{next_version} is ready for review. Approve to instantiate "
                f"{len(task_outline)} tasks; reject to discard; or open a discussion.",
                [
                    {"label": "Approve", "value": "approve"},
                    {"label": "Approve with edits", "value": "approve_with_edits"},
                    {"label": "Reject", "value": "reject"},
                    {"label": "Discuss", "value": "discuss"},
                ],
            )
            conn.execute(
                "UPDATE plans SET approval_question_id = ? WHERE id = ?",
                (qid, plan_id),
            )
            emit(conn, "plan.created", "plan", plan_id,
                 project_id=row["project_id"], goal_id=row["goal_id"],
                 task_id=task_id, agent_id=row["assigned_agent_id"],
                 actor=f"agent:{row['assigned_agent_id']}" if row['assigned_agent_id'] else 'system',
                 detail={"version": next_version, "task_count": len(task_outline)})
            new_status = "blocked_on_human"

    elif outcome == "success":
        new_status = "done"
    elif outcome == "fix_needed":
        # Retry path — back to ready, lease cleared, agent unassigned
        if row["attempt_count"] >= row["max_attempts"]:
            # Out of retries. Before declaring failed, try ONE self-repair pass:
            # spawn a `revise` task pointed at this failed task so the agent can
            # rewrite the description/criteria and re-queue. Only do this for
            # implement/review tasks (plan/discuss/revise tasks aren't auto-repairable
            # in the same way), and only ONCE per task (loop guard).
            existing_payload = json_loads(row["payload"], {})
            already_repaired = bool(existing_payload.get("repair_attempted"))
            repairable_types = {"implement", "review"}
            if row["type"] in repairable_types and not already_repaired:
                _spawn_task_repair(conn, row)
                # Mark this task failed for now; the repair task will PATCH + retry it.
                new_status = "failed"
                # Set the loop-guard flag in payload so the next failure (if any)
                # doesn't try to repair again.
                existing_payload["repair_attempted"] = True
                conn.execute(
                    "UPDATE tasks SET payload = ? WHERE id = ?",
                    (json_dumps(existing_payload), task_id),
                )
            else:
                new_status = "failed"
        else:
            new_status = "ready"
    elif outcome == "failed":
        new_status = "failed"
    elif outcome == "needs_human":
        new_status = "blocked_on_human"
    else:
        raise HTTPException(400, detail={"error": {
            "code": "bad_outcome", "message": f"unknown outcome '{outcome}'",
        }})

    # Persist any free-form questions submitted with the result (other than plan_approval handled above)
    if questions and row["type"] != "plan":
        for q in questions:
            # Be tolerant: LLM sometimes returns strings instead of {kind, prompt_md} dicts
            if isinstance(q, str):
                _insert_question(conn, task_id, "clarification", q, [])
            elif isinstance(q, dict):
                _insert_question(conn, task_id, q.get("kind", "clarification"),
                                 q.get("prompt_md", ""), q.get("options", []))
            # else: silently skip malformed entries
        if new_status == "done":
            new_status = "blocked_on_human"

    # Update task row
    finished_at = now if new_status in ("done", "failed") else None
    conn.execute(
        """
        UPDATE tasks
        SET status = ?, result = ?, finished_at = ?,
            assigned_agent_id = CASE WHEN ? IN ('ready','blocked_on_human','blocked_on_dep')
                                     THEN NULL ELSE assigned_agent_id END,
            lease_expires_at  = CASE WHEN ? IN ('ready','blocked_on_human','blocked_on_dep')
                                     THEN NULL ELSE lease_expires_at END
        WHERE id = ?
        """,
        (new_status, json_dumps(result), finished_at, new_status, new_status, task_id),
    )
    # If the task is leaving an agent, also free the agent's current_task_id
    if new_status in ("ready", "blocked_on_human", "blocked_on_dep", "done", "failed"):
        if row["assigned_agent_id"]:
            conn.execute(
                "UPDATE agents SET current_task_id = NULL, status='idle' "
                "WHERE id = ? AND current_task_id = ?",
                (row["assigned_agent_id"], task_id),
            )

    emit(conn, "task.status_changed", "task", task_id,
         project_id=row["project_id"], goal_id=row["goal_id"],
         task_id=task_id, agent_id=row["assigned_agent_id"],
         actor=f"agent:{row['assigned_agent_id']}" if row['assigned_agent_id'] else 'system',
         detail={"from": row["status"], "to": new_status, "outcome": outcome})

    # Dep cascade: if THIS task just became 'done', any blocked_on_dep task
    # whose deps are now all done should transition to 'ready'.
    if new_status == "done":
        _cascade_dep_unblock(conn, task_id)

    # Goal completion: if all of a goal's tasks are terminal-success, mark goal done.
    if new_status in ("done", "cancelled") and row["goal_id"]:
        _maybe_complete_goal(conn, row["goal_id"])

    conn.commit()

    return {"ok": True, "status": new_status, "plan_id": plan_id}


def _cascade_dep_unblock(conn, completed_task_id: str) -> None:
    """For every blocked_on_dep task that depends on `completed_task_id`, recheck deps."""
    rows = conn.execute(
        """
        SELECT t.id, t.project_id, t.goal_id, t.depends_on
        FROM tasks t
        WHERE t.status = 'blocked_on_dep'
          AND EXISTS (
            SELECT 1 FROM json_each(t.depends_on) AS dep WHERE dep.value = ?
          )
        """,
        (completed_task_id,),
    ).fetchall()
    for r in rows:
        deps = json_loads(r["depends_on"], [])
        if not deps:
            continue
        placeholders = ",".join("?" * len(deps))
        unmet = conn.execute(
            f"SELECT COUNT(*) AS n FROM tasks WHERE id IN ({placeholders}) AND status != 'done'",
            deps,
        ).fetchone()
        if unmet["n"] == 0:
            conn.execute(
                "UPDATE tasks SET status='ready' WHERE id = ?",
                (r["id"],),
            )
            emit(conn, "task.status_changed", "task", r["id"],
                 project_id=r["project_id"], goal_id=r["goal_id"],
                 task_id=r["id"], actor="system",
                 detail={"from": "blocked_on_dep", "to": "ready",
                         "reason": "all deps satisfied"})


def _maybe_complete_goal(conn, goal_id: str) -> None:
    """If every task in the goal is in a terminal state and at least one is done,
    move the goal to `done`."""
    g = conn.execute(
        "SELECT id, project_id, status FROM goals WHERE id = ?",
        (goal_id,),
    ).fetchone()
    if not g or g["status"] in ("done", "abandoned", "rejected"):
        return
    counts = conn.execute(
        """
        SELECT
          SUM(CASE WHEN status IN ('done','cancelled') THEN 1 ELSE 0 END) AS terminal,
          SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done_n,
          SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_n,
          COUNT(*) AS total
        FROM tasks WHERE goal_id = ?
        """,
        (goal_id,),
    ).fetchone()
    if (counts["total"] or 0) == 0:
        return
    if counts["failed_n"] and int(counts["failed_n"]) > 0:
        return  # don't auto-complete if any task failed
    if int(counts["terminal"] or 0) == int(counts["total"]) and int(counts["done_n"] or 0) > 0:
        conn.execute(
            "UPDATE goals SET status='done', updated_at=? WHERE id = ?",
            (utcnow_iso(), goal_id),
        )
        emit(conn, "goal.status_changed", "goal", goal_id,
             project_id=g["project_id"], goal_id=goal_id, actor="system",
             detail={"to": "done", "reason": "all tasks complete"})


def _insert_question(conn, task_id, kind, prompt_md, options) -> str:
    qid = new_id()
    conn.execute(
        """
        INSERT INTO questions (id, task_id, kind, prompt_md, options_json,
                               status, created_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (qid, task_id, kind, prompt_md, json_dumps(options), utcnow_iso()),
    )
    return qid


def _spawn_task_repair(conn, failed_row) -> str:
    """Create a `revise` task whose payload tells the agent to repair the
    given failed task. Priority `high` so it gets picked up promptly.
    """
    rtid = new_id()
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO tasks (id, project_id, goal_id, type, title, description_md,
                           status, priority, depends_on, acceptance_criteria,
                           payload, attempt_count, max_attempts, created_at)
        VALUES (?, ?, ?, 'revise', ?, ?, 'ready', 'high', '[]', '[]', ?, 0, 2, ?)
        """,
        (rtid, failed_row["project_id"], failed_row["goal_id"],
         f"Repair: {failed_row['title']}",
         (f"Auto-spawned because task `{failed_row['title']}` failed after "
          f"{failed_row['max_attempts']} attempts. Diagnose the failure and "
          f"rewrite the task so a fresh attempt can succeed."),
         json_dumps({"failed_task_id": failed_row["id"]}),
         now),
    )
    emit(conn, "task.created", "task", rtid,
         project_id=failed_row["project_id"],
         goal_id=failed_row["goal_id"], task_id=rtid, actor="system",
         detail={"type": "revise", "mode": "task_repair",
                 "failed_task_id": failed_row["id"]})
    emit(conn, "task.repair_spawned", "task", failed_row["id"],
         project_id=failed_row["project_id"],
         goal_id=failed_row["goal_id"], task_id=failed_row["id"], actor="system",
         detail={"repair_task_id": rtid})
    return rtid
