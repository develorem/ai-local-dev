"""Secrets vault — encrypted-at-rest credentials.

UI endpoints manage names + metadata only; values are write-only.
Agent endpoint fetches decrypted values with full access-control + audit.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException

from server.db.connection import db_dep
from server.events import emit
from server.services.crypto import decrypt, encrypt
from server.util import new_id, utcnow_iso, json_loads

router = APIRouter(prefix="/secrets", tags=["secrets"])


def _row_to_metadata(row) -> dict:
    return {
        "name": row["name"],
        "description": row["description"],
        "scope": row["scope"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_accessed_at": row["last_accessed_at"],
        "access_count": row["access_count"],
    }


# ---------- UI endpoints ---------------------------------------------------

@router.get("")
def list_secrets(project_id: Optional[str] = None, conn=Depends(db_dep)):
    """All secrets, or — with project_id — the secrets visible to that project:
    global ones (inherited) + ones scoped to it. Each item is tagged `inherited`.
    """
    if project_id:
        rows = conn.execute(
            "SELECT * FROM secrets WHERE scope = 'global' OR scope = ? ORDER BY name",
            (f"project:{project_id}",)).fetchall()
        items = []
        for r in rows:
            m = _row_to_metadata(r)
            m["inherited"] = (r["scope"] == "global")
            items.append(m)
        return {"items": items}
    rows = conn.execute("SELECT * FROM secrets ORDER BY name").fetchall()
    return {"items": [_row_to_metadata(r) for r in rows]}


@router.post("", status_code=201)
def create_secret(body: dict, conn=Depends(db_dep)):
    body = body or {}
    name = (body.get("name") or "").strip()
    value = body.get("value")
    description = body.get("description") or ""
    scope = body.get("scope") or "global"
    if not name or value is None:
        raise HTTPException(400, detail={"error": {"code": "name_and_value_required"}})
    if not all(c.isalnum() or c == "_" for c in name):
        raise HTTPException(400, detail={"error": {"code": "bad_name",
                                                   "message": "use UPPER_SNAKE_CASE alphanumerics"}})

    now = utcnow_iso()
    try:
        ct = encrypt(str(value))
    except Exception as e:
        raise HTTPException(500, detail={"error": {"code": "encrypt_failed", "message": str(e)}})

    try:
        conn.execute(
            """
            INSERT INTO secrets (name, ciphertext, description, scope, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, ct, description, scope, now, now),
        )
    except Exception as e:
        if "UNIQUE" in str(e) or "PRIMARY KEY" in str(e).upper():
            raise HTTPException(409, detail={"error": {"code": "name_taken"}})
        raise

    emit(conn, "secret.created", "secret", name, actor="user",
         detail={"scope": scope})
    conn.commit()
    row = conn.execute("SELECT * FROM secrets WHERE name = ?", (name,)).fetchone()
    return _row_to_metadata(row)


@router.patch("/{name}")
def update_secret(name: str, body: dict, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM secrets WHERE name = ?", (name,)).fetchone()
    if not row:
        raise HTTPException(404)
    body = body or {}
    fields, params = [], []
    if "value" in body and body["value"] is not None:
        ct = encrypt(str(body["value"]))
        fields.append("ciphertext = ?"); params.append(ct)
    if "description" in body and body["description"] is not None:
        fields.append("description = ?"); params.append(body["description"])
    if "scope" in body and body["scope"] is not None:
        fields.append("scope = ?"); params.append(body["scope"])
    if not fields:
        return _row_to_metadata(row)
    fields.append("updated_at = ?"); params.append(utcnow_iso())
    params.append(name)
    conn.execute(f"UPDATE secrets SET {', '.join(fields)} WHERE name = ?", params)
    emit(conn, "secret.updated", "secret", name, actor="user",
         detail={"changed": [f.split(" = ")[0] for f in fields if " = ?" in f]})
    conn.commit()
    row = conn.execute("SELECT * FROM secrets WHERE name = ?", (name,)).fetchone()
    return _row_to_metadata(row)


@router.delete("/{name}")
def delete_secret(name: str, conn=Depends(db_dep)):
    row = conn.execute("SELECT * FROM secrets WHERE name = ?", (name,)).fetchone()
    if not row:
        raise HTTPException(404)
    conn.execute("DELETE FROM secrets WHERE name = ?", (name,))
    emit(conn, "secret.deleted", "secret", name, actor="user", detail={})
    conn.commit()
    return {"ok": True}


@router.get("/{name}/accesses")
def list_accesses(name: str, limit: int = 100, conn=Depends(db_dep)):
    limit = max(1, min(limit, 500))
    rows = conn.execute(
        """
        SELECT sa.*, a.name AS agent_name, t.title AS task_title
        FROM secret_accesses sa
        LEFT JOIN agents a ON a.id = sa.agent_id
        LEFT JOIN tasks t ON t.id = sa.task_id
        WHERE sa.secret_name = ?
        ORDER BY sa.ts DESC LIMIT ?
        """,
        (name, limit),
    ).fetchall()
    return {"items": [dict(r) for r in rows]}


# ---------- Agent endpoint --------------------------------------------------

def _auth_agent(authorization: Optional[str], conn) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, detail={"error": {"code": "missing_token"}})
    token = authorization.split(None, 1)[1].strip()
    row = conn.execute(
        "SELECT * FROM agents WHERE lease_token = ? AND status IN ('idle','busy','connected')",
        (token,),
    ).fetchone()
    if not row:
        raise HTTPException(401, detail={"error": {"code": "invalid_lease"}})
    return dict(row)


