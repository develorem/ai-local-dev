"""SQLite connection management.

WAL mode for concurrent readers + single writer. One connection per thread
(SQLite's default semantics) — FastAPI's async stack reuses connections via
the dependency, but each request thread gets its own.
"""

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from server.config import config


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")


def init_db() -> None:
    """Ensure the DB file exists with PRAGMAs applied. Run before migrations."""
    config.ensure_dirs()
    with sqlite3.connect(config.DB_PATH) as conn:
        _configure(conn)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _configure(conn)
    return conn


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    """Context-managed connection. Caller commits/rolls back."""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def db_dep() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency form of get_db()."""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()
