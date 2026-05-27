"""Health endpoint."""

import httpx
from fastapi import APIRouter, Depends

from server.config import config
from server.db.connection import db_dep
from server.db.migrations import current_version

router = APIRouter(tags=["health"])


@router.get("/health")
async def get_health(conn=Depends(db_dep)):
    # DB
    db_ok = True
    db_ver = 0
    try:
        db_ver = current_version(conn)
    except Exception:
        db_ok = False

    # Ollama
    ollama_ok = False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{config.OLLAMA_URL}/api/tags")
            ollama_ok = r.status_code == 200
    except Exception:
        ollama_ok = False

    # Agents
    agents_row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN status IN ('connected','idle','busy') THEN 1 ELSE 0 END) AS registered,
          SUM(CASE WHEN status IN ('connected','idle','busy') THEN 1 ELSE 0 END) AS connected,
          SUM(CASE WHEN status = 'busy' THEN 1 ELSE 0 END) AS busy,
          SUM(CASE WHEN status = 'lost' THEN 1 ELSE 0 END) AS lost
        FROM agents
        """
    ).fetchone()

    return {
        "status": "ok" if db_ok else "degraded",
        "version": config.VERSION,
        "ollama": {"reachable": ollama_ok, "host": config.OLLAMA_URL},
        "db": {"schema_version": db_ver, "ok": db_ok},
        "agents": {
            "registered": int(agents_row["registered"] or 0),
            "connected": int(agents_row["connected"] or 0),
            "busy": int(agents_row["busy"] or 0),
            "lost": int(agents_row["lost"] or 0),
        },
    }
