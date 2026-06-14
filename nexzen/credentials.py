"""WhatsApp credentials onboarding (master prompt §4).

Tokens are AES-256-GCM encrypted at rest, never returned to the browser after
save. Save-&-verify makes a live Graph call. A background health job validates
each org's token daily and before every scheduled campaign.
"""
import threading
import time

import requests
from flask import Blueprint, request, jsonify, g

from .db import q, q1, ex, now, raw_conn
from .auth import require_org, require_role, audit
from . import security as sec
from .meta import MetaClient, GRAPH_VERSION, GRAPH

bp = Blueprint("credentials", __name__)


def get_client(org_id):
    """Build a MetaClient from stored encrypted creds, or None if not connected."""
    c = q1("SELECT * FROM credentials WHERE org_id=?", (org_id,))
    if not c or not c["token_enc"]:
        return None
    try:
        token = sec.decrypt(c["token_enc"])
    except Exception:
        return None
    return MetaClient(token, c["phone_id"], c["waba_id"], c.get("graph_version") or GRAPH_VERSION)


import os

# ===== Embedded Signup (master prompt §4, PRIMARY onboarding) ================
# Goes live only when NexZen has: a registered Meta app (META_APP_ID/SECRET), an
# Embedded-Signup configuration id (META_CONFIG_ID), Tech-Provider status and
# App Review for Advanced Access to whatsapp_business_messaging +
# whatsapp_business_management. Until then the UI hides the button and manual
# entry (below) is the working path.
META_APP_ID = os.environ.get("META_APP_ID", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
META_CONFIG_ID = os.environ.get("META_CONFIG_ID", "")
GRAPH_OAUTH = f"{GRAPH}/oauth/access_token"


@bp.get("/api/credentials/embedded/config")
@require_org
def embedded_config():
    """Frontend uses this to launch Meta's Embedded Signup popup."""
    return jsonify({"enabled": bool(META_APP_ID and META_CONFIG_ID),
                    "app_id": META_APP_ID, "config_id": META_CONFIG_ID,
                    "graph_version": GRAPH_VERSION})


@bp.post("/api/credentials/embedded/exchange")
@require_role("admin")
def embedded_exchange():
    """Exchange the Embedded-Signup auth code for a long-lived token and store
    the WABA/Phone the popup created. Phone Number ID + WABA ID come from the
    popup's WA_EMBEDDED_SIGNUP message event."""
    if not (META_APP_ID and META_APP_SECRET):
        return jsonify({"ok": False, "error": "Embedded Signup not configured (Meta app + App Review required). Use manual entry."}), 400
    d = request.get_json(force=True) or {}
    code = (d.get("code") or "").strip()
    phone_id = (d.get("phone_number_id") or "").strip()
    waba_id = (d.get("waba_id") or "").strip()
    if not code or not phone_id:
        return jsonify({"ok": False, "error": "Missing code or phone_number_id from the popup."}), 400
    if not q1("SELECT prereqs_ack FROM orgs WHERE id=?", (g.org["id"],))["prereqs_ack"]:
        return jsonify({"ok": False, "error": "Acknowledge the prerequisites checklist first.",
                        "need_prereqs": True}), 400
    try:
        r = requests.get(GRAPH_OAUTH, params={
            "client_id": META_APP_ID, "client_secret": META_APP_SECRET,
            "code": code, "grant_type": "authorization_code"}, timeout=20)
        j = r.json()
        token = j.get("access_token")
        if not token:
            return jsonify({"ok": False, "error": j.get("error", {}).get("message", "Token exchange failed")}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Token exchange error: {e}"}), 502

    ok, info = MetaClient(token, phone_id, waba_id).verify()
    if not ok:
        return jsonify({"ok": False, "error": info.get("label", "Verification failed"),
                        "fix": info.get("fix")}), 400
    _store_creds(g.org["id"], token, phone_id, waba_id, info)
    audit("credentials.embedded_connected", phone_id)
    return jsonify({"ok": True, "name": info.get("verified_name"),
                    "number": info.get("display_phone_number")})


PREREQS = [
    {"key": "number_free", "label": "A phone number NOT active on the WhatsApp / WhatsApp Business mobile app"},
    {"key": "business_docs", "label": "Registered business with valid business documents"},
    {"key": "website_privacy", "label": "Live business website with a privacy-policy page"},
    {"key": "payment_method", "label": "A debit/credit card for Meta's per-message API charges"},
    {"key": "gst", "label": "GST number (mandatory for Indian businesses — Meta needs it to activate billing)"},
]


@bp.get("/api/onboarding/prereqs")
@require_org
def get_prereqs():
    o = q1("SELECT prereqs_ack, gst_number, category FROM orgs WHERE id=?", (g.org["id"],))
    return jsonify({"items": PREREQS, "acknowledged": bool(o["prereqs_ack"]),
                    "gst_number": o["gst_number"] or "", "is_indian": True})


@bp.post("/api/onboarding/prereqs")
@require_role("admin")
def ack_prereqs():
    d = request.get_json(force=True) or {}
    gst = (d.get("gst_number") or "").strip()
    # GST mandatory to acknowledge (Indian-business requirement).
    if not gst:
        return jsonify({"error": "GST number is required to proceed (Meta billing prerequisite)."}), 400
    ex("UPDATE orgs SET prereqs_ack=1, gst_number=? WHERE id=?", (gst, g.org["id"]))
    audit("onboarding.prereqs_ack", g.org["id"])
    return jsonify({"ok": True})


@bp.get("/api/credentials")
@require_org
def get_creds():
    c = q1("SELECT * FROM credentials WHERE org_id=?", (g.org["id"],))
    if not c:
        return jsonify({"connected": False})
    # Never leak the token. Only metadata.
    return jsonify({"connected": bool(c["token_enc"]), "phone_id": c["phone_id"],
                    "waba_id": c["waba_id"], "verified_name": c["verified_name"],
                    "display_number": c["display_number"], "health": c["last_health"],
                    "graph_version": c["graph_version"]})


@bp.post("/api/credentials/verify")
@require_role("admin")
def verify():
    d = request.get_json(force=True) or {}
    token = (d.get("token") or "").strip()
    phone_id = (d.get("phone_id") or "").strip()
    waba_id = (d.get("waba_id") or "").strip()
    if not token or not phone_id:
        return jsonify({"ok": False, "error": "Access token and Phone Number ID are required."}), 400

    # Onboarding gate: prerequisites (incl. GST) must be acknowledged first.
    if not q1("SELECT prereqs_ack FROM orgs WHERE id=?", (g.org["id"],))["prereqs_ack"]:
        return jsonify({"ok": False, "error": "Complete the onboarding prerequisites checklist first.",
                        "need_prereqs": True}), 400

    ok, info = MetaClient(token, phone_id, waba_id).verify()
    if not ok:
        return jsonify({"ok": False, "error": info["label"], "fix": info.get("fix"),
                        "code": info.get("code")}), 400

    tier = _store_creds(g.org["id"], token, phone_id, waba_id, info)
    audit("credentials.connected", phone_id)
    return jsonify({"ok": True, "name": info.get("verified_name"),
                    "number": info.get("display_phone_number"),
                    "quality": info.get("quality_rating"), "tier": tier})


def _store_creds(org_id, token, phone_id, waba_id, info):
    """Encrypt + persist credentials and lift the org out of demo mode."""
    tier_map = {"TIER_250": 250, "TIER_1K": 1000, "TIER_10K": 10000,
                "TIER_100K": 100000, "TIER_UNLIMITED": 10 ** 9}
    tier = tier_map.get(info.get("messaging_limit_tier"), 250)
    ex("""INSERT INTO credentials (org_id, token_enc, phone_id, waba_id, verified_name,
           display_number, last_health, last_checked, graph_version, updated_at)
          VALUES (?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(org_id) DO UPDATE SET token_enc=excluded.token_enc,
           phone_id=excluded.phone_id, waba_id=excluded.waba_id,
           verified_name=excluded.verified_name, display_number=excluded.display_number,
           last_health='healthy', last_checked=excluded.last_checked, updated_at=excluded.updated_at""",
       (org_id, sec.encrypt(token), phone_id, waba_id, info.get("verified_name"),
        info.get("display_phone_number"), "healthy", now(), GRAPH_VERSION, now()))
    ex("UPDATE orgs SET demo_mode=0, messaging_tier=?, quality_rating=? WHERE id=?",
       (tier, info.get("quality_rating", "UNKNOWN"), org_id))
    return tier


@bp.post("/api/credentials/disconnect")
@require_role("admin")
def disconnect():
    ex("DELETE FROM credentials WHERE org_id=?", (g.org["id"],))
    ex("UPDATE orgs SET demo_mode=1 WHERE id=?", (g.org["id"],))
    audit("credentials.disconnected", g.org["id"])
    return jsonify({"ok": True})


def check_token_health(org_id, conn=None):
    """Validate one org's token now. Used by the daily job and pre-launch."""
    own = conn is None
    c = conn or raw_conn()
    row = q1("SELECT * FROM credentials WHERE org_id=?", (org_id,), conn=c)
    if not row or not row["token_enc"]:
        if own:
            c.close()
        return "disconnected"
    try:
        token = sec.decrypt(row["token_enc"])
        ok, info = MetaClient(token, row["phone_id"], row["waba_id"]).verify()
        health = "healthy" if ok else "broken"
        ex("UPDATE credentials SET last_health=?, last_checked=? WHERE org_id=?",
           (health, now(), org_id), conn=c)
        if not ok:
            # PRODUCTION: also send email + in-app alert here.
            ex("INSERT INTO audit_logs (id, org_id, actor, action, target, meta, created_at) "
               "VALUES (?,?,?,?,?,?,?)",
               ("aud_" + str(now()), org_id, "system", "token.health_alert",
                row["phone_id"], info.get("label", "token broken"), now()), conn=c)
        return health
    finally:
        if own:
            c.close()


_health_thread = None


def start_health_monitor():
    """Daily background validation of every org's token."""
    global _health_thread
    if _health_thread:
        return

    def loop():
        while True:
            try:
                conn = raw_conn()
                for o in q("SELECT id FROM orgs WHERE suspended=0", conn=conn):
                    check_token_health(o["id"], conn=conn)
                conn.close()
            except Exception:
                pass
            time.sleep(86400)  # daily

    _health_thread = threading.Thread(target=loop, daemon=True)
    _health_thread.start()
