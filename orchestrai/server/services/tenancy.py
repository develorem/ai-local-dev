"""Multi-tenancy helpers: users, sessions, org membership, and plan limits.

The principal model has three kinds (see server/auth.py):
  - operator : the configured operator token — superadmin, full access.
  - agent    : a registered agent's lease token (worker = full /api, external = MCP).
  - user     : a signed-in human (Google or dev login), scoped to their orgs.
"""

import secrets

from server.util import new_id, utcnow_iso

# None = unlimited. Prices are monthly USD (display only; Stripe is source of truth).
PLAN_LIMITS = {
    "free": {"projects": 2,    "own_agents": 1,    "can_invite": False, "max_members": 1,    "leasing": False},
    "pro":  {"projects": 10,   "own_agents": None, "can_invite": True,  "max_members": 10,   "leasing": True},
    "team": {"projects": None, "own_agents": None, "can_invite": True,  "max_members": None, "leasing": True},
}
PLAN_PRICES = {"free": 0.0, "pro": 4.95, "team": 19.90}


# ---- sessions --------------------------------------------------------------

def create_session(conn, user_id: str, ttl_days: int = 30) -> str:
    token = secrets.token_urlsafe(48)
    now = utcnow_iso()
    expires = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', ?)",
        (f"+{int(ttl_days)} days",)).fetchone()[0]
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, now, expires))
    return token


def resolve_session(conn, token: str | None) -> dict | None:
    """Return the user row for a non-expired session token, or None."""
    if not token:
        return None
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ? AND datetime(s.expires_at) > datetime('now')",
        (token,)).fetchone()
    return dict(row) if row else None


def delete_session(conn, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# ---- users -----------------------------------------------------------------

def upsert_user(conn, *, email: str, name: str = "", picture_url: str | None = None,
                google_sub: str | None = None) -> dict:
    """Find or create a user by google_sub (preferred) or email; refresh profile."""
    now = utcnow_iso()
    row = None
    if google_sub:
        row = conn.execute("SELECT * FROM users WHERE google_sub = ?", (google_sub,)).fetchone()
    if not row:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        conn.execute(
            "UPDATE users SET name = COALESCE(NULLIF(?, ''), name), "
            "picture_url = COALESCE(?, picture_url), "
            "google_sub = COALESCE(?, google_sub), last_login_at = ? WHERE id = ?",
            (name, picture_url, google_sub, now, row["id"]))
        return dict(conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone())
    uid = new_id()
    conn.execute(
        "INSERT INTO users (id, email, name, picture_url, google_sub, created_at, last_login_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (uid, email, name or email.split("@")[0], picture_url, google_sub, now, now))
    return dict(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


# ---- org membership --------------------------------------------------------

def member_role(conn, user_id: str, org_id: str) -> str | None:
    r = conn.execute("SELECT role FROM org_members WHERE org_id = ? AND user_id = ?",
                     (org_id, user_id)).fetchone()
    return r["role"] if r else None


def accessible_org_ids(conn, user: dict) -> list[str]:
    if user.get("is_superadmin"):
        return [r["id"] for r in conn.execute("SELECT id FROM organizations")]
    return [r["org_id"] for r in conn.execute(
        "SELECT org_id FROM org_members WHERE user_id = ?", (user["id"],))]


def user_can_access_project(conn, user: dict, project_id: str) -> bool:
    if user.get("is_superadmin"):
        return True
    r = conn.execute("SELECT org_id FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not r or not r["org_id"]:
        return False
    return member_role(conn, user["id"], r["org_id"]) is not None


def org_plan(conn, org_id: str) -> str:
    r = conn.execute("SELECT plan FROM organizations WHERE id = ?", (org_id,)).fetchone()
    return (r["plan"] if r else "free") or "free"


def limits_for(plan: str) -> dict:
    return PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])


# ---- plan-limit checks (return (ok, message)) ------------------------------

def can_add_project(conn, org_id: str) -> tuple[bool, str]:
    lim = limits_for(org_plan(conn, org_id))["projects"]
    if lim is None:
        return True, ""
    n = conn.execute("SELECT COUNT(*) FROM projects WHERE org_id = ? AND status != 'archived'",
                     (org_id,)).fetchone()[0]
    if n >= lim:
        return False, f"Plan limit reached: {lim} projects. Upgrade to add more."
    return True, ""


def can_add_agent(conn, org_id: str) -> tuple[bool, str]:
    lim = limits_for(org_plan(conn, org_id))["own_agents"]
    if lim is None:
        return True, ""
    n = conn.execute("SELECT COUNT(*) FROM agents WHERE org_id = ? AND status != 'released'",
                     (org_id,)).fetchone()[0]
    if n >= lim:
        return False, f"Plan limit reached: {lim} connected agent(s). Upgrade for more."
    return True, ""


def can_invite(conn, org_id: str) -> tuple[bool, str]:
    plan = org_plan(conn, org_id)
    lim = limits_for(plan)
    if not lim["can_invite"]:
        return False, "Inviting members requires a paid plan."
    maxm = lim["max_members"]
    if maxm is not None:
        n = conn.execute("SELECT COUNT(*) FROM org_members WHERE org_id = ?",
                         (org_id,)).fetchone()[0]
        if n >= maxm:
            return False, f"Plan limit reached: {maxm} members."
    return True, ""
