"""Events: REST query + WebSocket live stream."""

from typing import Optional

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from server.auth import AUTH_ENABLED, resolve_principal
from server.db.connection import db_dep
from server.events import event_row_to_dict
from server.ws import manager

router = APIRouter(tags=["events"])


@router.get("/events")
def list_events(
    since: Optional[str] = None,
    kind: Optional[str] = None,
    project_id: Optional[str] = None,
    task_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    limit: int = 200,
    conn=Depends(db_dep),
):
    limit = max(1, min(limit, 1000))
    where, params = [], []
    if since:
        where.append("ts > ?"); params.append(since)
    if kind:
        kinds = [k.strip() for k in kind.split(",")]
        conds = []
        for k in kinds:
            if k.endswith("*"):
                conds.append("kind LIKE ?")
                params.append(k[:-1] + "%")
            else:
                conds.append("kind = ?")
                params.append(k)
        where.append("(" + " OR ".join(conds) + ")")
    for col, val in (("project_id", project_id), ("task_id", task_id), ("agent_id", agent_id)):
        if val:
            where.append(f"{col} = ?"); params.append(val)

    q = "SELECT * FROM events"
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    return {"items": [event_row_to_dict(r) for r in rows], "next_cursor": None}


@router.websocket("/events")
async def ws_events(ws: WebSocket):
    # Browsers can't set headers on a WS upgrade: operator/agent tokens ride in
    # ?token=, while a signed-in user's session arrives as the cookie.
    if AUTH_ENABLED:
        principal = resolve_principal(
            ws.query_params.get("token"),
            session_token=ws.cookies.get("orchestrai_session"))
        if principal is None:
            await ws.close(code=1008)  # policy violation
            return
    await manager.connect(ws)
    try:
        while True:
            # Push-only stream; we still need to read to detect disconnect.
            await ws.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(ws)
    except Exception:
        await manager.disconnect(ws)
