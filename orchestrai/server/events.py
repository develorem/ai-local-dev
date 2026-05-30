"""Event emission.

Every state change calls `emit(...)` which:
  1. Writes a row to `events` table (durable audit log)
  2. Schedules a WebSocket broadcast (live UI updates)

Caller is responsible for the surrounding DB transaction. `emit` participates
in the caller's transaction by reusing the passed connection.
"""

import asyncio
import sqlite3
from typing import Optional

from server.util import new_id, utcnow_iso, json_dumps, json_loads
from server.ws import manager


def emit(
    conn: sqlite3.Connection,
    kind: str,
    entity_type: str,
    entity_id: str,
    *,
    project_id: Optional[str] = None,
    outcome_id: Optional[str] = None,
    task_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    actor: str = "system",
    detail: Optional[dict] = None,
) -> str:
    event_id = new_id()
    ts = utcnow_iso()
    conn.execute(
        """
        INSERT INTO events (id, ts, kind, entity_type, entity_id,
                            project_id, outcome_id, task_id, agent_id, actor, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (event_id, ts, kind, entity_type, entity_id,
         project_id, outcome_id, task_id, agent_id, actor, json_dumps(detail or {})),
    )

    # Schedule async broadcast — don't block the request thread.
    frame = {
        "type": "event",
        "event": {
            "id": event_id, "ts": ts, "kind": kind,
            "entity_type": entity_type, "entity_id": entity_id,
            "project_id": project_id, "outcome_id": outcome_id,
            "task_id": task_id, "agent_id": agent_id,
            "actor": actor, "detail": detail or {},
        },
    }
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(manager.broadcast(frame))
    except RuntimeError:
        # Called outside an event loop (e.g. CLI) — silently drop the broadcast.
        pass

    return event_id


def event_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "kind": row["kind"],
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "project_id": row["project_id"],
        "outcome_id": row["outcome_id"],
        "task_id": row["task_id"],
        "agent_id": row["agent_id"],
        "actor": row["actor"],
        "detail": json_loads(row["detail"], {}),
    }
