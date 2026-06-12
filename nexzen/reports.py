"""Reports & analytics (master prompt §10) + click tracking (§16).

Per-campaign stat cards, delivery funnel, why-it-failed breakdown by Meta error
code, per-contact log, org dashboard, A/B winner, and async CSV/XLSX exports.
Click tracking routes URL-button taps through org short links.
"""
import io
import os
import threading

import pandas as pd
from flask import Blueprint, request, jsonify, g, send_file, redirect

from .db import q, q1, ex, insert, update, uid, now, raw_conn
from .auth import require_org, require_role
from .meta import explain

bp = Blueprint("reports", __name__)
EXPORT_DIR = os.path.join(os.path.dirname(__file__), "..", "exports")
os.makedirs(EXPORT_DIR, exist_ok=True)


@bp.get("/api/reports/campaign/<cid>")
@require_org
def campaign_report(cid):
    c = q1("SELECT * FROM campaigns WHERE id=? AND org_id=?", (cid, g.org["id"]))
    if not c:
        return jsonify({"error": "Not found"}), 404
    recips = q("SELECT * FROM campaign_recipients WHERE campaign_id=?", (cid,))

    cards = {"total": c["total"], "attempted": c["attempted"], "skipped": c["skipped"],
             "sent": c["sent"], "delivered": c["delivered"], "read": c["read"],
             "failed": c["failed"], "clicks": c["clicks"]}
    cards["read_rate"] = round(100 * c["read"] / c["delivered"], 1) if c["delivered"] else 0
    cards["delivery_rate"] = round(100 * c["delivered"] / c["attempted"], 1) if c["attempted"] else 0

    # Why it failed — grouped by error code w/ plain-English label + fix.
    fails = {}
    for r in recips:
        if r["status"] == "failed":
            code = r["error_code"] or 0
            info = explain(code, r["error_label"] or "Unknown error")
            key = info["label"]
            fails.setdefault(key, {"count": 0, "code": code, "fix": info["fix"]})
            fails[key]["count"] += 1
    skips = {}
    for r in recips:
        if r["status"] == "skipped":
            skips[r["skip_reason"] or "other"] = skips.get(r["skip_reason"] or "other", 0) + 1

    # delivery funnel
    funnel = [("Attempted", c["attempted"]), ("Sent", c["sent"]),
              ("Delivered", c["delivered"]), ("Read", c["read"])]
    return jsonify({"campaign": c, "cards": cards, "failures": fails, "skips": skips,
                    "funnel": funnel, "recipients": recips[:1000]})


@bp.get("/api/reports/dashboard")
@require_org
def dashboard():
    oid = g.org["id"]
    camps = q("SELECT * FROM campaigns WHERE org_id=? ORDER BY created_at DESC LIMIT 50", (oid,))
    totals = q1("""SELECT COALESCE(SUM(attempted),0) attempted, COALESCE(SUM(delivered),0) delivered,
                          COALESCE(SUM(read),0) read, COALESCE(SUM(failed),0) failed,
                          COALESCE(SUM(clicks),0) clicks FROM campaigns WHERE org_id=?""", (oid,))
    best = q("""SELECT template_name, AVG(CASE WHEN delivered>0 THEN 100.0*read/delivered ELSE 0 END) read_rate,
                COUNT(*) runs FROM campaigns WHERE org_id=? AND template_name IS NOT NULL
                GROUP BY template_name ORDER BY read_rate DESC LIMIT 5""", (oid,))
    org = q1("SELECT quality_rating, messaging_tier FROM orgs WHERE id=?", (oid,))
    optout = q1("SELECT COUNT(*) c FROM suppression WHERE org_id=?", (oid,))["c"]
    spend = q1("SELECT COALESCE(SUM(cost_micros),0) m FROM usage_records WHERE org_id=?", (oid,))["m"]
    return jsonify({"totals": totals, "best_templates": best, "quality": org["quality_rating"],
                    "tier": org["messaging_tier"], "opt_outs": optout,
                    "spend_inr": round(spend / 1_000_000, 2), "campaigns_over_time": camps})


