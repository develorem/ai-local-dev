"""Agent registration, heartbeat, claim, release.

The claim endpoint runs the single atomic UPDATE…RETURNING that arbitrates
task pickup. See SCHEMA.md / docs/API.md for the full contract.
"""

import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException

from server.config import config
from server.db.connection import db_dep
from server.events import emit
from server.models import (
    AgentRegister, AgentRegisterResponse,
)
from server.routes.tasks import row_to_task
from server.util import new_id, utcnow_iso, json_dumps, json_loads

router = APIRouter(prefix="/agents", tags=["agents"])


def _auth(authorization: Optional[str], agent_id: str, conn) -> dict:
    """Validate the Bearer token matches this agent's lease_token."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, detail={"error": {"code": "missing_token"}})
    token = authorization.split(None, 1)[1].strip()
    row = conn.execute(
        "SELECT * FROM agents WHERE id = ? AND lease_token = ?",
        (agent_id, token),
    ).fetchone()
    if not row:
        raise HTTPException(401, detail={"error": {"code": "invalid_lease"}})
    return dict(row)


def _row_to_agent(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "host": row["host"],
        "version": row["version"],
        "kind": row["kind"] if "kind" in row.keys() else "worker",
        "capabilities": json_loads(row["capabilities"], []),
        "status": row["status"],
        "last_heartbeat_at": row["last_heartbeat_at"],
        "current_task_id": row["current_task_id"],
        "registered_at": row["registered_at"],
        "released_at": row["released_at"],
    }


@router.post("/register", response_model=AgentRegisterResponse)
def register(body: AgentRegister, conn=Depends(db_dep)):
    aid = new_id()
    token = secrets.token_urlsafe(48)
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO agents (id, name, host, version, kind, capabilities,
                            status, lease_token, last_heartbeat_at, registered_at)
        VALUES (?, ?, ?, ?, ?, ?, 'idle', ?, ?, ?)
        """,
        (aid, body.name, body.host, body.version, body.kind,
         json_dumps(body.capabilities), token, now, now),
    )
    emit(conn, "agent.registered", "agent", aid,
         agent_id=aid, actor="agent:" + aid,
         detail={"name": body.name, "host": body.host, "version": body.version,
                 "kind": body.kind})
    conn.commit()
    return AgentRegisterResponse(
        agent_id=aid,
        lease_token=token,
        hub_version=config.VERSION,
        heartbeat_interval_sec=config.AGENT_HEARTBEAT_INTERVAL_SEC,
        lease_timeout_sec=config.AGENT_LEASE_TIMEOUT_SEC,
    )


@router.post("/{agent_id}/heartbeat")
def heartbeat(agent_id: str, body: dict = None,
              authorization: Optional[str] = Header(default=None),
              conn=Depends(db_dep)):
    agent = _auth(authorization, agent_id, conn)
    body = body or {}
    current_task_id = body.get("current_task_id")
    now = utcnow_iso()

    # Update agent heartbeat
    conn.execute(
        "UPDATE agents SET last_heartbeat_at = ?, current_task_id = ?, "
        "                  status = CASE WHEN ? IS NOT NULL THEN 'busy' "
        "                                ELSE 'idle' END WHERE id = ?",
        (now, current_task_id, current_task_id, agent_id),
    )

    # If holding a task, extend its lease.
    if current_task_id:
        conn.execute(
            f"""
            UPDATE tasks
            SET lease_expires_at = datetime('now', '+{config.AGENT_LEASE_TIMEOUT_SEC} seconds')
            WHERE id = ? AND assigned_agent_id = ?
            """,
            (current_task_id, agent_id),
        )
    conn.commit()
    return {"ok": True}


