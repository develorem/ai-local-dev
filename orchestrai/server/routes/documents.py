"""Project documents — context for a project (human- and agent-readable)."""

import json

from fastapi import APIRouter, Depends, HTTPException, Request

from server.db.connection import db_dep
from server.events import emit
from server.services import access
from server.services.doc_index import extract_headings, enqueue_reindex_if_needed
from server.util import new_id, utcnow_iso

router = APIRouter(prefix="/documents", tags=["documents"])


def _row(r) -> dict:
    keys = r.keys()
    return {"id": r["id"], "project_id": r["project_id"], "title": r["title"],
            "content_md": r["content_md"], "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "source": r["source"] if "source" in keys else "manual",
            "repo_path": r["repo_path"] if "repo_path" in keys else None,
            "headings": json.loads(r["headings"]) if "headings" in keys and r["headings"] else [],
            "purpose": r["purpose"] if "purpose" in keys else ""}


@router.get("")
def list_documents(project_id: str, request: Request, conn=Depends(db_dep)):
    access.assert_project(request, conn, project_id)
    rows = conn.execute(
        "SELECT * FROM project_documents WHERE project_id = ? ORDER BY updated_at DESC",
        (project_id,)).fetchall()
    return {"items": [_row(r) for r in rows]}


@router.post("", status_code=201)
def create_document(body: dict, request: Request, conn=Depends(db_dep)):
    pid = (body or {}).get("project_id")
    title = (body or {}).get("title", "").strip()
    if not pid or not conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone():
        raise HTTPException(404, detail={"error": {"code": "project_not_found"}})
    access.assert_project(request, conn, pid)
    if not title:
        raise HTTPException(400, detail={"error": {"code": "title_required"}})
    did = new_id()
    now = utcnow_iso()
    content = (body or {}).get("content_md", "")
    headings = json.dumps(extract_headings(content))
    conn.execute(
        "INSERT INTO project_documents (id, project_id, title, content_md, "
        "headings, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (did, pid, title, content, headings, now, now))
    emit(conn, "document.created", "project", pid, project_id=pid, actor="user",
         detail={"document_id": did, "title": title})
    # New doc has no purpose yet → queue the model to write its index entry.
    enqueue_reindex_if_needed(conn, pid)
    conn.commit()
    return _row(conn.execute("SELECT * FROM project_documents WHERE id = ?", (did,)).fetchone())


@router.get("/{document_id}")
def get_document(document_id: str, request: Request, conn=Depends(db_dep)):
    access.assert_document(request, conn, document_id)
    r = conn.execute("SELECT * FROM project_documents WHERE id = ?", (document_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    return _row(r)


@router.patch("/{document_id}")
def update_document(document_id: str, body: dict, request: Request, conn=Depends(db_dep)):
    access.assert_document(request, conn, document_id)
    r = conn.execute("SELECT * FROM project_documents WHERE id = ?", (document_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    fields, params = [], []
    for f in ("title", "content_md"):
        if (body or {}).get(f) is not None:
            fields.append(f"{f} = ?"); params.append(body[f])
    if fields:
        # Recompute the mechanical headings from whatever content this update
        # lands on (cheap, no model). The purpose is left alone — it's stale now,
        # which enqueue_reindex_if_needed detects via the signature hash.
        new_content = body["content_md"] if (body or {}).get("content_md") is not None else r["content_md"]
        fields.append("headings = ?"); params.append(json.dumps(extract_headings(new_content)))
        fields.append("updated_at = ?"); params.append(utcnow_iso())
        params.append(document_id)
        conn.execute(f"UPDATE project_documents SET {', '.join(fields)} WHERE id = ?", params)
        emit(conn, "document.updated", "project", r["project_id"],
             project_id=r["project_id"], actor="user", detail={"document_id": document_id})
        enqueue_reindex_if_needed(conn, r["project_id"])
        conn.commit()
    return _row(conn.execute("SELECT * FROM project_documents WHERE id = ?", (document_id,)).fetchone())


@router.delete("/{document_id}")
def delete_document(document_id: str, request: Request, conn=Depends(db_dep)):
    access.assert_document(request, conn, document_id)
    r = conn.execute("SELECT * FROM project_documents WHERE id = ?", (document_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    conn.execute("DELETE FROM project_documents WHERE id = ?", (document_id,))
    emit(conn, "document.deleted", "project", r["project_id"],
         project_id=r["project_id"], actor="user", detail={"document_id": document_id})
    conn.commit()
    return {"ok": True}
