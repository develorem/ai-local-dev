"""Organizations: create, members, roles, invitations.

A user can create their own org (becomes owner) and accept invitations to others.
Inviting requires a paid plan (see tenancy.can_invite). Email delivery isn't wired
yet, so invitations return a link the inviter can share (tracked follow-up).
"""

import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request

from server.auth import require_user
from server.config import config
from server.db.connection import db_dep
from server.events import emit
from server.services.tenancy import (
    member_role, can_invite, limits_for, org_plan, PLAN_PRICES,
)
from server.util import new_id, utcnow_iso

router = APIRouter(tags=["orgs"])


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "org"


def _require_role(conn, user: dict, org_id: str, allowed: set) -> str:
    if user.get("is_superadmin"):
        return "owner"
    role = member_role(conn, user["user_id"], org_id)
    if role is None:
        raise HTTPException(404, detail={"error": {"code": "org_not_found"}})
    if role not in allowed:
        raise HTTPException(403, detail={"error": {"code": "insufficient_role",
                            "message": f"requires one of {sorted(allowed)}"}})
    return role


@router.post("/orgs", status_code=201)
def create_org(body: dict, request: Request, conn=Depends(db_dep)):
    user = require_user(request)
    name = ((body or {}).get("name") or "").strip()
    if not name:
        raise HTTPException(400, detail={"error": {"code": "name_required"}})
    base = ((body or {}).get("slug") or _slugify(name))
    slug, n = base, 1
    while conn.execute("SELECT 1 FROM organizations WHERE slug = ?", (slug,)).fetchone():
        n += 1
        slug = f"{base}-{n}"
    oid = new_id()
    now = utcnow_iso()
    conn.execute(
        "INSERT INTO organizations (id, name, slug, owner_user_id, plan, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'free', ?, ?)",
        (oid, name, slug, user["user_id"], now, now))
    conn.execute("INSERT INTO org_members (org_id, user_id, role, created_at) "
                 "VALUES (?, ?, 'owner', ?)", (oid, user["user_id"], now))
    emit(conn, "org.created", "org", oid, actor=f"user:{user['user_id']}",
         detail={"name": name, "slug": slug})
    conn.commit()
    return {"id": oid, "name": name, "slug": slug, "plan": "free", "role": "owner"}


@router.get("/orgs")
def list_orgs(request: Request, conn=Depends(db_dep)):
    user = require_user(request)
    if user.get("is_superadmin"):
        rows = conn.execute("SELECT *, 'owner' AS _role FROM organizations ORDER BY created_at").fetchall()
    else:
        rows = conn.execute(
            "SELECT o.*, m.role AS _role FROM organizations o "
            "JOIN org_members m ON m.org_id = o.id WHERE m.user_id = ? ORDER BY o.created_at",
            (user["user_id"],)).fetchall()
    return {"items": [{"id": r["id"], "name": r["name"], "slug": r["slug"],
                       "plan": r["plan"], "role": r["_role"],
                       "subscription_status": r["subscription_status"]} for r in rows]}


@router.get("/orgs/{org_id}")
def get_org(org_id: str, request: Request, conn=Depends(db_dep)):
    user = require_user(request)
    role = _require_role(conn, user, org_id, {"owner", "admin", "member"})
    o = conn.execute("SELECT * FROM organizations WHERE id = ?", (org_id,)).fetchone()
    members = [{"user_id": r["user_id"], "role": r["role"], "email": r["email"],
                "name": r["name"], "picture_url": r["picture_url"]}
               for r in conn.execute(
        "SELECT m.user_id, m.role, u.email, u.name, u.picture_url FROM org_members m "
        "JOIN users u ON u.id = m.user_id WHERE m.org_id = ? ORDER BY m.created_at", (org_id,))]
    invitations = [{"id": r["id"], "email": r["email"], "role": r["role"],
                    "status": r["status"], "created_at": r["created_at"],
                    "link": f"{config.PUBLIC_BASE_URL}/#/accept-invite/{r['token']}"}
                   for r in conn.execute(
        "SELECT * FROM org_invitations WHERE org_id = ? AND status = 'pending' "
        "ORDER BY created_at DESC", (org_id,))]
    plan = o["plan"]
    lim = limits_for(plan)
    proj_n = conn.execute("SELECT COUNT(*) FROM projects WHERE org_id = ? AND status != 'archived'",
                          (org_id,)).fetchone()[0]
    agent_n = conn.execute("SELECT COUNT(*) FROM agents WHERE org_id = ? AND status != 'released'",
                           (org_id,)).fetchone()[0]
    return {"org": {"id": o["id"], "name": o["name"], "slug": o["slug"], "plan": plan,
                    "subscription_status": o["subscription_status"],
                    "owner_user_id": o["owner_user_id"]},
            "my_role": role, "members": members, "invitations": invitations,
            "limits": {**lim, "price": PLAN_PRICES.get(plan, 0)},
            "usage": {"projects": proj_n, "agents": agent_n}}


