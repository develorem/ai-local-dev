"""Token authentication for the hub.

The token IS the auth. Two kinds of principal:
  - operator: presents the configured operator token. Full admin: the UI and
    every /api route. This is the human.
  - agent:    presents a registered agent's lease token. Scoped to the agent
    pull endpoints (/api/agents/{id}/...) and the MCP endpoint.

Auth is enforced only when an operator token is configured (config.OPERATOR_TOKEN).
With none set the hub runs open (localhost-dev convenience) — set the token
before exposing the hub anywhere untrusted.

Open paths (no auth even when enabled):
  - /api/health                  (monitoring)
  - /api/webhooks/...            (self-authed by a per-project URL secret)
  - everything NOT under /api or /mcp  (static UI + the client-side login page)

/mcp auth is handled in mcp_integration (the request must carry a valid agent
token); this middleware leaves /mcp alone.
"""

import logging
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from server.config import config
from server.db.connection import get_db

log = logging.getLogger("orchestrai.auth")

AUTH_ENABLED = bool(config.OPERATOR_TOKEN)

# Agent self-endpoints authenticate with the agent's own lease token (the route
# re-checks it). The path looks like /api/agents/{id}/<verb>.
_AGENT_SELF_VERBS = {"heartbeat", "claim", "release", "result", "events"}


def bearer_token(request: Request) -> Optional[str]:
    """Pull a bearer token from the Authorization header, or the `token` cookie
    / query param (the UI stores it; WS upgrades can't set headers)."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(None, 1)[1].strip()
    return request.cookies.get("orchestrai_token") or request.query_params.get("token")


def resolve_principal(token: Optional[str]) -> Optional[dict]:
    """Map a token to a principal, or None. {kind: 'operator'} or
    {kind: 'agent', id, name, agent_kind}."""
    if not token:
        return None
    if config.OPERATOR_TOKEN and token == config.OPERATOR_TOKEN:
        return {"kind": "operator"}
    with get_db() as conn:
        r = conn.execute("SELECT id, name, kind FROM agents WHERE lease_token = ?",
                         (token,)).fetchone()
    if r:
        return {"kind": "agent", "id": r["id"], "name": r["name"],
                "agent_kind": (r["kind"] if "kind" in r.keys() else "worker")}
    return None


def is_token_valid(token: Optional[str]) -> bool:
    return resolve_principal(token) is not None


def _is_open(path: str) -> bool:
    if not (path.startswith("/api") or path.startswith("/mcp")):
        return True  # static UI + client-side login page
    if path == "/api/health":
        return True
    if path.startswith("/api/webhooks/"):
        return True
    if path.startswith("/mcp"):
        return True  # enforced in the MCP layer, which knows the agent context
    return False


def _resp(code: int, detail: str) -> JSONResponse:
    return JSONResponse({"error": {"code": "unauthorized" if code == 401 else "forbidden",
                                   "message": detail}}, status_code=code)


async def auth_middleware(request: Request, call_next):
    if not AUTH_ENABLED or request.method == "OPTIONS" or _is_open(request.url.path):
        return await call_next(request)

    principal = resolve_principal(bearer_token(request))
    if principal is None:
        return _resp(401, "Missing or invalid token.")

    # operator: full admin API. worker: the trusted internal executor — it drives
    # the whole task lifecycle over /api (claim/result/events/notes, reads of
    # outcomes/plans/tasks, secret values, clone-info), so it gets the full API
    # too. external agents do NOT touch /api — they use the /mcp endpoint.
    if principal["kind"] == "operator" or principal.get("agent_kind") == "worker":
        request.state.principal = principal
        return await call_next(request)
    return _resp(403, "This token is not allowed on the admin API. "
                      "External agents connect via the /mcp endpoint.")
