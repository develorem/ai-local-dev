"""Authentication routes: Google sign-in, dev login, session, current user.

Google OAuth is env-gated (GOOGLE_CLIENT_ID/SECRET). Until those exist, the
dev-login endpoint (DEV_LOGIN_ENABLED, on by default when Google is unset)
creates/logs in a user by email so the app is fully usable locally.

These routes are 'open' in the auth middleware (pre-auth); each resolves the
session itself where it needs the caller's identity.
"""

import json
import secrets
import urllib.parse
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from server.config import config
from server.db.connection import db_dep
from server.services.tenancy import (
    create_session, delete_session, resolve_session, upsert_user,
    PLAN_LIMITS, PLAN_PRICES,
)

router = APIRouter(prefix="/auth", tags=["auth"])

_COOKIE = "orchestrai_session"
_GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"


def _redirect_uri() -> str:
    return f"{config.PUBLIC_BASE_URL}/api/auth/google/callback"


def _set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(_COOKIE, token, max_age=config.SESSION_TTL_DAYS * 86400,
                    httponly=True, samesite="lax", path="/")


def _org_summaries(conn, user: dict) -> list[dict]:
    if user.get("is_superadmin"):
        rows = conn.execute(
            "SELECT o.*, 'owner' AS role FROM organizations o ORDER BY o.created_at").fetchall()
    else:
        rows = conn.execute(
            "SELECT o.*, m.role AS role FROM organizations o "
            "JOIN org_members m ON m.org_id = o.id WHERE m.user_id = ? "
            "ORDER BY o.created_at", (user["id"],)).fetchall()
    out = []
    for r in rows:
        out.append({"id": r["id"], "name": r["name"], "slug": r["slug"],
                    "role": r["role"], "plan": r["plan"],
                    "subscription_status": r["subscription_status"]})
    return out


@router.get("/config")
def auth_config():
    """What the login screen needs to render."""
    return {"google_enabled": bool(config.GOOGLE_CLIENT_ID),
            "dev_login_enabled": config.DEV_LOGIN_ENABLED,
            "plans": {p: {**PLAN_LIMITS[p], "price": PLAN_PRICES[p]} for p in PLAN_LIMITS}}


@router.get("/me")
def me(request: Request, conn=Depends(db_dep)):
    token = request.cookies.get(_COOKIE) or request.query_params.get("token")
    user = resolve_session(conn, token)
    if not user:
        # operator token also counts as a (superadmin) session for the UI
        from server.auth import bearer_token, resolve_principal
        p = resolve_principal(bearer_token(request))
        if p and p.get("kind") == "operator":
            user = dict(conn.execute("SELECT * FROM users WHERE id = 'user_operator'").fetchone())
    if not user:
        return {"user": None, "orgs": []}
    return {"user": {"id": user["id"], "email": user["email"], "name": user["name"],
                     "picture_url": user["picture_url"],
                     "is_superadmin": bool(user["is_superadmin"])},
            "orgs": _org_summaries(conn, user)}


@router.post("/logout")
def logout(request: Request, conn=Depends(db_dep)):
    token = request.cookies.get(_COOKIE)
    if token:
        delete_session(conn, token)
        conn.commit()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_COOKIE, path="/")
    return resp


@router.post("/dev-login")
def dev_login(body: dict, conn=Depends(db_dep)):
    if not config.DEV_LOGIN_ENABLED:
        raise HTTPException(403, detail={"error": {"code": "dev_login_disabled"}})
    email = ((body or {}).get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, detail={"error": {"code": "email_required"}})
    user = upsert_user(conn, email=email, name=(body or {}).get("name") or "")
    token = create_session(conn, user["id"], config.SESSION_TTL_DAYS)
    conn.commit()
    resp = JSONResponse({"ok": True, "user": {"id": user["id"], "email": user["email"]}})
    _set_session_cookie(resp, token)
    return resp


@router.get("/google/login")
def google_login():
    if not config.GOOGLE_CLIENT_ID:
        raise HTTPException(503, detail={"error": {"code": "google_not_configured"}})
    state = secrets.token_urlsafe(24)
    params = urllib.parse.urlencode({
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    })
    resp = RedirectResponse(f"{_GOOGLE_AUTH}?{params}")
    resp.set_cookie("oauth_state", state, max_age=600, httponly=True, samesite="lax", path="/")
    return resp


@router.get("/google/callback")
def google_callback(request: Request, code: str = "", state: str = "", conn=Depends(db_dep)):
    if not config.GOOGLE_CLIENT_ID or not config.GOOGLE_CLIENT_SECRET:
        raise HTTPException(503, detail={"error": {"code": "google_not_configured"}})
    if not code or state != request.cookies.get("oauth_state"):
        raise HTTPException(400, detail={"error": {"code": "bad_oauth_state"}})
    # Exchange the code for tokens (sync urllib — this route runs in a threadpool).
    tok = _post_form(_GOOGLE_TOKEN, {
        "code": code, "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "redirect_uri": _redirect_uri(), "grant_type": "authorization_code"})
    access = tok.get("access_token")
    if not access:
        raise HTTPException(400, detail={"error": {"code": "token_exchange_failed"}})
    info = _get_json(_GOOGLE_USERINFO, access)
    email = (info.get("email") or "").lower()
    if not email:
        raise HTTPException(400, detail={"error": {"code": "no_email_from_google"}})
    user = upsert_user(conn, email=email, name=info.get("name") or "",
                       picture_url=info.get("picture"), google_sub=info.get("sub"))
    token = create_session(conn, user["id"], config.SESSION_TTL_DAYS)
    conn.commit()
    resp = RedirectResponse("/")
    _set_session_cookie(resp, token)
    resp.delete_cookie("oauth_state", path="/")
    return resp


# ---- small sync HTTP helpers (login callback only) -------------------------

def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _get_json(url: str, bearer: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())