@bp.get("/api/reports/campaign/<cid>/ab")
@require_org
def ab_result(cid):
    """A/B: compare variants by read or reply rate, declare a winner."""
    c = q1("SELECT * FROM campaigns WHERE id=? AND org_id=?", (cid, g.org["id"]))
    if not c or not c["variant_b"]:
        return jsonify({"error": "Not an A/B campaign"}), 400
    out = {}
    for v in ("a", "b"):
        s = q1("""SELECT COUNT(*) n, SUM(status IN ('delivered','read')) delivered, SUM(status='read') read
                  FROM campaign_recipients WHERE campaign_id=? AND variant=?""", (cid, v))
        rate = round(100 * (s["read"] or 0) / s["delivered"], 1) if s["delivered"] else 0
        out[v] = {"template": c["variant_a"] if v == "a" else c["variant_b"],
                  "n": s["n"], "read_rate": rate}
    out["winner"] = "a" if out["a"]["read_rate"] >= out["b"]["read_rate"] else "b"
    return jsonify(out)


# ---- async exports ----------------------------------------------------------
@bp.post("/api/reports/campaign/<cid>/export")
@require_role("agent")
def start_export(cid):
    fmt = (request.get_json(force=True) or {}).get("format", "xlsx")
    eid = uid("exp")
    insert("exports", {"id": eid, "org_id": g.org["id"], "kind": f"campaign:{cid}:{fmt}",
                       "status": "pending", "created_at": now()})
    threading.Thread(target=_build_export, args=(eid, g.org["id"], cid, fmt), daemon=True).start()
    return jsonify({"ok": True, "export_id": eid})


def _build_export(eid, org_id, cid, fmt):
    conn = raw_conn()
    try:
        recips = q("SELECT contact_name, contact_phone, status, error_label, skip_reason "
                   "FROM campaign_recipients WHERE campaign_id=?", (cid,), conn=conn)
        df = pd.DataFrame(recips or [], columns=["contact_name", "contact_phone", "status",
                                                 "error_label", "skip_reason"])
        path = os.path.join(EXPORT_DIR, f"{eid}.{ 'csv' if fmt=='csv' else 'xlsx'}")
        if fmt == "csv":
            df.to_csv(path, index=False)
        else:
            with pd.ExcelWriter(path, engine="openpyxl") as w:
                df.to_excel(w, index=False, sheet_name="Recipients")
        ex("UPDATE exports SET status='ready', path=? WHERE id=?", (path, eid), conn=conn)
        # PRODUCTION: email the download link when ready.
    except Exception as e:
        ex("UPDATE exports SET status=? WHERE id=?", (f"error:{e}", eid), conn=conn)
    finally:
        conn.close()


@bp.get("/api/exports")
@require_org
def list_exports():
    return jsonify({"exports": q("SELECT id, kind, status, created_at FROM exports "
                                 "WHERE org_id=? ORDER BY created_at DESC LIMIT 50", (g.org["id"],))})


@bp.get("/api/exports/<eid>/download")
@require_org
def download_export(eid):
    e = q1("SELECT * FROM exports WHERE id=? AND org_id=? AND status='ready'", (eid, g.org["id"]))
    if not e:
        return jsonify({"error": "Not ready"}), 404
    return send_file(e["path"], as_attachment=True, download_name=os.path.basename(e["path"]))


# ---- click tracking ---------------------------------------------------------
@bp.post("/api/links")
@require_role("agent")
def make_link():
    d = request.get_json(force=True) or {}
    lid = uid("lnk")
    insert("short_links", {"id": lid, "org_id": g.org["id"], "campaign_id": d.get("campaign_id"),
                           "contact_phone": d.get("contact_phone"), "target": d["target"],
                           "clicks": 0, "created_at": now()})
    return jsonify({"ok": True, "short": f"/l/{lid}"})


@bp.get("/l/<lid>")
def follow_link(lid):
    link = q1("SELECT * FROM short_links WHERE id=?", (lid,))
    if not link:
        return "Not found", 404
    ex("UPDATE short_links SET clicks=clicks+1 WHERE id=?", (lid,))
    if link["campaign_id"]:
        ex("UPDATE campaigns SET clicks=clicks+1 WHERE id=?", (link["campaign_id"],))
        if link["contact_phone"]:
            ex("UPDATE contacts SET last_engaged=? WHERE org_id=? AND phone=?",
               (now(), link["org_id"], link["contact_phone"]))
    return redirect(link["target"])
