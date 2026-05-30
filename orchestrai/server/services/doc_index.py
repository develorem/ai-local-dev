"""Document index helpers — the seek structure agents use to decide WHICH doc
to fetch and WHEN.

Two tiers of freshness:
  - headings: mechanical (markdown ATX headings). Recomputed synchronously on
    every save — always current, no model needed.
  - purpose: a one-line "what this is / when to consult", written by the model
    in a 'reindex' task. Refreshed only when the doc's signature changes.

A document is "stale" (needs its purpose regenerated) when its stored
indexed_hash doesn't match the signature of its current title+content. On any
such change we enqueue ONE 'reindex' task per project, coalescing: if an
unstarted reindex is already queued it will pick up the latest content when it
runs, so we don't pile up duplicates.
"""

import hashlib
import re

from server.events import emit
from server.util import new_id, utcnow_iso

# Markdown ATX heading: 1-6 leading '#', then text. We capture the text only.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)

_MAX_HEADINGS = 40          # a TOC, not the whole doc
_MAX_HEADING_LEN = 120


def extract_headings(content_md: str | None) -> list[str]:
    """Mechanical TOC: the markdown headings in document order. No model."""
    out: list[str] = []
    for m in _HEADING_RE.finditer(content_md or ""):
        text = m.group(2).strip()
        if text:
            out.append(text[:_MAX_HEADING_LEN])
        if len(out) >= _MAX_HEADINGS:
            break
    return out


def doc_signature(title: str | None, content_md: str | None) -> str:
    """Stable hash of the indexable text. The purpose is regenerated whenever
    this changes (title edits change routing intent too, so both feed it)."""
    h = hashlib.sha256()
    h.update((title or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((content_md or "").encode("utf-8"))
    return h.hexdigest()[:32]


def is_stale(doc: dict) -> bool:
    """True if the doc's purpose is missing or built from older content."""
    sig = doc_signature(doc.get("title"), doc.get("content_md"))
    return (not (doc.get("purpose") or "").strip()
            or doc.get("indexed_hash") != sig)


def enqueue_reindex_if_needed(conn, project_id: str) -> str | None:
    """If any doc in the project is stale, ensure a 'reindex' task is queued.

    Coalesces: a reindex task already in 'ready' (queued, not yet started) will
    read the latest content when it runs, so we don't enqueue a second one. A
    reindex already 'in_progress' may have read pre-edit content, so we DO queue
    a fresh one in that case (its hash check will catch what the running one
    missed). Returns the task id of the queued/existing reindex, or None.
    """
    rows = conn.execute(
        "SELECT title, content_md, purpose, indexed_hash "
        "FROM project_documents WHERE project_id = ?", (project_id,)).fetchall()
    if not any(is_stale(dict(r)) for r in rows):
        return None

    existing = conn.execute(
        "SELECT id FROM tasks WHERE project_id = ? AND type = 'reindex' "
        "AND status = 'ready' LIMIT 1", (project_id,)).fetchone()
    if existing:
        return existing["id"]

    tid = new_id()
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO tasks (id, project_id, type, title, description_md, status,
                           priority, depends_on, acceptance_criteria, payload,
                           attempt_count, max_attempts, created_at)
        VALUES (?, ?, 'reindex', ?, ?, 'ready', 'high', '[]', '[]', '{}', 0, 2, ?)
        """,
        (tid, project_id, "Reindex project documents",
         "Regenerate the one-line purpose for documents whose content changed, "
         "so the document index stays an accurate seek structure for agents.",
         now),
    )
    emit(conn, "task.created", "task", tid,
         project_id=project_id, task_id=tid, actor="system",
         detail={"type": "reindex"})
    emit(conn, "document.reindex_enqueued", "project", project_id,
         project_id=project_id, task_id=tid, actor="system", detail={})
    return tid
