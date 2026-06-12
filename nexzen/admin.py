"""NexZen super-admin panel (master prompt §3, separate authority).

Lists all orgs with plan/usage/connection-health/quality; suspend & reactivate;
consented impersonation with audit trail; platform revenue + usage dashboards.
Guarded by require_superadmin (first registered user is bootstrapped as one).
"""
import json
import threading
import time

from flask import Blueprint, request, jsonify, session

from .db import q, q1, ex, now, raw_conn, PLATFORM
from .auth import require_superadmin, audit, current_user

bp = Blueprint("admin", __name__)


# ─── Billing control (plans, trial, subscriptions) ───────────────────────────
@bp.get("/api/admin/plans")
@require_superadmin
def admin_plans():
    return jsonify({"plans": q("SELECT * FROM plans ORDER BY price_inr")})


@bp.put("/api/admin/plans/<pid>")
@require_superadmin
def edit_plan(pid):
    """Edit a plan's price, limits, features and description."""
    if not q1("SELECT id FROM plans WHERE id=?", (pid,)):
        return jsonify({"error": "Plan not found"}), 404
    d = request.get_json(force=True) or {}
    allowed = ["name", "price_inr", "price_usd", "monthly_messages", "seats",
               "contacts", "white_label", "api_access", "description"]
    fields = {k: d[k] for k in allowed if k in d}
    if not fields:
        return jsonify({"error": "Nothing to update"}), 400
    sets = ", ".join(f"{k}=?" for k in fields)
    ex(f"UPDATE plans SET {sets} WHERE id=?", (*fields.values(), pid))
    audit("admin.plan_edited", pid, json.dumps(fields))
    return jsonify({"ok": True})


@bp.post("/api/admin/plans")
@require_superadmin
def create_plan():
    d = request.get_json(force=True) or {}
    pid = (d.get("id") or "").strip().lower().replace(" ", "_")
    if not pid or q1("SELECT id FROM plans WHERE id=?", (pid,)):
        return jsonify({"error": "Provide a unique plan id"}), 400
    ex("""INSERT INTO plans (id,name,price_inr,price_usd,monthly_messages,seats,contacts,
          white_label,api_access,description) VALUES (?,?,?,?,?,?,?,?,?,?)""",
       (pid, d.get("name", pid), int(d.get("price_inr", 0)), int(d.get("price_usd", 0)),
        int(d.get("monthly_messages", 1000)), int(d.get("seats", 2)), int(d.get("contacts", 5000)),
        1 if d.get("white_label") else 0, 1 if d.get("api_access") else 0, d.get("description", "")))
    audit("admin.plan_created", pid)
    return jsonify({"ok": True, "id": pid})


@bp.get("/api/admin/trial")
@require_superadmin
def get_trial():
    row = q1("SELECT value FROM settings WHERE org_id=? AND key='trial'", (PLATFORM,))
    return jsonify(json.loads(row["value"]) if row else {"trial_days": 14, "grace_days": 3})


@bp.put("/api/admin/trial")
@require_superadmin
def set_trial():
    """Control free-trial length + grace days before auto-suspend."""
    d = request.get_json(force=True) or {}
    val = json.dumps({"trial_days": int(d.get("trial_days", 14)),
                      "grace_days": int(d.get("grace_days", 3))})
    ex("INSERT INTO settings (org_id,key,value) VALUES (?,?,?) "
       "ON CONFLICT(org_id,key) DO UPDATE SET value=?", (PLATFORM, "trial", val, val))
    audit("admin.trial_updated", PLATFORM, val)
    return jsonify({"ok": True})


@bp.get("/api/admin/subscriptions")
@require_superadmin
def subscriptions():
    rows = q("""SELECT o.id, o.name, o.plan_id, o.suspended, o.trial_ends,
                p.name plan_name, p.price_inr,
                (SELECT paid FROM subscriptions s WHERE s.org_id=o.id ORDER BY created_at DESC LIMIT 1) paid
                FROM orgs o LEFT JOIN plans p ON p.id=o.plan_id ORDER BY o.created_at DESC""")
    n = now()
    for r in rows:
        r["trial_active"] = bool(r["trial_ends"] and r["trial_ends"] > n and r["plan_id"] == "starter")
        r["trial_days_left"] = max(0, round((r["trial_ends"] - n) / 86400)) if r["trial_ends"] else 0
    return jsonify({"subscriptions": rows})


@bp.post("/api/admin/orgs/<oid>/plan")
@require_superadmin
def set_org_plan(oid):
    d = request.get_json(force=True) or {}
    pid = d.get("plan_id")
    if not q1("SELECT id FROM plans WHERE id=?", (pid,)):
        return jsonify({"error": "Unknown plan"}), 400
    ex("UPDATE orgs SET plan_id=? WHERE id=?", (pid, oid))
    from .db import uid
    ex("""INSERT INTO subscriptions (id,org_id,plan_id,status,gateway,period_start,period_end,paid,created_at)
          VALUES (?,?,?,?,?,?,?,?,?)""",
       (uid("sub"), oid, pid, "active", "manual", now(), now() + 30 * 86400,
        1 if d.get("mark_paid") else 0, now()))
    audit("admin.org_plan_set", oid, pid)
    return jsonify({"ok": True})