@router.post("/{agent_id}/claim")
def claim(agent_id: str,
          authorization: Optional[str] = Header(default=None),
          conn=Depends(db_dep)):
    agent = _auth(authorization, agent_id, conn)
    now = utcnow_iso()

    # Atomic claim: pick highest-priority ready task whose deps are all done
    # AND no other agent is on the same (repo, branch).
    row = conn.execute(
        f"""
        UPDATE tasks
        SET assigned_agent_id = ?,
            status            = 'in_progress',
            started_at        = COALESCE(started_at, ?),
            lease_expires_at  = datetime('now', '+{config.AGENT_LEASE_TIMEOUT_SEC} seconds'),
            attempt_count     = attempt_count + 1
        WHERE id = (
          SELECT t.id FROM tasks t
          WHERE t.status = 'ready'
            AND t.assigned_agent_id IS NULL
            -- Claim only tasks whose project grants this agent access — by its
            -- id, or by its kind (e.g. all 'worker' instances). No grant => the
            -- worker leaves the project alone (the default for a new project).
            AND EXISTS (
              SELECT 1 FROM project_agents pa
              WHERE pa.project_id = t.project_id
                AND ((pa.grantee_type = 'agent' AND pa.grantee = ?)
                  OR (pa.grantee_type = 'kind'  AND pa.grantee =
                       (SELECT a.kind FROM agents a WHERE a.id = ?)))
            )
            AND NOT EXISTS (
              SELECT 1 FROM json_each(t.depends_on) AS dep
              JOIN tasks dt ON dt.id = dep.value
              WHERE dt.status != 'done'
            )
            AND (
              t.branch_name IS NULL
              OR NOT EXISTS (
                SELECT 1 FROM tasks ot
                WHERE ot.status = 'in_progress'
                  AND ot.repo_id = t.repo_id
                  AND ot.branch_name = t.branch_name
                  AND ot.id != t.id
              )
            )
          ORDER BY
            CASE t.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                            WHEN 'normal' THEN 2 ELSE 3 END,
            t.created_at ASC
          LIMIT 1
        )
        RETURNING *
        """,
        (agent_id, now, agent_id, agent_id),
    ).fetchone()

    if not row:
        conn.commit()
        return {"task": None}

    # Update agent state to busy
    conn.execute(
        "UPDATE agents SET status='busy', current_task_id=?, last_heartbeat_at=? WHERE id = ?",
        (row["id"], now, agent_id),
    )

    task = row_to_task(row)

    # Bundle project + repo context
    project = conn.execute(
        "SELECT id, name, slug, description_md, context_md, tools FROM projects WHERE id = ?",
        (row["project_id"],),
    ).fetchone()
    project_dict = dict(project) if project else None
    if project_dict and "tools" in project_dict:
        try:
            project_dict["tools"] = json_loads(project_dict["tools"], {})
        except Exception:
            project_dict["tools"] = {}

    repo = None
    if row["repo_id"]:
        repo_row = conn.execute(
            "SELECT id, name, role, url, default_branch, description_md "
            "FROM project_repos WHERE id = ?",
            (row["repo_id"],),
        ).fetchone()
        repo = dict(repo_row) if repo_row else None

    emit(conn, "task.claimed", "task", row["id"],
         project_id=row["project_id"], goal_id=row["goal_id"],
         task_id=row["id"], agent_id=agent_id, actor=f"agent:{agent_id}",
         detail={"branch": row["branch_name"]})
    conn.commit()

    return {
        "task": task,
        "project": project_dict,
        "repo": repo,
        "branch_name": row["branch_name"],
        "lease_expires_at": row["lease_expires_at"],
    }


@router.post("/{agent_id}/release")
def release(agent_id: str, body: dict = None,
            authorization: Optional[str] = Header(default=None),
            conn=Depends(db_dep)):
    agent = _auth(authorization, agent_id, conn)
    body = body or {}
    release_task = body.get("release_task", True)
    now = utcnow_iso()

    if release_task and agent["current_task_id"]:
        conn.execute(
            """
            UPDATE tasks
            SET status='ready', assigned_agent_id=NULL, lease_expires_at=NULL,
                notes = notes || char(10) || ?
            WHERE id = ? AND assigned_agent_id = ?
            """,
            (f"[{now}] released by agent (graceful)", agent["current_task_id"], agent_id),
        )

    conn.execute(
        "UPDATE agents SET status='released', released_at=?, current_task_id=NULL WHERE id = ?",
        (now, agent_id),
    )
    emit(conn, "agent.released", "agent", agent_id,
         agent_id=agent_id, actor=f"agent:{agent_id}",
         detail={"released_task": agent["current_task_id"] if release_task else None})
    conn.commit()
    return {"ok": True}