@router.patch("/orgs/{org_id}")
def update_org(org_id: str, body: dict, request: Request, conn=Depends(db_dep)):
    user = require_user(request)
    _require_role(conn, user, org_id, {"owner", "admin"})
    name = ((body or {}).get("name") or "").strip()
    if name:
        conn.execute("UPDATE organizations SET name = ?, updated_at = ? WHERE id = ?",
                     (name, utcnow_iso(), org_id))
        conn.commit()
    return {"ok": True}


@router.post("/orgs/{org_id}/invitations", status_code=201)
def invite(org_id: str, body: dict, request: Request, conn=Depends(db_dep)):
    user = require_user(request)
    _require_role(conn, user, org_id, {"owner", "admin"})
    ok, msg = can_invite(conn, org_id)
    if not ok:
        raise HTTPException(402, detail={"error": {"code": "plan_limit", "message": msg}})
    email = ((body or {}).get("email") or "").strip().lower()
    role = (body or {}).get("role", "member")
    if not email or "@" not in email:
        raise HTTPException(400, detail={"error": {"code": "email_required"}})
    if role not in ("admin", "member"):
        role = "member"
    token = secrets.token_urlsafe(24)
    iid = new_id()
    conn.execute(
        "INSERT INTO org_invitations (id, org_id, email, role, token, invited_by_user_id, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (iid, org_id, email, role, token, user["user_id"], utcnow_iso()))
    emit(conn, "org.invited", "org", org_id, actor=f"user:{user['user_id']}",
         detail={"email": email, "role": role})
    conn.commit()
    return {"id": iid, "email": email, "role": role,
            "link": f"{config.PUBLIC_BASE_URL}/#/accept-invite/{token}"}


@router.delete("/orgs/{org_id}/invitations/{inv_id}")
def revoke_invite(org_id: str, inv_id: str, request: Request, conn=Depends(db_dep)):
    user = require_user(request)
    _require_role(conn, user, org_id, {"owner", "admin"})
    conn.execute("UPDATE org_invitations SET status = 'revoked' WHERE id = ? AND org_id = ?",
                 (inv_id, org_id))
    conn.commit()
    return {"ok": True}


@router.get("/invitations/{token}")
def preview_invite(token: str, conn=Depends(db_dep)):
    r = conn.execute(
        "SELECT i.*, o.name AS org_name FROM org_invitations i "
        "JOIN organizations o ON o.id = i.org_id WHERE i.token = ?", (token,)).fetchone()
    if not r or r["status"] != "pending":
        raise HTTPException(404, detail={"error": {"code": "invite_not_found"}})
    return {"org_id": r["org_id"], "org_name": r["org_name"], "email": r["email"],
            "role": r["role"]}


@router.post("/invitations/accept")
def accept_invite(body: dict, request: Request, conn=Depends(db_dep)):
    user = require_user(request)
    token = (body or {}).get("token")
    r = conn.execute("SELECT * FROM org_invitations WHERE token = ?", (token,)).fetchone()
    if not r or r["status"] != "pending":
        raise HTTPException(404, detail={"error": {"code": "invite_not_found"}})
    now = utcnow_iso()
    conn.execute("INSERT OR IGNORE INTO org_members (org_id, user_id, role, created_at) "
                 "VALUES (?, ?, ?, ?)", (r["org_id"], user["user_id"], r["role"], now))
    conn.execute("UPDATE org_invitations SET status = 'accepted', accepted_at = ? WHERE id = ?",
                 (now, r["id"]))
    emit(conn, "org.member_joined", "org", r["org_id"], actor=f"user:{user['user_id']}",
         detail={"role": r["role"]})
    conn.commit()
    return {"ok": True, "org_id": r["org_id"]}


@router.delete("/orgs/{org_id}/members/{user_id}")
def remove_member(org_id: str, user_id: str, request: Request, conn=Depends(db_dep)):
    user = require_user(request)
    _require_role(conn, user, org_id, {"owner", "admin"})
    o = conn.execute("SELECT owner_user_id FROM organizations WHERE id = ?", (org_id,)).fetchone()
    if o and o["owner_user_id"] == user_id:
        raise HTTPException(400, detail={"error": {"code": "cannot_remove_owner"}})
    conn.execute("DELETE FROM org_members WHERE org_id = ? AND user_id = ?", (org_id, user_id))
    conn.commit()
    return {"ok": True}
