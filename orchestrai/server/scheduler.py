"""Scheduled-task scheduler.

Periodically materialises due scheduled_tasks into the backlog as real tasks.
Each scheduled_task carries a cron spec + a task template; when due, a 'ready'
task is created (the worker still only claims it if the project grants an agent).
"""

import asyncio
import datetime as dt
import sqlite3

from croniter import croniter

from server.db.connection import get_db
from server.events import emit
from server.util import new_id, utcnow_iso, json_dumps

SCHEDULER_INTERVAL_SEC = 30


def next_run(cron_expr: str, after_iso: str | None = None) -> str:
    """Next fire time (UTC ISO, seconds) strictly after `after_iso` (or now)."""
    base = (dt.datetime.fromisoformat(after_iso) if after_iso
            else dt.datetime.now(dt.timezone.utc))
    if base.tzinfo is None:
        base = base.replace(tzinfo=dt.timezone.utc)
    nxt = croniter(cron_expr, base).get_next(dt.datetime)
    return nxt.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _due(next_run_at: str | None, now: dt.datetime) -> bool:
    if not next_run_at:
        return True  # never computed — treat as due so it gets scheduled
    try:
        t = dt.datetime.fromisoformat(next_run_at)
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t <= now
    except ValueError:
        return False


async def _tick() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_tasks WHERE enabled = 1").fetchall()
        for s in rows:
            if not _due(s["next_run_at"], now):
                continue
            stamp = utcnow_iso()
            tid = new_id()
            conn.execute(
                """
                INSERT INTO tasks (id, project_id, type, title, description_md,
                                   status, priority, depends_on, acceptance_criteria,
                                   payload, attempt_count, max_attempts, created_at)
                VALUES (?, ?, ?, ?, ?, 'ready', ?, '[]', ?, ?, 0, 3, ?)
                """,
                (tid, s["project_id"], s["task_type"], s["title"], s["description_md"],
                 s["priority"], s["acceptance_criteria"],
                 json_dumps({"scheduled_task_id": s["id"]}), stamp),
            )
            try:
                nxt = next_run(s["cron"], stamp)
            except Exception:
                nxt = None  # bad cron — leave due so it's visible/fixable
            conn.execute(
                "UPDATE scheduled_tasks SET last_run_at = ?, next_run_at = ?, "
                "updated_at = ? WHERE id = ?", (stamp, nxt, stamp, s["id"]))
            emit(conn, "scheduled_task.fired", "project", s["project_id"],
                 project_id=s["project_id"], task_id=tid, actor="system",
                 detail={"scheduled_task_id": s["id"], "name": s["name"]})
            emit(conn, "task.created", "task", tid,
                 project_id=s["project_id"], task_id=tid, actor="system",
                 detail={"title": s["title"], "type": s["task_type"],
                         "source": "scheduled"})
        conn.commit()


async def _loop() -> None:
    while True:
        try:
            await _tick()
        except sqlite3.OperationalError as e:
            print(f"[scheduler] sqlite busy: {e}", flush=True)
        except Exception as e:
            print(f"[scheduler] error: {e}", flush=True)
        await asyncio.sleep(SCHEDULER_INTERVAL_SEC)


def start_scheduler() -> asyncio.Task:
    return asyncio.create_task(_loop(), name="orchestrai-scheduler")


async def stop_scheduler(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
