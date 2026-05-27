"""Versioned SQL migrations.

Migrations live in `server/db/migrations/NNN_name.sql`. On startup we read
`PRAGMA user_version`, apply any newer migrations in order, then set the
new version. The service refuses to start serving traffic if migrations fail.
"""

import re
import sqlite3
from pathlib import Path

from server.config import config

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME_RE = re.compile(r"^(\d+)_(.+)\.sql$")


def _discover() -> list[tuple[int, str, Path]]:
    out = []
    if not MIGRATIONS_DIR.exists():
        return out
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _NAME_RE.match(path.name)
        if not m:
            continue
        out.append((int(m.group(1)), m.group(2), path))
    return out


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def run_migrations() -> dict:
    """Apply any pending migrations. Returns {applied: [...], version: N}."""
    migrations = _discover()
    if not migrations:
        return {"applied": [], "version": 0}

    applied = []
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = OFF")  # off during DDL
        version = current_version(conn)

        for num, name, path in migrations:
            if num <= version:
                continue
            sql = path.read_text(encoding="utf-8")
            try:
                conn.executescript(sql)
                conn.execute(f"PRAGMA user_version = {num}")
                conn.commit()
            except sqlite3.Error as e:
                conn.rollback()
                raise RuntimeError(f"migration {num}_{name} failed: {e}") from e
            applied.append(f"{num}_{name}")

        conn.execute("PRAGMA foreign_keys = ON")
        final = current_version(conn)

    return {"applied": applied, "version": final}