@router.get("")
def list_agents(conn=Depends(db_dep)):
    rows = conn.execute(
        """
        SELECT a.*, t.title AS _current_task_title, t.type AS _current_task_type
        FROM agents a
        LEFT JOIN tasks t ON t.id = a.current_task_id
        ORDER BY
          CASE a.status WHEN 'busy' THEN 0 WHEN 'idle' THEN 1 WHEN 'connected' THEN 2
                        WHEN 'lost' THEN 3 ELSE 4 END, a.registered_at DESC
        """
    ).fetchall()
    items = []
    for r in rows:
        d = _row_to_agent(r)
        d["current_task_title"] = r["_current_task_title"]
        d["current_task_type"] = r["_current_task_type"]
        items.append(d)
    return {"items": items}


@router.get("/{agent_id}")
def get_agent(agent_id: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        raise HTTPException(404)

    current_task = None
    if row["current_task_id"]:
        ct = conn.execute(
            "SELECT id, title, type, status, branch_name, project_id, goal_id, "
            "       started_at, lease_expires_at "
            "FROM tasks WHERE id = ?",
            (row["current_task_id"],),
        ).fetchone()
        if ct:
            current_task = dict(ct)

    recent = [dict(r) for r in conn.execute(
        "SELECT id, title, type, status, branch_name, finished_at "
        "FROM tasks WHERE assigned_agent_id = ? ORDER BY finished_at DESC NULLS LAST, "
        "started_at DESC LIMIT 5",
        (agent_id,),
    ).fetchall()]

    history = [dict(r) for r in conn.execute(
        "SELECT id, ts, kind, detail FROM events WHERE agent_id = ? "
        "ORDER BY ts DESC LIMIT 50",
        (agent_id,),
    ).fetchall()]
    for h in history:
        h["detail"] = json_loads(h.get("detail"), {})

    return {
        "agent": _row_to_agent(row),
        "current_task": current_task,
        "recent_tasks": recent,
        "recent_events": history,
    }


@router.get("/{agent_id}/config")
def agent_config(agent_id: str, conn=Depends(db_dep)):
    """Return an agent's connection token so the UI can (re)build its mcp.json.
    Local single-user tool — the token is shown to the operator on purpose."""
    r = conn.execute("SELECT id, name, lease_token FROM agents WHERE id = ?",
                     (agent_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    return {"agent_id": r["id"], "name": r["name"], "token": r["lease_token"]}


@router.get("/{agent_id}/projects")
def agent_projects(agent_id: str, conn=Depends(db_dep)):
    """Projects this agent may act on — granted directly (by id) or via its kind."""
    a = conn.execute("SELECT kind FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not a:
        raise HTTPException(404)
    rows = conn.execute(
        """SELECT p.id, p.name, p.slug, pa.grantee_type
           FROM project_agents pa JOIN projects p ON p.id = pa.project_id
           WHERE (pa.grantee_type = 'agent' AND pa.grantee = ?)
              OR (pa.grantee_type = 'kind'  AND pa.grantee = ?)
           ORDER BY p.name""", (agent_id, a["kind"])).fetchall()
    return {"items": [{"id": r["id"], "name": r["name"], "slug": r["slug"],
                       "via": r["grantee_type"]} for r in rows]}


@router.delete("/{agent_id}")
def delete_agent(agent_id: str, conn=Depends(db_dep)):
    """Remove an agent and any access grants made to it specifically."""
    if not conn.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,)).fetchone():
        raise HTTPException(404)
    conn.execute("DELETE FROM project_agents WHERE grantee_type = 'agent' AND grantee = ?",
                 (agent_id,))
    # Emit before deleting the agent: the event FKs agents(id) (ON DELETE SET
    # NULL), so it must reference an existing row, then null out on the delete.
    emit(conn, "agent.deleted", "agent", agent_id, agent_id=agent_id, actor="user", detail={})
    conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    conn.commit()
    return {"ok": True}
