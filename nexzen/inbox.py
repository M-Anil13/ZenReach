"""Shared team inbox + auto-replies (master prompt §9).

Conversations from campaign replies; free-form replies allowed only inside the
24-hour customer-service window (enforced here), template-only outside it.
Keyword automation drives auto-replies and STOP→suppression. Plus a
click-to-WhatsApp link / QR target per org for opt-in collection.
"""
import os
import time

from flask import Blueprint, request, jsonify, g

from .db import q, q1, ex, insert, update, uid, now
from .auth import require_org, require_role, audit
from .credentials import get_client

bp = Blueprint("inbox", __name__)
WINDOW = 24 * 3600


def ingest_inbound(conn, org_id, phone, text, message_id):
    """Called by the webhook layer for every inbound message."""
    from .contacts import is_stop_keyword, suppress
    convo = q1("SELECT * FROM conversations WHERE org_id=? AND contact_phone=?", (org_id, phone), conn=conn)
    if convo:
        ex("""UPDATE conversations SET unread=unread+1, last_msg_at=?, window_expires=?
              WHERE id=?""", (now(), now() + WINDOW, convo["id"]), conn=conn)
        convo_id = convo["id"]
    else:
        convo_id = uid("cnv")
        ex("""INSERT INTO conversations (id, org_id, contact_phone, unread, window_expires, last_msg_at)
              VALUES (?,?,?,?,?,?)""", (convo_id, org_id, phone, 1, now() + WINDOW, now()), conn=conn)
    ex("""INSERT INTO messages (id, org_id, conversation_id, direction, body, message_id, created_at)
          VALUES (?,?,?,?,?,?,?)""",
       (uid("msg"), org_id, convo_id, "in", text, message_id, now()), conn=conn)
    # Mark contact engaged (feeds segments).
    ex("UPDATE contacts SET last_engaged=? WHERE org_id=? AND phone=?", (now(), org_id, phone), conn=conn)
    # Outbound webhook hook handled by caller.

    if is_stop_keyword(text):
        suppress(org_id, phone, "inbound_stop")
        _auto_send(conn, org_id, phone, "You've been unsubscribed. Reply START to opt back in.")
        return

    # Keyword automations
    kw = (text or "").strip().lower()
    for a in q("SELECT * FROM automations WHERE org_id=? AND enabled=1", (org_id,), conn=conn):
        if a["keyword"].lower() in kw:
            if a["reply"]:
                _auto_send(conn, org_id, phone, a["reply"])
            if a["tag"]:
                _tag_contact(conn, org_id, phone, a["tag"])
            break


def _auto_send(conn, org_id, phone, text):
    client = get_client(org_id)
    mid = None
    if client:
        ok, res = client.send_text(phone, text)   # valid: inside 24h window
        mid = res if ok else None
    convo = q1("SELECT id FROM conversations WHERE org_id=? AND contact_phone=?", (org_id, phone), conn=conn)
    if convo:
        ex("""INSERT INTO messages (id, org_id, conversation_id, direction, body, message_id, created_at)
              VALUES (?,?,?,?,?,?,?)""",
           (uid("msg"), org_id, convo["id"], "out", text, mid, now()), conn=conn)


def _tag_contact(conn, org_id, phone, tag):
    c = q1("SELECT id, attrs FROM contacts WHERE org_id=? AND phone=?", (org_id, phone), conn=conn)
    if c:
        import json
        attrs = json.loads(c["attrs"] or "{}")
        attrs["tag"] = tag
        ex("UPDATE contacts SET attrs=? WHERE id=?", (json.dumps(attrs), c["id"]), conn=conn)


# ---- routes -----------------------------------------------------------------
@bp.get("/api/inbox")
@require_org
def conversations():
    rows = q("""SELECT * FROM conversations WHERE org_id=? ORDER BY last_msg_at DESC LIMIT 200""",
             (g.org["id"],))
    for r in rows:
        r["window_open"] = (r["window_expires"] or 0) > now()
    return jsonify({"conversations": rows})


@bp.get("/api/inbox/<cid>")
@require_org
def thread(cid):
    convo = q1("SELECT * FROM conversations WHERE id=? AND org_id=?", (cid, g.org["id"]))
    if not convo:
        return jsonify({"error": "Not found"}), 404
    ex("UPDATE conversations SET unread=0 WHERE id=?", (cid,))
    convo["window_open"] = (convo["window_expires"] or 0) > now()
    return jsonify({"conversation": convo,
                    "messages": q("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at", (cid,))})


@bp.post("/api/inbox/<cid>/reply")
@require_role("agent")
def reply(cid):
    convo = q1("SELECT * FROM conversations WHERE id=? AND org_id=?", (cid, g.org["id"]))
    if not convo:
        return jsonify({"error": "Not found"}), 404
    text = (request.get_json(force=True) or {}).get("text", "")
    if (convo["window_expires"] or 0) <= now():
        return jsonify({"error": "24h service window closed — send an approved template instead."}), 400
    client = get_client(g.org["id"])
    mid = None
    if client:
        ok, res = client.send_text(convo["contact_phone"], text)
        if not ok:
            return jsonify({"error": res.get("label", "Send failed")}), 400
        mid = res
    insert("messages", {"id": uid("msg"), "org_id": g.org["id"], "conversation_id": cid,
                        "direction": "out", "body": text, "message_id": mid, "created_at": now()})
    ex("UPDATE conversations SET last_msg_at=? WHERE id=?", (now(), cid))
    return jsonify({"ok": True, "demo": client is None})


@bp.post("/api/inbox/<cid>/assign")
@require_role("agent")
def assign(cid):
    update("conversations", cid, {"assigned_to": (request.get_json(force=True) or {}).get("agent")})
    return jsonify({"ok": True})


@bp.post("/api/inbox/<cid>/note")
@require_role("agent")
def note(cid):
    update("conversations", cid, {"note": (request.get_json(force=True) or {}).get("note")})
    return jsonify({"ok": True})


# ---- automations ------------------------------------------------------------
@bp.get("/api/automations")
@require_org
def list_automations():
    return jsonify({"automations": q("SELECT * FROM automations WHERE org_id=?", (g.org["id"],))})


@bp.post("/api/automations")
@require_role("agent")
def create_automation():
    d = request.get_json(force=True) or {}
    aid = uid("aut")
    insert("automations", {"id": aid, "org_id": g.org["id"], "keyword": d.get("keyword", ""),
                           "reply": d.get("reply", ""), "tag": d.get("tag"), "enabled": 1})
    audit("automation.created", d.get("keyword", ""))
    return jsonify({"ok": True, "id": aid})


# ---- click-to-WhatsApp + QR target ------------------------------------------
@bp.get("/api/inbox/cta-link")
@require_org
def cta_link():
    cred = q1("SELECT display_number FROM credentials WHERE org_id=?", (g.org["id"],))
    num = (cred or {}).get("display_number", "").replace("+", "").replace(" ", "") if cred else ""
    text = request.args.get("text", "Hi! I'd like to opt in.")
    link = f"https://wa.me/{num}?text={text.replace(' ', '%20')}" if num else ""
    return jsonify({"link": link, "qr": f"https://api.qrserver.com/v1/create-qr-code/?data={link}" if link else ""})
