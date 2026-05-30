"""Authentication for the hub.

Three principal kinds:
  - operator: the configured operator token. Superadmin: full /api, every org.
  - agent:    a registered agent's lease token. worker = full /api; external = MCP only.
  - user:     a signed-in human (Google or dev login) with a session token.
              Allowed on /api; per-route checks scope them to their orgs.

Auth is enforced only when an operator token is configured (config.OPERATOR_TOKEN).
With none set the hub runs open (localhost-dev convenience).

Open paths (no auth even when enabled):
  - /api/health
  - /api/auth/...              (login, oauth callback, dev-login)
  - /api/billing/webhook       (Stripe — verified by signature)
  - /api/webhooks/...          (per-project URL secret)
  - everything NOT under /api or /mcp  (static UI + client-side login page)
  - /mcp...                    (enforced in the MCP layer)
"""

import logging
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from server.config import config
from server.db.connection import get_db
from server.services.tenancy import resolve_session

log = logging.getLogger("orchestrai.auth")

AUTH_ENABLED = bool(config.OPERATOR_TOKEN)


def _candidate_token(request: Request) -> Optional[str]:
    """A bearer-style token from header, the operator cookie, or query param."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(None, 1)[1].strip()
    return (request.cookies.get("orchestrai_token")
            or request.query_params.get("token"))


def bearer_token(request: Request) -> Optional[str]:
    return _candidate_token(request)


def resolve_principal(token: Optional[str], session_token: Optional[str] = None) -> Optional[dict]:
    """Map credentials to a principal, or None.

    `token` is checked against the operator token and agent lease tokens.
    `session_token` (and `token`, as a fallback) is checked against user sessions.
    """
    if token and config.OPERATOR_TOKEN and token == config.OPERATOR_TOKEN:
        return {"kind": "operator", "is_superadmin": True}
    with get_db() as conn:
        if token:
            r = conn.execute("SELECT id, name, kind FROM agents WHERE lease_token = ?",
                             (token,)).fetchone()
            if r:
                return {"kind": "agent", "id": r["id"], "name": r["name"],
                        "agent_kind": (r["kind"] if "kind" in r.keys() else "worker")}
        for tok in (session_token, token):
            if not tok:
                continue
            u = resolve_session(conn, tok)
            if u:
                return {"kind": "user", "user_id": u["id"], "email": u["email"],
                        "name": u["name"], "picture_url": u["picture_url"],
                        "is_superadmin": bool(u["is_superadmin"])}
    return None


def is_token_valid(token: Optional[str]) -> bool:
    return resolve_principal(token) is not None


def current_principal(request: Request) -> dict:
    """The principal set by the middleware. When auth is disabled (no operator
    token) there's none, so treat the caller as a local superadmin operator."""
    p = getattr(request.state, "principal", None)
    if p:
        return p
    return {"kind": "operator", "is_superadmin": True}


def require_user(request: Request) -> dict:
    """For routes that need a human (orgs, billing). operator counts as superadmin."""
    from fastapi import HTTPException
    p = current_principal(request)
    if p["kind"] == "user":
        return p
    if p["kind"] == "operator":
        return {"kind": "user", "user_id": "user_operator", "is_superadmin": True,
                "email": "operator@localhost", "name": "Operator"}
    raise HTTPException(401, detail={"error": {"code": "login_required"}})


def _is_open(path: str) -> bool:
    if not (path.startswith("/api") or path.startswith("/mcp")):
        return True
    if path == "/api/health":
        return True
    if path.startswith("/api/auth/"):
        return True
    if path == "/api/billing/webhook":
        return True
    if path.startswith("/api/webhooks/"):
        return True
    if path.startswith("/mcp"):
        return True
    return False


def _resp(code: int, detail: str) -> JSONResponse:
    return JSONResponse({"error": {"code": "unauthorized" if code == 401 else "forbidden",
                                   "message": detail}}, status_code=code)


async def auth_middleware(request: Request, call_next):
    if not AUTH_ENABLED or request.method == "OPTIONS" or _is_open(request.url.path):
        return await call_next(request)

    principal = resolve_principal(
        _candidate_token(request),
        session_token=request.cookies.get("orchestrai_session"))
    if principal is None:
        return _resp(401, "Missing or invalid token.")

    # operator + worker agent: full admin API. user: allowed (routes scope by org).
    # external agents: MCP only.
    if (principal["kind"] in ("operator", "user")
            or principal.get("agent_kind") == "worker"):
        request.state.principal = principal
        return await call_next(request)
    return _resp(403, "This token is not allowed on the admin API. "
                      "External agents connect via the /mcp endpoint.")
