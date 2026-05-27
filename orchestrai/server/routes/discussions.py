"""Discussions: multi-turn chat threads linked to a project/goal/task,
plus proposed-actions that mutate the task graph when applied by the human.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from server.db.connection import db_dep
from server.events import emit
from server.util import new_id, utcnow_iso, json_dumps, json_loads

router = APIRouter(tags=["discussions"])


def _row_to_discussion(row) -> dict:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "goal_id": row["goal_id"],
        "task_id": row["task_id"],
        "title": row["title"],
        "status": row["status"],
        "created_at": row["created_at"],
        "closed_at": row["closed_at"],
    }


def _row_to_message(row) -> dict:
    return {
        "id": row["id"],
        "discussion_id": row["discussion_id"],
        "role": row["role"],
        "content_md": row["content_md"],
        "meta": json_loads(row["meta"], {}),
        "created_at": row["created_at"],
    }


def _row_to_proposed(row) -> dict:
    return {
        "id": row["id"],
        "discussion_id": row["discussion_id"],
        "message_id": row["message_id"],
        "action_type": row["action_type"],
        "payload": json_loads(row["payload"], {}),
        "human_summary": row["human_summary"],
        "status": row["status"],
        "created_at": row["created_at"],
        "applied_at": row["applied_at"],
        "applied_by": row["applied_by"],
    }


# ---------- Discussions -----------------------------------------------------

@router.post("/discussions", status_code=201)
def create_discussion(body: dict, conn=Depends(db_dep)):
    body = body or {}
    title = (body.get("title") or "").strip()
    project_id = body.get("project_id")
    goal_id = body.get("goal_id")
    task_id = body.get("task_id")
    initial_msg = body.get("initial_message_md")

    if not title:
        raise HTTPException(400, detail={"error": {"code": "title_required"}})

    # Resolve project_id if only goal_id or task_id was given
    if not project_id and task_id:
        tr = conn.execute("SELECT project_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if tr:
            project_id = tr["project_id"]
    if not project_id and goal_id:
        gr = conn.execute("SELECT project_id FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if gr:
            project_id = gr["project_id"]

    did = new_id()
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO discussions (id, project_id, goal_id, task_id, title, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'open', ?)
        """,
        (did, project_id, goal_id, task_id, title, now),
    )
    emit(conn, "discussion.created", "discussion", did,
         project_id=project_id, goal_id=goal_id, task_id=task_id, actor="user",
         detail={"title": title})

    if initial_msg:
        _post_message(conn, did, "user", initial_msg)
        _enqueue_discuss_task(conn, did, project_id, goal_id, task_id, title)

    conn.commit()
    row = conn.execute("SELECT * FROM discussions WHERE id = ?", (did,)).fetchone()
    return _row_to_discussion(row)


@router.get("/discussions")
def list_discussions(status: Optional[str] = None, project_id: Optional[str] = None,
                     limit: int = 50, conn=Depends(db_dep)):
    limit = max(1, min(limit, 500))
    where, params = [], []
    if status:
        where.append("status = ?"); params.append(status)
    if project_id:
        where.append("project_id = ?"); params.append(project_id)
    q = "SELECT * FROM discussions"
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    return {"items": [_row_to_discussion(r) for r in rows]}


@router.get("/discussions/{discussion_id}")
def get_discussion(discussion_id: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM discussions WHERE id = ?", (discussion_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    messages = [_row_to_message(m) for m in conn.execute(
        "SELECT * FROM messages WHERE discussion_id = ? ORDER BY created_at ASC",
        (discussion_id,),
    ).fetchall()]
    proposed = [_row_to_proposed(p) for p in conn.execute(
        "SELECT * FROM proposed_actions WHERE discussion_id = ? ORDER BY created_at ASC",
        (discussion_id,),
    ).fetchall()]
    return {
        "discussion": _row_to_discussion(row),
        "messages": messages,
        "proposed_actions": proposed,
    }


