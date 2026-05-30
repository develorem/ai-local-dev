"""Billing (Stripe). Env-gated — endpoints return 503 until keys are set.

Plans: free / pro ($4.95) / team ($19.90). Checkout + customer portal + a webhook
that syncs the org's plan and subscription_status. The webhook is an open path
(verified by Stripe signature). Leased agents are gated by plan but NOT built
here — see the leasing stub (separate session).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from server.auth import require_user
from server.config import config
from server.db.connection import db_dep
from server.events import emit
from server.services.tenancy import (
    member_role, limits_for, PLAN_LIMITS, PLAN_PRICES,
)
from server.util import utcnow_iso

log = logging.getLogger("orchestrai.billing")
router = APIRouter(prefix="/billing", tags=["billing"])

_PRICE_BY_PLAN = {"pro": lambda: config.STRIPE_PRICE_PRO, "team": lambda: config.STRIPE_PRICE_TEAM}


def _stripe():
    if not config.STRIPE_SECRET_KEY:
        raise HTTPException(503, detail={"error": {"code": "billing_not_configured",
                            "message": "Stripe is not configured yet."}})
    try:
        import stripe
    except ImportError:
        raise HTTPException(503, detail={"error": {"code": "stripe_not_installed"}})
    stripe.api_key = config.STRIPE_SECRET_KEY
    return stripe


def _plan_for_price(price_id: str) -> str | None:
    if price_id and price_id == config.STRIPE_PRICE_PRO:
        return "pro"
    if price_id and price_id == config.STRIPE_PRICE_TEAM:
        return "team"
    return None


@router.get("/plans")
def plans():
    return {"plans": [{"id": p, "price": PLAN_PRICES[p], **PLAN_LIMITS[p]}
                      for p in ("free", "pro", "team")],
            "configured": bool(config.STRIPE_SECRET_KEY)}


def _require_owner(conn, user, org_id):
    if user.get("is_superadmin"):
        return
    if member_role(conn, user["user_id"], org_id) != "owner":
        raise HTTPException(403, detail={"error": {"code": "owner_required"}})


@router.post("/checkout")
def checkout(body: dict, request: Request, conn=Depends(db_dep)):
    user = require_user(request)
    org_id = (body or {}).get("org_id")
    plan = (body or {}).get("plan")
    if plan not in ("pro", "team"):
        raise HTTPException(400, detail={"error": {"code": "bad_plan"}})
    o = conn.execute("SELECT * FROM organizations WHERE id = ?", (org_id,)).fetchone()
    if not o:
        raise HTTPException(404)
    _require_owner(conn, user, org_id)
    price_id = _PRICE_BY_PLAN[plan]()
    if not price_id:
        raise HTTPException(503, detail={"error": {"code": "price_not_configured",
                            "message": f"No Stripe price id set for the {plan} plan."}})
    stripe = _stripe()
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{config.PUBLIC_BASE_URL}/#/settings?billing=success",
        cancel_url=f"{config.PUBLIC_BASE_URL}/#/settings?billing=cancel",
        customer=o["stripe_customer_id"] or None,
        client_reference_id=org_id,
        metadata={"org_id": org_id, "plan": plan},
        subscription_data={"metadata": {"org_id": org_id, "plan": plan}},
    )
    return {"url": session.url}


@router.post("/portal")
def portal(body: dict, request: Request, conn=Depends(db_dep)):
    user = require_user(request)
    org_id = (body or {}).get("org_id")
    o = conn.execute("SELECT * FROM organizations WHERE id = ?", (org_id,)).fetchone()
    if not o:
        raise HTTPException(404)
    _require_owner(conn, user, org_id)
    if not o["stripe_customer_id"]:
        raise HTTPException(400, detail={"error": {"code": "no_customer",
                            "message": "No Stripe customer yet — subscribe first."}})
    stripe = _stripe()
    sess = stripe.billing_portal.Session.create(
        customer=o["stripe_customer_id"],
        return_url=f"{config.PUBLIC_BASE_URL}/#/settings")
    return {"url": sess.url}


@router.post("/webhook")
async def webhook(request: Request, conn=Depends(db_dep)):
    if not config.STRIPE_SECRET_KEY:
        raise HTTPException(503)
    stripe = _stripe()
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        if config.STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig, config.STRIPE_WEBHOOK_SECRET)
        else:
            import json
            event = json.loads(payload)  # dev only — no signature verification
    except Exception as e:
        raise HTTPException(400, detail={"error": {"code": "bad_signature", "message": str(e)}})

    etype = event["type"]
    obj = event["data"]["object"]
    now = utcnow_iso()

    def _set(org_id, **cols):
        sets = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(f"UPDATE organizations SET {sets}, updated_at = ? WHERE id = ?",
                     (*cols.values(), now, org_id))

    if etype == "checkout.session.completed":
        org_id = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("org_id")
        plan = (obj.get("metadata") or {}).get("plan") or "pro"
        if org_id:
            _set(org_id, plan=plan, subscription_status="active",
                 stripe_customer_id=obj.get("customer"),
                 stripe_subscription_id=obj.get("subscription"))
            emit(conn, "org.subscribed", "org", org_id, actor="stripe",
                 detail={"plan": plan})
    elif etype in ("customer.subscription.updated", "customer.subscription.created"):
        org_id = (obj.get("metadata") or {}).get("org_id")
        status = obj.get("status")
        price_id = (((obj.get("items") or {}).get("data") or [{}])[0].get("price") or {}).get("id")
        plan = _plan_for_price(price_id)
        if not org_id:
            r = conn.execute("SELECT id FROM organizations WHERE stripe_subscription_id = ?",
                             (obj.get("id"),)).fetchone()
            org_id = r["id"] if r else None
        if org_id:
            cols = {"subscription_status": status, "stripe_subscription_id": obj.get("id")}
            if plan:
                cols["plan"] = plan
            _set(org_id, **cols)
    elif etype == "customer.subscription.deleted":
        r = conn.execute("SELECT id FROM organizations WHERE stripe_subscription_id = ?",
                         (obj.get("id"),)).fetchone()
        if r:
            _set(r["id"], plan="free", subscription_status="canceled",
                 stripe_subscription_id=None)
            emit(conn, "org.unsubscribed", "org", r["id"], actor="stripe", detail={})
    conn.commit()
    return {"received": True}


@router.get("/leasing")
def leasing_status(request: Request, conn=Depends(db_dep)):
    """FOUNDATION ONLY for cloud-hosted leased agents (built in a later session).
    Reports whether the org's plan unlocks leasing; provisioning is not implemented."""
    user = require_user(request)
    org_id = request.query_params.get("org_id")
    if not org_id:
        raise HTTPException(400, detail={"error": {"code": "org_id_required"}})
    o = conn.execute("SELECT plan FROM organizations WHERE id = ?", (org_id,)).fetchone()
    if not o:
        raise HTTPException(404)
    return {"available": bool(limits_for(o["plan"])["leasing"]),
            "status": "not_implemented",
            "message": "Leased (cloud-hosted) agents are coming soon."}
