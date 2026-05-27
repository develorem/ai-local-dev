"""Webhooks: minimal GitHub-style ingest that turns events into tasks.

v1 supports two event shapes (mapped by the body's `event` field):
  - "pull_request": creates a `review_pr` task
  - "workflow_run": if conclusion=failure, creates a `respond_to_ci_failure` task

Authentication for v1 is a shared secret in the URL: /api/webhooks/{project_id}/{secret}
where {secret} is `WEBHOOK_SECRET_<project_id>` stored in the vault. No HMAC
signature validation in v1 — that's a v2 hardening.
"""

import json as _json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from server.db.connection import db_dep
from server.events import emit
from server.services.crypto import decrypt
from server.util import new_id, utcnow_iso, json_dumps

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _verify_secret(conn, project_id: str, presented: str) -> bool:
    name = f"WEBHOOK_SECRET_{project_id}"
    row = conn.execute("SELECT ciphertext FROM secrets WHERE name = ?", (name,)).fetchone()
    if not row:
        return False
    try:
        expected = decrypt(row["ciphertext"])
    except Exception:
        return False
    return expected == presented


@router.post("/{project_id}/{secret}")
async def ingest(project_id: str, secret: str, request: Request, conn=Depends(db_dep)):
    proj = conn.execute("SELECT id, slug FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not proj:
        raise HTTPException(404, detail={"error": {"code": "project_not_found"}})

    if not _verify_secret(conn, project_id, secret):
        raise HTTPException(401, detail={"error": {"code": "bad_secret"}})

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, detail={"error": {"code": "invalid_json"}})

    event_kind = body.get("event") or request.headers.get("x-github-event") or ""
    now = utcnow_iso()
    tid: Optional[str] = None

    if event_kind == "pull_request":
        action = body.get("action") or "opened"
        if action not in ("opened", "synchronize", "reopened", "ready_for_review"):
            return {"ok": True, "ignored": True, "reason": f"action={action}"}
        pr = body.get("pull_request") or {}
        pr_url = pr.get("html_url") or body.get("pr_url")
        if not pr_url:
            raise HTTPException(400, detail={"error": {"code": "missing_pr_url"}})
        tid = _create_task(
            conn, project_id, type_="review_pr",
            title=f"Review PR: {pr.get('title') or pr_url}",
            description_md=f"Review PR opened by webhook.\n\nURL: {pr_url}",
            payload={"pr_url": pr_url, "secrets_needed": ["GITHUB_TOKEN"]},
            priority="normal",
        )

    elif event_kind == "workflow_run":
        run = body.get("workflow_run") or {}
        conclusion = run.get("conclusion") or body.get("conclusion")
        if conclusion != "failure":
            return {"ok": True, "ignored": True, "reason": f"conclusion={conclusion}"}
        log_tail = body.get("log_tail") or ""
        tid = _create_task(
            conn, project_id, type_="respond_to_ci_failure",
            title=f"CI fix: {run.get('name', 'unknown workflow')}",
            description_md=f"Build failed: {run.get('html_url') or '(no url)'}",
            payload={
                "workflow": run.get("name"),
                "step_name": run.get("step_name") or "(unknown)",
                "build_url": run.get("html_url"),
                "branch_name": run.get("head_branch"),
                "log_tail": log_tail,
                "secrets_needed": ["GITHUB_TOKEN"],
            },
            branch_name=run.get("head_branch"),
            priority="high",
        )

    else:
        return {"ok": True, "ignored": True, "reason": f"event={event_kind}"}

    emit(conn, "webhook.received", "project", project_id,
         project_id=project_id, actor="system",
         detail={"event": event_kind, "created_task": tid})
    conn.commit()
    return {"ok": True, "task_id": tid}


def _create_task(conn, project_id: str, *, type_: str, title: str,
                 description_md: str, payload: dict,
                 branch_name: Optional[str] = None,
                 priority: str = "normal") -> str:
    tid = new_id()
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO tasks (id, project_id, type, title, description_md,
                           status, priority, depends_on, acceptance_criteria,
                           payload, attempt_count, max_attempts, branch_name, created_at)
        VALUES (?, ?, ?, ?, ?, 'ready', ?, '[]', '[]', ?, 0, 3, ?, ?)
        """,
        (tid, project_id, type_, title, description_md, priority,
         json_dumps(payload), branch_name, now),
    )
    emit(conn, "task.created", "task", tid,
         project_id=project_id, task_id=tid, actor="webhook",
         detail={"title": title, "type": type_, "via": "webhook"})
    return tid
