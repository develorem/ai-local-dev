"""Small cross-cutting utilities."""

import datetime as dt
import json
from typing import Any

import ulid


def new_id() -> str:
    """Sortable, unique ID as a 26-char string."""
    return str(ulid.new())


def utcnow_iso() -> str:
    """ISO-8601 UTC string with seconds precision."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def json_loads(s: str | None, default: Any) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return default


def json_dumps(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)