def _audit(conn, name, agent_id, task_id, result, reason=None) -> None:
    conn.execute(
        """
        INSERT INTO secret_accesses (id, secret_name, agent_id, task_id, ts, result, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (new_id(), name, agent_id, task_id, utcnow_iso(), result, reason),
    )


@router.get("/{name}/value")
def fetch_value(name: str,
                authorization: Optional[str] = Header(default=None),
                conn=Depends(db_dep)):
    """Agent-only. Requires:
      - valid Bearer lease_token
      - agent holds an in-progress task whose payload.secrets_needed includes this name
      - secret scope matches the task's project (or is global)
    """
    agent = _auth_agent(authorization, conn)
    secret = conn.execute("SELECT * FROM secrets WHERE name = ?", (name,)).fetchone()
    if not secret:
        _audit(conn, name, agent["id"], None, "denied", "not_found")
        conn.commit()
        raise HTTPException(404)

    current_task_id = agent.get("current_task_id")
    if not current_task_id:
        _audit(conn, name, agent["id"], None, "denied", "no_active_task")
        conn.commit()
        raise HTTPException(403, detail={"error": {"code": "no_active_task"}})

    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (current_task_id,)).fetchone()
    if not task or task["status"] != "in_progress":
        _audit(conn, name, agent["id"], current_task_id, "denied", "task_not_running")
        conn.commit()
        raise HTTPException(403, detail={"error": {"code": "task_not_running"}})

    # The task must declare this secret in payload.secrets_needed
    payload = json_loads(task["payload"], {})
    needed = payload.get("secrets_needed") or []
    if name not in needed:
        _audit(conn, name, agent["id"], current_task_id, "denied", "not_declared")
        conn.commit()
        raise HTTPException(403, detail={"error": {
            "code": "not_declared",
            "message": f"task.payload.secrets_needed does not include '{name}'",
        }})

    # Scope check
    scope = secret["scope"] or "global"
    if scope != "global":
        if scope.startswith("project:"):
            target = scope.split(":", 1)[1]
            if target != task["project_id"]:
                _audit(conn, name, agent["id"], current_task_id, "denied", "scope_mismatch")
                conn.commit()
                raise HTTPException(403, detail={"error": {"code": "scope_mismatch"}})
        # additional scope kinds can be added later

    # Issue the value
    try:
        value = decrypt(secret["ciphertext"])
    except Exception as e:
        _audit(conn, name, agent["id"], current_task_id, "denied", f"decrypt_failed: {e}")
        conn.commit()
        raise HTTPException(500, detail={"error": {"code": "decrypt_failed"}})

    conn.execute(
        "UPDATE secrets SET last_accessed_at = ?, access_count = access_count + 1 WHERE name = ?",
        (utcnow_iso(), name),
    )
    _audit(conn, name, agent["id"], current_task_id, "issued")
    emit(conn, "secret.accessed", "secret", name,
         project_id=task["project_id"], task_id=current_task_id, agent_id=agent["id"],
         actor=f"agent:{agent['id']}", detail={"task_id": current_task_id})
    conn.commit()

    return {"name": name, "value": value, "expires_in_sec": 60}
