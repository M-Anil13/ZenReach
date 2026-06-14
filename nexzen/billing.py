"""Billing & metering (master prompt §11).

Each org connects its own WABA and pays Meta directly; NexZen charges a SaaS
subscription (Starter / Growth / Enterprise) with limits enforced as soft
warnings then hard caps. Usage is metered per message by template category and
recipient country against an admin-editable per-message rate table (Meta moved
to per-message pricing 2025-07-01). Payment gateways (Razorpay/Stripe) are
wired as clearly-marked integration stubs needing live API keys.
"""
import time

from flask import Blueprint, request, jsonify, g

from .db import q, q1, ex, insert, uid, now
from .auth import require_org, require_role, audit

bp = Blueprint("billing", __name__)


def _period():
    return time.strftime("%Y-%m")


def record_usage(org_id, category, country, count=1, conn=None):
    """Meter one (or N) billable template messages. Called by the sender."""
    category = (category or "MARKETING").lower()
    rate = q1("SELECT price_micros FROM pricing WHERE country=? AND category=?",
              (country, category), conn=conn) or \
        q1("SELECT price_micros FROM pricing WHERE country='DEFAULT' AND category=?", (category,), conn=conn)
    price = (rate or {}).get("price_micros", 0) * count
    row = q1("""SELECT id, count, cost_micros FROM usage_records
                WHERE org_id=? AND period=? AND category=? AND country=?""",
             (org_id, _period(), category, country), conn=conn)
    if row:
        ex("UPDATE usage_records SET count=count+?, cost_micros=cost_micros+? WHERE id=?",
           (count, price, row["id"]), conn=conn)
    else:
        ex("INSERT INTO usage_records (id, org_id, period, category, country, count, cost_micros) "
           "VALUES (?,?,?,?,?,?,?)",
           (uid("use"), org_id, _period(), category, country, count, price), conn=conn)


def plan_limit_status(org_id):
    """Returns (used, limit, blocked, warn) for this period's messages."""
    org = q1("SELECT plan_id FROM orgs WHERE id=?", (org_id,))
    plan = q1("SELECT * FROM plans WHERE id=?", ((org or {}).get("plan_id", "starter"),))
    used = q1("SELECT COALESCE(SUM(count),0) c FROM usage_records WHERE org_id=? AND period=?",
              (org_id, _period()))["c"]
    limit = (plan or {}).get("monthly_messages", 1000)
    return used, limit, used >= limit, used >= 0.8 * limit


@bp.get("/api/billing/plans")
def plans():
    return jsonify({"plans": q("SELECT * FROM plans ORDER BY price_inr")})


@bp.get("/api/billing/features")
def features():
    """Plan→feature comparison matrix for the upgrade screen (§11)."""
    import json
    rows = q("SELECT id, name, features FROM plans ORDER BY price_inr")
    keys = ["inbox", "automations", "sequences", "journeys", "chatbot",
            "integrations", "white_label", "api_access"]
    for r in rows:
        r["features"] = json.loads(r["features"] or "{}")
    return jsonify({"keys": keys, "plans": rows})


@bp.get("/api/billing/usage")
@require_org
def usage():
    used, limit, blocked, warn = plan_limit_status(g.org["id"])
    by_cat = q("""SELECT category, SUM(count) count, SUM(cost_micros) cost_micros
                  FROM usage_records WHERE org_id=? AND period=? GROUP BY category""",
               (g.org["id"], _period()))
    for r in by_cat:
        r["cost_inr"] = round(r["cost_micros"] / 1_000_000, 2)
    seats = q1("SELECT COUNT(*) c FROM memberships WHERE org_id=?", (g.org["id"],))["c"]
    contacts = q1("SELECT COUNT(*) c FROM contacts WHERE org_id=?", (g.org["id"],))["c"]
    gst = q1("SELECT gst_number FROM orgs WHERE id=?", (g.org["id"],))["gst_number"]
    return jsonify({"period": _period(), "messages_used": used, "messages_limit": limit,
                    "blocked": blocked, "warning": warn, "by_category": by_cat,
                    "seats": seats, "contacts": contacts, "gst_number": gst or "",
                    "tip": "Classify reminders as Utility — Utility messages cost far less than Marketing."})


@bp.post("/api/billing/subscribe")
@require_role("owner")
def subscribe():
    """Create/upgrade subscription. Razorpay (India) / Stripe (intl) stub."""
    d = request.get_json(force=True) or {}
    plan_id = d.get("plan_id", "growth")
    gateway = d.get("gateway", "razorpay")
    if not q1("SELECT id FROM plans WHERE id=?", (plan_id,)):
        return jsonify({"error": "Unknown plan"}), 400
    # PRODUCTION: create gateway order/checkout session here and confirm via webhook.
    insert("subscriptions", {"id": uid("sub"), "org_id": g.org["id"], "plan_id": plan_id,
                             "status": "active", "gateway": gateway,
                             "period_start": now(), "period_end": now() + 30 * 86400, "created_at": now()})
    ex("UPDATE orgs SET plan_id=? WHERE id=?", (plan_id, g.org["id"]))
    audit("billing.subscribed", plan_id, gateway)
    return jsonify({"ok": True, "plan": plan_id,
                    "checkout_stub": f"{gateway}://checkout/{plan_id}  (wire live keys to enable)"})


# ---- admin-editable per-message rate table ----------------------------------
@bp.get("/api/billing/pricing")
@require_org
def get_pricing():
    return jsonify({"pricing": q("SELECT * FROM pricing ORDER BY country, category"),
                    "rates_updated": "configurable — edit via /api/billing/pricing"})


@bp.put("/api/billing/pricing")
@require_role("owner")
def set_pricing():
    d = request.get_json(force=True) or {}
    ex("INSERT INTO pricing VALUES (?,?,?) ON CONFLICT(country,category) DO UPDATE SET price_micros=?",
       (d["country"], d["category"], int(d["price_micros"]), int(d["price_micros"])))
    return jsonify({"ok": True})
