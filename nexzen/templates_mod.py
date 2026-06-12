"""Templates: prebuilt gallery, in-app authoring, Meta submission + status sync,
multi-language variants, header media, and interactive buttons (§6, §16).
"""
import json

from flask import Blueprint, request, jsonify, g

from .db import q, q1, ex, insert, update, uid, now
from .auth import require_org, require_role, audit
from .credentials import get_client

bp = Blueprint("templates", __name__)

GALLERY = [
    {"cat": "Festival", "label": "Diwali greeting", "emoji": "🪔", "category": "MARKETING",
     "text": "Hi {{1}}! 🪔 Team {{2}} wishes you a sparkling Diwali. Enjoy a festive 20% off — valid till Sunday."},
    {"cat": "Festival", "label": "Eid greeting", "emoji": "🌙", "category": "MARKETING",
     "text": "Eid Mubarak, {{1}}! 🌙 As a token of celebration, enjoy free delivery on your next order from {{2}}."},
    {"cat": "Festival", "label": "New Year", "emoji": "🎆", "category": "MARKETING",
     "text": "Happy New Year, {{1}}! 🎆 Thank you for being with {{2}} this year — your loyalty reward is waiting inside."},
    {"cat": "Invitation", "label": "Event invite", "emoji": "🎟️", "category": "MARKETING",
     "text": "Hello {{1}}, you're invited to {{2}}'s product launch on 28 June, 6 PM. Reply CONFIRM to reserve your seat."},
    {"cat": "Invitation", "label": "Webinar invite", "emoji": "💻", "category": "MARKETING",
     "text": "Hi {{1}}, join our free live webinar this Saturday at 11 AM. Reply JOIN and we'll send the link."},
    {"cat": "Collaboration", "label": "Partner outreach", "emoji": "🤝", "category": "MARKETING",
     "text": "Hi {{1}}, {{2}} is planning a collaboration and we'd love to share a short brief. Open to a quick chat?"},
    {"cat": "Promotion", "label": "Flash sale", "emoji": "⚡", "category": "MARKETING",
     "text": "{{1}}, our 48-hour flash sale is LIVE ⚡ Up to 40% off across {{2}}. Tap below before it ends."},
    {"cat": "Reminder", "label": "Payment reminder", "emoji": "🧾", "category": "UTILITY",
     "text": "Hi {{1}}, a gentle reminder that your invoice from {{2}} is due this Friday. Reply PAID once done."},
]


@bp.get("/api/templates/gallery")
def gallery():
    return jsonify({"gallery": GALLERY})


@bp.get("/api/templates")
@require_org
def list_templates():
    rows = q("SELECT * FROM templates WHERE org_id=? ORDER BY created_at DESC", (g.org["id"],))
    for t in rows:
        t["variants"] = q("SELECT * FROM template_variants WHERE template_id=?", (t["id"],))
    return jsonify({"templates": rows})


@bp.post("/api/templates")
@require_role("agent")
def create_template():
    """Create locally and (if connected) submit to Meta for approval."""
    d = request.get_json(force=True) or {}
    name = (d.get("name") or "").strip().lower().replace(" ", "_")
    category = (d.get("category") or "MARKETING").upper()
    language = d.get("language", "en_US")
    body = d.get("body", "")
    header = d.get("header")              # {format:'TEXT'|'IMAGE'|..., text?}
    buttons = d.get("buttons")           # [{type:'QUICK_REPLY'|'URL'|'COPY_CODE', text, url?}]
    example = d.get("example")           # [["Ravi","BrightMart"]] sample for {{n}}
    if not name or not body:
        return jsonify({"error": "Template name and body are required."}), 400

    tid = uid("tpl")
    insert("templates", {"id": tid, "org_id": g.org["id"], "name": name, "category": category,
                         "status": "DRAFT", "created_at": now(), "updated_at": now()})
    insert("template_variants", {"id": uid("tvr"), "template_id": tid, "org_id": g.org["id"],
                                 "language": language, "body": body,
                                 "header_kind": (header or {}).get("format"),
                                 "header_value": (header or {}).get("text"),
                                 "buttons_json": json.dumps(buttons) if buttons else None,
                                 "example_json": json.dumps(example) if example else None})

    client = get_client(g.org["id"])
    submitted = False
    meta_resp = None
    if client:
        ok, resp = client.create_template(name, category, language, body, header,
                                           _meta_buttons(buttons), example[0] if example else None)
        submitted = ok
        meta_resp = resp
        update("templates", tid, {"status": "PENDING" if ok else "ERROR",
                                  "meta_id": resp.get("id") if ok else None,
                                  "rejection_reason": None if ok else json.dumps(resp.get("error", {}))})
    audit("template.created", name, "submitted" if submitted else "draft")
    return jsonify({"ok": True, "id": tid, "submitted": submitted, "meta": meta_resp})


def _meta_buttons(buttons):
    if not buttons:
        return None
    out = []
    for b in buttons:
        t = b.get("type", "QUICK_REPLY").upper()
        if t == "URL":
            out.append({"type": "URL", "text": b["text"], "url": b["url"]})
        elif t == "COPY_CODE":
            out.append({"type": "COPY_CODE", "example": b.get("code", "SAVE20")})
        else:
            out.append({"type": "QUICK_REPLY", "text": b["text"]})
    return out


@bp.post("/api/templates/sync")
@require_role("agent")
def sync_templates():
    """Pull live statuses from Meta and upsert into the local catalog."""
    client = get_client(g.org["id"])
    if not client:
        return jsonify({"error": "Connect WhatsApp credentials first."}), 400
    live = client.list_templates()
    for t in live:
        row = q1("SELECT id FROM templates WHERE org_id=? AND name=?", (g.org["id"], t["name"]))
        if row:
            update("templates", row["id"], {"status": t["status"], "meta_id": t.get("id"),
                                            "category": t.get("category", "MARKETING"),
                                            "rejection_reason": t.get("rejected_reason"), "updated_at": now()})
        else:
            tid = uid("tpl")
            insert("templates", {"id": tid, "org_id": g.org["id"], "name": t["name"],
                                 "category": t.get("category", "MARKETING"), "status": t["status"],
                                 "meta_id": t.get("id"), "created_at": now(), "updated_at": now()})
    return jsonify({"ok": True, "synced": len(live),
                    "approved": [t for t in live if t["status"] == "APPROVED"]})


def approved_template(org_id, name, language=None):
    """Pre-launch guard: returns the template row only if APPROVED, else None."""
    row = q1("SELECT * FROM templates WHERE org_id=? AND name=? AND status='APPROVED'", (org_id, name))
    return row


@bp.post("/api/templates/media")
@require_role("agent")
def upload_media():
    f = request.files.get("file")
    client = get_client(g.org["id"])
    if not f:
        return jsonify({"error": "No file"}), 400
    if not client:
        return jsonify({"error": "Connect credentials to upload media."}), 400
    mid = client.upload_media(f.read(), f.mimetype)
    if not mid:
        return jsonify({"error": "Media upload failed (check size/type)."}), 400
    return jsonify({"ok": True, "media_id": mid})