@bp.post("/api/admin/orgs/<oid>/extend-trial")
@require_superadmin
def extend_trial(oid):
    days = int((request.get_json(force=True) or {}).get("days", 7))
    o = q1("SELECT trial_ends FROM orgs WHERE id=?", (oid,))
    if not o:
        return jsonify({"error": "Org not found"}), 404
    base = max(o["trial_ends"] or now(), now())
    ex("UPDATE orgs SET trial_ends=?, suspended=0 WHERE id=?", (base + days * 86400, oid))
    audit("admin.trial_extended", oid, f"+{days}d")
    return jsonify({"ok": True, "trial_ends": base + days * 86400})


@bp.post("/api/admin/orgs/<oid>/mark-paid")
@require_superadmin
def mark_paid(oid):
    paid = 1 if (request.get_json(force=True) or {}).get("paid", True) else 0
    sub = q1("SELECT id FROM subscriptions WHERE org_id=? ORDER BY created_at DESC LIMIT 1", (oid,))
    if sub:
        ex("UPDATE subscriptions SET paid=? WHERE id=?", (paid, sub["id"]))
    audit("admin.mark_paid", oid, str(bool(paid)))
    return jsonify({"ok": True})


# ─── Trial auto-suspend job ──────────────────────────────────────────────────
def enforce_trials(conn=None):
    """Suspend free-trial orgs past trial_ends + grace; reactivation only via
    upgrade or admin extend. Paid plans are never auto-suspended."""
    own = conn is None
    c = conn or raw_conn()
    row = q1("SELECT value FROM settings WHERE org_id=? AND key='trial'", (PLATFORM,), conn=c)
    grace = (json.loads(row["value"]).get("grace_days", 3) if row else 3)
    cutoff = now() - grace * 86400
    expired = q("""SELECT id FROM orgs WHERE plan_id='starter' AND suspended=0
                   AND trial_ends IS NOT NULL AND trial_ends < ?""", (cutoff,), conn=c)
    for o in expired:
        ex("UPDATE orgs SET suspended=1 WHERE id=?", (o["id"],), conn=c)
        ex("INSERT INTO audit_logs (id,org_id,actor,action,target,meta,created_at) VALUES (?,?,?,?,?,?,?)",
           ("aud_trial_" + o["id"][-8:], o["id"], "system", "trial.expired_suspend", o["id"],
            f"grace {grace}d", now()), conn=c)
    if own:
        c.close()
    return len(expired)


_trial_thread = None


def start_trial_enforcer():
    global _trial_thread
    if _trial_thread:
        return

    def loop():
        while True:
            try:
                enforce_trials()
            except Exception:
                pass
            time.sleep(3600)  # hourly
    _trial_thread = threading.Thread(target=loop, daemon=True)
    _trial_thread.start()


@bp.get("/api/admin/orgs")
@require_superadmin
def list_orgs():
    orgs = q("SELECT * FROM orgs ORDER BY created_at DESC")
    for o in orgs:
        cred = q1("SELECT last_health, phone_id FROM credentials WHERE org_id=?", (o["id"],))
        o["connection"] = (cred or {}).get("last_health", "disconnected")
        o["members"] = q1("SELECT COUNT(*) c FROM memberships WHERE org_id=?", (o["id"],))["c"]
        o["messages_period"] = q1("""SELECT COALESCE(SUM(count),0) c FROM usage_records
                                     WHERE org_id=? AND period=?""", (o["id"], time.strftime("%Y-%m")))["c"]
    return jsonify({"orgs": orgs})


@bp.get("/api/admin/revenue")
@require_superadmin
def revenue():
    """Platform revenue (subscriptions) + metered Meta spend across all orgs."""
    by_plan = q("""SELECT o.plan_id, COUNT(*) orgs, COALESCE(SUM(p.price_inr),0) mrr_inr
                   FROM orgs o JOIN plans p ON p.id=o.plan_id GROUP BY o.plan_id""")
    spend = q1("SELECT COALESCE(SUM(cost_micros),0) m FROM usage_records")["m"]
    return jsonify({"mrr_by_plan": by_plan, "platform_meta_spend_inr": round(spend / 1_000_000, 2),
                    "total_orgs": q1("SELECT COUNT(*) c FROM orgs")["c"]})


@bp.post("/api/admin/orgs/<oid>/suspend")
@require_superadmin
def suspend(oid):
    susp = 1 if (request.get_json(force=True) or {}).get("suspend", True) else 0
    ex("UPDATE orgs SET suspended=? WHERE id=?", (susp, oid))
    audit("admin.suspend" if susp else "admin.reactivate", oid)
    return jsonify({"ok": True, "suspended": bool(susp)})


@bp.post("/api/admin/impersonate/<oid>")
@require_superadmin
def impersonate(oid):
    """Switch the admin's active org context (consent banner shown in UI)."""
    if not q1("SELECT id FROM orgs WHERE id=?", (oid,)):
        return jsonify({"error": "Org not found"}), 404
    # Ensure a membership exists so RBAC works during the session.
    if not q1("SELECT id FROM memberships WHERE org_id=? AND user_id=?", (oid, session["uid"])):
        from .db import insert, uid
        insert("memberships", {"id": uid("mem"), "org_id": oid, "user_id": session["uid"],
                               "role": "admin", "created_at": now()})
    session["org_id"] = oid
    session["impersonating"] = True
    audit("admin.impersonate", oid, "consent acknowledged")
    return jsonify({"ok": True, "org_id": oid, "banner": "You are impersonating this org (audited)."})


@bp.get("/api/admin/audit")
@require_superadmin
def global_audit():
    return jsonify({"events": q("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 500")})
