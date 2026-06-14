"""Lead capture (master prompt §17c) — grow the audience.

- Capture forms with public submit endpoints (embed on any site).
- A floating website widget (JS snippet) that posts opt-in contacts straight
  into a list.
- Click-to-WhatsApp Ad lead ingestion via a per-org token (generic; a real
  Meta Lead Ads integration needs Meta app review — this accepts the lead
  payload and routes it identically).
- Every captured contact stores a `source` attribution and can auto-enter a
  journey / sequence.
"""
import json

from flask import Blueprint, request, jsonify, g, Response

from .db import q, q1, ex, insert, uid, now
from .auth import require_org, require_role, audit
from . import phones

bp = Blueprint("capture", __name__)


# ---- form CRUD (authenticated) ----------------------------------------------
@bp.get("/api/capture/forms")
@require_org
def list_forms():
    return jsonify({"forms": q("SELECT * FROM capture_forms WHERE org_id=? ORDER BY created_at DESC",
                               (g.org["id"],))})


@bp.post("/api/capture/forms")
@require_role("agent")
def create_form():
    d = request.get_json(force=True) or {}
    fid = uid("frm")
    insert("capture_forms", {"id": fid, "org_id": g.org["id"], "name": d.get("name", "Form"),
                             "fields_json": json.dumps(d.get("fields", ["name", "phone"])),
                             "list_id": d.get("list_id"), "journey_id": d.get("journey_id"),
                             "sequence_id": d.get("sequence_id"),
                             "source_tag": d.get("source_tag", "website_form"), "created_at": now()})
    audit("capture_form.created", d.get("name", fid))
    return jsonify({"ok": True, "id": fid,
                    "embed": request.host_url.rstrip("/") + f"/capture/{fid}",
                    "widget": request.host_url.rstrip("/") + f"/capture/widget.js?form={fid}"})


# ---- public submit ----------------------------------------------------------
@bp.get("/capture/<fid>")
def form_config(fid):
    f = q1("SELECT id, name, fields_json FROM capture_forms WHERE id=?", (fid,))
    if not f:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"id": f["id"], "name": f["name"], "fields": json.loads(f["fields_json"] or "[]")})


@bp.post("/capture/<fid>")
def submit(fid):
    f = q1("SELECT * FROM capture_forms WHERE id=?", (fid,))
    if not f:
        return jsonify({"error": "Not found"}), 404
    d = request.get_json(force=True) or request.form.to_dict() or {}
    phone = phones.to_e164(d.get("phone"), d.get("cc", "91"))
    if not phones.is_valid(phone):
        return jsonify({"error": "Valid phone required"}), 400
    name = (d.get("name") or "").strip()
    _ingest_lead(f["org_id"], phone, name, f["source_tag"], d,
                 list_id=f["list_id"], journey_id=f["journey_id"], sequence_id=f["sequence_id"])
    return jsonify({"ok": True, "message": "Thanks! You'll hear from us on WhatsApp."})


def _ingest_lead(org_id, phone, name, source, attrs, list_id=None, journey_id=None, sequence_id=None):
    """Shared path for forms, widget and ad leads."""
    existing = q1("SELECT id FROM contacts WHERE org_id=? AND phone=?", (org_id, phone))
    country, tz = phones.country_tz(phone)
    if existing:
        cid = existing["id"]
        ex("UPDATE contacts SET name=COALESCE(NULLIF(?,''),name), opt_in=1, opt_in_source=?, "
           "opt_in_at=?, source=COALESCE(source,?) WHERE id=?",
           (name, source, now(), source, cid))
    else:
        cid = uid("con")
        insert("contacts", {"id": cid, "org_id": org_id, "phone": phone, "name": name,
                            "country": country, "timezone": tz, "attrs": json.dumps(attrs or {}),
                            "opt_in": 1, "opt_in_source": source, "opt_in_at": now(),
                            "source": source, "created_at": now()})
    insert("consent_records", {"id": uid("cns"), "org_id": org_id, "contact_phone": phone,
                               "action": "opt_in", "source": source, "actor": "public_capture",
                               "created_at": now()})
    if list_id:
        ex("INSERT OR IGNORE INTO list_contacts VALUES (?,?)", (list_id, cid))
    # fire automations
    from .journeys import fire_trigger
    fire_trigger(org_id, "form_submitted", phone, name)
    if journey_id:
        j = q1("SELECT * FROM journeys WHERE id=? AND org_id=?", (journey_id, org_id))
        if j:
            from .journeys import _enroll
            _enroll(org_id, j, phone, name)
    if sequence_id:
        first = q1("SELECT delay_secs FROM sequence_steps WHERE sequence_id=? ORDER BY ord LIMIT 1",
                   (sequence_id,))
        if first:
            try:
                insert("sequence_enrollments", {"id": uid("enr"), "sequence_id": sequence_id, "org_id": org_id,
                                                "contact_phone": phone, "contact_name": name, "step_idx": 0,
                                                "status": "active", "next_run": now() + int(first["delay_secs"]),
                                                "enrolled_at": now()})
            except Exception:
                pass
    return cid


# ---- website widget (embeddable JS) -----------------------------------------
@bp.get("/capture/widget.js")
def widget_js():
    fid = request.args.get("form", "")
    base = request.host_url.rstrip("/")
    js = f"""(function(){{
  var B=document.createElement('div');B.innerHTML='\\u{{1F4AC}}';
  B.style.cssText='position:fixed;right:20px;bottom:20px;width:56px;height:56px;border-radius:50%;background:linear-gradient(92deg,#4F7CFF,#22D3EE);color:#fff;font-size:26px;display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:99999;box-shadow:0 6px 20px rgba(0,0,0,.3)';
  B.onclick=function(){{
    var n=prompt('Your name');var p=prompt('Your WhatsApp number');
    if(!p)return;
    fetch('{base}/capture/{fid}',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{name:n,phone:p}})}}).then(function(){{alert('Thanks! We\\'ll reach you on WhatsApp.');}});
  }};
  document.body.appendChild(B);
}})();"""
    return Response(js, mimetype="application/javascript")


# ---- Click-to-WhatsApp ad lead ingestion ------------------------------------
@bp.post("/capture/ad-lead/<token>")
def ad_lead(token):
    """Accept a CTWA / lead-ad payload routed by a per-org integration token."""
    integ = q1("SELECT * FROM integrations WHERE token=? AND kind='ad_leads' AND enabled=1", (token,))
    if not integ:
        return jsonify({"error": "Invalid token"}), 401
    d = request.get_json(force=True) or {}
    phone = phones.to_e164(d.get("phone"), d.get("cc", "91"))
    if not phones.is_valid(phone):
        return jsonify({"error": "Valid phone required"}), 400
    insert("ad_leads", {"id": uid("adl"), "org_id": integ["org_id"], "source": d.get("ad_source", "ctwa"),
                        "payload": json.dumps(d), "contact_phone": phone, "created_at": now()})
    cfg = json.loads(integ["config"] or "{}")
    _ingest_lead(integ["org_id"], phone, d.get("name", ""), d.get("ad_source", "ctwa_ad"), d,
                 list_id=cfg.get("list_id"), journey_id=cfg.get("journey_id"),
                 sequence_id=cfg.get("sequence_id"))
    return jsonify({"ok": True})
