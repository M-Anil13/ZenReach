"""Affiliate / referral program (master prompt §17f).

Any user can get a referral code/link; new orgs that sign up via that link are
tracked, and recurring commission accrues when they pay. A payout dashboard
shows referrals, status and earnings. Referral attribution is captured at
signup via the ?ref= code.
"""
from flask import Blueprint, request, jsonify, session

from .db import q, q1, ex, insert, uid, now
from .auth import require_login, current_user, audit

bp = Blueprint("affiliates", __name__)
DEFAULT_COMMISSION = 25  # percent, recurring


@bp.post("/api/affiliate/join")
@require_login
def join():
    u = current_user()
    existing = q1("SELECT * FROM affiliates WHERE user_id=?", (u["id"],))
    if existing:
        return jsonify({"ok": True, "code": existing["code"], "already": True})
    code = (u["email"].split("@")[0][:8] + uid()[:4]).lower()
    aid = uid("aff")
    insert("affiliates", {"id": aid, "user_id": u["id"], "code": code,
                          "commission_pct": DEFAULT_COMMISSION, "created_at": now()})
    audit("affiliate.joined", code)
    return jsonify({"ok": True, "code": code})


@bp.get("/api/affiliate/dashboard")
@require_login
def dashboard():
    u = current_user()
    aff = q1("SELECT * FROM affiliates WHERE user_id=?", (u["id"],))
    if not aff:
        return jsonify({"joined": False})
    refs = q("SELECT * FROM referrals WHERE affiliate_id=? ORDER BY created_at DESC", (aff["id"],))
    earned = q1("SELECT COALESCE(SUM(amount_micros),0) m FROM referrals WHERE affiliate_id=?",
                (aff["id"],))["m"]
    link = request.host_url.rstrip("/") + "/console?ref=" + aff["code"]
    return jsonify({"joined": True, "code": aff["code"], "commission_pct": aff["commission_pct"],
                    "referral_link": link, "referrals": refs,
                    "earned_inr": round(earned / 1_000_000, 2),
                    "paid_out_inr": round(aff["paid_out_micros"] / 1_000_000, 2),
                    "signups": len(refs),
                    "paying": sum(1 for r in refs if r["status"] == "paying")})


def attribute_signup(ref_code, org_id):
    """Called at signup when a ?ref= code is present."""
    if not ref_code:
        return
    aff = q1("SELECT id FROM affiliates WHERE code=?", (ref_code,))
    if aff:
        insert("referrals", {"id": uid("ref"), "affiliate_id": aff["id"], "referred_org": org_id,
                             "status": "signed_up", "amount_micros": 0, "created_at": now()})


def record_commission(org_id, paid_micros):
    """Called when a referred org pays; accrues recurring commission."""
    ref = q1("SELECT r.*, a.commission_pct FROM referrals r JOIN affiliates a ON a.id=r.affiliate_id "
             "WHERE r.referred_org=?", (org_id,))
    if not ref:
        return
    commission = int(paid_micros * ref["commission_pct"] / 100)
    ex("UPDATE referrals SET status='paying', amount_micros=amount_micros+? WHERE id=?",
       (commission, ref["id"]))


# ---- super-admin payout view ------------------------------------------------
@bp.get("/api/admin/affiliates")
def admin_affiliates():
    u = current_user()
    if not u or not u["is_superadmin"]:
        return jsonify({"error": "Super-admin only"}), 403
    rows = q("""SELECT a.code, a.commission_pct, a.paid_out_micros,
                (SELECT COUNT(*) FROM referrals r WHERE r.affiliate_id=a.id) signups,
                (SELECT COALESCE(SUM(amount_micros),0) FROM referrals r WHERE r.affiliate_id=a.id) owed
                FROM affiliates a""")
    for r in rows:
        r["owed_inr"] = round(r["owed"] / 1_000_000, 2)
    return jsonify({"affiliates": rows})
