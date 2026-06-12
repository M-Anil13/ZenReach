"""Public API & integrations (master prompt §12, Enterprise).

Org-scoped API keys (shown once, stored hashed) authorize a documented REST
surface: manage contacts, trigger campaigns, fetch reports. Outbound webhooks
push campaign.completed / message.failed / contact.opted_out to client systems,
signed with a per-hook secret.
"""
import functools
import json
import threading

import requests
from flask import Blueprint, request, jsonify, g

from .db import q, q1, ex, insert, uid, now, raw_conn
from .auth import require_role, require_org
from . import security as sec

bp = Blueprint("publicapi", __name__)


# ---- key management (dashboard, session-auth) -------------------------------
@bp.get("/api/keys")
@require_role("admin")
def list_keys():
    return jsonify({"keys": q("SELECT id, name, prefix, last_used, revoked, created_at "
                              "FROM api_keys WHERE org_id=?", (g.org["id"],))})


@bp.post("/api/keys")
@require_role("admin")
def create_key():
    raw, prefix, h = sec.new_api_key()
    kid = uid("key")
    insert("api_keys", {"id": kid, "org_id": g.org["id"], "name": (request.get_json(force=True) or {}).get("name", "key"),
                        "prefix": prefix, "key_hash": h, "created_at": now()})
    # Plaintext returned exactly once.
    return jsonify({"ok": True, "id": kid, "api_key": raw, "note": "Shown once — store it now."})


@bp.delete("/api/keys/<kid>")
@require_role("admin")
def revoke_key(kid):
    ex("UPDATE api_keys SET revoked=1 WHERE id=? AND org_id=?", (kid, g.org["id"]))
    return jsonify({"ok": True})


@bp.post("/api/webhooks/outbound")
@require_role("admin")
def add_outbound():
    d = request.get_json(force=True) or {}
    wid = uid("owh")
    insert("outbound_webhooks", {"id": wid, "org_id": g.org["id"], "url": d["url"],
                                 "secret": d.get("secret", uid("whs")),
                                 "events": json.dumps(d.get("events", ["campaign.completed"])), "enabled": 1})
    return jsonify({"ok": True, "id": wid})


# ---- API-key authentication for the public surface --------------------------
def require_api_key(fn):
    @functools.wraps(fn)
    def w(*a, **k):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Missing API key"}), 401
        row = q1("SELECT * FROM api_keys WHERE key_hash=? AND revoked=0",
                 (sec.hash_api_key(auth[7:]),))
        if not row:
            return jsonify({"error": "Invalid API key"}), 401
        org = q1("SELECT * FROM orgs WHERE id=?", (row["org_id"],))
        plan = q1("SELECT api_access FROM plans WHERE id=?", (org["plan_id"],))
        if not (plan or {}).get("api_access"):
            return jsonify({"error": "API access requires the Enterprise plan."}), 403
        ex("UPDATE api_keys SET last_used=? WHERE id=?", (now(), row["id"]))
        g.api_org = org
        return fn(*a, **k)
    return w


@bp.get("/v1/contacts")
@require_api_key
def api_contacts():
    return jsonify({"contacts": q("SELECT phone, name, opt_in, country FROM contacts WHERE org_id=? LIMIT 1000",
                                   (g.api_org["id"],))})


@bp.post("/v1/contacts")
@require_api_key
def api_add_contact():
    from . import phones
    d = request.get_json(force=True) or {}
    ph = phones.to_e164(d.get("phone"), d.get("default_cc", "91"))
    if not phones.is_valid(ph):
        return jsonify({"error": "Invalid phone"}), 400
    country, tz = phones.country_tz(ph)
    ex("""INSERT OR IGNORE INTO contacts (id, org_id, phone, name, country, timezone, opt_in,
          opt_in_source, opt_in_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
       (uid("con"), g.api_org["id"], ph, d.get("name", ""), country, tz, 1 if d.get("opt_in") else 0,
        "api", now() if d.get("opt_in") else None, now()))
    return jsonify({"ok": True, "phone": ph})


@bp.post("/v1/campaigns")
@require_api_key
def api_trigger_campaign():
    """Trigger a campaign via API. Reuses the same engine + guards as the UI."""
    from .campaigns import _spawn
    from .templates_mod import approved_template
    d = request.get_json(force=True) or {}
    org = g.api_org
    if not org["demo_mode"] and not approved_template(org["id"], d.get("template")):
        return jsonify({"error": "Template must be APPROVED"}), 400
    cid = uid("cmp")
    insert("campaigns", {"id": cid, "org_id": org["id"], "name": d.get("name", "API campaign"),
                         "template_name": d.get("template"), "template_lang": d.get("lang", "en_US"),
                         "status": "running", "variant_a": d.get("template"),
                         "total": 0, "created_by": "api", "created_at": now()})
    from . import phones
    n = 0
    for c in d.get("contacts", []):
        ph = phones.to_e164(c.get("phone"), d.get("default_cc", "91"))
        if not phones.is_valid(ph):
            continue
        insert("campaign_recipients", {"id": uid("rcp"), "campaign_id": cid, "org_id": org["id"],
                                       "contact_phone": ph, "contact_name": c.get("name", ""),
                                       "idem_key": f"{cid}:{ph}", "status": "queued", "queued_at": now()})
        n += 1
    ex("UPDATE campaigns SET total=?, attempted=? WHERE id=?", (n, n, cid))
    _spawn(cid)
    return jsonify({"ok": True, "campaign_id": cid, "queued": n})


@bp.get("/v1/reports/<cid>")
@require_api_key
def api_report(cid):
    c = q1("SELECT * FROM campaigns WHERE id=? AND org_id=?", (cid, g.api_org["id"]))
    if not c:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"campaign": c})


# ---- outbound webhook dispatch ----------------------------------------------
def fire_webhook(org_id, event, data):
    """Best-effort signed POST to every subscribed outbound webhook."""
    if not org_id:
        return

    def send():
        conn = raw_conn()
        try:
            for w in q("SELECT * FROM outbound_webhooks WHERE org_id=? AND enabled=1", (org_id,), conn=conn):
                if event not in json.loads(w["events"] or "[]"):
                    continue
                body = json.dumps({"event": event, "data": data, "ts": now()}).encode()
                try:
                    requests.post(w["url"], data=body, timeout=10,
                                  headers={"Content-Type": "application/json",
                                           "X-NexZen-Signature": sec.sign_payload(w["secret"], body)})
                except Exception:
                    pass
        finally:
            conn.close()

    threading.Thread(target=send, daemon=True).start()