@router.post("/discussions/{discussion_id}/close")
def close_discussion(discussion_id: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM discussions WHERE id = ?", (discussion_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    now = utcnow_iso()
    conn.execute(
        "UPDATE discussions SET status='closed', closed_at=? WHERE id = ?",
        (now, discussion_id),
    )
    emit(conn, "discussion.closed", "discussion", discussion_id,
         project_id=row["project_id"], goal_id=row["goal_id"], task_id=row["task_id"],
         actor="user", detail={})
    conn.commit()
    return {"ok": True}


# ---------- Messages -------------------------------------------------------

@router.post("/discussions/{discussion_id}/messages", status_code=201)
def post_user_message(discussion_id: str, body: dict, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM discussions WHERE id = ?", (discussion_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    if row["status"] == "closed":
        raise HTTPException(409, detail={"error": {"code": "discussion_closed"}})
    content = (body or {}).get("content_md", "").strip()
    if not content:
        raise HTTPException(400, detail={"error": {"code": "content_required"}})

    mid = _post_message(conn, discussion_id, "user", content)
    # Enqueue a discuss task if there isn't already one in flight for this discussion
    existing = conn.execute(
        """
        SELECT 1 FROM tasks
        WHERE type = 'discuss'
          AND status IN ('ready','in_progress')
          AND payload LIKE ?
        LIMIT 1
        """,
        (f'%"discussion_id": "{discussion_id}"%',),
    ).fetchone()
    if not existing:
        _enqueue_discuss_task(conn, discussion_id, row["project_id"], row["goal_id"],
                              row["task_id"], row["title"])

    conn.commit()
    return {"id": mid}


# ---------- Agent endpoint for posting agent replies ------------------------

@router.post("/discussions/{discussion_id}/agent-message", status_code=201)
def post_agent_message(discussion_id: str, body: dict, conn=Depends(db_dep)):
    """Called by the agent's discuss handler. Adds the agent's reply and any
    proposed actions atomically."""
    row = conn.execute("SELECT * FROM discussions WHERE id = ?", (discussion_id,)).fetchone()
    if not row:
        raise HTTPException(404)

    content = (body or {}).get("content_md", "").strip()
    if not content:
        raise HTTPException(400, detail={"error": {"code": "content_required"}})
    mid = _post_message(conn, discussion_id, "agent", content)

    actions = (body or {}).get("proposed_actions") or []
    inserted: list[str] = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        aid = new_id()
        conn.execute(
            """
            INSERT INTO proposed_actions
                (id, discussion_id, message_id, action_type, payload,
                 human_summary, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?)
            """,
            (aid, discussion_id, mid,
             a.get("action_type", "create_task"),
             json_dumps(a.get("payload") or {}),
             a.get("human_summary") or "",
             utcnow_iso()),
        )
        emit(conn, "proposed_action.added", "proposed_action", aid,
             project_id=row["project_id"], goal_id=row["goal_id"], task_id=row["task_id"],
             actor="agent",
             detail={"action_type": a.get("action_type"),
                     "human_summary": a.get("human_summary")})
        inserted.append(aid)

    conn.commit()
    return {"message_id": mid, "proposed_action_ids": inserted}


# ---------- Proposed actions ------------------------------------------------

proposed_router = APIRouter(prefix="/proposed-actions", tags=["proposed_actions"])


@proposed_router.get("")
def list_proposed(conn=Depends(db_dep)):
    rows = conn.execute(
        "SELECT * FROM proposed_actions WHERE status='proposed' ORDER BY created_at ASC"
    ).fetchall()
    return {"items": [_row_to_proposed(r) for r in rows]}


@proposed_router.post("/{action_id}/apply")
def apply_action(action_id: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM proposed_actions WHERE id = ?", (action_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    if row["status"] != "proposed":
        raise HTTPException(409, detail={"error": {"code": "not_proposed"}})
    disc = conn.execute("SELECT * FROM discussions WHERE id = ?", (row["discussion_id"],)).fetchone()
    payload = json_loads(row["payload"], {})
    action_type = row["action_type"]
    now = utcnow_iso()
    side_effects: list[dict] = []

    try:
        if action_type == "create_task":
            tid = new_id()
            conn.execute(
                """
                INSERT INTO tasks (id, project_id, goal_id, type, title, description_md,
                                   status, priority, depends_on, acceptance_criteria,
                                   payload, attempt_count, max_attempts, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, '[]', ?, ?, 0, 3, ?)
                """,
                (tid, disc["project_id"], payload.get("goal_id") or disc["goal_id"],
                 payload.get("type", "implement"),
                 payload.get("title", "Untitled"),
                 payload.get("description_md", ""),
                 payload.get("priority", "normal"),
                 json_dumps(payload.get("acceptance_criteria") or []),
                 json_dumps(payload.get("payload") or {}),
                 now),
            )
            emit(conn, "task.created", "task", tid,
                 project_id=disc["project_id"], goal_id=disc["goal_id"], task_id=tid,
                 actor="user", detail={"from_proposed_action": action_id,
                                       "title": payload.get("title")})
            side_effects.append({"kind": "task.created", "id": tid})

        elif action_type == "modify_task":
            task_id = payload.get("task_id")
            changes = payload.get("changes") or {}
            if not task_id:
                raise HTTPException(400, detail={"error": {"code": "missing_task_id"}})
            allowed = {"title", "description_md", "priority", "max_attempts"}
            sets, params = [], []
            for k, v in changes.items():
                if k in allowed:
                    sets.append(f"{k} = ?"); params.append(v)
                elif k == "acceptance_criteria":
                    sets.append("acceptance_criteria = ?"); params.append(json_dumps(v))
            if sets:
                params.append(task_id)
                conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)
                emit(conn, "task.updated", "task", task_id,
                     project_id=disc["project_id"], goal_id=disc["goal_id"], task_id=task_id,
                     actor="user", detail={"from_proposed_action": action_id,
                                           "changed": list(changes.keys())})
                side_effects.append({"kind": "task.modified", "id": task_id})

        elif action_type == "cancel_task":
            task_id = payload.get("task_id")
            if not task_id:
                raise HTTPException(400, detail={"error": {"code": "missing_task_id"}})
            conn.execute(
                "UPDATE tasks SET status='cancelled', finished_at=? "
                "WHERE id = ? AND status NOT IN ('done','failed','cancelled')",
                (now, task_id),
            )
            emit(conn, "task.cancelled", "task", task_id,
                 project_id=disc["project_id"], goal_id=disc["goal_id"], task_id=task_id,
                 actor="user", detail={"from_proposed_action": action_id})
            side_effects.append({"kind": "task.cancelled", "id": task_id})

        else:
            # reorder_dependencies / edit_plan: stub for v1
            raise HTTPException(501, detail={"error": {
                "code": "not_implemented",
                "message": f"action_type '{action_type}' not yet implemented",
            }})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail={"error": {"code": "apply_failed", "message": str(e)}})

    conn.execute(
        "UPDATE proposed_actions SET status='applied', applied_at=?, applied_by='user' WHERE id = ?",
        (now, action_id),
    )
    emit(conn, "proposed_action.applied", "proposed_action", action_id,
         project_id=disc["project_id"], goal_id=disc["goal_id"],
         actor="user", detail={"side_effects": side_effects})
    conn.commit()
    return {"ok": True, "applied_at": now, "side_effects": side_effects}


@proposed_router.post("/{action_id}/reject")
def reject_action(action_id: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM proposed_actions WHERE id = ?", (action_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    conn.execute(
        "UPDATE proposed_actions SET status='rejected' WHERE id = ?",
        (action_id,),
    )
    emit(conn, "proposed_action.rejected", "proposed_action", action_id,
         actor="user", detail={})
    conn.commit()
    return {"ok": True}


# Register proposed router under the same prefix family
router.include_router(proposed_router)


# ---------- Helpers --------------------------------------------------------

def _post_message(conn, discussion_id: str, role: str, content_md: str) -> str:
    mid = new_id()
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO messages (id, discussion_id, role, content_md, meta, created_at)
        VALUES (?, ?, ?, ?, '{}', ?)
        """,
        (mid, discussion_id, role, content_md, now),
    )
    disc = conn.execute("SELECT project_id, goal_id, task_id FROM discussions WHERE id = ?",
                        (discussion_id,)).fetchone()
    emit(conn, "discussion.message", "discussion", discussion_id,
         project_id=disc["project_id"] if disc else None,
         goal_id=disc["goal_id"] if disc else None,
         task_id=disc["task_id"] if disc else None,
         actor=role,
         detail={"role": role, "chars": len(content_md)})
    return mid


def _enqueue_discuss_task(conn, discussion_id: str, project_id: Optional[str],
                          goal_id: Optional[str], task_id: Optional[str],
                          discussion_title: str) -> str:
    tid = new_id()
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO tasks (id, project_id, goal_id, type, title, description_md,
                           status, priority, depends_on, acceptance_criteria,
                           payload, attempt_count, max_attempts, created_at)
        VALUES (?, ?, ?, 'discuss', ?, ?, 'ready', 'critical', '[]', '[]', ?, 0, 3, ?)
        """,
        (tid, project_id or "", goal_id,
         f"Discuss: {discussion_title}",
         f"Respond to the latest message in discussion {discussion_id}.",
         json_dumps({"discussion_id": discussion_id, "linked_task_id": task_id}),
         now),
    )
    emit(conn, "task.created", "task", tid,
         project_id=project_id, goal_id=goal_id, task_id=tid, actor="user",
         detail={"type": "discuss", "discussion_id": discussion_id})
    return tid
