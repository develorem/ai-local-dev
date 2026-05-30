"""Scheduled tasks — cron specs that materialise tasks into the backlog."""

from fastapi import APIRouter, Depends, HTTPException, Request

from server.db.connection import db_dep
from server.events import emit
from server.scheduler import next_run
from server.services import access
from server.util import new_id, utcnow_iso, json_dumps, json_loads

router = APIRouter(prefix="/scheduled", tags=["scheduled"])

_PRIORITIES = {"low", "normal", "high", "critical"}
_TASK_TYPES = {"plan", "implement", "review"}


def _row(r) -> dict:
    return {
        "id": r["id"], "project_id": r["project_id"], "name": r["name"],
        "cron": r["cron"], "task_type": r["task_type"], "title": r["title"],
        "description_md": r["description_md"], "priority": r["priority"],
        "acceptance_criteria": json_loads(r["acceptance_criteria"], []),
        "enabled": bool(r["enabled"]), "last_run_at": r["last_run_at"],
        "next_run_at": r["next_run_at"], "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


@router.get("")
def list_scheduled(project_id: str, request: Request, conn=Depends(db_dep)):
    access.assert_project(request, conn, project_id)
    rows = conn.execute(
        "SELECT * FROM scheduled_tasks WHERE project_id = ? ORDER BY created_at DESC",
        (project_id,)).fetchall()
    return {"items": [_row(r) for r in rows]}


@router.post("", status_code=201)
def create_scheduled(body: dict, request: Request, conn=Depends(db_dep)):
    b = body or {}
    pid = b.get("project_id")
    if not pid or not conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone():
        raise HTTPException(404, detail={"error": {"code": "project_not_found"}})
    access.assert_project(request, conn, pid)
    cron = (b.get("cron") or "").strip()
    title = (b.get("title") or "").strip()
    name = (b.get("name") or title).strip()
    if not cron or not title:
        raise HTTPException(400, detail={"error": {"code": "cron_and_title_required"}})
    try:
        nxt = next_run(cron)
    except Exception:
        raise HTTPException(400, detail={"error": {"code": "bad_cron",
                            "message": "Could not parse cron expression."}})
    task_type = b.get("task_type", "implement")
    if task_type not in _TASK_TYPES:
        task_type = "implement"
    priority = b.get("priority", "normal")
    if priority not in _PRIORITIES:
        priority = "normal"
    sid = new_id()
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO scheduled_tasks (id, project_id, name, cron, task_type, title,
                                     description_md, priority, acceptance_criteria,
                                     enabled, next_run_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (sid, pid, name, cron, task_type, title, b.get("description_md", ""),
         priority, json_dumps(b.get("acceptance_criteria", [])),
         1 if b.get("enabled", True) else 0, nxt, now, now))
    emit(conn, "scheduled_task.created", "project", pid, project_id=pid, actor="user",
         detail={"scheduled_task_id": sid, "name": name, "cron": cron})
    conn.commit()
    return _row(conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (sid,)).fetchone())


@router.patch("/{scheduled_id}")
def update_scheduled(scheduled_id: str, body: dict, request: Request, conn=Depends(db_dep)):
    r = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (scheduled_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    access.assert_scheduled(request, conn, scheduled_id)
    b = body or {}
    fields, params = [], []
    for f in ("name", "title", "description_md", "task_type", "priority", "cron"):
        if b.get(f) is not None:
            fields.append(f"{f} = ?"); params.append(b[f])
    if "acceptance_criteria" in b:
        fields.append("acceptance_criteria = ?"); params.append(json_dumps(b["acceptance_criteria"]))
    if "enabled" in b:
        fields.append("enabled = ?"); params.append(1 if b["enabled"] else 0)
    if b.get("cron"):
        try:
            fields.append("next_run_at = ?"); params.append(next_run(b["cron"]))
        except Exception:
            raise HTTPException(400, detail={"error": {"code": "bad_cron"}})
    if fields:
        fields.append("updated_at = ?"); params.append(utcnow_iso())
        params.append(scheduled_id)
        conn.execute(f"UPDATE scheduled_tasks SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
    return _row(conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (scheduled_id,)).fetchone())


@router.delete("/{scheduled_id}")
def delete_scheduled(scheduled_id: str, request: Request, conn=Depends(db_dep)):
    r = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (scheduled_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    access.assert_scheduled(request, conn, scheduled_id)
    conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (scheduled_id,))
    emit(conn, "scheduled_task.deleted", "project", r["project_id"],
         project_id=r["project_id"], actor="user", detail={"scheduled_task_id": scheduled_id})
    conn.commit()
    return {"ok": True}


@router.post("/{scheduled_id}/run-now", status_code=201)
def run_now(scheduled_id: str, request: Request, conn=Depends(db_dep)):
    s = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (scheduled_id,)).fetchone()
    if not s:
        raise HTTPException(404)
    access.assert_scheduled(request, conn, scheduled_id)
    tid = new_id()
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO tasks (id, project_id, type, title, description_md, status,
                           priority, depends_on, acceptance_criteria, payload,
                           attempt_count, max_attempts, created_at)
        VALUES (?, ?, ?, ?, ?, 'ready', ?, '[]', ?, ?, 0, 3, ?)
        """,
        (tid, s["project_id"], s["task_type"], s["title"], s["description_md"],
         s["priority"], s["acceptance_criteria"],
         json_dumps({"scheduled_task_id": s["id"], "manual": True}), now))
    emit(conn, "task.created", "task", tid, project_id=s["project_id"], task_id=tid,
         actor="user", detail={"title": s["title"], "type": s["task_type"], "source": "scheduled"})
    conn.commit()
    return {"ok": True, "task_id": tid}
