"""Preview servers — launch a project's app on an agent port and link to it.

The operator clicks "Launch app"; we pick a free port from the agents' published
ports, record a preview_servers row, and queue a 'preview' task. The agent runs
the repo's start_command via orchestrai-serve (leaving it running) and reports
back, flipping the row to 'running'. The UI then shows a clickable link built
from the browser's own host + that port. Stop queues a teardown task.
"""

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from server.db.connection import db_dep
from server.events import emit
from server.util import new_id, utcnow_iso, json_dumps, json_loads

router = APIRouter(tags=["previews"])

_PORT_CAP_RE = re.compile(r"^port:(\d+):http$")


def _row(r) -> dict:
    return {"id": r["id"], "project_id": r["project_id"], "repo_id": r["repo_id"],
            "port": r["port"], "command": r["command"], "status": r["status"],
            "agent_id": r["agent_id"], "task_id": r["task_id"],
            "detail": r["detail"], "started_at": r["started_at"],
            "last_seen_at": r["last_seen_at"]}


def _available_port(conn) -> Optional[int]:
    """A port advertised by some live agent and not already held by a
    starting/running preview."""
    used = {r["port"] for r in conn.execute(
        "SELECT port FROM preview_servers WHERE status IN ('starting','running')")}
    ports: set[int] = set()
    for a in conn.execute(
        "SELECT capabilities FROM agents WHERE status IN ('idle','busy','connected')"):
        for cap in json_loads(a["capabilities"], []):
            m = _PORT_CAP_RE.match(cap or "")
            if m:
                ports.add(int(m.group(1)))
    for p in sorted(ports):
        if p not in used:
            return p
    return None


@router.get("/projects/{project_id}/previews")
def list_previews(project_id: str, conn=Depends(db_dep)):
    rows = conn.execute(
        "SELECT * FROM preview_servers WHERE project_id = ? "
        "ORDER BY started_at DESC LIMIT 20", (project_id,)).fetchall()
    return {"items": [_row(r) for r in rows]}


@router.post("/projects/{project_id}/previews/launch", status_code=201)
def launch_preview(project_id: str, conn=Depends(db_dep)):
    if not conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone():
        raise HTTPException(404, detail={"error": {"code": "project_not_found"}})
    # The primary repo that has a start command configured.
    repo = conn.execute(
        "SELECT id, start_command FROM project_repos WHERE project_id = ? "
        "AND start_command IS NOT NULL AND start_command != '' "
        "ORDER BY created_at LIMIT 1", (project_id,)).fetchone()
    if not repo:
        raise HTTPException(400, detail={"error": {"code": "no_start_command",
            "message": "Set a start command on the project's repo first."}})

    port = _available_port(conn)
    if port is None:
        raise HTTPException(409, detail={"error": {"code": "no_free_port",
            "message": "No agent port available (all in use or no agent online)."}})

    now = utcnow_iso()
    pid = new_id()
    conn.execute(
        "INSERT INTO preview_servers (id, project_id, repo_id, port, command, "
        "status, started_at) VALUES (?, ?, ?, ?, ?, 'starting', ?)",
        (pid, project_id, repo["id"], port, repo["start_command"], now))

    tid = new_id()
    conn.execute(
        """
        INSERT INTO tasks (id, project_id, repo_id, type, title, description_md,
                           status, priority, depends_on, acceptance_criteria,
                           payload, attempt_count, max_attempts, created_at)
        VALUES (?, ?, ?, 'preview', ?, ?, 'ready', 'high', '[]', '[]', ?, 0, 1, ?)
        """,
        (tid, project_id, repo["id"], f"Launch app on port {port}",
         "Start the project's app for preview.",
         json_dumps({"action": "start", "port": port,
                     "command": repo["start_command"], "preview_id": pid}), now))
    conn.execute("UPDATE preview_servers SET task_id = ? WHERE id = ?", (tid, pid))
    emit(conn, "preview.launch_requested", "project", project_id,
         project_id=project_id, task_id=tid, actor="user",
         detail={"port": port, "preview_id": pid})
    conn.commit()
    return _row(conn.execute("SELECT * FROM preview_servers WHERE id = ?", (pid,)).fetchone())


@router.post("/previews/{preview_id}/stop")
def stop_preview(preview_id: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM preview_servers WHERE id = ?", (preview_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    now = utcnow_iso()
    # Mark stopped immediately (link disappears) and queue the actual teardown.
    conn.execute("UPDATE preview_servers SET status = 'stopped', last_seen_at = ? "
                 "WHERE id = ?", (now, preview_id))
    tid = new_id()
    conn.execute(
        """
        INSERT INTO tasks (id, project_id, repo_id, type, title, description_md,
                           status, priority, depends_on, acceptance_criteria,
                           payload, attempt_count, max_attempts, created_at)
        VALUES (?, ?, ?, 'preview', ?, ?, 'ready', 'high', '[]', '[]', ?, 0, 1, ?)
        """,
        (tid, row["project_id"], row["repo_id"], f"Stop app on port {row['port']}",
         "Stop the project's preview app.",
         json_dumps({"action": "stop", "port": row["port"],
                     "preview_id": preview_id}), now))
    emit(conn, "preview.stop_requested", "project", row["project_id"],
         project_id=row["project_id"], task_id=tid, actor="user",
         detail={"port": row["port"], "preview_id": preview_id})
    conn.commit()
    return {"ok": True}
