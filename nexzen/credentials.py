"""WhatsApp credentials onboarding (master prompt §4).

Tokens are AES-256-GCM encrypted at rest, never returned to the browser after
save. Save-&-verify makes a live Graph call. A background health job validates
each org's token daily and before every scheduled campaign.
"""
import threading
import time

from flask import Blueprint, request, jsonify, g

from .db import q, q1, ex, now, raw_conn
from .auth import require_org, require_role, audit
from . import security as sec
from .meta import MetaClient, GRAPH_VERSION

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

    ok, info = MetaClient(token, phone_id, waba_id).verify()
    if not ok:
        return jsonify({"ok": False, "error": info["label"], "fix": info.get("fix"),
                        "code": info.get("code")}), 400

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
       (g.org["id"], sec.encrypt(token), phone_id, waba_id, info.get("verified_name"),
        info.get("display_phone_number"), "healthy", now(), GRAPH_VERSION, now()))
    ex("UPDATE orgs SET demo_mode=0, messaging_tier=?, quality_rating=? WHERE id=?",
       (tier, info.get("quality_rating", "UNKNOWN"), g.org["id"]))
    audit("credentials.connected", phone_id)
    return jsonify({"ok": True, "name": info.get("verified_name"),
                    "number": info.get("display_phone_number"),
                    "quality": info.get("quality_rating"), "tier": tier})


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
