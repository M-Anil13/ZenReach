"""Meta webhook infrastructure (master prompt §8) — the heart of analytics.

GET  challenge verification.
POST X-Hub-Signature-256 validation, then idempotent fan-out:
  - message status (sent/delivered/read/failed) → projects onto recipients+campaigns
  - inbound messages → inbox + STOP→suppression + keyword automation
  - template status changes → templates catalog
  - account quality/tier changes → org + alerts
Events are written append-only to message_events (dedup by event_key); failed
processing is isolated per-event so one bad event can't drop a batch.
"""
import json
import os

from flask import Blueprint, request, jsonify

from .db import q1, ex, insert, uid, now, raw_conn
from . import security as sec

bp = Blueprint("webhooks", __name__)

VERIFY_TOKEN = os.environ.get("NEXZEN_VERIFY_TOKEN", "nexzen-verify")
APP_SECRET = os.environ.get("NEXZEN_META_APP_SECRET", "")   # PRODUCTION: set this


@bp.get("/webhooks/meta")
@bp.get("/api/webhooks/whatsapp")
@bp.get("/webhooks/whatsapp")
def verify_challenge():
    if (request.args.get("hub.mode") == "subscribe"
            and request.args.get("hub.verify_token") == VERIFY_TOKEN):
        return request.args.get("hub.challenge", ""), 200
    return "forbidden", 403


@bp.post("/webhooks/meta")
@bp.post("/api/webhooks/whatsapp")
@bp.post("/webhooks/whatsapp")
def receive():
    raw = request.get_data()
    if not sec.verify_meta_signature(APP_SECRET, raw, request.headers.get("X-Hub-Signature-256")):
        return "bad signature", 401
    try:
        body = json.loads(raw or b"{}")
    except Exception:
        return "bad json", 400

    conn = raw_conn()
    try:
        for entry in body.get("entry", []):
            for ch in entry.get("changes", []):
                field = ch.get("field")
                value = ch.get("value", {})
                try:
                    if field == "messages":
                        _handle_messages(conn, value)
                    elif field == "message_template_status_update":
                        _handle_template_status(conn, value)
                    elif field in ("account_update", "phone_number_quality_update", "account_alerts"):
                        _handle_account(conn, value)
                except Exception:
                    # Isolate per-event failures (real systems dead-letter here).
                    pass
    finally:
        conn.close()
    return jsonify({"ok": True})


def _org_for_phone(conn, phone_id):
    r = q1("SELECT org_id FROM credentials WHERE phone_id=?", (phone_id,), conn=conn)
    return r["org_id"] if r else None


def _dedup(conn, event_key):
    """True if this event is new (insert succeeds), False if already seen."""
    try:
        ex("INSERT INTO message_events (id, event_key, created_at) VALUES (?,?,?)",
           (uid("evt"), event_key, now()), conn=conn)
        return True
    except Exception:
        return False


def _handle_messages(conn, value):
    phone_id = value.get("metadata", {}).get("phone_number_id")
    org_id = _org_for_phone(conn, phone_id)

    # delivery status callbacks
    for st in value.get("statuses", []):
        mid, status = st.get("id"), st.get("status")
        key = f"{mid}:{status}"
        if not _dedup(conn, key):
            continue
        ex("UPDATE message_events SET org_id=?, message_id=?, status=?, payload=? WHERE event_key=?",
           (org_id, mid, status, json.dumps(st), key), conn=conn)
        col = {"sent": "sent_at", "delivered": "delivered_at", "read": "read_at"}.get(status)
        err = (st.get("errors") or [{}])[0]
        if status == "failed":
            ex("""UPDATE campaign_recipients SET status='failed', error_code=?, error_label=?
                  WHERE message_id=?""", (err.get("code"), err.get("title"), mid), conn=conn)
        elif col:
            new_status = "read" if status == "read" else ("delivered" if status == "delivered" else "sent")
            ex(f"UPDATE campaign_recipients SET status=?, {col}=? WHERE message_id=?",
               (new_status, now(), mid), conn=conn)
        _reproject(conn, mid)

    # inbound messages
    for msg in value.get("messages", []):
        key = f"in:{msg.get('id')}"
        if not _dedup(conn, key):
            continue
        frm = "+" + msg.get("from", "")
        text = (msg.get("text") or {}).get("body", "") or (msg.get("button") or {}).get("text", "")
        _route_inbound(conn, org_id, frm, text, msg.get("id"))


def _reproject(conn, mid):
    """Recompute the parent campaign's counters from its recipients (idempotent)."""
    r = q1("SELECT campaign_id FROM campaign_recipients WHERE message_id=?", (mid,), conn=conn)
    if not r:
        return
    cid = r["campaign_id"]
    s = q1("""SELECT SUM(status IN ('sent','delivered','read')) sent,
                     SUM(status IN ('delivered','read')) delivered,
                     SUM(status='read') read, SUM(status='failed') failed
              FROM campaign_recipients WHERE campaign_id=?""", (cid,), conn=conn)
    ex("UPDATE campaigns SET sent=?, delivered=?, read=?, failed=? WHERE id=?",
       (s["sent"] or 0, s["delivered"] or 0, s["read"] or 0, s["failed"] or 0, cid), conn=conn)


def _route_inbound(conn, org_id, phone, text, message_id):
    if not org_id:
        return
    from .inbox import ingest_inbound
    ingest_inbound(conn, org_id, phone, text, message_id)


def _handle_template_status(conn, value):
    name = value.get("message_template_name")
    status = value.get("event") or value.get("status")
    org_id = None
    waba = value.get("waba_id")
    if waba:
        r = q1("SELECT org_id FROM credentials WHERE waba_id=?", (waba,), conn=conn)
        org_id = r["org_id"] if r else None
    if org_id and name:
        ex("UPDATE templates SET status=?, rejection_reason=?, updated_at=? WHERE org_id=? AND name=?",
           (status, value.get("reason"), now(), org_id, name), conn=conn)


def _handle_account(conn, value):
    waba = value.get("waba_id")
    r = q1("SELECT org_id FROM credentials WHERE waba_id=?", (waba,), conn=conn) if waba else None
    if not r:
        return
    org_id = r["org_id"]
    rating = value.get("current_limit") or value.get("quality_rating")
    if rating:
        ex("UPDATE orgs SET quality_rating=? WHERE id=?", (str(rating), org_id), conn=conn)
        # PRODUCTION: email + in-app alert if rating drops to yellow/red.
        ex("INSERT INTO audit_logs (id, org_id, actor, action, target, meta, created_at) "
           "VALUES (?,?,?,?,?,?,?)",
           (uid("aud"), org_id, "system", "quality.changed", waba, str(rating), now()), conn=conn)
