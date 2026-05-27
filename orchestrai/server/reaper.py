"""Reaper background job.

Runs periodically:
  - Reclaim expired-lease tasks back to 'ready'
  - Mark agents that haven't heartbeated recently as 'lost'

Designed for SQLite single-writer semantics. The reaper is the only background
writer outside of HTTP request handlers.
"""

import asyncio
import sqlite3

from server.config import config
from server.db.connection import get_db
from server.events import emit


async def _tick() -> None:
    with get_db() as conn:
        # 1. Reclaim expired-lease tasks
        rows = conn.execute(
            """
            SELECT id, project_id, goal_id, assigned_agent_id
            FROM tasks
            WHERE status = 'in_progress'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at < datetime('now')
            """
        ).fetchall()
        for r in rows:
            conn.execute(
                """
                UPDATE tasks
                SET status='ready',
                    assigned_agent_id=NULL,
                    lease_expires_at=NULL,
                    notes = notes || char(10) || ?
                WHERE id = ?
                """,
                (f"[reaper] reclaimed: lease expired", r["id"]),
            )
            emit(conn, "task.lease_expired_reclaimed", "task", r["id"],
                 project_id=r["project_id"], goal_id=r["goal_id"],
                 task_id=r["id"], agent_id=r["assigned_agent_id"],
                 actor="system", detail={})

        # 2. Mark stale agents lost
        stale = conn.execute(
            f"""
            SELECT id FROM agents
            WHERE status IN ('connected','idle','busy')
              AND (last_heartbeat_at IS NULL OR
                   last_heartbeat_at < datetime('now', '-{config.AGENT_LEASE_TIMEOUT_SEC} seconds'))
            """
        ).fetchall()
        for r in stale:
            conn.execute(
                "UPDATE agents SET status='lost' WHERE id = ?",
                (r["id"],),
            )
            emit(conn, "agent.lost", "agent", r["id"],
                 agent_id=r["id"], actor="system",
                 detail={"reason": "heartbeat_timeout"})

        conn.commit()


async def _loop() -> None:
    while True:
        try:
            await _tick()
        except sqlite3.OperationalError as e:
            # busy / locked — retry next tick
            print(f"[reaper] sqlite busy: {e}", flush=True)
        except Exception as e:
            print(f"[reaper] error: {e}", flush=True)
        await asyncio.sleep(config.REAPER_INTERVAL_SEC)


def start_reaper() -> asyncio.Task:
    return asyncio.create_task(_loop(), name="orchestrai-reaper")


async def stop_reaper(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
